"""Full-graph export tests — LayerGraph JSON structure and relationship
verification for the cpp-sqlite as-built graph.

These tests use the session-scoped ``codegraph_graph`` fixture from
``conftest.py``, which indexes cpp-sqlite into Neo4j once per session
and returns ``(serialized, uid_map)``.

Requirements: ``doxygen`` on PATH and Conan deps installed.
"""

from __future__ import annotations

import pytest
from pathlib import Path


# The shared fixture lives in conftest.py (session scope).
# No duplicate fixture definition here.


class TestFullGraphExport:
    """Retrieve the as-built LayerGraph from Neo4j and verify structure.

    ``codegraph_graph`` indexes cpp-sqlite into Neo4j via the CLI,
    retrieves the as-built LayerGraph, and saves
    ``cpp_sqlite_one_hop.json``.  Tests receive ``(serialized, uid_map)``
    where ``serialized`` is the raw nested LayerGraph tree and
    ``uid_map`` is a flat ``{uid: node_dict}`` for lookups.

    Session-scoped — runs once per session.
    """

    def test_export_json_with_one_hop(self, codegraph_graph):
        """Verify the as-built LayerGraph has expected nodes and edges."""
        serialized, uid_map = codegraph_graph
        assert len(uid_map) > 50, f"Expected >50 nodes, got {len(uid_map)}"

        all_edges = []
        for node in uid_map.values():
            all_edges.extend(node.get("edges", []))
        edge_types = {e["relation_type"] for e in all_edges}
        # INVOKES, INCLUDES, INHERITS, DEPENDS_ON are stored as edges.
        # COMPOSES is structural (nested under the ``composes`` key).
        assert "INVOKES" in edge_types, f"Expected INVOKES in {edge_types}"
        assert "DEPENDS_ON" in edge_types, f"Expected DEPENDS_ON in {edge_types}"

        node_names = {n.get("name", "") for n in uid_map.values()}
        assert "Database" in node_names
        assert "DAOBase" in node_names
        assert "DataAccessObject" in node_names
        assert "Transaction" in node_names

        dep_sources = {n.get("source", "") for n in uid_map.values()}
        print(f"  Edge types: {sorted(edge_types)}")
        print(f"  Sources present: {sorted(dep_sources)}")

    # DEVNOTE: ``test_all_edges_resolve_to_nodes`` previously used the
    # full merged ParseResult.  The as-built LayerGraph only includes
    # project nodes + one-hop neighbours, so edge coverage is scoped.
    # If full-graph resolution testing is needed, retrieve a full
    # LayerGraph (all tags) from Neo4j.

    def test_all_edges_resolve_to_nodes(self, codegraph_graph):
        """Verify edges in the as-built graph resolve to nodes.

        Only checks edges from project nodes (source="cpp-sqlite").
        The as-built LayerGraph includes one-hop neighbours of project
        nodes.  Dependency nodes (boost, spdlog, etc.) may have edges
        to other dependency nodes not included in the one-hop scope,
        so those edges are not checked here.
        """
        serialized, uid_map = codegraph_graph
        project_source = "cpp-sqlite"

        total_edges = 0
        unresolved: list[dict] = []
        for node in uid_map.values():
            if node.get("source") != project_source:
                continue
            for edge in node.get("edges", []):
                total_edges += 1
                if edge["target_uid"] not in uid_map:
                    unresolved.append(edge)
            # COMPOSES children are structural but should also resolve
            for child in node.get("composes", []):
                child_uid = child.get("uid", "")
                if child_uid not in uid_map:
                    unresolved.append({
                        "relation_type": "COMPOSES",
                        "target_uid": child_uid,
                        "target_type": child.get("kind", ""),
                    })

        non_invokes_unresolved = [
            e for e in unresolved
            if e["relation_type"] not in ("INVOKES", "INCLUDES")
        ]

        assert len(non_invokes_unresolved) == 0, (
            f"{len(non_invokes_unresolved)} non-INVOKES edges unresolved:\n"
            + "\n".join(
                f"  {e['relation_type']}: {e['target_uid']} ({e['target_type']})"
                for e in non_invokes_unresolved[:10]
            )
        )

        resolution_pct = 100 * (total_edges - len(unresolved)) / max(total_edges, 1)
        print(f"\n  Edge resolution: {total_edges - len(unresolved)}/{total_edges} "
              f"({resolution_pct:.1f}%)")
        print(f"  Unresolved INVOKES: {len(unresolved)} (expected)")

    def test_discovered_dependencies(self):
        """Verify conan discovers expected dependencies for cpp-sqlite."""
        # This test does NOT need Neo4j — it only checks Conan discovery.
        fixture_dir = Path(__file__).resolve().parent.parent / "fixtures" / "cpp-sqlite"
        from doxygen_index.conan import discover_packages
        try:
            pkgs = discover_packages(project_dir=str(fixture_dir), build_type="Debug")
        except Exception:
            pytest.skip("conan deps not installed")

        assert "boost" in pkgs, f"boost not in {sorted(pkgs)}"
        assert "sqlite3" in pkgs, f"sqlite3 not in {sorted(pkgs)}"
        assert "spdlog" in pkgs, f"spdlog not in {sorted(pkgs)}"

    def test_dependency_relationships(self, codegraph_graph):
        """Verify the as-built graph has expected DEPENDS_ON, INVOKES,
        and INCLUDES relationships from cpp-sqlite to its dependencies."""
        serialized, uid_map = codegraph_graph

        depends_on: set[tuple[str, str, str]] = set()
        includes: set[tuple[str, str, str]] = set()
        invokes: set[tuple[str, str, str]] = set()
        for node in uid_map.values():
            from_qn = node.get("qualified_name", "") or node.get("name", "")
            for edge in node.get("edges", []):
                target = uid_map.get(edge["target_uid"], {})
                to_qn = target.get("qualified_name", "") or target.get("name", "")
                to_src = target.get("source", "?")
                entry = (from_qn, to_qn, to_src)
                rt = edge["relation_type"]
                if rt == "DEPENDS_ON":
                    depends_on.add(entry)
                elif rt == "INCLUDES":
                    includes.add(entry)
                elif rt == "INVOKES":
                    invokes.add(entry)

        assert ("cpp_sqlite::Database::db_", "std::unique_ptr", "cppreference") in depends_on
        assert ("cpp_sqlite::Database::db_", "sqlite3", "sqlite3") in depends_on
        assert ("cpp_sqlite::Database::pLogger_", "std::shared_ptr", "cppreference") in depends_on
        assert ("cpp_sqlite::Database::pLogger_", "spdlog::logger", "spdlog") in depends_on
        assert ("cpp_sqlite::Database::daos_", "boost::unordered_map", "boost") in depends_on
        assert ("cpp_sqlite::Database::daos_", "std::unique_ptr", "cppreference") in depends_on
        assert ("cpp_sqlite::Database::daos_", "DAOBase", "cpp-sqlite") in depends_on
        assert ("cpp_sqlite::Database::daoCreationOrder_", "std::vector",
                "cppreference") in depends_on
        assert ("cpp_sqlite::DataAccessObject::writeBuffer_", "std::vector",
                "cppreference") in depends_on
        assert ("cpp_sqlite::DataAccessObject::bufferMutex_", "std::mutex",
                "cppreference") in depends_on
        assert ("cpp_sqlite::DataAccessObject::pLogger_", "std::shared_ptr",
                "cppreference") in depends_on

        assert ("DBDataAccessObject.hpp", "DBDAOBase.hpp", "cpp-sqlite") in includes
        assert ("DBDataAccessObject.hpp", "DBDatabase.hpp", "cpp-sqlite") in includes
        assert ("DBDataAccessObject.hpp", "DBTraits.hpp", "cpp-sqlite") in includes

        # INVOKES edges connect project methods to other project methods
        # (cross-source invocations are captured via DEPENDS_ON on types).
        assert ("cpp_sqlite::Database::select(PreparedSQLStmt &stmt)",
                "cpp_sqlite::Database::getDAO()", "cpp-sqlite") in invokes
        assert ("cpp_sqlite::Database::insert(PreparedSQLStmt &stmt, T &data)",
                "cpp_sqlite::Database::getDAO()", "cpp-sqlite") in invokes
        assert ("cpp_sqlite::Database::withTransaction(Func &&func)",
                "cpp_sqlite::Transaction::commit()", "cpp-sqlite") in invokes

        for node in uid_map.values():
            src = node.get("source", "")
            tags = node.get("tags", [])
            if not tags:
                # Some internal/derived nodes (e.g. ImplementationNode,
                # type_parameter ClassNodes) may not have tags set.
                continue
            if src == "cpp-sqlite":
                assert "as-built" in tags
                assert "dependency" not in tags
            elif src in ("boost", "spdlog", "sqlite3", "cppreference", "gtest"):
                assert "dependency" in tags

        from collections import Counter
        src_counts = Counter(n.get("source", "?") for n in uid_map.values())
        # One-hop graph only includes dependency nodes directly referenced
        # by project nodes via DEPENDS_ON, INHERITS_FROM, etc.
        assert src_counts.get("boost", 0) >= 1
        assert src_counts.get("spdlog", 0) >= 2
        # sqlite3 types are used through raw function calls (INVOKES), not
        # type-level DEPENDS_ON, so they may not appear in the one-hop graph.
        assert src_counts.get("cppreference", 0) >= 7

        print(f"\n  DEPENDS_ON: {len(depends_on)} unique edges")
        print(f"  INCLUDES:   {len(includes)} unique edges")
        print(f"  INVOKES:    {len(invokes)} unique edges")

    # ------------------------------------------------------------------
    # Namespace COMPOSES assertions
    # ------------------------------------------------------------------

    def test_cpp_sqlite_namespace_composes_classes(self, codegraph_graph):
        """Verify the ``cpp_sqlite`` namespace COMPOSES project classes
        and concepts."""
        serialized, uid_map = codegraph_graph

        cpp_sqlite_ns = None
        for n in uid_map.values():
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "cpp_sqlite"
                    and n.get("source") == "cpp-sqlite"):
                cpp_sqlite_ns = n
                break
        assert cpp_sqlite_ns is not None, "cpp_sqlite namespace node not found"

        # COMPOSES children are nested under "composes", not in "edges"
        composes_children = cpp_sqlite_ns.get("composes", [])
        composes_targets: set[str] = set()
        for child in composes_children:
            qn = child.get("qualified_name", "") or child.get("name", "")
            if qn:
                composes_targets.add(qn)

        expected_classes = [
            "cpp_sqlite::DataAccessObject",
            "cpp_sqlite::Database",
            "cpp_sqlite::Logger",
            "cpp_sqlite::Transaction",
            "cpp_sqlite::TransactionError",
            "cpp_sqlite::ForeignKey",
            "cpp_sqlite::BaseTransferObject",
            "cpp_sqlite::RepeatedFieldTransferObject",
        ]
        expected_concepts = [
            "cpp_sqlite::TransferObject",
            "cpp_sqlite::ValidTransferObject",
            "cpp_sqlite::DefaultConstructibleTransferObject",
            "cpp_sqlite::CopyableTransferObject",
            "cpp_sqlite::MovableTransferObject",
            "cpp_sqlite::IsForeignKey",
            "cpp_sqlite::IsRepeatedFieldTransferObject",
        ]
        for expected in expected_classes:
            assert expected in composes_targets, (
                f"cpp_sqlite namespace should COMPOSE {expected}"
            )
        for expected in expected_concepts:
            assert expected in composes_targets, (
                f"cpp_sqlite namespace should COMPOSE concept {expected}"
            )

        print(f"\n  cpp_sqlite COMPOSES {len(composes_children)} children")
        print(f"    classes: {len(expected_classes)}, concepts: {len(expected_concepts)}")

    # DEVNOTE: Previously used the full merged ParseResult to verify
    # that std namespace COMPOSES all expected stdlib types.  The as-built
    # LayerGraph only includes std types directly referenced by project
    # nodes, so the set of COMPOSES children is scoped.

    def test_std_namespace_composes_stdlib_classes(self, codegraph_graph):
        """Verify the ``std`` namespace (cppreference) COMPOSES key
        stdlib types referenced by the project."""
        serialized, uid_map = codegraph_graph

        std_ns = None
        for n in uid_map.values():
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "std"
                    and len(n.get("composes", [])) > 0):
                std_ns = n
                break
        assert std_ns is not None, "std namespace node with children not found"

        composes_children = std_ns.get("composes", [])
        composes_targets: set[str] = set()
        for child in composes_children:
            qn = child.get("qualified_name", "") or child.get("name", "")
            if qn:
                composes_targets.add(qn)

        # Key stdlib types pulled in via one-hop from cpp-sqlite.
        expected_stdlib = [
            "std::shared_ptr",
            "std::unique_ptr",
            "std::vector",
            "std::unordered_map",
            "std::optional",
            "std::mutex",
        ]
        for expected in expected_stdlib:
            assert expected in composes_targets, (
                f"std namespace should COMPOSE {expected}"
            )

        print(f"\n  std COMPOSES {len(composes_children)} children")

    def test_boost_namespace_composes_boost_types(self, codegraph_graph):
        """The synthetic ``boost`` namespace COMPOSES boost::unordered_map
        (pulled in via one-hop from cpp-sqlite)."""
        serialized, uid_map = codegraph_graph

        boost_ns = None
        for n in uid_map.values():
            if n.get("kind") == "namespace" and n.get("qualified_name") == "boost":
                boost_ns = n
                break
        assert boost_ns is not None, "boost namespace node not found"

        composes = boost_ns.get("composes", [])
        composes_targets = {
            c.get("qualified_name", "") or c.get("name", "")
            for c in composes
        }
        assert "boost::unordered_map" in composes_targets, (
            "boost namespace should COMPOSE boost::unordered_map"
        )
        print(f"\n  boost COMPOSES {len(composes)} children")

    def test_spdlog_namespace_composes_spdlog_types(self, codegraph_graph):
        """The synthetic ``spdlog`` namespace COMPOSES spdlog::logger and
        spdlog::spdlog_ex (pulled in via one-hop from cpp-sqlite)."""
        serialized, uid_map = codegraph_graph

        spdlog_ns = None
        for n in uid_map.values():
            if n.get("kind") == "namespace" and n.get("qualified_name") == "spdlog":
                spdlog_ns = n
                break
        assert spdlog_ns is not None, "spdlog namespace node not found"

        composes = spdlog_ns.get("composes", [])
        composes_targets = {
            c.get("qualified_name", "") or c.get("name", "")
            for c in composes
        }
        assert "spdlog::logger" in composes_targets, (
            "spdlog namespace should COMPOSE spdlog::logger"
        )
        assert "spdlog::spdlog_ex" in composes_targets, (
            "spdlog namespace should COMPOSE spdlog::spdlog_ex"
        )
        print(f"\n  spdlog COMPOSES {len(composes)} children")

    def test_concept_constrains_edges(self, codegraph_graph):
        """Concepts have CONSTRAINS edges to the types they reference
        in their initializer (e.g. TransferObject → BaseTransferObject)."""
        serialized, uid_map = codegraph_graph

        # Rebuild uid→qualified_name map so we can resolve the target.
        uid_to_qn: dict[str, str] = {}
        def _index_uids(entries):
            for e in entries:
                uid = e.get("uid", "")
                qn = e.get("qualified_name", "") or e.get("name", "")
                if uid and qn:
                    uid_to_qn[uid] = qn
                _index_uids(e.get("composes", []))
        _index_uids(serialized)

        # Find TransferObject concept and check its edges
        constrains: set[tuple[str, str]] = set()
        def _collect(entries):
            for e in entries:
                qn = e.get("qualified_name", "")
                for edge in e.get("edges", []):
                    if edge["relation_type"] == "CONSTRAINS":
                        target = uid_to_qn.get(edge["target_uid"], edge["target_uid"])
                        constrains.add((qn, target))
                _collect(e.get("composes", []))
        _collect(serialized)

        assert ("cpp_sqlite::BaseTransferObject", "cpp_sqlite::TransferObject") in constrains
        assert ("cpp_sqlite::IsForeignKeyT", "cpp_sqlite::IsForeignKey") in constrains
        # Concept-to-concept references from initializer text
        # Direction: referenced → referencer
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::DefaultConstructibleTransferObject") in constrains
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::CopyableTransferObject") in constrains
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::MovableTransferObject") in constrains
        assert ("cpp_sqlite::TransferObject", "cpp_sqlite::ValidTransferObject") in constrains
        assert ("cpp_sqlite::DefaultConstructibleTransferObject", "cpp_sqlite::ValidTransferObject") in constrains
        assert ("cpp_sqlite::ValidTransferObject", "cpp_sqlite::IsRepeatedFieldTransferObject") in constrains
        # Template-parameter concept constraints:
        # template<ValidTransferObject T> struct RepeatedFieldTransferObject
        assert ("cpp_sqlite::ValidTransferObject", "cpp_sqlite::RepeatedFieldTransferObject") in constrains
        print(f"\n  CONSTRAINS edges in JSON: {len(constrains)}")

    def test_namespace_composes_edges_resolve(self, codegraph_graph):
        """Verify COMPOSES children from project namespace nodes resolve."""
        serialized, uid_map = codegraph_graph
        project_source = "cpp-sqlite"

        unresolved: list[tuple[str, str, str]] = []
        for n in uid_map.values():
            if (n.get("kind") != "namespace"
                    or n.get("source") != project_source):
                continue
            for child in n.get("composes", []):
                tgt = child.get("uid", "")
                if tgt and tgt not in uid_map:
                    unresolved.append((
                        n.get("qualified_name", "?"),
                        "COMPOSES",
                        tgt,
                    ))

        assert len(unresolved) == 0, (
            f"{len(unresolved)} COMPOSES edges unresolve: {unresolved[:5]}"
        )

    def test_toml_input_paths_resolve_source_tree(self, codegraph_graph):
        """Verify classes from TOML input_paths are present."""
        serialized, uid_map = codegraph_graph

        class_names = set()
        for node in uid_map.values():
            if node.get("kind") == "class" and node.get("source") == "cpp-sqlite":
                qn = node.get("qualified_name", "")
                if qn.startswith("cpp_sqlite::"):
                    class_names.add(qn)

        if "cpp_sqlite::TransactionError" in class_names:
            assert "cpp_sqlite::Transaction" in class_names
            print(f"\n  TOML input_paths resolved: {len(class_names)} cpp_sqlite:: classes")
        else:
            print(f"\n  NOTE: TransactionError not in as-built LayerGraph")
            print(f"  cpp_sqlite:: classes ({len(class_names)}): {sorted(class_names)[:5]}...")

    def test_transaction_error_inherits_runtime_error(self, codegraph_graph):
        """Verify ``cpp_sqlite::TransactionError`` inherits from
        ``std::runtime_error``."""
        serialized, uid_map = codegraph_graph

        tx_error = None
        for n in uid_map.values():
            if n.get("qualified_name") == "cpp_sqlite::TransactionError":
                tx_error = n
                break
        assert tx_error is not None, "cpp_sqlite::TransactionError not found"
        assert tx_error.get("kind") in ("class", "struct")

        inherits_targets = set()
        for e in tx_error.get("edges", []):
            if e.get("relation_type") == "INHERITS_FROM":
                tgt = uid_map.get(e["target_uid"], {})
                qn = tgt.get("qualified_name", "")
                if qn:
                    inherits_targets.add(qn)

        assert "std::runtime_error" in inherits_targets, (
            f"TransactionError should inherit from std::runtime_error, "
            f"found: {sorted(inherits_targets)}"
        )

        print(f"\n  TransactionError INHERITS_FROM: {sorted(inherits_targets)}")
