"""Full-graph export tests — LayerGraph JSON structure and relationship
verification for the doxygen-index dogfood graph.

These tests use the session-scoped ``codegraph_graph`` fixture from
``conftest.py``, which indexes THIS repository's own Python source
(``src/doxygen_index`` — the dogfooding fixture) into the active
backend once per session and returns ``(serialized, uid_map)``.

Requirements: only the ``doxygen-index`` CLI on PATH (the Python parser
uses ``ast`` — no doxygen, no Conan).

Unlike the cpp-sqlite suite, the dogfood graph is single-source: every
node is ``source="doxygen-index"`` and tagged ``as-built``, and there
are no dependency packages.  That makes the graph *complete* — every
edge target resolves (0 dangling edges), which is the marquee invariant
asserted here.
"""

from __future__ import annotations

from pathlib import Path

from collections import Counter

import pytest


def _file_identity(qn: str, name: str) -> str:
    """Return the identity used for FileNode endpoints of INCLUDES edges.

    FileNodes carry the absolute source path in ``qualified_name`` (and
    ``path``) with the basename in ``name``.  Tests assert basenames, so
    a path-looking qualified_name is reduced to its basename — accepts
    either representation.
    """
    if not qn or "/" not in qn:
        return qn or name or ""
    return Path(qn).name or name or ""


class TestFullGraphExport:
    """Retrieve the as-built LayerGraph from the backend and verify
    structure.

    ``codegraph_graph`` indexes ``src/doxygen_index`` via the CLI,
    retrieves the as-built LayerGraph, and saves
    ``doxygen_index_one_hop.json``.  Tests receive ``(serialized,
    uid_map)`` where ``serialized`` is the raw nested LayerGraph tree
    and ``uid_map`` is a flat ``{uid: node_dict}`` for lookups.

    Session-scoped — runs once per session.
    """

    def test_export_json_with_one_hop(self, codegraph_graph):
        """Verify the as-built LayerGraph has expected nodes and edges."""
        serialized, uid_map = codegraph_graph
        assert len(uid_map) > 200, f"Expected >200 nodes, got {len(uid_map)}"

        all_edges = []
        for node in uid_map.values():
            all_edges.extend(node.get("edges", []))
        edge_types = {e["relation_type"] for e in all_edges}
        # INVOKES, INCLUDES, INHERITS_FROM, HAS_PARAMETER, DEPENDS_ON
        # are stored as edges.  COMPOSES is structural (nested under the
        # ``composes`` key).
        assert "INVOKES" in edge_types, f"Expected INVOKES in {edge_types}"
        assert "INCLUDES" in edge_types, f"Expected INCLUDES in {edge_types}"
        assert "INHERITS_FROM" in edge_types, f"Expected INHERITS_FROM in {edge_types}"
        assert "HAS_PARAMETER" in edge_types, f"Expected HAS_PARAMETER in {edge_types}"

        node_names = {n.get("name", "") for n in uid_map.values()}
        assert "LanguageParser" in node_names
        assert "CppParser" in node_names
        assert "PythonParser" in node_names
        assert "PageType" in node_names
        assert "ParseResult" in node_names
        assert "cmd_project" in node_names

        # Single-source graph: everything is the project.
        sources = {n.get("source", "") for n in uid_map.values()}
        assert sources == {"doxygen-index"}, f"unexpected sources: {sources}"

        print(f"  Edge types: {sorted(edge_types)}")
        print(f"  Node count: {len(uid_map)}")

    def test_all_edges_resolve_to_nodes(self, codegraph_graph):
        """EVERY edge in the as-built graph resolves to a node.

        The marquee invariant: the dogfood graph is single-source (no
        dependency packages), so unlike cpp-sqlite's one-hop view (59%
        dangling edges) there is nothing that can point outside the
        loaded set.  Every edge target — INCLUDES, INVOKES, DEPENDS_ON,
        DEFINED_IN, HAS_PARAMETER, INHERITS_FROM — must resolve, and
        every COMPOSES child must be present in the flat map.
        """
        serialized, uid_map = codegraph_graph

        unresolved: list[dict] = []
        total_edges = 0
        for node in uid_map.values():
            for edge in node.get("edges", []):
                total_edges += 1
                if edge["target_key"] not in uid_map:
                    unresolved.append(edge)
            for child in node.get("composes", []):
                if child.get("canonical_key") not in uid_map:
                    unresolved.append({
                        "relation_type": "COMPOSES",
                    "target_key": child.get("canonical_key", ""),
                        "target_type": child.get("kind", ""),
                    })

        assert not unresolved, (
            f"{len(unresolved)} unresolved edges out of {total_edges}:\n"
            + "\n".join(
                f"  {e['relation_type']}: {e['target_key']} ({e.get('target_type')})"
                for e in unresolved[:10]
            )
        )

        print(f"  Edge resolution: {total_edges}/{total_edges} (100%)")

    def test_namespace_tree(self, codegraph_graph):
        """The namespace hierarchy mirrors the package tree.

        ``doxygen_index`` composes its top-level modules; the parser
        subpackage composes its modules; ``doxygen_index.parser.base``
        composes the ``LanguageParser`` interface.
        """
        serialized, uid_map = codegraph_graph

        def composes_children(ns_qn: str) -> set[str]:
            for n in uid_map.values():
                if n.get("qualified_name") == ns_qn:
                    return {
                        c.get("qualified_name") or c.get("name")
                        for c in n.get("composes", [])
                    }
            return set()

        assert "doxygen_index.parser" in composes_children("doxygen_index"), (
            "doxygen_index should COMPOSE the parser subpackage"
        )
        assert "doxygen_index.cli" in composes_children("doxygen_index")
        assert "doxygen_index.conan" in composes_children("doxygen_index")

        parser_children = composes_children("doxygen_index.parser")
        assert "doxygen_index.parser.base" in parser_children
        assert "doxygen_index.parser.cpp_parser" in parser_children
        assert "doxygen_index.parser.model" in parser_children
        assert "doxygen_index.parser.python" in parser_children
        # module-level functions live under the package namespace too
        assert "doxygen_index.parser.parse_xml_dir" in parser_children
        assert "doxygen_index.parser.parse_python_dir" in parser_children

        base_children = composes_children("doxygen_index.parser.base")
        assert "doxygen_index.parser.base.LanguageParser" in base_children, (
            "doxygen_index.parser.base should COMPOSE LanguageParser"
        )

    def test_parser_hierarchy(self, codegraph_graph):
        """Both concrete parsers inherit from the LanguageParser interface."""
        serialized, uid_map = codegraph_graph

        inherits: list[tuple[str, str]] = []
        for node in uid_map.values():
            from_qn = node.get("qualified_name", "") or node.get("name", "")
            for edge in node.get("edges", []):
                if edge["relation_type"] != "INHERITS_FROM":
                    continue
                target = uid_map.get(edge["target_key"], {})
                to_qn = target.get("qualified_name", "") or target.get("name", "")
                inherits.append((from_qn, to_qn))

        assert (
            "doxygen_index.parser.cpp_parser.CppParser",
            "doxygen_index.parser.base.LanguageParser",
        ) in inherits
        assert (
            "doxygen_index.parser.python._parser.PythonParser",
            "doxygen_index.parser.base.LanguageParser",
        ) in inherits

        # LanguageParser itself is an InterfaceNode with the abstract
        # surface: parse_source_dir + post_process.
        lp = next(
            n for n in uid_map.values()
            if n.get("qualified_name") == "doxygen_index.parser.base.LanguageParser"
        )
        assert lp.get("type") == "InterfaceNode", (
            f"LanguageParser should be an InterfaceNode, got {lp.get('type')}"
        )
        assert lp.get("is_abstract") is True
        method_names = {c.get("name") for c in lp.get("composes", [])}
        assert "parse_source_dir" in method_names
        assert "post_process" in method_names

    def test_page_type_enum(self, codegraph_graph):
        """The PageType enum survives the round-trip with all six values,
        composed under its module namespace."""
        serialized, uid_map = codegraph_graph

        enum = next(
            n for n in uid_map.values()
            if n.get("qualified_name") == "doxygen_index.cppreference.classifier.PageType"
        )
        assert enum.get("type") == "EnumNode"
        values = [c.get("name") for c in enum.get("composes", [])]
        assert values == [
            "HEADER", "CLASS", "MEMBER", "FREE_FUNCTION", "NAMESPACE", "SKIP",
        ], values

    def test_invokes_edges(self, codegraph_graph):
        """INVOKES edges capture cross-module and cross-package calls."""
        serialized, uid_map = codegraph_graph

        invokes: set[tuple[str, str]] = set()
        for node in uid_map.values():
            from_qn = node.get("qualified_name", "") or node.get("name", "")
            for edge in node.get("edges", []):
                if edge["relation_type"] != "INVOKES":
                    continue
                target = uid_map.get(edge["target_key"], {})
                to_qn = target.get("qualified_name", "") or target.get("name", "")
                invokes.add((from_qn, to_qn))

        # CLI dispatch → parser entry points (cross-package calls).
        assert (
            "doxygen_index.cli._parse_python_project",
            "doxygen_index.parser.parse_python_dir",
        ) in invokes
        assert (
            "doxygen_index.cli._parse_cpp_project",
            "doxygen_index.parser.parse_xml_dir",
        ) in invokes
        # CLI → conan discovery.
        assert (
            "doxygen_index.cli.cmd_codegraph",
            "doxygen_index.conan.discover_packages",
        ) in invokes

        print(f"  INVOKES: {len(invokes)} unique edges")

    def test_includes_edges(self, codegraph_graph):
        """INCLUDES edges capture the Python import graph (file → symbol)."""
        serialized, uid_map = codegraph_graph

        includes: set[tuple[str, str]] = set()
        for node in uid_map.values():
            from_qn = _file_identity(
                node.get("qualified_name", ""), node.get("name", "")
            )
            for edge in node.get("edges", []):
                if edge["relation_type"] != "INCLUDES":
                    continue
                target = uid_map.get(edge["target_key"], {})
                to_qn = target.get("qualified_name", "") or target.get("name", "")
                includes.add((from_qn, to_qn))

        # cli.py imports the conan + parser + doxygen machinery.
        assert ("cli.py", "doxygen_index.conan.discover_packages") in includes
        assert ("cli.py", "doxygen_index.doxygen.run_doxygen") in includes

        print(f"  INCLUDES: {len(includes)} unique edges")

    def test_depends_on_edges(self, codegraph_graph):
        """DEPENDS_ON edges capture type-level dependencies.

        The Python postprocess derives type dependencies from annotations
        and returns: parsers depend on ParseResult, page parsers depend
        on PageInfo.
        """
        serialized, uid_map = codegraph_graph

        depends_on: set[tuple[str, str]] = set()
        for node in uid_map.values():
            from_qn = node.get("qualified_name", "") or node.get("name", "")
            for edge in node.get("edges", []):
                if edge["relation_type"] != "DEPENDS_ON":
                    continue
                target = uid_map.get(edge["target_key"], {})
                to_qn = target.get("qualified_name", "") or target.get("name", "")
                depends_on.add((from_qn, to_qn))

        assert (
            "doxygen_index.parser.python._parser.PythonParser.parse_source_dir",
            "doxygen_index.parser.model.ParseResult",
        ) in depends_on
        assert (
            "doxygen_index.cppreference.page_parser.parse_header_page",
            "doxygen_index.cppreference.classifier.PageInfo",
        ) in depends_on

        print(f"  DEPENDS_ON: {len(depends_on)} unique edges")

    def test_single_source_tag_integrity(self, codegraph_graph):
        """Every node is project source + as-built tagged; no dependency
        tags anywhere (single-source dogfood graph)."""
        serialized, uid_map = codegraph_graph

        for node in uid_map.values():
            assert node.get("source") == "doxygen-index", node.get("qualified_name")
            tags = node.get("tags", [])
            assert "as-built" in tags, (
                f"node missing as-built tag: {node.get('qualified_name')}"
            )
            assert "dependency" not in tags

        src_counts = Counter(n.get("source", "?") for n in uid_map.values())
        assert src_counts == {"doxygen-index": len(uid_map)}


class TestArchivedSqliteReference:
    """The generated sqlite database is archived for external validation.

    The session ``codegraph_graph`` fixture copies the backend database
    to ``tests/unit_test_data/python_integration.sqlite3`` so external
    tooling can open the exact database the suite validated.  These
    tests pin that artifact: it must exist, be a valid sqlite database,
    and contain every node of the retrieved as-built graph.
    """

    @pytest.fixture(scope="class")
    def archived_db(self):
        import sqlite3

        db_path = (
            Path(__file__).resolve().parent.parent
            / "unit_test_data" / "python_integration.sqlite3"
        )
        assert db_path.exists(), (
            f"archived sqlite artifact missing: {db_path}"
        )
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        yield con, db_path
        con.close()

    def test_artifact_is_valid_database(self, archived_db):
        """The artifact is a queryable sqlite db with node rows."""
        con, db_path = archived_db
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "nodes" in tables, f"nodes table missing in {db_path}"
        assert "node_tags" in tables
        n_nodes = con.execute("SELECT count(*) FROM nodes").fetchone()[0]
        assert n_nodes > 0, "archived database has no nodes"
        n_as_built = con.execute(
            "SELECT count(*) FROM node_tags WHERE tag='as-built'"
        ).fetchone()[0]
        assert n_as_built > 0, "archived database has no as-built nodes"

    def test_artifact_contains_validated_graph(self, codegraph_graph, archived_db):
        """Every node uid the suite validated exists in the archived db.

        The retrieved as-built LayerGraph (serialized to
        ``doxygen_index_one_hop.json``) must be exactly reproducible
        from the archived database — any uid missing from the archive
        means external consumers cannot trust it.
        """
        serialized, uid_map = codegraph_graph
        con, _ = archived_db

        uids = {n["canonical_key"] for n in uid_map.values()}
        assert uids

        placeholders = ",".join("?" * len(uids))
        archived = {
            row[0] for row in con.execute(
                f"SELECT canonical_key FROM nodes WHERE canonical_key IN ({placeholders})",
                list(uids),
            )
        }
        missing = uids - archived
        assert not missing, (
            f"{len(missing)} of {len(uids)} validated node uids missing "
            f"from archived sqlite database: {sorted(missing)[:5]}"
        )
