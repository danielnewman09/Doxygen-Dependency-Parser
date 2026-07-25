"""Class-scoped LayerGraph and HTML visualisation tests.

Scopes the as-built cpp-sqlite LayerGraph to a single compound
(``cpp_sqlite::Database``) and verifies the resulting subgraph and
HTML rendering.

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
            uid_map[node["uid"]] = node
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
            uid = node.get("uid", "")
            if uid and qn:
                uid_to_qn[uid] = qn

        select_edges: dict[str, str] = {}
        for node in scoped_uid_map.values():
            qn = node.get("qualified_name", "")
            if "Database::select" in qn:
                for edge in node.get("edges", []):
                    select_edges[edge["relation_type"]] = (
                        uid_to_qn.get(edge["target_uid"], edge["target_uid"])
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
                            uid_to_qn.get(edge["target_uid"], edge["target_uid"])
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

    # ------------------------------------------------------------------
    # HTML rendering for scoped graph
    # ------------------------------------------------------------------

    @pytest.fixture(scope="class")
    def scoped_html_data(self, scoped_graph):
        """Export the Database subgraph as Cytoscape elements and HTML.

        Uses ``collapse_members=False`` so that all INVOKES and
        DEPENDS_ON edges between individual members are visible in
        the graph — the class-scoped view is about internal
        relationships, not external aggregation.

        Saves JSON + HTML to the codegraph_output directory alongside
        the full-graph outputs.
        """
        from codegraph.export.viz.transform import layer_graph_to_cytoscape
        from codegraph.export.viz import export_html_from_json
        import json as _json
        from pathlib import Path as _Path

        output_dir = _Path(__file__).resolve().parent.parent / "codegraph_output"
        output_dir.mkdir(parents=True, exist_ok=True)

        json_path = output_dir / "database_subgraph.json"
        json_path.write_text(_json.dumps(
            scoped_graph.serialize(fields="all"), indent=2, default=str
        ))

        html_path = output_dir / "database_subgraph.html"
        export_html_from_json(
            str(json_path), str(html_path),
            title="cpp-sqlite::Database (scoped)",
            collapse_members=False,
        )

        cy = layer_graph_to_cytoscape(scoped_graph, collapse_members=False)

        print(f"\n  Scoped JSON: {json_path} ({json_path.stat().st_size:,} bytes)")
        print(f"  Scoped HTML: {html_path} ({html_path.stat().st_size:,} bytes)")

        return {"cy": cy, "html_path": html_path}

    def test_scoped_html_has_database_node(self, scoped_html_data):
        """The HTML Cytoscape data includes the Database node."""
        node_ids = {n["data"]["id"] for n in scoped_html_data["cy"]["nodes"]}
        assert "cpp_sqlite::Database" in node_ids

    def test_scoped_html_no_dangling_edges(self, scoped_html_data):
        """All edges in the scoped graph resolve to existing nodes."""
        cy = scoped_html_data["cy"]
        node_ids = {n["data"]["id"] for n in cy["nodes"]}
        dangling: list[dict] = []
        for e in cy["edges"]:
            src = e["data"]["source"]
            tgt = e["data"]["target"]
            if src not in node_ids:
                dangling.append({"source": src, "issue": "source missing"})
            if tgt not in node_ids:
                dangling.append({"target": tgt, "issue": "target missing"})
        assert len(dangling) == 0, (
            f"{len(dangling)} dangling edges in scoped Cytoscape"
        )

    def test_scoped_html_has_select_invokes_edges(self, scoped_html_data):
        """The scoped HTML Cytoscape data shows INVOKES edges from
        Database::select() to sqlite3 C API functions — these are
        intra-class implementation dependencies visible because
        ``collapse_members=False``."""
        cy = scoped_html_data["cy"]

        # Build node ID → kind map for member-level nodes
        member_nodes = {
            n["data"]["id"]
            for n in cy["nodes"]
            if "::select" in n["data"]["id"]
            or "::getDAO" in n["data"]["id"]
            or "::insert" in n["data"]["id"]
            or "sqlite3_" in n["data"]["id"]
        }
        # Members should appear as separate nodes (not collapsed)
        assert "cpp_sqlite::Database::select(PreparedSQLStmt &stmt)" in member_nodes

        invokes: set[tuple[str, str]] = set()
        for e in cy["edges"]:
            if e["data"]["label"] == "INVOKES":
                invokes.add((e["data"]["source"], e["data"]["target"]))

        # select() → sqlite3_step (implementation-level INVOKES)
        select_qn = "cpp_sqlite::Database::select(PreparedSQLStmt &stmt)"
        for expected_target in ["sqlite3_step", "sqlite3_bind_int64",
                                 "sqlite3_column_int64", "sqlite3_prepare_v2",
                                 "sqlite3_finalize", "sqlite3_reset"]:
            assert (select_qn, expected_target) in invokes, (
                f"select() should INVOKES {expected_target}"
            )

        # select() → getDAO() (self-method INVOKES)
        assert (select_qn, "cpp_sqlite::Database::getDAO()") in invokes

        print(f"\n  select() INVOKES in Cytoscape: {len([e for e in invokes if e[0] == select_qn])}")

    def test_scoped_html_file_created(self, scoped_html_data):
        """The HTML file was written and contains Cytoscape init code."""
        html = scoped_html_data["html_path"].read_text()
        assert len(html) > 1000, f"HTML too small: {len(html)} bytes"
        assert "cytoscape" in html.lower(), "HTML should contain cytoscape"
        assert "elements" in html.lower(), "HTML should contain elements"
        print(f"\n  Scoped HTML: {scoped_html_data['html_path']} "
              f"({len(html):,} bytes)")
