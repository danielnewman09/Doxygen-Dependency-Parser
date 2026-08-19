"""Class-scoped LayerGraph tests.

Scopes the as-built cpp-sqlite LayerGraph to a single compound
(``cpp_sqlite::Database``) and verifies the resulting subgraph.

Uses the session-scoped ``codegraph_graph`` fixture from ``conftest.py``.
"""

from __future__ import annotations

import pytest
from pathlib import Path


class TestScopedClassGraph:
    """Verify that scoping a LayerGraph to a single class produces a
    correct subgraph with that class, its members, and its immediate
    neighbours."""

    @pytest.fixture(scope="class")
    def scoped_graph(self, codegraph_graph):
        """Return a LayerGraph scoped to ``cpp_sqlite::Database``."""
        from codegraph.graph import LayerGraph

        serialized, _uid_map = codegraph_graph
        full_graph = LayerGraph.deserialize(serialized)
        return full_graph.subgraph("cpp_sqlite::Database")

    @pytest.fixture(scope="class")
    def scoped_serialized(self, scoped_graph):
        """Serialized form of the Database subgraph."""
        return scoped_graph.serialize(fields="all")

    @pytest.fixture(scope="class")
    def scoped_uid_map(self, scoped_serialized):
        """Flat {uid: node_dict} for the Database subgraph."""
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

    def test_database_node_is_root(self, scoped_serialized):
        """The serialized subgraph has Database as the root node."""
        # The subgraph serialization starts with the focal node's tree.
        assert len(scoped_serialized) >= 1
        root = scoped_serialized[0]
        qn = root.get("qualified_name", "")
        assert qn == "cpp_sqlite::Database", (
            f"Expected Database as root, got {qn}"
        )

    def test_database_has_expected_members(self, scoped_uid_map):
        """Database node composes its member attributes and methods."""
        qnames: set[str] = set()
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "") or node.get("name", "")
            if qn:
                qnames.add(qn)

        # Member attributes
        expected_members = [
            "cpp_sqlite::Database::db_",
            "cpp_sqlite::Database::pLogger_",
            "cpp_sqlite::Database::daos_",
            "cpp_sqlite::Database::daoCreationOrder_",
        ]
        for expected in expected_members:
            assert expected in qnames, (
                f"Database should composes member {expected}"
            )

        # Member methods
        expected_methods = [
            "cpp_sqlite::Database::getDAO()",
            "cpp_sqlite::Database::select(PreparedSQLStmt &stmt)",
            "cpp_sqlite::Database::withTransaction(Func &&func)",
        ]
        for expected in expected_methods:
            assert expected in qnames, (
                f"Database should composes method {expected}"
            )

        print(f"\n  Database members: {len(expected_members + expected_methods)} expected")

    def test_database_depends_on_neighbours(self, scoped_uid_map):
        """The subgraph includes 1-hop neighbours that Database
        depends on."""
        qnames: set[str] = set()
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "") or node.get("name", "")
            if qn:
                qnames.add(qn)

        # Dependency types that Database members reference
        expected_neighbors = [
            "std::unique_ptr",
            "std::shared_ptr",
            "std::vector",
            "boost::unordered_map",
            "sqlite3",
        ]
        for expected in expected_neighbors:
            assert expected in qnames, (
                f"Subgraph should include neighbour {expected}"
            )

        print(f"\n  Subgraph neighbours: {len(expected_neighbors)} expected")

    def test_unrelated_classes_absent(self, scoped_uid_map):
        """Classes unrelated to Database should NOT be in the subgraph."""
        qnames: set[str] = set()
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "") or node.get("name", "")
            if qn:
                qnames.add(qn)

        # These are project classes that have no relation to Database
        unrelated = [
            "cpp_sqlite::RepeatedFieldTransferObject",
            "cpp_sqlite::ForeignKey",
        ]
        for u in unrelated:
            assert u not in qnames, (
                f"Unrelated class {u} should not be in Database subgraph"
            )

    def test_select_method_has_implementation_invokes(self, scoped_uid_map):
        """Database::select() has INVOKES edges to sqlite3 C API
        functions and helper methods — these are tracked from Doxygen's
        ``<references>`` in the method's definition body."""
        uid_to_qn: dict[str, str] = {}
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "") or node.get("name", "")
            uid = node.get("canonical_key", "")
            if uid and qn:
                uid_to_qn[uid] = qn

        select_edges: dict[str, str] = {}
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "")
            if "Database::select" in qn:
                for edge in node.get("edges", []):
                    select_edges[edge["relation_type"]] = (
                        uid_to_qn.get(edge["target_key"], edge["target_key"])
                    )
                break

        # INVOKES to sqlite3 C API functions captured from body
        sqlite_invokes = [
            "sqlite3_step",
            "sqlite3_bind_int64",
            "sqlite3_column_int64",
            "sqlite3_prepare_v2",
            "sqlite3_finalize",
            "sqlite3_reset",
        ]
        invokes_targets = set()
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "")
            if "Database::select" in qn:
                for edge in node.get("edges", []):
                    if edge["relation_type"] == "INVOKES":
                        invokes_targets.add(
                            uid_to_qn.get(edge["target_key"], edge["target_key"])
                        )
                break

        for expected in sqlite_invokes:
            assert expected in invokes_targets, (
                f"select() should INVOKES {expected}; got {sorted(invokes_targets)}"
            )

        # Also invokes getDAO (self-method)
        assert "cpp_sqlite::Database::getDAO()" in invokes_targets

        print(f"\n  select() INVOKES: {len(invokes_targets)} targets")
        print(f"    sqlite3 C API: {[t for t in invokes_targets if 'sqlite3' in t]}")

    def test_subgraph_is_smaller_than_full(self, scoped_uid_map, codegraph_graph):
        """The scoped subgraph should be substantially smaller than the
        full 1-hop graph."""
        _serialized, full_uid_map = codegraph_graph
        assert len(scoped_uid_map) < len(full_uid_map), (
            f"Subgraph ({len(scoped_uid_map)}) should be smaller than "
            f"full graph ({len(full_uid_map)})"
        )
        # Should be at least 3x smaller
        assert len(scoped_uid_map) * 3 < len(full_uid_map), (
            f"Subgraph ({len(scoped_uid_map)}) should be substantially "
            f"smaller than full graph ({len(full_uid_map)})"
        )
        print(f"\n  Full graph: {len(full_uid_map)} nodes")
        print(f"  Database subgraph: {len(scoped_uid_map)} nodes "
              f"(~{100*len(scoped_uid_map)/len(full_uid_map):.0f}%)")
