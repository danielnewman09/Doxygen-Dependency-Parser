"""
Pytest test function, assertion, step, and fixture parsing from Python AST.

Handles ParseResult elements: ``tests``, ``assertions``, ``test_steps``,
``test_fixtures``, ``literals``, ``verifies``, ``operands``, ``callees``,
``test_compositions``, ``fixture_of_types``, ``fixture_checked_by``,
``fixture_defined_in``, ``implementations``, ``implementation_refs``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from codegraph import ClassNode, ImplementationNode
from codegraph.models.test import (
    TestNode, AssertionNode, TestStepNode, TestFixtureNode,
)
from codegraph.models.literal import LiteralNode

from doxygen_index.parser.model import (
    ImplementationRef,
    VerifiesEntry,
    OperandEntry,
    CalleeEntry,
    TestCompositionEntry,
    FixtureOfTypeEntry,
    FixtureCheckedByEntry,
    FixtureDefinedInEntry,
)
from doxygen_index.parser.python._ast_utils import (
    qualified_name,
    get_docstring,
    compare_op_to_str,
    annotation_to_str,
)
from doxygen_index.parser.python._context import ParseContext


# ---------------------------------------------------------------------------
# Test function entry point
# ---------------------------------------------------------------------------


def visit_test_function(
    ctx: ParseContext,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    parent_qname: str | None = None,
) -> None:
    """Parse a pytest test function/method into a TestNode + children.

    Splits the function body into logical segments separated by
    ``assert`` statements:

    1. **Setup block** — all statements before the first ``assert``
       become a single :class:`TestStepNode` (order 0) with its source
       code in an :class:`ImplementationNode` linked via
       ``HAS_IMPLEMENTATION``.
    2. **Assert statements** — each ``assert`` becomes an
       :class:`AssertionNode` (phase ``"post"``).
    3. **Action blocks** — non-assert statements between/after asserts
       become additional :class:`TestStepNode` instances.

    All function calls within a step block are resolved to ``CALLEE``
    edges on that step (not one step per call).  Each resolved callee
    also produces a ``VERIFIES`` edge from the TestNode.
    """
    if parent_qname:
        test_qname = f"{parent_qname}.{node.name}"
    else:
        test_qname = qualified_name(ctx.module_name, node.name)
    test_refid = test_qname

    docstring = get_docstring(node)
    brief = docstring.split("\n")[0] if docstring else ""

    test_node = TestNode(
        refid=test_refid,
        name=node.name,
        qualified_name=test_qname,
        kind="test",
        test_name=node.name,
        test_module=ctx.module_name,
        description=brief,
        file_path=ctx.file_path,
        line_number=node.lineno,
        source=ctx.source,
    )
    test_node.layer = ctx.layer
    ctx.result.tests.append(test_node)

    # --- Extract named test instances (test_fixture nodes) ---
    # Walk the body for assignments like ``evaluator = Evaluator(0.0)``
    # and create TestFixtureNode entries with OF_TYPE edges to the
    # type definition.
    var_to_type = _extract_test_instances(ctx, node, test_refid, test_qname)

    # Segment the body: split on ast.Assert, including asserts
    # nested inside compound statements (try/except, for, if, etc.).
    body = node.body
    # Skip docstring (first Expr with Constant str)
    start_idx = 0
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        start_idx = 1

    # Find all assert statements and their containing top-level stmt
    def _find_asserts(stmt):
        """Yield (lineno, assert_node) for all asserts inside stmt."""
        for child in ast.walk(stmt):
            if isinstance(child, ast.Assert):
                yield (child.lineno, child)

    # Collect all asserts with their line numbers, sorted
    all_asserts: list[tuple[int, ast.Assert]] = []
    for stmt in body[start_idx:]:
        all_asserts.extend(_find_asserts(stmt))
    all_asserts.sort(key=lambda x: x[0])

    step_order = 0
    assert_order = 0
    current_block: list[ast.stmt] = []

    for stmt in body[start_idx:]:
        # Check if this statement contains any asserts
        stmt_asserts = [a for ln, a in all_asserts
                       if ln >= stmt.lineno and ln <= (stmt.end_lineno or stmt.lineno)]

        if not stmt_asserts:
            # No asserts in this statement — add to current block
            current_block.append(stmt)
        else:
            # This statement contains asserts — flush current block
            if current_block:
                _process_step_block(
                    ctx, current_block, test_refid, test_qname,
                    step_order, node, var_to_type,
                )
                step_order += 1
                current_block = []

            # Process all asserts inside this statement (in line order)
            stmt_asserts.sort(key=lambda a: a.lineno)
            for assert_stmt in stmt_asserts:
                _process_assert(
                    ctx, assert_stmt, test_refid, test_qname, assert_order,
                    var_to_type,
                )
                assert_order += 1

    # Flush trailing block (statements after the last assert)
    if current_block:
        _process_step_block(
            ctx, current_block, test_refid, test_qname,
            step_order, node, var_to_type,
        )

    # --- Link fixtures to their defining steps (DEFINED_IN) ---
    for fixture in ctx.result.test_fixtures:
        if fixture.qualified_name.startswith(test_qname + "::"):
            fixture_line = getattr(fixture, "line_number", 0)
            for step in ctx.result.test_steps:
                if (step.qualified_name.startswith(test_qname + "::")
                        and step.body_start <= fixture_line <= step.body_end):
                    ctx.result.fixture_defined_in.append(
                        FixtureDefinedInEntry(
                            from_refid=fixture.refid,
                            to_refid=step.refid,
                        )
                    )
                    break


# ---------------------------------------------------------------------------
# Test fixture extraction
# ---------------------------------------------------------------------------


def _extract_test_instances(
    ctx: ParseContext,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    test_refid: str,
    test_qname: str,
) -> dict[str, str]:
    """Extract named variable instances from test assignments.

    Scans the test function body for assignments of the form::

        var = ClassName(args)
        var = ClassName.classmethod(args)
        var = module.ClassName(args)

    For each match, creates a :class:`TestFixtureNode` with an
    ``OF_TYPE`` relationship to the type definition and a
    ``COMPOSES`` edge from the parent TestNode.

    Returns:
        A dict mapping variable names (e.g. ``"evaluator"``) to
        the refid of the type definition's ClassNode (e.g.
        ``"samplepkg.backend.Evaluator"``).  This dict is passed
        to ``_resolve_operand`` and ``_resolve_callee`` for
        type-aware resolution.
    """
    var_to_type: dict[str, str] = {}

    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Assign):
            var_name, class_refid, class_type = _resolve_assignment_type(ctx, stmt)
            if var_name is None or class_refid is None:
                continue
            # Create a TestFixtureNode for this instance
            fixture_qname = f"{test_qname}::{var_name}"
            fixture = TestFixtureNode(
                refid=fixture_qname,
                name=var_name,
                qualified_name=fixture_qname,
                kind="test_fixture",
                type_signature=class_refid.rpartition(".")[2] if class_refid else "",
                tags=["as-built"],
                source=ctx.source,
            )
            # Plain attributes for parser-internal use
            fixture.layer = ctx.layer
            fixture.file_path = ctx.file_path
            fixture.line_number = stmt.lineno
            ctx.result.test_fixtures.append(fixture)

            ctx.result.fixture_of_types.append(FixtureOfTypeEntry(
                from_refid=fixture_qname,
                to_refid=class_refid,
                to_type=class_type,
            ))
            # Compose the fixture within the test
            ctx.result.test_compositions.append(TestCompositionEntry(
                parent_refid=test_refid,
                child_refid=fixture_qname,
                child_type="TestFixtureNode",
            ))
            var_to_type[var_name] = class_refid

    return var_to_type


def _resolve_assignment_type(
    ctx: ParseContext,
    stmt: ast.Assign,
) -> tuple[str | None, str | None, str]:
    """Determine the variable name and class refid from an assignment.

    Handles:
    - ``var = ClassName(args)`` → ("var", "ClassName refid", "ClassNode")
    - ``var = ClassName.classmethod(args)`` → same
    - ``var = module.ClassName(args)`` → same
    - ``var = ClassName(args).save()`` → same (unwraps .save())
    - ``var = ClassName.classmethod(args).save()`` → same

    Returns:
        (var_name, class_refid, class_type) or (None, None, "")
    """
    # Must have exactly one target that is a simple Name
    if len(stmt.targets) != 1:
        return (None, None, "")
    target = stmt.targets[0]
    if not isinstance(target, ast.Name):
        return (None, None, "")
    var_name = target.id

    # The value must be a Call
    if not isinstance(stmt.value, ast.Call):
        return (None, None, "")

    call = stmt.value
    func = call.func

    # Unwrap .save() pattern: ClassName(args).save()
    # The outer call is .save(), the inner call is the constructor
    if isinstance(func, ast.Attribute) and func.attr == "save":
        inner = func.value
        if isinstance(inner, ast.Call):
            call = inner
            func = call.func

    # Direct constructor: ClassName(args)
    if isinstance(func, ast.Name):
        class_name = func.id
        class_refid, class_type = _find_class_by_name(ctx, class_name)
        return (var_name, class_refid, class_type)

    # Attribute call: ClassName.classmethod(args) or obj.method(args)
    if isinstance(func, ast.Attribute):
        obj = func.value
        # ClassName.classmethod(args) — obj is a Name referencing the class
        if isinstance(obj, ast.Name):
            class_name = obj.id
            class_refid, class_type = _find_class_by_name(ctx, class_name)
            if class_refid is not None:
                return (var_name, class_refid, class_type)
        # module.ClassName(args) — obj is module, func is ClassName
        elif isinstance(obj, ast.Attribute):
            class_name = func.attr
            class_refid, class_type = _find_class_by_name(ctx, class_name)
            return (var_name, class_refid, class_type)

    return (None, None, "")


def _find_class_by_name(ctx: ParseContext, name: str) -> tuple[str | None, str]:
    """Find a ClassNode by short name and return (refid, type).

    Returns (None, "") if no class matches.
    """
    for c in ctx.result.classes:
        if c.name == name:
            return (c.refid, "ClassNode")
    return (None, "")


def _find_class_by_refid(ctx: ParseContext, refid: str) -> ClassNode | None:
    """Find a ClassNode by its refid."""
    for c in ctx.result.classes:
        if c.refid == refid:
            return c
    return None


# ---------------------------------------------------------------------------
# Assertion processing
# ---------------------------------------------------------------------------


def _process_assert(
    ctx: ParseContext,
    stmt: ast.Assert,
    test_refid: str,
    test_qname: str,
    order: int,
    var_to_type: dict[str, str] | None = None,
) -> None:
    """Extract an AssertionNode from an ``assert`` statement."""
    assertion_qname = f"{test_qname}::post_{order}"
    assertion_refid = assertion_qname

    operator = "truthy"
    left_node = stmt.test
    right_node = None

    # Unwrap boolean negation
    if isinstance(left_node, ast.UnaryOp) and isinstance(left_node.op, ast.Not):
        operator = "not_truthy"
        left_node = left_node.operand

    # Comparison: assert left OP right
    if isinstance(left_node, ast.Compare) and len(left_node.ops) == 1:
        op = left_node.ops[0]
        right_node = left_node.comparators[0]
        left_node = left_node.left
        operator = compare_op_to_str(op)

    # isinstance check: assert isinstance(x, T) → left_node = x
    if isinstance(left_node, ast.Call):
        if (isinstance(left_node.func, ast.Name)
                and left_node.func.id == "isinstance"):
            operator = "isinstance"
            if left_node.args:
                left_node = left_node.args[0]

    assertion = AssertionNode(
        refid=assertion_refid,
        name=f"post_{order}",
        qualified_name=assertion_qname,
        kind="assertion",
        phase="post",
        order=order,
        operator=operator,
        description=f"assert {operator}",
        source=ctx.source,
    )
    assertion.layer = ctx.layer
    assertion.file_path = ctx.file_path
    ctx.result.assertions.append(assertion)

    ctx.result.test_compositions.append(TestCompositionEntry(
        parent_refid=test_refid,
        child_refid=assertion_refid,
        child_type="AssertionNode",
    ))

    # Track operand count before processing
    ops_before = len(ctx.result.operands)

    # Left operand
    _process_operand(ctx, left_node, assertion_refid, "left", var_to_type)
    left_resolved = len(ctx.result.operands) > ops_before

    # Right operand (only for comparisons)
    right_resolved = False
    if right_node is not None:
        _process_operand(ctx, right_node, assertion_refid, "right", var_to_type)
        right_resolved = len(ctx.result.operands) > ops_before + (1 if left_resolved else 0)

    # Fallback: if the expected operand pattern is incomplete,
    # put the full assert text in the operator field so the
    # assertion still carries useful info.
    # Both LEFT_OPERAND and RIGHT_OPERAND must be extracted;
    # truthy assertions (which have no right operand) always
    # fall back to the full text.
    pattern_complete = left_resolved and right_resolved
    if not pattern_complete:
        # Remove any partial operand edges that were added —
        # the full text in the operator field is the complete
        # representation, and partial edges alongside it would
        # be inconsistent.
        ctx.result.operands = [
            op for op in ctx.result.operands
            if op.from_refid != assertion_refid
        ]
        try:
            full_text = ast.unparse(stmt)
        except Exception:
            full_text = f"assert {operator}"
        assertion.operator = full_text
        assertion.description = full_text

    # --- Record CHECKED_BY edges from fixtures to this assertion ---
    if var_to_type:
        for fixture_name in ctx.checked_fixtures:
            fixture_refid = f"{test_qname}::{fixture_name}"
            ctx.result.fixture_checked_by.append(FixtureCheckedByEntry(
                from_refid=fixture_refid,
                to_refid=assertion_refid,
            ))
    ctx.checked_fixtures.clear()


def _process_operand(
    ctx: ParseContext,
    expr: ast.expr,
    assertion_refid: str,
    side: str,
    var_to_type: dict[str, str] | None = None,
) -> None:
    """Resolve an operand expression to a code node or literal."""
    result = _resolve_operand(ctx, expr, var_to_type)
    if result is None:
        return
    refid, target_type = result
    ctx.result.operands.append(OperandEntry(
        from_refid=assertion_refid,
        to_refid=refid,
        to_type=target_type,
        side=side,
    ))


def _resolve_operand(
    ctx: ParseContext,
    expr: ast.expr,
    var_to_type: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve an operand expression to a (refid, type) tuple.

    Handles:
    - Attribute access (e.g. ``evaluator.current``) → AttributeNode/MethodNode by name
    - Name (e.g. ``result``) → matching attribute/variable
    - Constant literals (numbers, strings, booleans) → LiteralNode
    - Method calls (e.g. ``evaluator.current``) → MethodNode

    When *var_to_type* is provided (a mapping of variable names to
    their class refids), attribute access is resolved more precisely:
    ``evaluator.current`` first looks for a ``current`` member on
    the ``Evaluator`` class specifically, rather than matching any
    attribute named ``current``.
    """
    # Attribute access: obj.attr or obj.method()
    if isinstance(expr, ast.Attribute):
        attr_name = expr.attr
        obj_expr = expr.value

        # If the object is a known test instance, resolve the
        # attribute against that specific class's members first.
        if var_to_type and isinstance(obj_expr, ast.Name):
            obj_name = obj_expr.id
            class_refid = var_to_type.get(obj_name)
            if class_refid:
                # Track that this fixture is checked by the assertion
                ctx.checked_fixtures.add(obj_name)
                # Try methods on this class
                for m in ctx.result.methods:
                    if m.name == attr_name and m.compound_refid == class_refid:
                        return (m.refid, "MethodNode")
                # Try attributes on this class
                for a in ctx.result.attributes:
                    if a.name == attr_name and a.compound_refid == class_refid:
                        return (a.refid, "AttributeNode")

        # Fall back to name-based matching
        # Try to find a matching AttributeNode or MethodNode by name suffix
        for m in ctx.result.methods:
            if m.name == attr_name:
                return (m.refid, "MethodNode")
        for a in ctx.result.attributes:
            if a.name == attr_name:
                return (a.refid, "AttributeNode")
        return None

    # Name: a variable or simple reference
    if isinstance(expr, ast.Name):
        name = expr.id
        # If this matches a test fixture, track it for CHECKED_BY
        if var_to_type and name in var_to_type:
            ctx.checked_fixtures.add(name)
            return None
        # Try to find matching attributes/functions by short name
        for a in ctx.result.attributes:
            if a.name == name:
                return (a.refid, "AttributeNode")
        for f in ctx.result.functions:
            if f.name == name:
                return (f.refid, "FunctionNode")
        return None

    # Constant literal (Python 3.8+)
    if isinstance(expr, ast.Constant):
        return _create_literal(ctx, expr.value)

    # Call: e.g. evaluator.current() — resolve to the method
    if isinstance(expr, ast.Call):
        return _resolve_operand(ctx, expr.func, var_to_type)

    return None


def _create_literal(
    ctx: ParseContext,
    value,
) -> tuple[str, str] | None:
    """Create a LiteralNode for a primitive value and return its refid."""
    if value is None:
        return None

    # Determine value_type
    if isinstance(value, bool):
        value_type = "boolean"
        val_str = str(value).lower()
    elif isinstance(value, int):
        value_type = "int"
        val_str = str(value)
    elif isinstance(value, float):
        value_type = "float"
        val_str = str(value)
    elif isinstance(value, str):
        value_type = "string"
        val_str = value
    else:
        return None

    lit_qname = f"literal::{val_str}"

    # Check if we already created this literal
    for lit in ctx.result.literals:
        if lit.qualified_name == lit_qname:
            return (lit.refid, "LiteralNode")

    lit = LiteralNode(
        refid=lit_qname,
        name=val_str,
        qualified_name=lit_qname,
        kind="literal",
        value=val_str,
        value_type=value_type,
        source=ctx.source,
    )
    lit.layer = ctx.layer
    ctx.result.literals.append(lit)
    return (lit_qname, "LiteralNode")


# ---------------------------------------------------------------------------
# Step block processing
# ---------------------------------------------------------------------------


def _process_step_block(
    ctx: ParseContext,
    statements: list[ast.stmt],
    test_refid: str,
    test_qname: str,
    order: int,
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    var_to_type: dict[str, str] | None = None,
) -> None:
    """Create a TestStepNode from a block of non-assert statements.

    The block carries its source code via an ImplementationNode linked
    through ``HAS_IMPLEMENTATION``.  All ``ast.Call`` nodes within the
    block are resolved to ``CALLEE`` edges on this single step.
    """
    step_qname = f"{test_qname}::step_{order}"
    step_refid = step_qname

    # Determine the source line range of this block
    body_start = statements[0].lineno
    body_end = max(
        (s.end_lineno or s.lineno) for s in statements
    )

    # Description: "Setup block" for order 0, "Action block" otherwise
    desc = "Setup block" if order == 0 else f"Action block {order}"

    step = TestStepNode(
        refid=step_refid,
        name=f"step_{order}",
        qualified_name=step_qname,
        kind="test_step",
        order=order,
        description=desc,
        body_start=body_start,
        body_end=body_end,
        source=ctx.source,
    )
    step.layer = ctx.layer
    step.file_path = ctx.file_path
    ctx.result.test_steps.append(step)

    ctx.result.test_compositions.append(TestCompositionEntry(
        parent_refid=test_refid,
        child_refid=step_refid,
        child_type="TestStepNode",
    ))

    # --- Extract ImplementationNode with source code ---
    source_text = _read_source_block(ctx, body_start, body_end)
    if source_text and source_text.strip():
        impl_node = ImplementationNode(
            qualified_name=step_qname,
            kind="implementation",
            implementation=source_text,
            impl_embedding=[],
            source=ctx.source,
        )
        impl_node.layer = ctx.layer
        ctx.result.implementations.append(impl_node)
        ctx.result.implementation_refs.append(ImplementationRef(
            member_refid=step_refid,
            implementation=impl_node,
        ))

    # --- Resolve all calls within the block as CALLEE edges ---
    seen_callees: set[str] = set()
    for stmt in statements:
        for child in ast.walk(stmt):
            if not isinstance(child, ast.Call):
                continue
            callee_result = _resolve_callee(ctx, child, var_to_type)
            if callee_result is not None:
                callee_refid, callee_type = callee_result
                if callee_refid in seen_callees:
                    continue
                seen_callees.add(callee_refid)
                ctx.result.callees.append(CalleeEntry(
                    from_refid=step_refid,
                    to_refid=callee_refid,
                    to_type=callee_type,
                ))
                ctx.result.verifies.append(VerifiesEntry(
                    from_refid=test_refid,
                    to_refid=callee_refid,
                    to_type=callee_type,
                ))


def _read_source_block(ctx: ParseContext, start_line: int, end_line: int) -> str:
    """Read source text for a line range from the current file.

    Lines are 1-based and inclusive on both ends.
    """
    try:
        lines = Path(ctx.file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
        start = start_line - 1  # 0-based
        end = end_line             # 1-based inclusive → slice end
        return "".join(lines[start:end]).rstrip("\n")
    except (FileNotFoundError, OSError):
        return ""


def _resolve_callee(
    ctx: ParseContext,
    call: ast.Call,
    var_to_type: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve a call expression to the called method/function.

    Handles:
    - ``obj.method(args)`` → find MethodNode by method name
    - ``func(args)`` → find FunctionNode by function name
    - ``Class()`` → find ClassNode by class name
    - ``module.func(args)`` → find FunctionNode by function name

    When *var_to_type* is provided, ``obj.method()`` first looks for
    a ``method`` on the specific class that ``obj`` is an instance of,
    rather than matching any method with that name.
    """
    func = call.func

    # Attribute call: obj.method()
    if isinstance(func, ast.Attribute):
        method_name = func.attr
        obj_expr = func.value

        # If the object is a known test instance, resolve the method
        # against that specific class's members first.
        if var_to_type and isinstance(obj_expr, ast.Name):
            obj_name = obj_expr.id
            class_refid = var_to_type.get(obj_name)
            if class_refid:
                for m in ctx.result.methods:
                    if m.name == method_name and m.compound_refid == class_refid:
                        return (m.refid, "MethodNode")
                # Also check classmethods like Evaluator.from_zero
                for c in ctx.result.classes:
                    if c.refid == class_refid:
                        for m in ctx.result.methods:
                            if m.name == method_name:
                                return (m.refid, "MethodNode")

        # Fall back to name-based matching
        for m in ctx.result.methods:
            if m.name == method_name:
                return (m.refid, "MethodNode")
        return None

    # Simple name call: func()
    if isinstance(func, ast.Name):
        name = func.id
        # Try functions first
        for f in ctx.result.functions:
            if f.name == name:
                return (f.refid, "FunctionNode")
        # Try classes (constructor call)
        for c in ctx.result.classes:
            if c.name == name:
                return (c.refid, "ClassNode")
        # Try class methods like Evaluator.from_zero
        for m in ctx.result.methods:
            if m.name == name:
                return (m.refid, "MethodNode")
        return None

    return None