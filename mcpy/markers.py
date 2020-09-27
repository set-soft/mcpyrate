# -*- coding: utf-8; -*-
"""AST markers for internal communication.

*Internal* here means they are to be never passed to Python's `compile`;
macros may use them to work together.
"""

__all__ = ["ASTMarker", "get_markers", "NestingLevelTracker"]

import ast
from contextlib import contextmanager
from .walkers import Walker

class ASTMarker(ast.AST):
    """Base class for AST markers.

    Markers are AST-node-like entities meant for communication between
    co-operating, related macros. They are also used within the `mcpy`
    macro expander and its subsystems (such as quasiquotes).

    We inherit from `ast.AST`, so that during macro expansion, a marker
    behaves like a single AST node.

    It is a postcondition of a completed macro expansion that no markers
    remain in the AST.

    To help fail-fast, if you define your own marker types, use `get_markers`
    to check (where appropriate) that the expanded AST has no instances of
    your own markers remaining.

    A typical usage example is in the quasiquote system, where the unquote
    operators (some of which expand to markers) may only appear inside a quoted
    section. So just before the quote operator exits, it checks that all
    quasiquote markers within that section have been compiled away.
    """
    def __init__(self, body):
        """body: the actual AST that is annotated by this marker"""
        self.body = body
        self._fields = ["body"]  # support ast.iterfields


def get_markers(tree, cls=ASTMarker):
    """Return a `list` of any `cls` instances found in `tree`. For output validation."""
    class ASTMarkerCollector(Walker):
        def transform(self, tree):
            if isinstance(tree, cls):
                self.collect(tree)
            self.generic_visit(tree)
            return tree
    p = ASTMarkerCollector()
    p.visit(tree)
    return p.collected


class NestingLevelTracker:
    """Track the nesting level in a set of co-operating, related macros.

    Useful for implementing macros that are only syntactically valid inside the
    invocation of another macro (i.e. when the level is `> 0`).
    """
    def __init__(self, start=0):
        """start: int, initial level"""
        self.stack = [start]

    def _get_value(self):
        return self.stack[-1]
    value = property(fget=_get_value, doc="The current level. Read-only.")

    def set_to(self, value):
        """Context manager. Run a section of code with the level set to `value`."""
        if not isinstance(value, int):
            raise TypeError(f"Expected integer `value`, got {type(value)} with value {repr(value)}")
        if value < 0:
            raise ValueError(f"`value` must be >= 0, got {repr(value)}")
        @contextmanager
        def _set_to():
            self.stack.append(value)
            try:
                yield
            finally:
                self.stack.pop()
                assert self.stack  # postcondition
        return _set_to()

    def changed_by(self, delta):
        """Context manager. Run a section of code with the level incremented by `delta`."""
        return self.set_to(self.value + delta)