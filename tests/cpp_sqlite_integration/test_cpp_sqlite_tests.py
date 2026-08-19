"""cpp-sqlite test-node coverage tests.

The cpp-sqlite fixture has real GoogleTest tests
(``cpp_sqlite/test/testDatabase.cpp``, 26 ``TEST_F(DatabaseTest, …)``
cases).  This module verifies that the as-built graph actually contains
them as first-class test nodes:

* every ``TEST_F`` in the fixture source has a ``TestNode``
  (``kind="test"``, qualified name ``testDatabase::DatabaseTest::<Name>``),
* every ``ASSERT_*`` / ``EXPECT_*`` statement became an
  ``AssertionNode`` (``kind="assertion"``) composed by its test,
* every setup/action block became a ``TestStepNode`` (``kind="test_step"``)
  composed by its test,
* tests carry ``VERIFIES`` edges to the cpp-sqlite code they exercise,
  and steps carry ``CALLEE`` edges — all resolving to nodes in the graph,
* all test nodes are tagged ``as-built`` with source ``cpp-sqlite``.

Runs against the session-scoped ``codegraph_graph`` fixture (shared
ingest — sqlite by default, ``CODEGRAPH_BACKEND=neo4j`` opt-in).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_FIXTURE_TEST_CPP = (
    _HERE.parent / "fixtures" / "cpp-sqlite"
    / "cpp_sqlite" / "test" / "testDatabase.cpp"
)

#: The C++ test file stem used as the ``test_module`` for every test.
TEST_FILE_STEM = "testDatabase"

#: Representative methods the tests exercise — VERIFIES edges should hit
#: these names on cpp-sqlite code.
VERIFIED_METHOD_NAMES = {
    "isInitialized",
    "insert",
    "selectAll",
    "selectById",
    "getDAO",
    "withTransaction",
    "flushAllDAOs",
    "clearBuffer",
    "addToBuffer",
}

#: Assertion macros that should appear as AssertionNode ``operator`` values.
EXPECTED_ASSERT_OPERATORS = {
    "ASSERT_EQ",
    "ASSERT_TRUE",
    "ASSERT_NO_THROW",
    "ASSERT_THROW",
    "ASSERT_FALSE",
    "EXPECT_EQ",
    "EXPECT_TRUE",
    "EXPECT_FALSE",
    "EXPECT_THROW",
    "EXPECT_FLOAT_EQ",
    "EXPECT_DOUBLE_EQ",
}

_TEST_F_RE = re.compile(r"^TEST_F\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)")


def _expected_test_cases() -> dict[str, str]:
    """Return {test_name: suite_name} parsed from the fixture test file."""
    cases: dict[str, str] = {}
    text = _FIXTURE_TEST_CPP.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = _TEST_F_RE.match(line.strip())
        if m:
            cases[m.group(2)] = m.group(1)
    assert cases, f"no TEST_F cases found in {_FIXTURE_TEST_CPP}"
    return cases


def _tests_by_kind(uid_map: dict) -> dict[str, list[dict]]:
    """Group graph nodes by kind (test / assertion / test_step)."""
    grouped: dict[str, list[dict]] = {}
    for node in uid_map.values():
        grouped.setdefault(node.get("kind", ""), []).append(node)
    return grouped


class TestTestNodesPopulated:
    """Every GoogleTest case in the fixture becomes a TestNode."""

    def test_all_fixture_tests_have_nodes(self, codegraph_graph):
        """The graph contains one TestNode per TEST_F in the source."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)
        test_nodes = by_kind.get("test", [])

        expected = _expected_test_cases()
        found = {n["name"] for n in test_nodes}

        missing = set(expected) - found
        assert not missing, (
            f"{len(missing)} TEST_F cases missing from graph "
            f"(of {len(expected)}): {sorted(missing)[:10]}"
        )
        assert len(test_nodes) >= len(expected), (
            f"expected >= {len(expected)} test nodes, got {len(test_nodes)}"
        )

    def test_qualified_names_and_suite(self, codegraph_graph):
        """TestNodes use ``testDatabase::DatabaseTest::<Name>`` names."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        expected = _expected_test_cases()
        suite_by_name = {
            n["name"]: n["qualified_name"]
            for n in by_kind.get("test", [])
        }

        for test_name, suite in expected.items():
            qn = suite_by_name.get(test_name)
            assert qn == f"{TEST_FILE_STEM}::{suite}::{test_name}", (
                f"unexpected qualified_name for {test_name!r}: {qn!r}"
            )

    def test_test_metadata(self, codegraph_graph):
        """Each TestNode carries test_name / test_module / method fields."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        for n in by_kind.get("test", []):
            assert n["test_name"] == n["name"]
            assert n["test_module"] == TEST_FILE_STEM
            assert n["method"] == "automated"
            assert n.get("file_path", "").endswith("testDatabase.cpp")
            assert n.get("line_number", 0) > 0


class TestAssertionAndStepNodes:
    """Assertion and step children are extracted from test bodies."""

    def test_assertion_count_matches_source(self, codegraph_graph):
        """Every ASSERT_*/EXPECT_* statement has an AssertionNode.

        The source assertion count is derived from the fixture body,
        not hardcoded — a fixture change shifts the expectation
        automatically.
        """
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)
        assertion_nodes = by_kind.get("assertion", [])

        source_asserts = _count_source_asserts()
        assert len(assertion_nodes) >= source_asserts, (
            f"expected >= {source_asserts} assertion nodes "
            f"(one per ASSERT_*/EXPECT_* statement), got "
            f"{len(assertion_nodes)}"
        )

    def test_assertion_operators(self, codegraph_graph):
        """AssertionNodes expose the gtest macro as their operator."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)
        assertion_nodes = by_kind.get("assertion", [])

        operators = {n["operator"] for n in assertion_nodes}
        found = EXPECTED_ASSERT_OPERATORS & operators
        assert len(found) >= 6, (
            f"expected a rich mix of assertion operators, got {found} "
            f"of {EXPECTED_ASSERT_OPERATORS}"
        )

    def test_steps_have_setup_and_action_blocks(self, codegraph_graph):
        """Setup/Action step blocks exist with body line ranges."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)
        steps = by_kind.get("test_step", [])

        assert len(steps) >= 50, f"expected >= 50 test steps, got {len(steps)}"
        descriptions = {n["description"] for n in steps}
        assert "Setup block" in descriptions
        assert any(d.startswith("Action block") for d in descriptions)

        for s in steps:
            assert s.get("body_start", 0) > 0, s["qualified_name"]
            assert s.get("body_end", 0) >= s.get("body_start", 0)

    def test_tests_compose_their_children(self, codegraph_graph):
        """Each test COMPOSES its assertion + step nodes.

        ``LayerGraph.serialize`` folds test→child COMPOSES into the
        nested ``composes`` tree (not ``edges``), so composition is
        asserted on the tree structure.
        """
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        test_nodes = by_kind.get("test", [])
        children = [
            c for n in test_nodes for c in n.get("composes", [])
        ]
        assert len(children) >= 200, (
            f"expected >= 200 composed children across tests, "
            f"got {len(children)}"
        )

        child_kinds = {c.get("kind") for c in children}
        assert child_kinds >= {"assertion", "test_step"}, child_kinds

        # Every test composes at least one child.
        empty = [
            n["qualified_name"] for n in test_nodes
            if not n.get("composes")
        ]
        assert not empty, f"tests with no children: {empty[:5]}"

        # Every child appears in the flat uid map (reachable + tagged).
        missing = [
            c.get("qualified_name") for n in test_nodes
            for c in n.get("composes", [])
            if c.get("canonical_key") not in uid_map
        ]
        assert not missing, f"composed children missing from uid_map: {missing[:5]}"


class TestVerificationEdges:
    """VERIFIES / CALLEE edges connect tests to the code they exercise."""

    def test_verifies_edges_exist_and_resolve(self, codegraph_graph):
        """Tests VERIFIES project methods; every target resolves."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        verifies = _edges_by_type(by_kind.get("test", []))["VERIFIES"]
        assert len(verifies) >= 100, (
            f"expected >= 100 VERIFIES edges, got {len(verifies)}"
        )

        unresolved = [e for e in verifies if e["target_key"] not in uid_map]
        assert not unresolved, (
            f"{len(unresolved)} VERIFIES edges do not resolve to a node: "
            f"{unresolved[:5]}"
        )

        verified = {e["target_key"] for e in verifies}
        verified_names = {
            uid_map[u].get("name", "") for u in verified
        }
        hits = VERIFIED_METHOD_NAMES & verified_names
        assert len(hits) >= 4, (
            f"expected VERIFIES edges to hit key cpp-sqlite methods "
            f"({VERIFIED_METHOD_NAMES}), only saw {hits}"
        )

    def test_callee_edges_exist_and_resolve(self, codegraph_graph):
        """Steps CALLEE the code they invoke; every target resolves."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        callees = _edges_by_type(by_kind.get("test_step", []))["CALLEE"]
        assert len(callees) >= 100, (
            f"expected >= 100 CALLEE edges, got {len(callees)}"
        )

        unresolved = [e for e in callees if e["target_key"] not in uid_map]
        assert not unresolved, (
            f"{len(unresolved)} CALLEE edges do not resolve to a node: "
            f"{unresolved[:5]}"
        )

    def test_verifies_targets_mostly_project_code(self, codegraph_graph):
        """The bulk of VERIFIES edges point at cpp-sqlite (as-built) nodes.

        A small minority may legitimately point at stdlib/boost helpers
        the tests call on locals (``std::vector::size``,
        ``std::optional::value``) — the overwhelming majority must be
        project code under test.
        """
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        targets = {
            e["target_key"]
            for n in by_kind.get("test", [])
            for e in n.get("edges", [])
            if e["relation_type"] == "VERIFIES"
        }
        assert targets
        project = {
            t for t in targets
            if uid_map.get(t, {}).get("source") == "cpp-sqlite"
        }
        fraction = len(project) / len(targets)
        assert fraction >= 0.6, (
            f"only {fraction:.0%} of {len(targets)} VERIFIES targets are "
            f"project code (expected >= 60%)"
        )


class TestTagIntegrityForTests:
    """Test nodes follow the as-built provenance rules."""

    def test_test_nodes_are_as_built(self, codegraph_graph):
        """Every test node is tagged as-built with source cpp-sqlite."""
        _, uid_map = codegraph_graph
        by_kind = _tests_by_kind(uid_map)

        for kind in ("test", "assertion", "test_step"):
            nodes = by_kind.get(kind, [])
            assert nodes, f"no {kind} nodes in graph"
            for n in nodes:
                assert n.get("source") == "cpp-sqlite", (
                    f"{kind} node {n.get('qualified_name')} has wrong source: "
                    f"{n.get('source')}"
                )
                assert "as-built" in (n.get("tags") or []), (
                    f"{kind} node {n.get('qualified_name')} not tagged as-built"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ASSERT_IN_SOURCE_RE = re.compile(
    r"^\s*(?:ASSERT|EXPECT)_[A-Z_0-9]+\s*\(",
    re.MULTILINE,
)


def _count_source_asserts() -> int:
    """Count ASSERT_*/EXPECT_* statements in the fixture test file.

    Simple per-line match (one assertion per line); the graph may
    legitimately have *more* nodes (an assertion per statement), so the
    assertion checks ``>=``.
    """
    text = _FIXTURE_TEST_CPP.read_text(encoding="utf-8")
    return len(_ASSERT_IN_SOURCE_RE.findall(text))


def _edges_by_type(nodes: list[dict]) -> dict[str, list[dict]]:
    """Group all edges from *nodes* by relation_type."""
    grouped: dict[str, list[dict]] = {}
    for n in nodes:
        for e in n.get("edges", []):
            grouped.setdefault(e["relation_type"], []).append(e)
    return grouped
