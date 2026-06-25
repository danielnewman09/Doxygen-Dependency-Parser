"""
Function, method, and parameter node creation from Python AST.

Handles ParseResult elements: ``methods``, ``functions``, ``parameters``.
"""

from __future__ import annotations

import ast

from codegraph import MethodNode, FunctionNode, ParameterNode

from doxygen_index.parser.python._ast_utils import (
    qualified_name,
    get_docstring,
    decorator_names,
    annotation_to_str,
    is_private,
)
from doxygen_index.parser.python._context import ParseContext
from doxygen_index.parser.python import tests as _tests


def visit_function(
    ctx: ParseContext,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    """Dispatch a ``FunctionDef`` or ``AsyncFunctionDef`` to the right handler.

    A top-level function whose name starts with ``test_`` is a pytest test
    function — it is delegated to :mod:`tests`.  Otherwise, a function
    inside a class becomes a :class:`MethodNode` and a module-level function
    becomes a :class:`FunctionNode`.
    """
    parent = ctx.current_class
    decs = decorator_names(node.decorator_list)
    docstring = get_docstring(node)
    brief = docstring.split("\n")[0] if docstring else ""

    # --- pytest test function handling ---
    # A top-level function whose name starts with "test_" is a pytest
    # test function.  Create a TestNode instead of a FunctionNode.
    if not parent and node.name.startswith("test_"):
        _tests.visit_test_function(ctx, node, parent_qname=None)
        return

    # Return type annotation
    return_type = annotation_to_str(node.returns) if node.returns else ""

    # Parameters
    args_info = extract_args(node)

    # Build argsstring
    argsstring = f"({args_info['signature']})"

    if parent:
        # Method inside a class
        compound_refid, parent_qname = parent
        qname = f"{parent_qname}.{node.name}"

        is_static = "staticmethod" in decs
        is_classmethod = "classmethod" in decs
        is_abstract = "abstractmethod" in decs
        is_property = "property" in decs
        is_priv = is_private(node.name)
        protection = "private" if is_priv else "public"

        # Determine method kind
        if is_property:
            kind = "property"
        elif is_classmethod:
            kind = "classmethod"
        elif is_static:
            kind = "staticmethod"
        elif is_abstract:
            kind = "method"  # abstract method is still a method
        else:
            kind = "method"

        definition = f"def {node.name}{argsstring}"
        if return_type:
            definition = f"def {node.name}{argsstring} -> {return_type}"

        ctx.result.methods.append(MethodNode(
            refid=qname,
            compound_refid=compound_refid,
            kind=kind,
            name=node.name,
            qualified_name=qname,
            type_signature=return_type,
            definition=definition,
            argsstring=argsstring,
            file_path=ctx.file_path,
            line_number=node.lineno,
            body_start=node.lineno,
            body_end=node.end_lineno or node.lineno,
            brief_description=brief,
            detailed_description=docstring,
            protection=protection,
            visibility=protection,
            is_static=is_static,
            is_const=False,
            is_constexpr=False,
            is_virtual=is_abstract,
            is_inline=False,
            is_explicit=False,
            source=ctx.source,
            source_type="source",
            layer=ctx.layer,
        ))

        # Add parameters
        for i, (pname, ptype, pdefault) in enumerate(args_info["params"]):
            ctx.result.parameters.append(ParameterNode(
                member_refid=qname,
                position=i,
                name=pname,
                type=ptype,
                default_value=pdefault,
            ))
    else:
        # Free function at module level
        qname = qualified_name(ctx.module_name, node.name)
        definition = f"def {node.name}{argsstring}"
        if return_type:
            definition = f"def {node.name}{argsstring} -> {return_type}"

        ctx.result.functions.append(FunctionNode(
            refid=qname,
            kind="function",
            name=node.name,
            qualified_name=qname,
            type_signature=return_type,
            definition=definition,
            argsstring=argsstring,
            file_path=ctx.file_path,
            line_number=node.lineno,
            body_start=node.lineno,
            body_end=node.end_lineno or node.lineno,
            brief_description=brief,
            detailed_description=docstring,
            source=ctx.source,
            source_type="source",
            layer=ctx.layer,
        ))

        # Add parameters
        for i, (pname, ptype, pdefault) in enumerate(args_info["params"]):
            ctx.result.parameters.append(ParameterNode(
                member_refid=qname,
                position=i,
                name=pname,
                type=ptype,
                default_value=pdefault,
            ))


def extract_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """Extract parameter info from a function definition.

    Returns a dict with:
        - signature: the argument signature string (e.g. "self, x: int, y: int = 0")
        - params: list of (name, type_str, default_str) tuples
    """
    args = node.args
    param_parts: list[str] = []
    params: list[tuple[str, str, str]] = []

    # Positional args
    for i, arg in enumerate(args.args):
        name = arg.arg
        type_str = annotation_to_str(arg.annotation) if arg.annotation else ""

        # Find default value
        default_idx = i - (len(args.args) - len(args.defaults))
        if default_idx >= 0:
            default_str = annotation_to_str(args.defaults[default_idx])
            params.append((name, type_str, default_str))
            param_parts.append(f"{name}: {type_str} = {default_str}" if type_str else f"{name}={default_str}")
        else:
            params.append((name, type_str, ""))
            param_parts.append(f"{name}: {type_str}" if type_str else name)

    # *args
    if args.vararg:
        name = args.vararg.arg
        type_str = annotation_to_str(args.vararg.annotation) if args.vararg.annotation else ""
        params.append((name, type_str, ""))
        param_parts.append(f"*{name}: {type_str}" if type_str else f"*{name}")

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        name = arg.arg
        type_str = annotation_to_str(arg.annotation) if arg.annotation else ""
        default_idx = i - (len(args.kwonlyargs) - len(args.kw_defaults))
        if default_idx >= 0 and args.kw_defaults[default_idx] is not None:
            default_str = annotation_to_str(args.kw_defaults[default_idx])
            params.append((name, type_str, default_str))
            param_parts.append(f"{name}: {type_str} = {default_str}" if type_str else f"{name}={default_str}")
        else:
            params.append((name, type_str, ""))
            param_parts.append(f"{name}: {type_str}" if type_str else name)

    # **kwargs
    if args.kwarg:
        name = args.kwarg.arg
        type_str = annotation_to_str(args.kwarg.annotation) if args.kwarg.annotation else ""
        params.append((name, type_str, ""))
        param_parts.append(f"**{name}: {type_str}" if type_str else f"**{name}")

    return {
        "signature": ", ".join(param_parts),
        "params": params,
    }