"""Cytoscape.js / HTML visualisation tests for the cpp-sqlite as-built graph.

Verifies the Cytoscape elements produced by ``layer_graph_to_cytoscape()``
contain the expected nodes and edges with correct structure.

Key differences from serialized JSON tests:
- Node IDs are ``qualified_name``, not ``uid``.
- Edge source/target are ``qualified_name``.
- Leaf members (methods, attributes) are collapsed into their parent
  compound's UML label; their edges are re-attached to the parent.
- COMPOSES is structural (Cytoscape ``parent`` field), not an edge.
"""

from __future__ import annotations

import pytest


class TestFullGraphViz:
    """Verify the HTML graph visualisation (Cytoscape.js) shows expected
    nodes and edges for the cpp-sqlite as-built graph.

    Uses ``layer_graph_to_cytoscape()`` to transform the serialized
    LayerGraph into Cytoscape elements — the same data embedded in the
    HTML template.
    """

    @pytest.fixture(scope="class")
    def cy_data(self, codegraph_graph):
        """Build Cytoscape {{nodes, edges}} from the as-built LayerGraph."""
        from codegraph.graph import LayerGraph
        from codegraph.export.viz.transform import layer_graph_to_cytoscape

        serialized, _uid_map = codegraph_graph
        graph = LayerGraph.deserialize(serialized)
        return layer_graph_to_cytoscape(graph)

    # ------------------------------------------------------------------
    # Node assertions
    # ------------------------------------------------------------------

    def test_viz_has_project_compound_nodes(self, cy_data):
        """Key project classes appear as Cytoscape nodes."""
        node_ids = {n["data"]["id"] for n in cy_data["nodes"]}

        expected = [
            "cpp_sqlite::Database",
            "cpp_sqlite::Transaction",
            "cpp_sqlite::TransactionError",
            "cpp_sqlite::DataAccessObject",
            "cpp_sqlite::Logger",
            "cpp_sqlite::BaseTransferObject",
            "cpp_sqlite::RepeatedFieldTransferObject",
            "cpp_sqlite::ForeignKey",
        ]
        for qn in expected:
            assert qn in node_ids, f"{qn} should appear in Cytoscape nodes"

    def test_viz_has_namespace_nodes(self, cy_data):
        """std, cpp_sqlite, boost, and spdlog namespaces appear as
        Cytoscape nodes — including synthetic namespaces created for
        external dependency aggregation."""
        node_ids = {n["data"]["id"] for n in cy_data["nodes"]}
        assert "std" in node_ids
        assert "cpp_sqlite" in node_ids
        assert "boost" in node_ids
        assert "spdlog" in node_ids

    def test_viz_has_dependency_nodes(self, cy_data):
        """Key dependency types appear as Cytoscape nodes."""
        node_ids = {n["data"]["id"] for n in cy_data["nodes"]}
        expected = [
            "std::unique_ptr",
            "std::shared_ptr",
            "std::vector",
            "std::mutex",
            "std::runtime_error",
            "sqlite3",
            "spdlog::logger",
        ]
        for qn in expected:
            assert qn in node_ids, f"{qn} should appear in Cytoscape nodes"

    def test_viz_compound_nodes_have_kind(self, cy_data):
        """Compound nodes carry their ``kind`` and ``layer`` metadata."""
        by_id = {n["data"]["id"]: n["data"] for n in cy_data["nodes"]}
        db_node = by_id.get("cpp_sqlite::Database")
        assert db_node is not None
        assert db_node.get("kind") == "class"
        assert db_node.get("layer") == "as-built"

    # ------------------------------------------------------------------
    # Edge assertions
    # ------------------------------------------------------------------

    def test_viz_depends_on_edges(self, cy_data):
        """DEPENDS_ON edges from project classes to dependency types
        appear in the Cytoscape graph."""
        node_ids = {n["data"]["id"] for n in cy_data["nodes"]}
        depends_on: set[tuple[str, str]] = set()
        for e in cy_data["edges"]:
            if e["data"]["label"] == "DEPENDS_ON":
                depends_on.add((e["data"]["source"], e["data"]["target"]))

        # Database member types — edges re-attached from collapsed
        # member attributes to the Database compound.
        assert ("cpp_sqlite::Database", "std::unique_ptr") in depends_on
        assert ("cpp_sqlite::Database", "sqlite3") in depends_on
        assert ("cpp_sqlite::Database", "std::shared_ptr") in depends_on
        assert ("cpp_sqlite::Database", "spdlog::logger") in depends_on
        assert ("cpp_sqlite::Database", "boost::unordered_map") in depends_on
        assert ("cpp_sqlite::Database", "std::vector") in depends_on

        # DataAccessObject member types
        assert ("cpp_sqlite::DataAccessObject", "std::vector") in depends_on
        assert ("cpp_sqlite::DataAccessObject", "std::mutex") in depends_on
        assert ("cpp_sqlite::DataAccessObject", "std::shared_ptr") in depends_on

        # DAOBase member types — DAOBase members self-reference for CRTP
        # or similar patterns; no std::shared_ptr dependency detected.

        print(f"\n  DEPENDS_ON cytoscape edges: {len(depends_on)}")

    def test_viz_no_self_referential_edges(self, cy_data):
        """No edge has source == target (self-loop).

        Self-referential edges arise when member methods depend-on or
        invoke other members of the same class — both source and target
        are hoisted to the parent compound.  They are filtered out
        during the Cytoscape transform because they convey no useful
        cross-class dependency signal."""
        self_edges: list[str] = []
        for e in cy_data["edges"]:
            if e["data"]["source"] == e["data"]["target"]:
                self_edges.append(
                    f"{e['data']['source']} → {e['data']['label']}"
                )

        assert len(self_edges) == 0, (
            f"Found {len(self_edges)} self-referential edge(s): "
            + "; ".join(self_edges)
        )
        print(f"\n  Self-referential edges: 0 ✓")

    def test_viz_constrains_edges(self, cy_data):
        """CONSTRAINS edges from concepts to referenced types appear
        in the Cytoscape graph."""
        constrains: set[tuple[str, str]] = set()
        for e in cy_data["edges"]:
            if e["data"]["label"] == "CONSTRAINS":
                constrains.add((e["data"]["source"], e["data"]["target"]))

        assert ("cpp_sqlite::BaseTransferObject", "cpp_sqlite::TransferObject") in constrains
        assert ("cpp_sqlite::IsForeignKeyT", "cpp_sqlite::IsForeignKey") in constrains
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::DefaultConstructibleTransferObject") in constrains
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::ValidTransferObject") in constrains
        assert ("cpp_sqlite::ValidTransferObject", "cpp_sqlite::IsRepeatedFieldTransferObject") in constrains
        assert ("cpp_sqlite::ValidTransferObject", "cpp_sqlite::RepeatedFieldTransferObject") in constrains
        print(f"\n  CONSTRAINS cytoscape edges: {len(constrains)}")

    def test_viz_invokes_edges(self, cy_data):
        """INVOKES edges from collapsed project methods appear as edges
        from their parent compound.  Self-referential edges (source ==
        target) are filtered out — a method calling another method of its
        own class produces no useful cross-class dependency signal."""
        invokes: set[tuple[str, str]] = set()
        for e in cy_data["edges"]:
            if e["data"]["label"] == "INVOKES":
                invokes.add((e["data"]["source"], e["data"]["target"]))

        # No self-referential invokes — filtered out.
        for (src, tgt) in invokes:
            assert src != tgt, (
                f"Self-referential INVOKES edge: {src} → {tgt}"
            )

        # At least some cross-class INVOKES edges exist.
        assert len(invokes) >= 1, (
            f"Expected at least 1 cross-class INVOKES edge, got {len(invokes)}"
        )

        print(f"\n  INVOKES cytoscape edges: {len(invokes)}")

    def test_viz_inherits_from_edges(self, cy_data):
        """INHERITS_FROM edges appear in the Cytoscape graph."""
        inherits: set[tuple[str, str]] = set()
        for e in cy_data["edges"]:
            if e["data"]["label"] == "INHERITS_FROM":
                inherits.add((e["data"]["source"], e["data"]["target"]))

        assert ("cpp_sqlite::TransactionError", "std::runtime_error") in inherits
        assert ("cpp_sqlite::DataAccessObject", "DAOBase") in inherits

        print(f"\n  INHERITS_FROM cytoscape edges: {len(inherits)}")

    def test_viz_includes_edges(self, cy_data):
        """INCLUDES edges are NOT in the Cytoscape output.

        INCLUDES edges connect FileNodes, which are excluded from the
        visualisation (file location is surfaced in the detail panel
        via ``Defined in``).  Emitting INCLUDES edges without their
        FileNode endpoints causes Cytoscape to reject the entire graph
        with "nonexistent source" errors."""
        includes: set[tuple[str, str]] = set()
        for e in cy_data["edges"]:
            if e["data"]["label"] == "INCLUDES":
                includes.add((e["data"]["source"], e["data"]["target"]))

        assert len(includes) == 0, (
            f"INCLUDES edges should be dropped (FileNodes excluded); "
            f"found {len(includes)}"
        )
        print(f"\n  INCLUDES cytoscape edges: {len(includes)}")

    def test_viz_expected_edge_types(self, cy_data):
        """The Cytoscape graph includes all expected relationship types."""
        edge_types = {e["data"]["label"] for e in cy_data["edges"]}
        assert "DEPENDS_ON" in edge_types
        assert "INVOKES" in edge_types
        assert "INHERITS_FROM" in edge_types
        # INCLUDES edges are dropped — FileNodes are excluded from
        # the visualisation, and Cytoscape rejects edges with
        # nonexistent source/target nodes.
        # COMPOSES is structural (parent/child), not an edge.
        print(f"\n  Cytoscape edge types: {sorted(edge_types)}")

    def test_viz_all_edge_targets_resolve(self, cy_data):
        """Every edge's source and target must have a matching node ID
        in the Cytoscape data, or the HTML graph will load with dangling
        references."""
        node_ids = {n["data"]["id"] for n in cy_data["nodes"]}
        dangling: list[dict] = []
        for e in cy_data["edges"]:
            src = e["data"]["source"]
            tgt = e["data"]["target"]
            rel = e["data"]["label"]
            # INCLUDES edges are not emitted — FileNodes excluded.
            if src not in node_ids:
                dangling.append({"relation_type": rel, "source": src, "issue": "source missing"})
            if tgt not in node_ids:
                dangling.append({"relation_type": rel, "target": tgt, "issue": "target missing"})

        assert len(dangling) == 0, (
            f"{len(dangling)} dangling Cytoscape edges:\n"
            + "\n".join(
                f"  {d['relation_type']}: {d.get('source', '?')} → {d.get('target', '?')} ({d['issue']})"
                for d in dangling[:15]
            )
        )

        print(f"\n  Cytoscape nodes: {len(node_ids)}, edges: {len(cy_data['edges'])}")

    def test_viz_node_count_bounds(self, cy_data):
        """The rendered graph has a plausible number of nodes."""
        n_nodes = len(cy_data["nodes"])
        n_edges = len(cy_data["edges"])
        # At minimum: project classes + dependency types + namespaces
        assert n_nodes >= 30, f"Expected >= 30 nodes, got {n_nodes}"
        assert n_edges >= 20, f"Expected >= 20 edges, got {n_edges}"
        print(f"\n  Cytoscape graph: {n_nodes} nodes, {n_edges} edges")

    def test_viz_no_duplicate_edges(self, cy_data):
        """No edge should appear more than once with the same
        (source, target, label) tuple.

        RelationshipFrom descriptors are excluded from serialize_edges
        (only RelationshipTo is emitted), and collapsed member refs
        are deduplicated — multiple members of the same class
        depending on the same type produce one edge, not N."""
        from collections import Counter

        edge_counts = Counter()
        for e in cy_data["edges"]:
            key = (
                e["data"]["source"],
                e["data"]["target"],
                e["data"]["label"],
            )
            edge_counts[key] += 1

        duplicates = [(k, v) for k, v in edge_counts.items() if v > 1]
        assert len(duplicates) == 0, (
            f"Found {len(duplicates)} duplicate edge(s): "
            + "; ".join(f"{s}→{t} [{l}] x{c}"
                        for (s, t, l), c in duplicates)
        )
        print(f"\n  Duplicate edges: {len(duplicates)} ✓")
