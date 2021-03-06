import os
import sys
import types
import inspect
import hashlib
import subprocess

from . import Parser
from .stdlib import get_full_std_lib  # noqa
from .modules import create_js_module


class JSString(str):
    """ A subclass of string, so we can add attributes to JS string objects.
    """
    pass


def py2js(ob=None, new_name=None, **parser_options):
    """ Convert Python to JavaScript.
    
    Parameters:
        ob (str, module, function, class): The code, function or class
            to transpile.
        new_name (str, optional): If given, renames the function or
            class. This argument is ignored if ob is a string.
        parser_options: Additional options for the parser. See Parser class
            for details.
    
    Returns:
        jscode (str): The JavaScript code as a special str object that
        has a ``meta`` attribute that contains the following fields:
        
        * filename (str): the name of the file that defines the object.
        * linenr (int): the starting linenr for the object definition.
        * pycode (str): the Python code used to generate the JS.
        * pyhash (str): a hash of the Python code.
        * vars_defined (set): names defined in the toplevel namespace.
        * vars_unknown (set): names used in the code but not defined in it.
        * vars_global (set): names explicitly declared global.
        * std_functions (set): stdlib functions used in this code.
        * std_method (set): stdlib methods used in this code.
    
    Notes:
        The Python source code for a class is acquired by name.
        Therefore one should avoid decorating classes in modules where
        multiple classes with the same name are defined. This is a
        consequence of classes not having a corresponding code object (in
        contrast to functions).
    
    """
    
    def py2js_(ob):
        if isinstance(ob, str):
            thetype = 'str'
            pycode = ob
            filename = None
            linenr = 0
        elif isinstance(ob, types.ModuleType) and hasattr(ob, '__file__'):
            thetype = 'str'
            filename = inspect.getsourcefile(ob)
            linenr = 0
            pycode = open(filename, 'rb').read().decode()
            if pycode.startswith('# -*- coding:'):
                pycode = '\n' + pycode.split('\n', 1)[-1]
        elif isinstance(ob, (type, types.FunctionType, types.MethodType)):
            thetype = 'class' if isinstance(ob, type) else 'def'
            # Get code
            try:
                filename = inspect.getsourcefile(ob)
                lines, linenr = inspect.getsourcelines(ob)
            except Exception as err:
                raise ValueError('Could not get source code for object %r: %s' %
                                 (ob, err))
            if getattr(ob, '__name__', '') in ('', '<lambda>'):
                raise ValueError('py2js() got anonymous function from '
                                 '"%s", line %i, %r.' % (filename, linenr, ob))
            # Normalize indentation
            indent = len(lines[0]) - len(lines[0].lstrip())
            lines = [line[indent:] for line in lines]
            # Skip any decorators
            while not lines[0].lstrip().startswith(thetype):
                lines.pop(0)
            # join lines and rename
            pycode = ''.join(lines)
        else:
            raise ValueError('py2js() only accepts non-builtin modules, '
                             'classes and functions.')
        
        # Get hash, in case we ever want to cache JS accross sessions
        h = hashlib.sha256('pyscript version 1'.encode())
        h.update(pycode.encode())
        hash = h.digest()
        
        # Get JS code
        if filename:
            p = Parser(pycode, (filename, linenr), **parser_options)
        else:
            p = Parser(pycode, **parser_options)
        jscode = p.dump()
        if new_name and thetype in ('class', 'def'):
            jscode = js_rename(jscode, ob.__name__, new_name)
        
        # todo: now that we have so much info in the meta, maybe we should
        # use use py2js everywhere where we now use Parser and move its docs here.
        
        # Wrap in JSString
        jscode = JSString(jscode)
        jscode.meta = {}
        jscode.meta['filename'] = filename
        jscode.meta['linenr'] = linenr
        jscode.meta['pycode'] = pycode
        jscode.meta['pyhash'] = hash
        jscode.meta['vars_defined'] = set(n for n in p.vars if p.vars[n])
        jscode.meta['vars_unknown'] = set(n for n in p.vars if not p.vars[n])
        jscode.meta['vars_global'] = set(n for n in p.vars if p.vars[n] is False)
        jscode.meta['std_functions'] = p._std_functions
        jscode.meta['std_methods'] = p._std_methods
        
        return jscode
    
    if ob is None:
        return py2js_  # uses as a decorator with some options set
    return py2js_(ob)


def js_rename(jscode, cur_name, new_name):
    """ Rename a function or class in a JavaScript code string.
    
    Parameters:
        jscode (str): the JavaScript source code
        cur_name (str): the current name
        new_name (str): the name to replace the current name with
    
    Returns:
        jscode (str): the modified JavaScript source code
    """
    
    jscode = jscode.replace('%s = function' % cur_name, 
                            '%s = function' % (new_name), 1)
    jscode = jscode.replace('%s.prototype' % cur_name, 
                            '%s.prototype' % new_name)
    jscode = jscode.replace('%s.Ƥ' % cur_name, 
                            '%s.Ƥ' % new_name)
    jscode = jscode.replace('_class_name = "%s"' % cur_name, 
                            '_class_name = "%s"' % new_name)
    if '.' in new_name:
        jscode = jscode.replace('var %s;\n' % cur_name, '', 1)
    else:
        jscode = jscode.replace('var %s;\n' % cur_name, 
                                'var %s;\n' % new_name, 1)
    return jscode


NODE_EXE = None
def get_node_exe():
    """ Small utility that provides the node exe. The first time this
    is called both 'nodejs' and 'node' are tried. To override the
    executable path, set the ``FLEXX_NODE_EXE`` environment variable.
    """
    # This makes things work on Ubuntu's nodejs as well as other node
    # implementations, and allows users to set the node exe if necessary
    global NODE_EXE
    NODE_EXE = os.getenv('FLEXX_NODE_EXE') or NODE_EXE
    if NODE_EXE is None:
        NODE_EXE = 'nodejs'
        try:
            subprocess.check_output([NODE_EXE, '-v'])
        except Exception:  # pragma: no cover
            NODE_EXE = 'node'
    return NODE_EXE


def evaljs(jscode, whitespace=True):
    """ Evaluate JavaScript code in Node.js.
    
    parameters:
        jscode (str): the JavaScript code to evaluate.
        whitespace (bool): if whitespace is False, the whitespace
            is removed from the result. Default True.
    returns:
        result (str): the last result as a string.
    """
    
    # Call node
    cmd = [get_node_exe(), '--use_strict', '-p', '-e', jscode]
    if sys.version_info[0] < 3:
        cmd = [c.encode('raw_unicode_escape') for c in cmd]
    try:
        res = subprocess.check_output(cmd)
    except FileNotFoundError as err:
        raise Exception('%s - The code is probably too big' % str(err))
    except Exception as err:
        err = err.output.decode()
        err = err[:200] + '...' if len(err) > 200 else err
        raise Exception(err)
    
    # process
    res = res.decode().rstrip()
    if res.endswith('undefined'):
        res = res[:-9].rstrip()
    if not whitespace:
        res = res.replace('\n', '').replace('\t', '').replace(' ', '')
    return res


def evalpy(pycode, whitespace=True):
    """ Evaluate PyScript code in Node.js (after translating to JS).
    
    parameters:
        pycode (str): the PyScript code to evaluate.
        whitespace (bool): if whitespace is False, the whitespace is
            removed from the result. Default True.
    
    returns:
        result (str): the last result as a string.
    """
    # delibirate numpy doc style to see if napoleon handles it the same
    return evaljs(py2js(pycode), whitespace)


def script2js(filename, namespace=None, target=None, module_type='umd',
              **parser_options):
    """ Export a .py file to a .js file.
    
    Parameters:
      filename (str): the filename of the .py file to transpile.
      namespace (str): the namespace for this module. (optional)
      target (str): the filename of the resulting .js file. If not given
        or None, will use the ``filename``, but with a ``.js`` extension.
      module_type (str): the type of module to produce (if namespace is given),
        can be 'hidden', 'simple', 'amd', 'umd', default 'umd'.
      parser_options: additional options for the parser. See Parser class
        for details.
    """
    # Import
    assert filename.endswith('.py')
    pycode = open(filename, 'rb').read().decode()
    # Convert
    parser = Parser(pycode, filename, **parser_options)
    jscode = '/* Do not edit, autogenerated by flexx.pyscript */\n\n' + parser.dump()
    # Wrap in module
    if namespace:
        exports = [name for name in parser.vars
                   if not (parser.vars[name] or name.startswith('_'))]
        jscode = create_js_module(namespace, jscode, [], exports, module_type)
    # Export
    if target is None:
        dirname, fname = os.path.split(filename)
        filename2 = os.path.join(dirname, fname[:-3] + '.js')
    else:
        filename2 = target
    with open(filename2, 'wb') as f:
        f.write(jscode.encode())
