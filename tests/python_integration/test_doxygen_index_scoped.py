"""Class-scoped LayerGraph tests for the
doxygen-index dogfood graph.

Scopes the as-built dogfood LayerGraph to a single compound
(``doxygen_index.parser.python._parser.PythonParser`` — the parser
that extracts THIS codebase) and verifies the resulting subgraph.

Mirrors ``test_cpp_sqlite_scoped.py`` (which scopes to
``cpp_sqlite::Database``) and writes an inspectable JSON artifact:

- ``tests/codegraph_output/python_parser_subgraph.json`` — the scoped
  serialized LayerGraph
These are the files to open side-by-side with
``src/doxygen_index/parser/python/_parser.py`` to directly check how
well the extracted data matches the existing code.

Uses the session-scoped ``codegraph_graph`` fixture from
``conftest.py``.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest


_CODEGRAPH_OUTPUT = Path(__file__).resolve().parent.parent / "codegraph_output"

#: The focal class: the Python parser that parses this repository.
SCOPE_QN = "doxygen_index.parser.python._parser.PythonParser"


class TestScopedClassGraph:
    """Verify that scoping a LayerGraph to a single class produces a
    correct subgraph with that class, its members, and its immediate
    neighbours."""

    @pytest.fixture(scope="class")
    def scoped_graph(self, codegraph_graph):
        """Return a LayerGraph scoped to PythonParser."""
        from codegraph.graph import LayerGraph

        serialized, _uid_map = codegraph_graph
        full_graph = LayerGraph.deserialize(serialized)
        return full_graph.subgraph(SCOPE_QN)

    @pytest.fixture(scope="class")
    def scoped_serialized(self, scoped_graph):
        """Serialized form of the PythonParser subgraph."""
        return scoped_graph.serialize(fields="all")

    @pytest.fixture(scope="class")
    def scoped_uid_map(self, scoped_serialized):
        """Flat {uid: node_dict} for the PythonParser subgraph."""
        uid_map: dict[str, dict] = {}
        stack = list(scoped_serialized)
        while stack:
            node = stack.pop()
            uid_map[node["canonical_key"]] = node
            stack.extend(node.get("composes", []))
        return uid_map

    # ------------------------------------------------------------------
    # Node membership assertions
    # ------------------------------------------------------------------

    def test_python_parser_node_is_root(self, scoped_serialized):
        """The serialized subgraph has PythonParser as the root node."""
        assert len(scoped_serialized) >= 1
        root = scoped_serialized[0]
        qn = root.get("qualified_name", "")
        assert qn == SCOPE_QN, f"Expected PythonParser as root, got {qn}"

    def test_python_parser_has_expected_members(self, scoped_uid_map):
        """PythonParser composes all four of its methods."""
        qnames: set[str] = {
            n.get("qualified_name", "") or n.get("name", "")
            for n in scoped_uid_map.values()
            if n.get("qualified_name") or n.get("name")
        }

        for method in (
            "doxygen_index.parser.python._parser.PythonParser.parse_source_dir",
            "doxygen_index.parser.python._parser.PythonParser.post_process",
            "doxygen_index.parser.python._parser.PythonParser._register_package",
            "doxygen_index.parser.python._parser.PythonParser._parse_python_file",
        ):
            assert method in qnames, f"PythonParser should compose member {method}"

    def test_python_parser_depends_on_neighbours(self, scoped_uid_map):
        """The subgraph includes 1-hop neighbours PythonParser depends
        on: the base interface, the ParseResult type, and the
        postprocess/_paths functions it invokes."""
        qnames: set[str] = {
            n.get("qualified_name", "") or n.get("name", "")
            for n in scoped_uid_map.values()
            if n.get("qualified_name") or n.get("name")
        }

        expected_neighbors = [
            # INHERITS_FROM target
            "doxygen_index.parser.base.LanguageParser",
            # DEPENDS_ON type
            "doxygen_index.parser.model.ParseResult",
            # INVOKES targets (postprocess + paths helpers)
            "doxygen_index.parser.python.postprocess.derive_invokes",
            "doxygen_index.parser.python.postprocess.derive_namespace_imports",
            "doxygen_index.parser.python._paths.is_excluded",
        ]
        for expected in expected_neighbors:
            assert expected in qnames, (
                f"Subgraph should include neighbour {expected}"
            )

    def test_unrelated_classes_absent(self, scoped_uid_map):
        """Classes unrelated to PythonParser should NOT be in the
        subgraph."""
        qnames: set[str] = {
            n.get("qualified_name", "") or n.get("name", "")
            for n in scoped_uid_map.values()
            if n.get("qualified_name") or n.get("name")
        }

        for unrelated in (
            "doxygen_index.parser.cpp_parser.CppParser",
            "doxygen_index.cppreference.classifier.PageType",
            "doxygen_index.enrich.EnrichmentResult",
            "doxygen_index.project.ProjectConfig",
        ):
            assert unrelated not in qnames, (
                f"Unrelated class {unrelated} should not appear in the subgraph"
            )

    def test_subgraph_is_smaller_than_full(self, scoped_uid_map, codegraph_graph):
        """The scoped subgraph is substantially smaller than the full
        graph (~3% — PythonParser is a leaf in the package tree)."""
        _serialized, full_uid_map = codegraph_graph
        assert len(scoped_uid_map) * 3 < len(full_uid_map), (
            f"Subgraph ({len(scoped_uid_map)}) should be substantially "
            f"smaller than full graph ({len(full_uid_map)})"
        )
        print(f"\n  Full graph: {len(full_uid_map)} nodes")
        print(f"  PythonParser subgraph: {len(scoped_uid_map)} nodes "
              f"(~{100*len(scoped_uid_map)/len(full_uid_map):.0f}%)")

    def test_scoped_no_dangling_edges(self, scoped_uid_map):
        """Every non-DEFINED_IN edge in the scoped graph resolves within
        it.

        The scoped window includes the compound + its neighbours, but
        NOT the FileNodes its members are defined in (same as the
        cpp-sqlite Database scoping) — so DEFINED_IN → FileNode edges
        legitimately point outside the window.  Everything else must
        resolve.
        """
        unresolved: list[dict] = []
        total = 0
        for node in scoped_uid_map.values():
            for edge in node.get("edges", []):
                total += 1
                if edge["target_key"] not in scoped_uid_map:
                    unresolved.append(edge)
            for child in node.get("composes", []):
                if child.get("canonical_key") not in scoped_uid_map:
                    unresolved.append({
                        "relation_type": "COMPOSES",
                        "target_key": child.get("canonical_key", ""),
                    })

        non_defined_in = [
            e for e in unresolved if e["relation_type"] != "DEFINED_IN"
        ]
        assert not non_defined_in, (
            f"{len(non_defined_in)} non-DEFINED_IN unresolved edges "
            f"(of {total}): "
            + ", ".join(f"{e['relation_type']}->{e['target_key'][:8]}"
                        for e in non_defined_in[:10])
        )
