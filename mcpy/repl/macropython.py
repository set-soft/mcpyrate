#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Universal bootstrapper for macro-enabled Python programs powered by mcpy."""

# TODO: Currently tested in CPython 3.6, and PyPy3 7.3.0 (Python 3.6). Test in 3.7+.

from importlib import import_module
from importlib.util import resolve_name, module_from_spec
import pathlib
import os
import sys
import argparse

from ..importer import relativize

from .. import activate  # noqa: F401

__version__ = "3.0.0"

def import_module_as_main(name, script_mode):
    """Import a module, pretending it's __main__.

    Upon success, replaces ``sys.modules["__main__"]`` with the module that
    was imported. Upon failure, propagates any exception raised.

    This is a customized approximation of the standard import semantics, based on:

        https://docs.python.org/3/library/importlib.html#approximating-importlib-import-module
        https://docs.python.org/3/reference/import.html#loading
        https://docs.python.org/3/reference/import.html#import-related-module-attributes
        https://docs.python.org/3/reference/import.html#module-path
        https://docs.python.org/3/reference/import.html#special-considerations-for-main
    """
    # We perform only the user-specified import ourselves; that we must, in order to
    # load it as "__main__". We delegate all the rest to the stdlib import machinery.
    #
    # We support only new-style loaders that have `exec_module`;
    # old-style loaders with `load_module` and `create_module` are not supported.

    absolute_name = resolve_name(name, package=None)

    # Normally we should return the module from sys.modules if already loaded,
    # but the __main__ in sys.modules is this bootstrapper program, not the user
    # __main__ we're loading. So pretend whatever we're loading isn't loaded yet.
    #
    # Note Python treats __main__ and somemod as distinct modules, even when it's
    # the same file, because __main__ triggers "if __name__ == '__main__':"
    # checks whereas somemod doesn't.
    #
    # try:
    #     return sys.modules[absolute_name]
    # except KeyError:
    #     pass

    path = None
    if '.' in absolute_name:
        # In script mode, our main() injects the directory containing the
        # script to `sys.path` (just like `python some/path/to/script.py`
        # does), so in that case we always perform a top-level import using
        # just the final component of the name. So the name shouldn't have dots.
        if script_mode:
            assert False
        # Module mode. Import the module's parent package normally; this will
        # initialize any parent packages up the chain. Set `path` for
        # `find_spec` so it can find subpackages and modules.
        parent_name, _, child_name = absolute_name.rpartition('.')
        parent_module = import_module(parent_name)
        path = parent_module.__spec__.submodule_search_locations

    for finder in sys.meta_path:
        if not hasattr(finder, "find_spec"):  # pkg_resources.extern.VendorImporter has no find_spec
            continue
        spec = finder.find_spec(absolute_name, path)
        if spec is not None:
            break
    else:
        msg = f"No module named {absolute_name}"
        raise ModuleNotFoundError(msg, name=absolute_name)

    spec.name = "__main__"
    if spec.loader:
        spec.loader.name = "__main__"  # fool importlib._bootstrap.check_name_wrapper

    module = module_from_spec(spec)
    try_mainpy = False
    if script_mode:  # e.g. "macropython somepackage/__init__.py"
        module.__package__ = ""
    elif path:  # e.g. "macropython -m somepackage.module"
        module.__package__ = parent_name
    elif spec.origin.endswith("__init__.py"):  # e.g. "macropython -m somepackage"
        module.__package__ = absolute_name
        try_mainpy = True

    if spec.origin == "namespace":
        module.__path__ = spec.submodule_search_locations

    if try_mainpy:
        # e.g. "import somepackage" in the above case; it's not the one running
        # as main, so import it normally.
        assert not script_mode
        parent_module = import_module(absolute_name)
    elif spec.loader is not None:  # namespace packages have loader=None
        # There's already a __main__ in sys.modules (this bootstrapper), so
        # the most logical thing to do is to switch it **after** a successful
        # import, not before import as for usual imports (where the ordering
        # prevents infinite recursion and multiple loading).
        spec.loader.exec_module(module)
        sys.modules["__main__"] = module
        # # __main__ has no parent module so we don't need to do this.
        # if path is not None:
        #     setattr(parent_module, child_name, module)
    else:  # namespace package
        try_mainpy = True

    # __init__.py (if any) has run; run __main__.py, like `python -m somepackage` does.
    if try_mainpy:
        has_mainpy = True
        try:
            # __main__.py doesn't need to have its name set to "__main__",
            # so we can just import it normally.
            import_module(f"{absolute_name}.__main__")
        except ImportError as e:
            if "No module named" in e.msg:
                has_mainpy = False
            else:
                raise
        if not has_mainpy:
            raise ImportError(f"No module named {absolute_name}.__main__; '{absolute_name}' is a package and cannot be directly executed")

    return module

def main():
    """Handle command-line arguments and run the specified main program."""
    parser = argparse.ArgumentParser(description="""Run a Python program or an interactive interpreter with mcpy enabled.""",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-v', '--version', action='version', version=('%(prog)s (mcpy ' + __version__ + ')'))
    parser.add_argument(dest='filename', nargs='?', default=None, type=str, metavar='file',
                        help='script to run')
    parser.add_argument('-m', '--module', dest='module', default=None, type=str, metavar='mod',
                        help='run library module as a script (like python -m mod)')
    parser.add_argument('-i', '--interactive', dest='interactive', action="store_true", default=False,
                        help='interactive mode (macro-enabled Python interpreter). '
                             'If -i is given, <filename> and -m <mod> are ignored.')
    parser.add_argument('-p', '--pylab', dest='pylab', action="store_true", default=False,
                        help='For use together with "-i". Automatically "import numpy as np", '
                             '"import matplotlib.pyplot as plt", and enable mpl\'s interactive '
                             'mode (somewhat like IPython\'s pylab mode).')
    opts = parser.parse_args()

    if opts.interactive:
        from .console import MacroConsole
        import readline  # noqa: F401, side effect: enable GNU readline in input()
        import rlcompleter  # noqa: F401, side effects: readline tab completion
        repl_locals = {}
        if opts.pylab:  # like IPython's pylab mode, but we keep things in separate namespaces.
            import numpy
            import matplotlib.pyplot
            repl_locals["np"] = numpy
            repl_locals["plt"] = matplotlib.pyplot
            matplotlib.pyplot.ion()
        readline.set_completer(rlcompleter.Completer(namespace=repl_locals).complete)
        readline.parse_and_bind("tab: complete")  # PyPy ignores this, but not needed there.

        # Add CWD to import path like the builtin interactive console does.
        if sys.path[0] != "":
            sys.path.insert(0, "")
        m = MacroConsole(locals=repl_locals)
        return m.interact()

    if not opts.filename and not opts.module:
        parser.print_help()
        sys.exit(0)
    if opts.filename and opts.module:
        raise ValueError("Please specify just one main program to run (either filename.py or -m module, not both).")

    # Import the module, pretending its __name__ is "__main__".
    #
    # We must import so that macros get expanded, so we can't use
    # runpy.run_module here (which just execs without importing).
    #
    # As for sys.path, we're not interested in the directory the `macropython`
    # script itself lives in. To imitate how Python itself behaves:
    #
    #   - If run as `macropython -m something`, then `sys.path[0]`
    #     should be set to the cwd.
    #   - If run as `macropython some/path/to/script.py`, then `sys.path[0]`
    #     should be set to the directory containing `script.py`.
    #
    if opts.module:
        # Python 3.7 and later resolve the cwd at interpreter startup time, and use that.
        # Python 3.6 and earlier use the empty string, which dynamically resolves to
        # whatever is the current cwd whenever a module is imported.
        #     https://bugs.python.org/issue33053
        if sys.version_info >= (3, 7, 0):
            cwd = str(pathlib.Path.cwd().expanduser().resolve())
            if sys.path[0] != cwd:
                sys.path.insert(0, cwd)
        else:
            if sys.path[0] != "":
                sys.path.insert(0, "")

        import_module_as_main(opts.module, script_mode=False)

    else:  # no module, so use opts.filename
        fullpath = pathlib.Path(opts.filename).expanduser().resolve()
        if not fullpath.is_file():
            raise FileNotFoundError(f"Can't open file '{opts.filename}'")

        containing_directory = str(fullpath.parent)
        if sys.path[0] != containing_directory:
            sys.path.insert(0, containing_directory)

        # This approach finds the standard loader even for macro-enabled scripts.
        # TODO: For mcpy, that could be ok, since we monkey-patch just that.
        # TODO: But maybe better to leave the option to replace the whole loader later.
        # spec = spec_from_file_location("__main__", str(fullpath))
        # if not spec:
        #     raise ImportError(f"Not a Python module: '{opts.filename}'"
        # module = module_from_spec(spec)
        # spec.loader.exec_module(module)

        root_path, relative_path = relativize(opts.filename)
        module_name = relative_path.replace(os.path.sep, '.')
        if module_name.endswith(".__init__.py"):
            module_name = module_name[:-12]
        elif module_name.endswith(".py"):
            module_name = module_name[:-3]

        import_module_as_main(module_name, script_mode=True)

if __name__ == '__main__':
    main()
