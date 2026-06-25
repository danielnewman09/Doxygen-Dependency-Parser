"""
Pure AST utility functions for Python source parsing.

These helpers operate on ``ast`` nodes directly and have no dependency on
:class:`~doxygen_index.parser.model.ParseResult`.  They are shared by all
Python parser handler modules.
"""

from __future__ import annotations

import ast
from typing import Optional


#: Directory names that are always excluded when parsing Python source.
#: These cover virtual environments, caches, build artifacts, and tool
#: directories that should never be indexed.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "build",
    "dist",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    ".idea",
    ".vscode",
})


def qualified_name(module: str, name: str) -> str:
    """Build a dotted qualified name from module and local name."""
    if module:
        return f"{module}.{name}"
    return name


def annotation_to_str(node: Optional[ast.expr]) -> str:
    """Convert an AST annotation node to a string representation."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return repr(node.value) if isinstance(node.value, str) else str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{annotation_to_str(node.value)}[{annotation_to_str(node.slice)}]"
    if isinstance(node, ast.List):
        inner = ", ".join(annotation_to_str(e) for e in node.elts)
        return f"[{inner}]"
    if isinstance(node, ast.Tuple):
        inner = ", ".join(annotation_to_str(e) for e in node.elts)
        return f"({inner})"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{annotation_to_str(node.left)} | {annotation_to_str(node.right)}"
    return ast.dump(node)


def is_interface(bases: list[ast.expr]) -> bool:
    """Return True if the class inherits from a known interface base."""
    interface_bases = {"ABC", "Protocol"}
    for base in bases:
        name = annotation_to_str(base)
        if name in interface_bases:
            return True
    return False


def is_enum(bases: list[ast.expr]) -> bool:
    """Return True if the class inherits from a known enum base."""
    enum_bases = {"Enum", "IntEnum", "Flag", "IntFlag", "StrEnum", "ReprEnum"}
    for base in bases:
        name = annotation_to_str(base)
        # Match both "Enum" and "enum.Enum"
        short = name.rsplit(".", 1)[-1] if "." in name else name
        if short in enum_bases:
            return True
    return False


def decorator_names(decorator_list: list[ast.expr]) -> set[str]:
    """Extract the simple names from a list of decorators."""
    names = set()
    for dec in decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                names.add(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.add(dec.func.attr)
    return names


def get_docstring(node: ast.AST) -> str:
    """Extract the docstring from an AST node, cleaning indentation."""
    ds = ast.get_docstring(node, clean=True)
    return ds if ds else ""


def is_private(name: str) -> bool:
    """Return True if a Python name is private (starts with _ but not __)."""
    return name.startswith("_") and not name.startswith("__")


def compare_op_to_str(op: ast.cmpop) -> str:
    """Convert an AST comparison operator to a string representation."""
    op_map = {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.Is: "is",
        ast.IsNot: "is not",
        ast.In: "in",
        ast.NotIn: "not in",
    }
    return op_map.get(type(op), "unknown")


def is_inside_assert(node: ast.AST, func_node: ast.AST) -> bool:
    """Check if *node* is a Call that appears inside an assert test.

    Walks the function body and returns True if *node* is contained
    within an ``ast.Assert`` node's ``test`` expression.
    """
    # Collect all assert nodes' descendant ids
    assert_descendants: set[int] = set()
    for ast_node in ast.walk(func_node):
        if isinstance(ast_node, ast.Assert):
            for child in ast.walk(ast_node.test):
                assert_descendants.add(id(child))
    return id(node) in assert_descendants