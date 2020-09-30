# -*- coding: utf-8; -*-
'''Find and expand macros.

This layer provides the actual macro expander, defining:

 - Macro invocation types:
   - expr: `macroname[...]`,
   - block: `with macroname:`,
   - decorator: `@macroname`,
   - name: `macroname`.
 - Syntax for establishing macro bindings:
   - `from module import macros, ...`.
'''

__all__ = ['expand_macros', 'find_macros', 'MacroExpander', 'MacroCollector']

import sys
from ast import (Name, Import, ImportFrom, alias, AST, Expr, Constant,
                 copy_location, iter_fields, NodeVisitor)
from .core import BaseMacroExpander, global_postprocess, Done

class MacroExpander(BaseMacroExpander):
    '''The actual macro expander.'''

    def visit_Subscript(self, subscript):
        '''Detect an expression (expr) macro invocation.

        Detected syntax::

            macroname['index expression is the target of the macro']

        Replace the `SubScript` node with the result of the macro.
        '''
        candidate = subscript.value
        if isinstance(candidate, Name) and self.ismacroname(candidate.id):
            macroname = candidate.id
            tree = subscript.slice.value
            new_tree = self.expand('expr', subscript, macroname, tree, fill_root_location=True)
        else:
            new_tree = self.generic_visit(subscript)

        return new_tree

    def visit_With(self, withstmt):
        '''Detect a block macro invocation.

        Detected syntax::

            with macroname:
                "with's body is the target of the macro"

        Replace the `With` node with the result of the macro.
        '''
        with_item = withstmt.items[0]
        candidate = with_item.context_expr
        if isinstance(candidate, Name) and self.ismacroname(candidate.id):
            macroname = candidate.id
            tree = withstmt.body
            kw = {'optional_vars': with_item.optional_vars}
            new_tree = self.expand('block', withstmt, macroname, tree, fill_root_location=False, kw=kw)
            new_tree = _add_coverage_dummy_node(new_tree, withstmt)
        else:
            new_tree = self.generic_visit(withstmt)

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
                "The whole function is the target of the macro"

        Or::

            @macroname
            class C():
                "The whole class is the target of the macro"

        Replace the whole decorated node with the result of the macro.
        '''
        macros, others = self._detect_decorator_macros(decorated.decorator_list)
        decorated.decorator_list = others
        if macros:
            macros_executed = []
            for macro in reversed(macros):
                macroname = macro.id
                new_tree = self.expand('decorator', decorated, macroname, decorated, fill_root_location=False)
                macros_executed.append(macro)
                if new_tree is None:
                    break
            for macro in macros_executed:
                new_tree = _add_coverage_dummy_node(new_tree, macro)
        else:
            new_tree = self.generic_visit(decorated)

        return new_tree

    def _detect_decorator_macros(self, decorator_list):
        '''Identify macros in a `decorator_list`.

        Return a pair `(macros, others)`, where `macros` is a `list` of macro
        decorator AST nodes, and `others` is a `list` of the decorator AST
        nodes not identified as macros. Ordering is preserved within each
        of the two subsets.
        '''
        macros, others = [], []
        for d in decorator_list:
            if isinstance(d, Name) and self.ismacroname(d.id):
                macros.append(d)
            else:
                others.append(d)

        return macros, others

    def visit_Name(self, name):
        '''Detect an identifier (name) macro invocation.

        Detected syntax::

            macroname

        Replace the `Name` node with the result of the macro.

        The main use case of identifier macros is to define magic variables
        that are only meaningful inside the invocation of some other macro.
        An classic example is the anaphoric if's `it`.

        Use an `mcpy.utilities.NestingLevelTracker` to keep track of whether
        your identifier macro is being expanded inside an invocation of your
        other macro (which must expand inner macros explicitly for the nesting
        level check to be able to detect that). Then, in your identifier macro,
        if it's trying to expand in an invalid context, raise `SyntaxError`
        with an appropriate explanation. When in a valid context, just `return
        tree`. This way any invalid, stray mentions of the magic variable will
        be promoted to compile-time errors.
        '''
        if self.ismacroname(name.id):
            macroname = name.id
            def ismodified(tree):
                return not (type(tree) is Name and tree.id == macroname)
            # Identifier macros are special in that for them, there's no part of the tree
            # that is guaranteed to be compiled away in the expansion.
            #
            # So prevent an infinite loop in case the macro no-ops, returning `tree` as-is.
            # (Most macros are not interested in acting as identifier macros.)
            with self._recursive_mode(False):
                new_tree = self.expand('name', name, macroname, name, fill_root_location=True)
            if self.recursive and new_tree is not None and ismodified(new_tree):
                new_tree = self.visit(new_tree)
        else:
            new_tree = self.generic_visit(name)

        return new_tree


class MacroCollector(NodeVisitor):
    '''Scan `tree` for macro invocations, with respect to a given expander.

    Collect a set where each item is `(macroname, syntax)`.

    Constructor parameters:

        - `expander`: a `MacroExpander` instance to query macro bindings from.

    Usage::

        mc = MacroCollector(expander)
        mc.visit(tree)
        print(mc.collected)

    For implementing debug utilities. The `collected` set being empty is
    especially useful as a stop condition for an automatically one-stepping
    expander.

    This is a sister of the actual `MacroExpander` and closely mirrors how it
    detects macro invocations that are currently in bindings.
    '''
    def __init__(self, expander):
        self.expander = expander
        self.clear()

    def clear(self):
        self.collected = set()

    def ismacroname(self, name):
        return self.expander.ismacroname(name)

    def visit_Subscript(self, subscript):
        candidate = subscript.value
        if isinstance(candidate, Name) and self.ismacroname(candidate.id):
            self.collected.add((candidate.id, 'expr'))
        # We can't just `self.generic_visit(subscript)`, because that'll incorrectly detect
        # the name part of the invocation as an identifier macro. So recurse only where safe.
        self.visit(subscript.slice.value)

    def visit_With(self, withstmt):
        with_item = withstmt.items[0]
        candidate = with_item.context_expr
        if isinstance(candidate, Name) and self.ismacroname(candidate.id):
            self.collected.add((candidate.id, 'block'))
        self.visit(withstmt.body)

    def visit_Decorated(self, decorated):
        macros, decorators = self.expander._detect_decorator_macros(decorated.decorator_list)
        for macro in macros:
            self.collected.add((macro.id, 'decorator'))
        for decorator in decorators:
            self.visit(decorator)
        for field, value in iter_fields(decorated):
            if field == "decorator_list":
                continue
            if isinstance(value, list):
                for node in value:
                    self.visit(node)
            elif isinstance(value, AST):
                self.visit(value)

    def visit_Name(self, name):
        if self.ismacroname(name.id):
            self.collected.add((name.id, 'name'))
        self.generic_visit(name)


def _add_coverage_dummy_node(tree, target):
    '''Force the `target` AST node to be reported as covered by coverage tools.

    This is intended to support tools such as `Coverage.py`, so they can report
    the coverage of block and decorator macro invocations correctly.

    This should be called for each block and decorator macro invocation that
    actually had its macro function called, to support the obvious notion of
    coverage. (For example, in a decorator chain, one decorator macro may
    prevent further ones from running, if it deletes the whole AST node.)

    The line invoking the macro is compiled away, so we insert a dummy node,
    copying source location information from the AST node `target`.

    `tree` must appear in a position where `ast.NodeTransformer.visit` is
    allowed to return a list of nodes. The return value is always a `list`
    of AST nodes.
    '''
    # `target` itself might be macro-generated. In that case don't bother.
    if not hasattr(target, 'lineno') and not hasattr(target, 'col_offset'):
        return tree
    if tree is None:
        tree = []
    elif isinstance(tree, AST):
        tree = [tree]
    # The dummy node must actually run to get coverage, an `ast.Pass` won't do.
    # We must set location info manually, because we run after `expand`.
    non = copy_location(Constant(value=None), target)
    dummy = copy_location(Expr(value=non), target)
    tree.insert(0, Done(dummy))  # mark as Done so any expansions further out won't mess this up.
    return tree


def expand_macros(tree, bindings, filename):
    '''
    Return an expanded version of `tree` with macros applied.
    Perform top-level postprocessing when done.

    This is meant to be called with `tree` the AST of a module that uses macros.

    `bindings` is a dictionary of the macro name/function pairs.

    `filename` is the full path to the `.py` being macroexpanded, for error reporting.
    '''
    expansion = MacroExpander(bindings, filename).visit(tree)
    expansion = global_postprocess(expansion)
    return expansion


def find_macros(tree):
    '''
    Look for `from ... import macros, ...` statements in the module body, and
    return a dict with names and implementations for found macros, or an empty
    dict if no macros are used.

    As a side effect, transform each macro import statement into `import ...`,
    where `...` is the module the macros are being imported from.

    This is meant to be called with `tree` the AST of a module that uses macros.
    '''
    bindings = {}
    for index, statement in enumerate(tree.body):
        if _is_macro_import(statement):
            bindings.update(_get_macros(statement))
            # Remove all names to prevent the macros being accidentally used as regular run-time objects.
            module = statement.module
            tree.body[index] = copy_location(
                Import(names=[alias(name=module, asname=None)]),
                statement
            )

    return bindings

def _is_macro_import(statement):
    '''
    A "macro import" is a statement of the form::

        from ... import macros, ...
    '''
    is_macro_import = False
    if isinstance(statement, ImportFrom):
        firstimport = statement.names[0]
        if firstimport.name == 'macros' and firstimport.asname is None:
            is_macro_import = True

    return is_macro_import

def _get_macros(macroimport):
    '''
    Return a dict with names and macros from the macro import statement.

    As a side effect, import the macro definition module.
    '''
    modulename = macroimport.module
    __import__(modulename)
    module = sys.modules[modulename]
    return {name.asname or name.name: getattr(module, name.name)
             for name in macroimport.names[1:]}
