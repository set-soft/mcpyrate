# -*- coding: utf-8; -*-
'''Find and expand macros.

This layer provides the actual macro expander, defining:

 - Syntax for establishing macro bindings::
       from ... import macros, ...

 - Macro invocation types:
   - expr:      `macroname[...]`, `macroname[arg0, ...][...]`
   - block:     `with macroname:`, `with macroname as result:`,
                `with macroname[arg0, ...]:`, `with macroname[arg0, ...] as result:`
   - decorator: `@macroname`, `@macroname[arg0, ...]`
   - name:      `macroname`
'''

# We use bracket syntax for sending macro arguments, because parentheses evoke
# the idea of full function-call syntax. This includes keyword arguments, and
# the distinction between parameter slots (which are always named) and how the
# actual arguments provided in a call are bound to those slots (whether by
# position or by name; and don't forget *args and **kwargs, both at the
# receiving and sending end).
#
# Nowadays it's possible to easily support all this properly via `inspect.signature`
# and `inspect.Signature.bind`. But to get a call signature to bind to, the
# macro function's own signature won't do - that's for when the function is
# called by the macro expander, not for sending macro arguments (which form
# a separate namespace).
#
# So we would need a reference callable to `@parametricmacro`, not to be
# called, but only to have its call signature extracted. In practice, it would
# be a `lambda ...: None`, with the confusing `lambda` and `None` mandatory,
# when the only interesting part is in the `...`, the parameter declarations.
#
# This could then be used to establish a *second* call signature for the
# parametric macro function, to receive the macro arguments in, say, a
# dictionary always named `args` (binding parameter names to values provided
# in the macro call; cf. our current list `args`, which just lists the values).
# Confused yet? See commit 10691ce for a sketch.
#
# The system is much simpler to explain if we just use brackets, and have
# positional args only.
#
# Even this choice of syntax unfortunately leads to an ambiguity as to what
# `macro[...][...]` and even just `macro[...]` mean, but perhaps it's the
# lesser evil. This is one reason why we require parametric macros to be
# explicitly declared - avoid that ambiguity when not needed.

__all__ = ['namemacro', 'isnamemacro',
           'parametricmacro', 'isparametricmacro',
           'MacroExpander', 'MacroCollector',
           'expand_macros', 'find_macros', 'ismacroimport']

import importlib
import importlib.util  # in PyPy3, this must be imported explicitly
from ast import (Name, Subscript, Tuple, Import, ImportFrom, alias, AST, Expr, Constant,
                 copy_location, iter_fields, NodeVisitor)
from warnings import warn_explicit
from .core import (BaseMacroExpander, global_postprocess, Done,
                   format_location, format_macrofunction)
from .importer import resolve_package
from .unparser import unparse_with_fallbacks
from .utilities import NodeVisitorListMixin

def namemacro(function):
    '''Decorator. Declare a macro function as an identifier macro.

    Identifier macros are a rarely needed feature. Hence, the expander invokes
    as identifier macros only macros that are declared as such.

    This (or `@parametricmacro`, if used too) must be the outermost decorator.
    '''
    function._isnamemacro = True
    return function

def isnamemacro(function):
    '''Return whether the macro function `function` has been declared as an identifier macro.'''
    return hasattr(function, '_isnamemacro')

def parametricmacro(function):
    '''Decorator. Declare a macro function as taking macro arguments.

    Macro arguments are a rarely needed feature. Hence, the expander interprets
    macro argument syntax only for macros that are declared as parametric.

    This (or `@namemacro`, if used too) must be the outermost decorator.
    '''
    function._isparametricmacro = True
    return function

def isparametricmacro(function):
    '''Return whether the macro function `function` has been declared as taking macro arguments.'''
    return hasattr(function, '_isparametricmacro')

# --------------------------------------------------------------------------------

def destructure_candidate(tree):
    '''Destructure a macro call candidate AST, `macroname` or `macroname[arg0, ...]`.'''
    if type(tree) is Name:
        return tree.id, []
    elif type(tree) is Subscript and type(tree.value) is Name:
        macroargs = tree.slice.value
        if type(macroargs) is Tuple:  # [a0, a1, ...]
            macroargs = macroargs.elts
        else:  # anything that doesn't have at least one comma at the top level
            macroargs = [macroargs]
        return tree.value.id, macroargs
    return None, None  # not a macro invocation


class MacroExpander(BaseMacroExpander):
    '''The actual macro expander.'''

    def ismacrocall(self, macroname, macroargs, syntax):
        '''Shorthand to check `destructure_candidate` output.

        Return whether that output is a macro call to a macro (of invocation
        type `syntax`) bound in this expander.
        '''
        if not (macroname and self.isbound(macroname)):
            return False
        if syntax == 'name':
            return isnamemacro(self.bindings[macroname])
        return not macroargs or isparametricmacro(self.bindings[macroname])

    def visit_Subscript(self, subscript):
        '''Detect an expression (expr) macro invocation.

        Detected syntax::

            macroname[...]
            macroname[arg0, ...][...]  # allowed if @parametricmacro

        Replace the `Subscript` node with the AST returned by the macro.
        The core controls whether to expand again in the result.
        '''
        candidate = subscript.value
        macroname, macroargs = destructure_candidate(candidate)
        if self.ismacrocall(macroname, macroargs, 'expr'):
            kw = {'args': macroargs}
            tree = subscript.slice.value
            sourcecode = unparse_with_fallbacks(subscript)
            new_tree = self.expand('expr', subscript, macroname, tree, sourcecode=sourcecode, fill_root_location=True, kw=kw)
        else:
            new_tree = self.generic_visit(subscript)
        return new_tree

    def visit_With(self, withstmt):
        '''Detect a block macro invocation.

        Detected syntax::

            with macroname:
                ...
            with macroname as result:
                ...
            with macroname[arg0, ...]:  # allowed if @parametricmacro
                ...
            with macroname[arg0, ...] as result:  # allowed if @parametricmacro
                ...

        Replace the `With` node with the AST returned by the macro.

        The `result` part is sent to the macro as `kw['optional_vars']`; it's a
        `Name`, `Tuple` or `List` node. What to do with it is up to the macro;
        the typical meaning is to assign something to the name(s).
            https://greentreesnakes.readthedocs.io/en/latest/nodes.html#withitem

        Invoking several block macros in the same `with` is shorthand for nesting::

            with macro1, macro2:
                ...

        is equivalent with::

            with macro1:
                with macro2:  # part of `tree` for `macro1`, in either notation
                    ...

        We pop the first block macro in the withitem list and expand it. The
        core controls whether to expand again in the result. A block macro may
        do anything it wants to its input tree. Any remaining block macro
        invocations are attached to the `With` node, so if that is removed,
        they will be skipped.
        '''
        macros, others = self._detect_macro_items(withstmt.items, "block")
        if not macros:
            return self.generic_visit(withstmt)
        with_item = macros[0]
        candidate = with_item.context_expr
        macroname, macroargs = destructure_candidate(candidate)
        sourcecode = unparse_with_fallbacks(withstmt)
        withstmt.items.remove(with_item)
        kw = {'args': macroargs}
        kw.update({'optional_vars': with_item.optional_vars})
        tree = withstmt.body if not withstmt.items else [withstmt]
        new_tree = self.expand('block', withstmt, macroname, tree, sourcecode=sourcecode, fill_root_location=False, kw=kw)
        new_tree = _add_coverage_dummy_node(new_tree, withstmt, macroname)
        return new_tree

    def visit_ClassDef(self, classdef):
        return self._visit_Decorated(classdef)

    def visit_FunctionDef(self, functiondef):
        return self._visit_Decorated(functiondef)

    def _visit_Decorated(self, decorated):
        '''Detect a decorator macro invocation.

        Detected syntax::

            @macroname
            def f():
                ...
            @macroname[arg0, ...]  # allowed if @parametricmacro
            def f():
                ...
            @macroname
            class C():
                ...
            @macroname[arg0, ...]  # allowed if @parametricmacro
            class C:
                ...

        Replace the whole decorated node with the AST returned by the macro.

        We pop the innermost decorator macro and expand it. The core controls
        whether to expand again in the result. A decorator macro may edit the
        decorator list; it may also emit additional nodes (by returning a
        `list`), or even delete the decorated node or replace it altogether.
        Any remaining decorator macro invocations are attached to the original
        decorated node, so if that is removed, they will be skipped.

        The body is expanded after the whole decorator list has been processed.
        '''
        macros, others = self._detect_macro_items(decorated.decorator_list, "decorator")
        if not macros:
            return self.generic_visit(decorated)
        innermost_macro = macros[-1]
        macroname, macroargs = destructure_candidate(innermost_macro)
        sourcecode = unparse_with_fallbacks(decorated)
        decorated.decorator_list.remove(innermost_macro)
        kw = {'args': macroargs}
        new_tree = self.expand('decorator', decorated, macroname, decorated, sourcecode=sourcecode, fill_root_location=True, kw=kw)
        new_tree = _add_coverage_dummy_node(new_tree, innermost_macro, macroname)
        return new_tree

    def _detect_macro_items(self, items, syntax):
        '''Split a list `items` into `(macros, others)`.

        `syntax`: str, "block" or "decorator"
            "block": `items` is a `With.items`
            "decorator": `items` is a `decorator_list`
        '''
        assert syntax in ("block", "decorator")
        context = "in `with` header" if syntax == "block" else "as decorator"

        macros, others = [], []
        for item in items:
            if syntax == "block":
                candidate = item.context_expr
            else:
                candidate = item
            macroname, macroargs = destructure_candidate(candidate)

            # warn about likely mistake
            if (macroname and self.isbound(macroname) and
                    (macroargs and not isparametricmacro(self.bindings[macroname]))):
                msg = f"expr macro `{macroname}` invoked {context}; `{format_macrofunction(self.bindings[macroname])}` maybe missing `@parametricmacro` declaration?"
                lineno = item.lineno if hasattr(item, "lineno") else 0
                warn_explicit(msg, SyntaxWarning, filename=self.filename, lineno=lineno)

            if self.ismacrocall(macroname, macroargs, syntax):
                macros.append(item)
            else:
                others.append(item)

        return macros, others

    def visit_Name(self, name):
        '''Detect an identifier (name) macro invocation.

        Detected syntax::

            macroname

        The `Name` node itself is the input tree for the macro.
        Replace the `Name` node with the AST returned by the macro.

        Otherwise the core controls whether to expand again in the
        result, but we stop if the macro returns the original `tree`,
        telling the expander to use the name as a regular run-time name.
        '''
        # We must silently ignore when a non-name macro is invoked as a name macro,
        # because things like `q[h[some_expr_macro][...]]` are valid.
        if self.ismacrocall(name.id, None, 'name'):
            macroname = name.id
            def ismodified(tree):
                return not (type(tree) is Name and tree.id == macroname)
            with self._recursive_mode(False):
                kw = {'args': None}
                sourcecode = unparse_with_fallbacks(name)
                new_tree = self.expand('name', name, macroname, name, sourcecode=sourcecode, fill_root_location=True, kw=kw)
            if new_tree is not None:
                if not ismodified(new_tree):
                    new_tree = Done(new_tree)
                elif self.recursive:  # and modified
                    new_tree = self.visit(new_tree)
        else:
            new_tree = name
        return new_tree


class MacroCollector(NodeVisitorListMixin, NodeVisitor):
    '''Scan `tree` for macro invocations, with respect to given `expander`.

    Collect a list of `(macroname, syntax)`. Usage::

        mc = MacroCollector(expander)
        mc.visit(tree)
        print(mc.collected)
        # ...do something to tree...
        mc.clear()
        mc.visit(tree)
        print(mc.collected)

    Sister class of the actual `MacroExpander`, mirroring its syntax detection.
    '''
    def __init__(self, expander):
        '''`expander`: a `MacroExpander` instance to query macro bindings from.'''
        self.expander = expander
        self.clear()

    def clear(self):
        self._seen = set()
        self.collected = []

    def visit(self, tree):
        if isinstance(tree, Done):
            return
        return super().visit(tree)

    def visit_Subscript(self, subscript):
        candidate = subscript.value
        macroname, macroargs = destructure_candidate(candidate)
        if self.expander.ismacrocall(macroname, macroargs, 'expr'):
            key = (macroname, 'expr')
            if key not in self._seen:
                self.collected.append(key)
                self._seen.add(key)
            self.visit(macroargs)
            # Don't `self.generic_visit(tree)`; that'll incorrectly detect
            # the name part as an identifier macro. Recurse only in the expr.
            self.visit(subscript.slice.value)
        else:
            self.generic_visit(subscript)

    def visit_With(self, withstmt):
        macros, others = self.expander._detect_macro_items(withstmt.items, "block")
        if macros:
            for with_item in macros:
                candidate = with_item.context_expr
                macroname, macroargs = destructure_candidate(candidate)
                key = (macroname, 'block')
                if key not in self._seen:
                    self.collected.append(key)
                    self._seen.add(key)
                self.visit(macroargs)
            for with_item in others:
                self.visit(with_item)
            self.visit(withstmt.body)
        else:
            self.generic_visit(withstmt)

    def visit_ClassDef(self, classdef):
        self._visit_Decorated(classdef)

    def visit_FunctionDef(self, functiondef):
        self._visit_Decorated(functiondef)

    def _visit_Decorated(self, decorated):
        macros, others = self.expander._detect_macro_items(decorated.decorator_list, "decorator")
        if macros:
            for macro in macros:
                macroname, macroargs = destructure_candidate(macro)
                key = (macroname, 'decorator')
                if key not in self._seen:
                    self.collected.append(key)
                    self._seen.add(key)
                self.visit(macroargs)
            for decorator in others:
                self.visit(decorator)
            for k, v in iter_fields(decorated):
                if k == "decorator_list":
                    continue
                self.visit(v)
        else:
            self.generic_visit(decorated)

    def visit_Name(self, name):
        macroname = name.id
        if self.expander.ismacrocall(macroname, None, 'name'):
            key = (macroname, 'name')
            if key not in self._seen:
                self.collected.append(key)
                self._seen.add(key)


def _add_coverage_dummy_node(tree, macronode, macroname):
    '''Force `macronode` to be reported as covered by coverage tools.

    The dummy node will be injected to `tree`. The `tree` must appear in a
    position where `ast.NodeTransformer.visit` may return a list of nodes.

    `macronode` is the macro invocation node to copy source location info from.
    `macroname` is included in the dummy node, to ease debugging.
    '''
    # `macronode` itself might be macro-generated. In that case don't bother.
    if not hasattr(macronode, 'lineno') and not hasattr(macronode, 'col_offset'):
        return tree
    if tree is None:
        tree = []
    elif isinstance(tree, AST):
        tree = [tree]
    # The dummy node must actually run to get coverage, an `ast.Pass` won't do.
    # We must set location info manually, because we run after `expand`.
    x = copy_location(Constant(value=f"mcpy coverage: source line {macronode.lineno} invoked macro {macroname}"),
                      macronode)
    dummy = copy_location(Expr(value=x), macronode)
    tree.insert(0, Done(dummy))  # mark as Done so any expansions further out won't mess this up.
    return tree

# --------------------------------------------------------------------------------

def expand_macros(tree, bindings, *, filename):
    '''Expand `tree` with macro bindings `bindings`. Top-level entrypoint.

    Primarily meant to be called with `tree` the AST of a module that uses
    macros, but works with any `tree` (even inside a macro, if you need an
    independent second instance of the expander with different bindings).

    `bindings`: dict of macro name/function pairs.

    `filename`: str, full path to the `.py` being macroexpanded, for error reporting.
                In interactive use, can be an arbitrary label.
    '''
    expansion = MacroExpander(bindings, filename).visit(tree)
    expansion = global_postprocess(expansion)
    return expansion


def find_macros(tree, *, filename, reload=False):
    '''Establish macro bindings from `tree`. Top-level entrypoint.

    Collect bindings from each macro-import statement (`from ... import macros, ...`)
    at the top level of `tree.body`. Transform each macro-import into `import ...`,
    where `...` is the absolute module name the macros are being imported from.

    Primarily meant to be called with `tree` the AST of a module that
    uses macros, but works with any `tree` that has a `body` attribute.

    `filename`: str, full path to the `.py` being macroexpanded, for resolving
                relative macro-imports and for error reporting. In interactive
                use, can be an arbitrary label.

    `reload`:   enable only if implementing a REPL. Will refresh modules, causing
                different uses of the same macros to point to different function objects.

    Return value is a dict `{macroname: function, ...}` with all collected bindings.
    '''
    bindings = {}
    for index, statement in enumerate(tree.body):
        if ismacroimport(statement):
            module_absname, more_bindings = _get_macros(statement, filename=filename, reload=reload)
            bindings.update(more_bindings)
            # Remove all names to prevent macros being used as regular run-time objects.
            # Always use an absolute import, for the unhygienic expose API guarantee.
            tree.body[index] = copy_location(Import(names=[alias(name=module_absname, asname=None)]),
                                             statement)
    return bindings

def ismacroimport(statement):
    '''Return whether `statement` is a macro-import.

    A macro-import is a statement of the form::

        from ... import macros, ...
    '''
    if isinstance(statement, ImportFrom):
        firstimport = statement.names[0]
        if firstimport.name == 'macros' and firstimport.asname is None:
            return True
    return False

def _get_macros(macroimport, *, filename, reload=False):
    '''Get absolute module name, macro names and macro functions from a macro-import.

    As a side effect, import the macro definition module.

    Use the `reload` flag only when implementing a REPL, because it'll refresh modules,
    causing different uses of the same macros to point to different function objects.
    '''
    package_absname = None
    if macroimport.level and filename.endswith(".py"):
        try:
            package_absname = resolve_package(filename)
        except (ValueError, ImportError) as err:
            raise ImportError(f"while resolving absolute package name of {filename}, which uses relative macro-imports") from err

    if macroimport.module is None:
        # fallbacks may trigger if the macro-import is programmatically generated.
        approx_sourcecode = unparse_with_fallbacks(macroimport)
        loc = format_location(filename, macroimport, approx_sourcecode)
        raise SyntaxError(f"{loc}\nmissing module name in macro-import")
    module_absname = importlib.util.resolve_name('.' * macroimport.level + macroimport.module, package_absname)

    module = importlib.import_module(module_absname)
    if reload:
        module = importlib.reload(module)

    return module_absname, {name.asname or name.name: getattr(module, name.name)
                            for name in macroimport.names[1:]}
