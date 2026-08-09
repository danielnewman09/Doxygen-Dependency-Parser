"""
GoogleTest test parsing from Doxygen XML + source bodies (C++).

Doxygen does not understand gtest: a ``TEST_F(DatabaseTest, Foo)`` macro
is emitted as a *file-level function* named ``TEST_F`` whose ``<param>``
elements are ``DatabaseTest`` and ``Foo``.  This module recognises those
macro-functions and converts them into first-class test nodes:

* :class:`~codegraph.models.test.TestNode` — one per gtest test case,
  composed by its source file (``FileNode → COMPOSES → TestNode``).
* :class:`~codegraph.models.test.AssertionNode` — one per ``ASSERT_*`` /
  ``EXPECT_*`` / ``FAIL`` / ``SUCCEED`` statement inside the test body.
* :class:`~codegraph.models.test.TestStepNode` — setup/action blocks
  between the assertion statements (mirroring the Python test parser).

The test body is read straight from the source file using the Doxygen
``location`` ``bodystart``/``bodyend`` line range, so the segmentation
does not depend on Doxygen understanding gtest internals.

Relationships produced (all via the shared
:class:`~doxygen_index.parser.model.ParseResult` lists, consumed by
``graph_json`` / ``neo4j_backend`` / ``json_backend`` / ``csv_export``):

* ``test_compositions`` — TestNode → AssertionNode / TestStepNode.
* ``pending_test_calls`` — every call site in a step/assertion body;
  resolved by ``CppParser.post_process`` into ``CALLEE`` (step → code)
  and ``VERIFIES`` (test → code) edges once the full symbol set is
  known (XML files are parsed in parallel, so parse-time resolution
  would be racy).
* ``implementation_refs`` — each step's source text as an
  :class:`~codegraph.models.implementation.ImplementationNode`
  (``HAS_IMPLEMENTATION``).
"""

from __future__ import annotations

import re
from pathlib import Path

from codegraph import ImplementationNode
from codegraph.models.test import TestNode, AssertionNode, TestStepNode

from doxygen_index.parser.helpers import get_text
from doxygen_index.parser.model import (
    ImplementationRef,
    PendingTestCall,
    TestCompositionEntry,
)

# ---------------------------------------------------------------------------
# Recognised macros
# ---------------------------------------------------------------------------

#: Test-definition macros Doxygen records as file-level functions whose
#: two ``<param>`` elements are ``(suite, test_name)``.
GTEST_TEST_MACROS = frozenset({
    "TEST",
    "TEST_F",
    "TEST_P",
    "TEST_T",
    "TEST_F_P",          # legacy alias
    "TYPED_TEST",
    "TYPED_TEST_P",
    "TYPED_TEST_CASE",
    "TYPED_TEST_CASE_P",
    "TYPED_TEST_CASE_T",
    "TYPED_TEST_SUITE",
    "TYPED_TEST_SUITE_P",
    "TEST_SUITE",
    "TEST_SUITE_P",
})

#: Assertion macros that split a test body into AssertionNode / TestStepNode.
#: The ``ASSERT_*`` / ``EXPECT_*`` families are matched structurally
#: (any suffix), the rest are listed explicitly.
_ASSERT_MACRO_RE = re.compile(
    r"^\s*(("
    r"ASSERT_[A-Z_0-9]+"
    r"|EXPECT_[A-Z_0-9]+"
    r"|GTEST_SKIP|GTEST_FAIL|GTEST_SUCCEED"
    r"|ADD_FAILURE(?:_AT)?"
    r"|FAIL|SUCCEED"
    r"))\s*\("
)

#: Call-site pattern used for CALLEE / VERIFIES resolution:
#: ``std::filesystem::remove(args)``, ``obj.method(args)``, ``func(args)``.
_CALL_RE = re.compile(r"\b((?:\w+::)*\w+)\s*(?:\.\s*(\w+)\s*)?\(")

#: Template method calls the main regex cannot match (``getDAO<TestProduct>()``).
_TEMPLATE_CALL_RE = re.compile(r"\b(\w+)\s*\.\s*(\w+)\s*<[^>]*>\s*\(")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_gtest_test_member(memberdef, fields: dict) -> bool:
    """Return True if *memberdef* is a gtest test-macro function.

    A gtest ``TEST_F(Suite, Name)`` macro is emitted by Doxygen as a
    file-level function named ``TEST_F`` with exactly two ``<param>``
    elements carrying the suite and test name.
    """
    if fields.get("name") not in GTEST_TEST_MACROS:
        return False
    return len(memberdef.findall("param")) == 2


def _test_params(memberdef) -> tuple[str, str] | None:
    """Return ``(suite, test_name)`` from a gtest macro memberdef."""
    params = memberdef.findall("param")
    if len(params) != 2:
        return None
    suite = (get_text(params[0].find("type")) or "").strip()
    test_name = (get_text(params[1].find("type")) or "").strip()
    if not suite or not test_name:
        return None
    # TYPED_TEST(TypedSuite, TestName<TypeParam>) style names keep
    # template args attached; strip them for the node name.
    test_name = re.sub(r"<.*>", "", test_name).strip()
    return suite, test_name


# ---------------------------------------------------------------------------
# Body segmentation
# ---------------------------------------------------------------------------


def _read_body(file_path: str, body_start: int, body_end: int) -> list[str] | None:
    """Read the source lines ``body_start..body_end`` (1-based, inclusive).

    Returns a list of lines (with newlines) or None if the file cannot
    be read.  Relative paths are resolved against the current working
    directory (the Doxygen run dir — same convention as
    ``extract_implementations``).
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except (FileNotFoundError, OSError):
        return None
    if body_start < 1 or body_end < body_start or body_end > len(lines):
        return None
    return lines[body_start - 1:body_end]


def _split_test_body(body_lines: list[str]) -> list[dict]:
    """Segment a test body into step and assertion spans.

    Returns a list of dicts in source order::

        {"type": "step", "start": i, "end": j}          # step block
        {"type": "assert", "start": i, "end": j,       # assertion
         "macro": "ASSERT_EQ", "text": "..."}

    Line indices are 0-based within *body_lines*.  The opening
    ``TEST_F(...) {`` lines (everything up to the first ``{``) are
    skipped.  An assertion span starts at a line whose first token is
    an assertion macro and extends until the statement is balanced
    (paren/brace depth 0) and terminated by ``;`` — so multi-line
    ``ASSERT_NO_THROW({ ... });`` blocks group into one assertion.
    """
    # Skip the macro signature and opening brace.
    start = 0
    for i, line in enumerate(body_lines):
        if "{" in line:
            start = i + 1
            break
    else:
        start = 0

    spans: list[dict] = []
    step_start: int | None = start
    i = start
    depth = 0
    in_assert = False
    assert_start = -1
    assert_macro = ""
    assert_text: list[str] = []

    def _flush_step():
        nonlocal step_start
        if step_start is not None and i > step_start:
            if any(l.strip() for l in body_lines[step_start:i]):
                spans.append({"type": "step", "start": step_start, "end": i})
        step_start = None

    def _flush_assert():
        nonlocal step_start
        spans.append({
            "type": "assert",
            "start": assert_start,
            "end": i,
            "macro": assert_macro,
            "text": "".join(assert_text).strip(),
        })
        step_start = i + 1

    for i in range(start, len(body_lines)):
        line = body_lines[i]
        stripped = line.strip()

        if in_assert:
            assert_text.append(line)
            depth += line.count("(") + line.count("{") + line.count("[")
            depth -= line.count(")") + line.count("}") + line.count("]")
            if depth <= 0 and line.rstrip().endswith(";"):
                _flush_assert()
                in_assert = False
                depth = 0
            continue

        m = _ASSERT_MACRO_RE.match(line)
        if m and step_start is not None and i > step_start:
            _flush_step()

        if m:
            in_assert = True
            depth = 0
            assert_start = i
            assert_macro = m.group(1)
            assert_text = [line]
            depth += line.count("(") + line.count("{") + line.count("[")
            depth -= line.count(")") + line.count("}") + line.count("]")
            if depth <= 0 and line.rstrip().endswith(";"):
                _flush_assert()
                in_assert = False
                depth = 0
        elif step_start is None:
            step_start = i

    if in_assert:
        # Unterminated assert (shouldn't happen with balanced bodies) —
        # emit it anyway so the test content is not lost.
        _flush_assert()

    if step_start is not None and i >= step_start:
        if any(l.strip() for l in body_lines[step_start:i + 1]):
            spans.append({"type": "step", "start": step_start, "end": i + 1})

    return spans


# ---------------------------------------------------------------------------
# CALLEE / VERIFIES resolution
# ---------------------------------------------------------------------------


def _resolve_callee(result, callee_text: str) -> tuple[str, str] | None:
    """Resolve a call-site name to a known node ``(refid, type)``.

    ``::``-qualified call sites (``std::to_string``, ``cpp_sqlite::Logger::getInstance``)
    are resolved exactly against qualified names first — this keeps
    explicit std/boost calls pointing at the real symbol instead of the
    first name match.  Bare names and ``obj.method`` fall back to
    name-based matching (first MethodNode / FunctionNode / ClassNode
    with that name), mirroring the Python parser.

    Called from post-processing (``CppParser.post_process``) when the
    full set of parsed symbols is known.
    """
    if callee_text.startswith("::"):
        callee_text = callee_text.lstrip(":")

    # Exact qualified-name match (handles ``std::to_string`` etc.).
    if "::" in callee_text:
        for m in result.methods:
            if m.qualified_name == callee_text:
                return (m.refid, "MethodNode")
        for f in result.functions:
            if f.qualified_name == callee_text:
                return (f.refid, "FunctionNode")
        for c in result.classes:
            if c.qualified_name == callee_text:
                return (c.refid, "ClassNode")
        # Qualified path with a template instantiation on the tail
        # (``cpp_sqlite::Database::getDAO<TestProduct>``): match by prefix.
        stem = callee_text.split("<")[0].strip()
        for m in result.methods:
            if m.qualified_name == stem:
                return (m.refid, "MethodNode")

    # Methods (obj.method / standalone method reference)
    for m in result.methods:
        if m.name == callee_text:
            return (m.refid, "MethodNode")

    # Functions
    for f in result.functions:
        if f.name == callee_text:
            return (f.refid, "FunctionNode")

    # Classes (constructor calls)
    for c in result.classes:
        if c.name == callee_text:
            return (c.refid, "ClassNode")

    return None





def call_sites(source_text: str) -> list[str]:
    """Extract call-site names from *source_text* (deduped, ordered).

    Yields bare or ``::``-qualified names — resolution against the
    parsed symbol set happens later in post-processing.
    """
    sites: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            sites.append(name)

    for m in _CALL_RE.finditer(source_text):
        # ``obj.method(args)`` → the method name; anything else
        # (``std::filesystem::remove``, ``func``, ``Class``) → the
        # qualified/bare name as-is.
        name = m.group(2) or m.group(1)
        if not name or name in {"if", "for", "while", "switch", "return", "sizeof",
                                "assert", "static_cast", "dynamic_cast", "const_cast",
                                "reinterpret_cast", "decltype", "new", "delete",
                                "noexcept", "alignof", "typeid", "std"}:
            continue
        _add(name)

    # Template calls: ``db.getDAO<TestProduct>()`` — the main regex stops
    # at ``<``, so match the method name explicitly.
    for m in _TEMPLATE_CALL_RE.finditer(source_text):
        _add(m.group(2))

    return sites


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def parse_gtest_test(
    memberdef,
    fields: dict,
    file_stem: str,
    source: str,
    result,
    layer: str,
) -> TestNode | None:
    """Convert a gtest test-macro memberdef into a TestNode + children.

    Creates:

    * a :class:`TestNode` (``kind="test"``),
    * :class:`AssertionNode` children (one per assert statement),
    * :class:`TestStepNode` children (setup/action blocks), each with an
      :class:`ImplementationNode` holding the block's source,
    * ``COMPOSES`` (test → children), ``CALLEE`` (step → code) and
      ``VERIFIES`` (test → code) relationship entries on *result*.

    Returns the TestNode, or None if the memberdef is not a valid gtest
    macro function.
    """
    params = _test_params(memberdef)
    if params is None:
        return None
    suite, test_name = params

    test_qname = f"{file_stem}::{suite}::{test_name}"
    test_refid = fields["refid"] or test_qname

    test_node = TestNode(
        refid=test_refid,
        name=test_name,
        qualified_name=test_qname,
        kind="test",
        test_name=test_name,
        test_module=file_stem,
        method="automated",
        description=fields.get("brief") or "",
        file_path=fields.get("file_path") or "",
        line_number=fields.get("line_number") or 0,
        source=source,
        tags=[layer],
    )
    test_node.layer = layer
    result.tests.append(test_node)

    body_start = fields.get("body_start") or 0
    body_end = fields.get("body_end") or 0
    if not body_start or not body_end or not fields.get("file_path"):
        return test_node  # no body — test node only

    body_lines = _read_body(fields["file_path"], body_start, body_end)
    if not body_lines:
        return test_node

    abs_line = body_start  # 1-based absolute line of the first body line
    spans = _split_test_body(body_lines)

    step_order = 0
    assert_order = 0
    for span in spans:
        if span["type"] == "step":
            start_abs = abs_line + span["start"]
            end_abs = abs_line + span["end"] - 1
            _emit_step(
                result, test_node, test_qname, step_order, source, layer,
                fields, start_abs, end_abs, body_lines[span["start"]:span["end"]],
            )
            step_order += 1
        else:
            _emit_assertion(
                result, test_node, test_qname, assert_order, source, layer,
                fields, span, abs_line + span["start"],
                body_lines[span["start"]:span["end"]],
            )
            assert_order += 1

    return test_node


def _emit_step(
    result,
    test_node: TestNode,
    test_qname: str,
    order: int,
    source: str,
    layer: str,
    fields: dict,
    start_abs: int,
    end_abs: int,
    lines: list[str],
) -> None:
    """Create a TestStepNode + ImplementationNode + CALLEE/VERIFIES."""
    step_qname = f"{test_qname}::step_{order}"
    desc = "Setup block" if order == 0 else f"Action block {order}"

    step = TestStepNode(
        refid=step_qname,
        name=f"step_{order}",
        qualified_name=step_qname,
        kind="test_step",
        order=order,
        description=desc,
        body_start=start_abs,
        body_end=end_abs,
        file_path=fields.get("file_path") or "",
        source=source,
        tags=[layer],
    )
    step.layer = layer
    result.test_steps.append(step)

    result.test_compositions.append(TestCompositionEntry(
        parent_refid=test_node.refid,
        child_refid=step_qname,
        child_type="TestStepNode",
    ))

    source_text = "".join(lines).rstrip("\n")
    if source_text.strip():
        impl_node = ImplementationNode(
            qualified_name=step_qname,
            kind="implementation",
            implementation=source_text,
            impl_embedding=[],
            source=source,
            layer=layer,
        )
        result.implementations.append(impl_node)
        result.implementation_refs.append(ImplementationRef(
            member_refid=step.refid,
            implementation=impl_node,
        ))

    # Record every call site in the block; resolved to CALLEE/VERIFIES
    # edges in post-processing (the full symbol set isn't known yet —
    # XML files are parsed in parallel).
    for callee_text in call_sites(source_text):
        result.pending_test_calls.append(PendingTestCall(
            from_refid=step.refid,
            test_refid=test_node.refid,
            callee_text=callee_text,
            is_assert=False,
        ))


def _emit_assertion(
    result,
    test_node: TestNode,
    test_qname: str,
    order: int,
    source: str,
    layer: str,
    fields: dict,
    span: dict,
    line_abs: int,
    lines: list[str],
) -> None:
    """Create an AssertionNode for one assert statement."""
    assertion_qname = f"{test_qname}::post_{order}"
    macro = span["macro"]
    text = span["text"]

    assertion = AssertionNode(
        refid=assertion_qname,
        name=f"post_{order}",
        qualified_name=assertion_qname,
        kind="assertion",
        phase="post",
        order=order,
        operator=macro,
        description=text or f"assert {macro}",
        file_path=fields.get("file_path") or "",
        line_number=line_abs,
        source=source,
        tags=[layer],
    )
    assertion.layer = layer
    result.assertions.append(assertion)

    result.test_compositions.append(TestCompositionEntry(
        parent_refid=test_node.refid,
        child_refid=assertion_qname,
        child_type="AssertionNode",
    ))

    # Calls inside the assertion body are what the test actually checks
    # (``ASSERT_TRUE(productDAO.isInitialized())``, ``ASSERT_NO_THROW(dao.insert())``)
    # — record them for VERIFIES edges on the test.  (No CALLEE: an
    # assertion is not a step.)  Resolved in post-processing.
    for callee_text in call_sites(span["text"]):
        result.pending_test_calls.append(PendingTestCall(
            from_refid=test_node.refid,
            test_refid=test_node.refid,
            callee_text=callee_text,
            is_assert=True,
        ))
