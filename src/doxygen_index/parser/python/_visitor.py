"""
Thin AST visitor dispatcher for Python source parsing.

Delegates each ``ast`` node type to the appropriate handler module
(:mod:`classes`, :mod:`functions`, :mod:`attributes`).  All state is
carried by :class:`~doxygen_index.parser.python._context.ParseContext`,
so this class is intentionally minimal.
"""

from __future__ import annotations

import ast

from doxygen_index.parser.python._context import ParseContext
from doxygen_index.parser.python import classes, functions, attributes


class _PythonVisitor(ast.NodeVisitor):
    """Walk an AST tree and dispatch to handler modules.

    Each ``visit_*`` method delegates to the corresponding handler function
    in :mod:`classes`, :mod:`functions`, or :mod:`attributes`.  The shared
    :class:`ParseContext` carries all mutable state (result accumulator,
    class stack, fixture tracking).
    """

    def __init__(self, ctx: ParseContext):
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Class definitions
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        classes.visit_classdef(self.ctx, node, self)

    # ------------------------------------------------------------------
    # Function / method definitions
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        functions.visit_function(self.ctx, node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        functions.visit_function(self.ctx, node)

    # ------------------------------------------------------------------
    # Class-level attributes
    # ------------------------------------------------------------------

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        attributes.visit_annassign(self.ctx, node)

    def visit_Assign(self, node: ast.Assign) -> None:
        attributes.visit_assign(self.ctx, node)