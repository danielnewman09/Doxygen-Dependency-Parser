"""
Class, interface, and enum node creation from Python AST.

Handles ParseResult elements: ``classes``, ``interfaces``, ``enums``,
``enum_values``.
"""

from __future__ import annotations

import ast

from codegraph import ClassNode, InterfaceNode, EnumNode, EnumValueNode

from doxygen_index.parser.python._ast_utils import (
    qualified_name,
    get_docstring,
    decorator_names,
    annotation_to_str,
    is_interface,
    is_enum,
)
from doxygen_index.parser.python._context import ParseContext
from doxygen_index.parser.python import tests as _tests


def visit_classdef(
    ctx: ParseContext,
    node: ast.ClassDef,
    visitor: ast.NodeVisitor,
) -> None:
    """Dispatch a ``ClassDef`` node to the appropriate handler.

    Determines whether the class is a pytest test class, an interface,
    an enum, or a regular class, and delegates accordingly.
    """
    class_qname = qualified_name(ctx.module_name, node.name)
    refid = class_qname
    docstring = get_docstring(node)
    brief = docstring.split("\n")[0] if docstring else ""
    detailed = docstring
    decs = decorator_names(node.decorator_list)
    base_classes = [annotation_to_str(b) for b in node.bases]

    # --- pytest test class handling ---
    # A class whose name starts with "Test" is a pytest test class.
    # It is NOT a regular ClassNode — its test_* methods become TestNodes.
    # Non-test methods are visited normally (helper/setup methods).
    if node.name.startswith("Test") and not is_interface(node.bases) and not is_enum(node.bases):
        ctx.class_stack.append((refid, class_qname))
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                _tests.visit_test_function(ctx, child, parent_qname=class_qname)
            else:
                visitor.generic_visit(child)
        ctx.class_stack.pop()
        return

    # Determine the node type
    if is_interface(node.bases):
        _add_interface(ctx, node, refid, class_qname, brief, detailed, base_classes, decs)
    elif is_enum(node.bases):
        _add_enum(ctx, node, refid, class_qname, brief, detailed, base_classes, decs)
    else:
        _add_class(ctx, node, refid, class_qname, brief, detailed, base_classes, decs)

    # Push class context and visit children
    ctx.class_stack.append((refid, class_qname))
    visitor.generic_visit(node)
    ctx.class_stack.pop()


def _add_class(
    ctx: ParseContext,
    node: ast.ClassDef,
    refid: str,
    qname: str,
    brief: str,
    detailed: str,
    base_classes: list[str],
    decs: set[str],
) -> None:
    is_abstract = "abstractmethod" in decs or "ABC" in {b for b in base_classes}
    module = qname.rsplit(".", 1)[0] if "." in qname else ""
    ctx.result.classes.append(ClassNode(
        refid=refid,
        kind="class",
        name=node.name,
        qualified_name=qname,
        file_path=ctx.file_path,
        line_number=node.lineno,
        body_start=node.lineno,
        body_end=node.end_lineno or node.lineno,
        brief_description=brief,
        detailed_description=detailed,
        definition=f"class {node.name}",
        module=module,
        base_classes=base_classes,
        is_final=False,
        is_abstract=is_abstract,
        source=ctx.source,
        source_type="source",
        layer=ctx.layer,
    ))


def _add_interface(
    ctx: ParseContext,
    node: ast.ClassDef,
    refid: str,
    qname: str,
    brief: str,
    detailed: str,
    base_classes: list[str],
    decs: set[str],
) -> None:
    module = qname.rsplit(".", 1)[0] if "." in qname else ""
    ctx.result.interfaces.append(InterfaceNode(
        refid=refid,
        kind="interface",
        name=node.name,
        qualified_name=qname,
        file_path=ctx.file_path,
        line_number=node.lineno,
        brief_description=brief,
        detailed_description=detailed,
        definition=f"class {node.name}",
        module=module,
        is_abstract=True,
        source=ctx.source,
        source_type="source",
        layer=ctx.layer,
    ))


def _add_enum(
    ctx: ParseContext,
    node: ast.ClassDef,
    refid: str,
    qname: str,
    brief: str,
    detailed: str,
    base_classes: list[str],
    decs: set[str],
) -> None:
    module = qname.rsplit(".", 1)[0] if "." in qname else ""
    ctx.result.enums.append(EnumNode(
        refid=refid,
        kind="enum",
        name=node.name,
        qualified_name=qname,
        file_path=ctx.file_path,
        line_number=node.lineno,
        brief_description=brief,
        detailed_description=detailed,
        definition=f"class {node.name}",
        module=module,
        source=ctx.source,
        source_type="source",
        layer=ctx.layer,
    ))
    # Extract enum values from class body
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    value_qname = f"{qname}.{target.id}"
                    ctx.result.enum_values.append(EnumValueNode(
                        refid=value_qname,
                        compound_refid=refid,
                        kind="enumvalue",
                        name=target.id,
                        qualified_name=value_qname,
                        file_path=ctx.file_path,
                        line_number=stmt.lineno,
                        body_start=stmt.lineno,
                        body_end=stmt.end_lineno or stmt.lineno,
                        brief_description="",
                        detailed_description="",
                        source=ctx.source,
                        layer=ctx.layer,
                    ))