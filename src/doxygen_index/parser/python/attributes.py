"""
Class-level attribute node creation from Python AST.

Handles ParseResult elements: ``attributes``.
"""

from __future__ import annotations

import ast

from codegraph import AttributeNode

from doxygen_index.parser.python._ast_utils import annotation_to_str, is_private
from doxygen_index.parser.python._context import ParseContext


def visit_annassign(ctx: ParseContext, node: ast.AnnAssign) -> None:
    """Handle annotated assignments (x: int = 5) inside classes."""
    parent = ctx.current_class
    if parent is None:
        return  # Module-level annotated assignments are not attributes
    if not isinstance(node.target, ast.Name):
        return

    compound_refid, parent_qname = parent
    name = node.target.id
    if name.startswith("_") and not name.startswith("__"):
        protection = "private"
    else:
        protection = "public"

    type_str = annotation_to_str(node.annotation)
    qname = f"{parent_qname}.{name}"

    ctx.result.attributes.append(AttributeNode(
        refid=qname,
        compound_refid=compound_refid,
        kind="variable",
        name=name,
        qualified_name=qname,
        type_signature=type_str,
        definition=f"{name}: {type_str}",
        file_path=ctx.file_path,
        line_number=node.lineno,
        body_start=node.lineno,
        body_end=node.end_lineno or node.lineno,
        brief_description="",
        detailed_description="",
        protection=protection,
        visibility=protection,
        is_static=True,  # class-level attributes are static
        is_const=False,
        source=ctx.source,
        layer=ctx.layer,
    ))


def visit_assign(ctx: ParseContext, node: ast.Assign) -> None:
    """Handle simple assignments (x = 5) inside classes."""
    parent = ctx.current_class
    if parent is None:
        return  # Module-level assignments → skip for now

    compound_refid, parent_qname = parent
    for target in node.targets:
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        # Skip dunder attributes
        if name.startswith("__") and name.endswith("__"):
            continue

        # Skip enum members — they are handled by _add_enum as EnumValueNode
        # Check if the parent class is an enum
        for enum_node in ctx.result.enums:
            if enum_node.refid == compound_refid:
                return

        protection = "private" if is_private(name) else "public"
        qname = f"{parent_qname}.{name}"

        ctx.result.attributes.append(AttributeNode(
            refid=qname,
            compound_refid=compound_refid,
            kind="variable",
            name=name,
            qualified_name=qname,
            type_signature="",
            definition=f"{name} = ...",
            file_path=ctx.file_path,
            line_number=node.lineno,
            body_start=node.lineno,
            body_end=node.end_lineno or node.lineno,
            brief_description="",
            detailed_description="",
            protection=protection,
            visibility=protection,
            is_static=True,
            is_const=False,
            source=ctx.source,
            layer=ctx.layer,
        ))