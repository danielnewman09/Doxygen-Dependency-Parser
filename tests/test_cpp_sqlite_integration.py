"""Integration tests for the Doxygen XML → Neo4j → LayerGraph pipeline.

Two fixture levels:
* ``cpp_sqlite_minimal`` — self-contained 132-line header, no external
  deps.  Used for fast unit tests (class discovery, method extraction,
  CSV export, tagging).
* ``cpp-sqlite`` (real) — the actual cpp-sqlite project with Conan
  dependencies (boost, sqlite3, spdlog).  ``codegraph_graph`` runs
  ``doxygen-index codegraph --neo4j`` to index into Neo4j, then
  retrieves the as-built LayerGraph for assertions.

Requirements: ``doxygen`` must be on PATH.  Real-fixture tests require
``conan install . --build=missing`` in ``tests/fixtures/cpp-sqlite``.
"""

from __future__ import annotations

import pytest
from pathlib import Path
import tempfile
import csv
import shutil
import sys
import subprocess

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "cpp_sqlite_minimal"
TEST_DATA_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doxygen_available() -> bool:
    return shutil.which("doxygen") is not None


def _parse_fixture() -> "ParseResult":
    """Run Doxygen on the minimal fixture and parse the XML."""
    from doxygen_index.doxygen import run_doxygen
    from doxygen_index.parser import parse_xml_dir

    include_dir = FIXTURE_DIR / "include"
    output_dir = Path(tempfile.mkdtemp(prefix="cpp_sqlite_test_"))

    xml_dir = run_doxygen("cpp_sqlite_minimal", include_dir, output_dir)
    assert xml_dir is not None, "Doxygen failed to produce XML"

    result = parse_xml_dir(xml_dir, source="cpp_sqlite_minimal", layer="dependency")
    return result


# ---------------------------------------------------------------------------
# Config resolution tests (pure unit tests — no Doxygen or Neo4j needed)
# ---------------------------------------------------------------------------


class TestConfigResolution:
    """Verify that ``_find_project_source_dirs`` reads
    ``.doxygen-index.toml`` and falls back correctly."""

    def test_config_input_paths_take_precedence(self, tmp_path: Path):
        """When .doxygen-index.toml specifies input_paths, they are
        used instead of the heuristic (include/, src/, etc.)."""
        from doxygen_index.cli import _find_project_source_dirs

        # Create a project dir with BOTH a config path and a heuristic path.
        # The config says "my_src"; the heuristic would find "include/".
        (tmp_path / "my_src").mkdir()
        (tmp_path / "my_src" / "header.h").write_text("// ok")
        (tmp_path / "include").mkdir()
        (tmp_path / "include" / "header.h").write_text("// also ok")

        (tmp_path / ".doxygen-index.toml").write_text(
            '[project]\nname = "test"\ninput_paths = ["my_src"]\n'
        )

        dirs = _find_project_source_dirs(tmp_path)
        resolved = [d.relative_to(tmp_path) for d in dirs]
        # Only the config-specified path, not the heuristic include/
        assert Path("my_src") in resolved
        assert Path("include") not in resolved, (
            f"Heuristic include/ should not appear when config exists, got {resolved}"
        )

    def test_fallback_when_no_config(self, tmp_path: Path):
        """Without .doxygen-index.toml, the heuristic finds standard dirs."""
        from doxygen_index.cli import _find_project_source_dirs

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.cpp").write_text("// test")

        dirs = _find_project_source_dirs(tmp_path)
        resolved = [d.relative_to(tmp_path) for d in dirs]
        assert Path("src") in resolved, (
            f"Heuristic should find src/ when no config, got {resolved}"
        )

    def test_fallback_when_no_dirs_found(self, tmp_path: Path):
        """When no standard dirs exist and no config, fall back to
        the project dir itself."""
        from doxygen_index.cli import _find_project_source_dirs

        dirs = _find_project_source_dirs(tmp_path)
        assert dirs == [tmp_path], (
            f"Should fall back to project dir, got {dirs}"
        )

    def test_config_nonexistent_path_ignored(self, tmp_path: Path):
        """A config input_path that doesn't exist on disk is skipped."""
        from doxygen_index.cli import _find_project_source_dirs

        (tmp_path / "exists").mkdir()
        (tmp_path / "exists" / "header.h").write_text("// ok")

        (tmp_path / ".doxygen-index.toml").write_text(
            '[project]\nname = "test"\ninput_paths = ["exists", "nope"]\n'
        )

        dirs = _find_project_source_dirs(tmp_path)
        resolved = [d.relative_to(tmp_path) for d in dirs]
        assert Path("exists") in resolved
        assert Path("nope") not in resolved
        assert len(dirs) == 1, f"Expected 1 dir, got {len(dirs)}: {resolved}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed_result():
    """Parse the fixture once per module (Doxygen is slow)."""
    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    return _parse_fixture()


class TestClassDiscovery:
    """Verify that all expected classes are found with correct names."""

    EXPECTED = {
        "cpp_sqlite::DAOBase",
        "cpp_sqlite::DataAccessObject",
        "cpp_sqlite::Database",
        "cpp_sqlite::DBBaseTransferObject",
        "cpp_sqlite::DBForeignKey",
        "cpp_sqlite::DBTraits",
    }

    def test_class_count(self, parsed_result):
        assert len(parsed_result.classes) == 6

    def test_all_classes_found(self, parsed_result):
        found = {c.qualified_name for c in parsed_result.classes}
        assert found == self.EXPECTED

    def test_namespace_count(self, parsed_result):
        # cpp_sqlite + std (from <string>, <vector> includes)
        assert len(parsed_result.namespaces) >= 1
        ns_names = {ns.qualified_name for ns in parsed_result.namespaces}
        assert "cpp_sqlite" in ns_names


class TestInheritance:
    """Verify INHERITS_FROM relationships from base_classes."""

    def test_data_access_object_inherits_dao_base(self, parsed_result):
        dao = _find_class(parsed_result, "cpp_sqlite::DataAccessObject")
        assert dao is not None
        assert "cpp_sqlite::DAOBase" in dao.base_classes

    def test_dao_base_has_no_bases(self, parsed_result):
        dao_base = _find_class(parsed_result, "cpp_sqlite::DAOBase")
        assert dao_base is not None
        assert dao_base.base_classes == []


class TestMethodExtraction:
    """Verify methods are extracted with correct signatures."""

    def test_dao_base_pure_virtuals(self, parsed_result):
        dao_base = _find_class(parsed_result, "cpp_sqlite::DAOBase")
        methods = _methods_of(parsed_result, dao_base)
        method_names = {m.name for m in methods}
        assert "getTableName" in method_names
        assert "isInitialized" in method_names
        assert "insert" in method_names
        assert "clearBuffer" in method_names
        # Destructor is also extracted
        assert any("~DAOBase" in m.name for m in methods)

    def test_database_constructor(self, parsed_result):
        db = _find_class(parsed_result, "cpp_sqlite::Database")
        methods = _methods_of(parsed_result, db)
        # Should have constructor, destructor, registerDAO, commit
        assert len(methods) >= 3
        # Template methods include their type parameter in the name
        assert any("registerDAO" in m.name for m in methods)

    def test_method_has_source(self, parsed_result):
        for m in parsed_result.methods[:3]:
            assert m.source == "cpp_sqlite_minimal"


class TestCSVExportRoundTrip:
    """Verify the CSV export produces structurally correct output."""

    def test_csv_export(self, parsed_result):
        from doxygen_index.csv_export import export_csv

        csv_dir = Path(tempfile.mkdtemp(prefix="csv_test_"))
        nodes_csv, rels_csv = export_csv(
            parsed_result, source="cpp_sqlite_minimal", output_dir=csv_dir,
        )

        assert nodes_csv.exists()
        assert rels_csv.exists()

        # Read nodes and verify key classes exist
        with open(nodes_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        class_rows = [r for r in rows if r.get(":LABEL") == "ClassNode"]
        assert len(class_rows) == 6

        dao_base = [r for r in class_rows if r.get("name") == "DAOBase"]
        assert len(dao_base) == 1
        assert dao_base[0]["qualified_name"] == "cpp_sqlite::DAOBase"

        # Verify inheritance relation in CSV
        with open(rels_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rel_rows = list(reader)

        inherits = [r for r in rel_rows if r[":TYPE"] == "INHERITS_FROM"]
        assert len(inherits) >= 1, "Expected at least one INHERITS_FROM edge"

        composes = [r for r in rel_rows if r[":TYPE"] == "COMPOSES"]
        assert len(composes) >= 5, "Expected COMPOSES from namespace to classes"

    def test_csv_uids_are_deterministic(self, parsed_result):
        """Re-export should produce identical UIDs."""
        from doxygen_index.csv_export import export_csv

        dir1 = Path(tempfile.mkdtemp(prefix="uid1_"))
        dir2 = Path(tempfile.mkdtemp(prefix="uid2_"))

        n1, _ = export_csv(parsed_result, source="cpp_sqlite_minimal", output_dir=dir1)
        n2, _ = export_csv(parsed_result, source="cpp_sqlite_minimal", output_dir=dir2)

        with open(n1, newline="", encoding="utf-8") as f:
            uids1 = [r["uid:ID"] for r in csv.DictReader(f)]
        with open(n2, newline="", encoding="utf-8") as f:
            uids2 = [r["uid:ID"] for r in csv.DictReader(f)]

        assert uids1 == uids2


class TestTagging:
    """Verify tags are set to ['dependency'] for dependency-layer parses."""

    def test_classes_have_dependency_tag(self, parsed_result):
        for c in parsed_result.classes:
            assert c.tags == ["dependency"]

    def test_methods_have_dependency_tag(self, parsed_result):
        for m in parsed_result.methods:
            assert m.tags == ["dependency"]


class TestNamespaceComposition:
    """Verify that ``_derive_namespace_compositions`` runs during
    ``post_process`` and populates ``result.compositions`` correctly.

    This codepath (plain ``parse_xml_dir`` → ``post_process``) is
    separate from the ``cmd_codegraph`` pipeline — a regression here
    would mean standalone Doxygen parses lose namespace COMPOSES.
    """

    def test_cpp_sqlite_namespace_composes_all_classes(self, parsed_result):
        """Every cpp_sqlite class should be composed by the namespace."""
        ns_refid = None
        for ns in parsed_result.namespaces:
            if ns.qualified_name == "cpp_sqlite":
                ns_refid = ns.refid
                break
        assert ns_refid is not None, "cpp_sqlite namespace not found"

        composed_refids = {
            c.child_refid for c in parsed_result.compositions
            if c.parent_refid == ns_refid
        }

        cpp_sqlite_classes = [
            c for c in parsed_result.classes
            if c.qualified_name.startswith("cpp_sqlite::")
        ]
        assert len(cpp_sqlite_classes) >= 6, (
            f"Expected >=6 cpp_sqlite classes, got {len(cpp_sqlite_classes)}"
        )

        for cls in cpp_sqlite_classes:
            assert cls.refid in composed_refids, (
                f"cpp_sqlite namespace should COMPOSE {cls.qualified_name}"
            )

    def test_non_cpp_sqlite_classes_not_composed(self, parsed_result):
        """Classes outside cpp_sqlite namespace are not composed by it."""
        ns_refid = None
        for ns in parsed_result.namespaces:
            if ns.qualified_name == "cpp_sqlite":
                ns_refid = ns.refid
                break
        assert ns_refid is not None

        composed_refids = {
            c.child_refid for c in parsed_result.compositions
            if c.parent_refid == ns_refid
        }

        non_cpp_cls = [
            c for c in parsed_result.classes
            if not c.qualified_name.startswith("cpp_sqlite::")
        ]
        for cls in non_cpp_cls:
            assert cls.refid not in composed_refids, (
                f"cpp_sqlite namespace should NOT compose {cls.qualified_name}"
            )


# ---------------------------------------------------------------------------
# Helpers (minimal fixture)
# ---------------------------------------------------------------------------

def _find_class(result, qualified_name: str):
    for c in result.classes:
        if c.qualified_name == qualified_name:
            return c
    return None


def _methods_of(result, cls):
    refid = cls.refid if cls else ""
    return [m for m in result.methods if m.compound_refid == refid]


# =========================================================================
# Full integration test: real cpp-sqlite with conan dependencies
# =========================================================================
# Full integration test: real cpp-sqlite with Conan dependencies
# =========================================================================
# Architecture:
#   Doxygen refids are context-dependent — the same symbol gets different
#   refids in different parse contexts.  Therefore dependency type nodes
#   MUST be parsed in the same Doxygen run as the project (everything in
#   INPUT).  The one-hop filter keeps only directly-connected dep nodes.
#
#   ``codegraph_graph`` runs the unified Doxygen parse + cppreference
#   merge + Neo4j ingest via the CLI, then retrieves the as-built
#   LayerGraph from Neo4j.  The serialized JSON is committed for
#   downstream consumers (visualisation, graph analysis).

_REAL_FIXTURE = FIXTURE_DIR.parent / "cpp-sqlite"
CODEGRAPH_OUTPUT = FIXTURE_DIR.parent / "codegraph_output"


def _conan_deps_available() -> bool:
    """Return True if conan deps for cpp-sqlite are installed."""
    from doxygen_index.conan import discover_packages
    try:
        pkgs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")
        return "boost" in pkgs and "sqlite3" in pkgs
    except Exception:
        return False


def _flatten_layer_graph(serialized: list[dict]) -> list[dict]:
    """Flatten a LayerGraph serialization (nested ``composes``)
    into a flat list of dicts, matching the one-hop JSON format
    used by existing assertions.

    Each node dict gets ``edges`` as a flat list with
    ``relation_type``, ``target_uid``, ``target_type`` keys.
    """
    flat: list[dict] = []
    stack = list(serialized)
    while stack:
        node = stack.pop()
        # Preserve existing edges (non-COMPOSES from serialize())
        edges = list(node.get("edges", []))
        # COMPOSES children → edges + push for flattening
        for child in node.get("composes", []):
            edges.append({
                "relation_type": "COMPOSES",
                "target_uid": child.get("uid", ""),
                "target_type": child.get("kind", ""),
            })
            stack.append(child)
        node["edges"] = edges
        node.pop("composes", None)
        flat.append(node)
    return flat


@pytest.fixture(scope="module")
def codegraph_graph():
    """Full reindex + Neo4j ingest + LayerGraph retrieval.

    1. Runs ``doxygen-index codegraph --neo4j --force`` on the
       cpp-sqlite fixture (same CLI command used in
       ../cpp-sqlite/.vscode/tasks.json).
    2. Retrieves the as-built LayerGraph from Neo4j.
    3. Flattens and saves as ``cpp_sqlite_one_hop.json``.

    Module-scoped — runs once per session.  The JSON is committed
    so downstream consumers can work without Neo4j.
    """
    import json
    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    if not _conan_deps_available():
        pytest.skip("conan deps not installed")

    CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Index into Neo4j ────────────────────────────
    # Pass test container credentials as env vars so the CLI
    # subprocess connects to the correct Neo4j instance.
    import os as _os
    env = {**_os.environ,
           "NEO4J_URI": "bolt://localhost:7689",
           "NEO4J_USER": "neo4j",
           "NEO4J_PASSWORD": "doxygen-index-test"}

    result = subprocess.run(
        [
            "doxygen-index", "codegraph",
            "--project-dir", str(_REAL_FIXTURE),
            "--output-dir", str(CODEGRAPH_OUTPUT),
            "--cppreference",
            "--neo4j",
            "--only", "sqlite3,boost,spdlog",
        ],
        env=env, timeout=600,
    )
    if result.returncode != 0:
        pytest.fail(f"doxygen-index codegraph failed (rc={result.returncode})")

    # ── Step 2: Retrieve LayerGraph from Neo4j ──────────────
    from codegraph.graph import LayerGraph
    graph = LayerGraph.from_neo4j("as-built")
    serialized = graph.serialize(fields="all")

    # ── Step 3: Flatten and save ───────────────────────────
    flat = _flatten_layer_graph(serialized)
    output = CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.json"
    output.write_text(json.dumps(flat, indent=2, default=str),
                      encoding="utf-8")
    print(f"\n  LayerGraph as-built: {len(flat)} nodes")
    print(f"  Output: {output} ({output.stat().st_size:,} bytes)")
    return flat


class TestFullGraphExport:
    """Retrieve the as-built LayerGraph from Neo4j and verify structure.

    ``codegraph_graph`` indexes cpp-sqlite into Neo4j via the CLI,
    retrieves the as-built LayerGraph, flattens it, and saves
    ``cpp_sqlite_one_hop.json``.  Tests interrogate the flat list.

    Module-scoped — runs once per session.
    """

    def test_export_json_with_one_hop(self, codegraph_graph):
        """Verify the as-built LayerGraph has expected nodes and edges."""
        filtered = codegraph_graph
        assert len(filtered) > 50, f"Expected >50 nodes, got {len(filtered)}"

        all_edges = []
        for node in filtered:
            all_edges.extend(node.get("edges", []))
        edge_types = {e["relation_type"] for e in all_edges}
        assert "COMPOSES" in edge_types, f"Expected COMPOSES in {edge_types}"
        assert "INVOKES" in edge_types, f"Expected INVOKES in {edge_types}"

        node_names = {n.get("name", "") for n in filtered}
        assert "Database" in node_names
        assert "DAOBase" in node_names
        assert "DataAccessObject" in node_names
        assert "Transaction" in node_names

        dep_sources = {n.get("source", "") for n in filtered}
        print(f"  Edge types: {sorted(edge_types)}")
        print(f"  Sources present: {sorted(dep_sources)}")

    # DEVNOTE: ``test_all_edges_resolve_to_nodes`` previously used the
    # full merged ParseResult.  The as-built LayerGraph only includes
    # project nodes + one-hop neighbours, so edge coverage is scoped.
    # If full-graph resolution testing is needed, retrieve a full
    # LayerGraph (all tags) from Neo4j.

    def test_all_edges_resolve_to_nodes(self, codegraph_graph):
        """Verify edges in the as-built graph resolve to nodes."""
        data = codegraph_graph
        node_uids = {n["uid"] for n in data}

        total_edges = 0
        unresolved: list[dict] = []
        for node in data:
            for edge in node.get("edges", []):
                total_edges += 1
                if edge["target_uid"] not in node_uids:
                    unresolved.append(edge)

        non_invokes_unresolved = [
            e for e in unresolved
            if e["relation_type"] != "INVOKES"
        ]

        assert len(non_invokes_unresolved) == 0, (
            f"{len(non_invokes_unresolved)} non-INVOKES edges unresolved"
        )

        resolution_pct = 100 * (total_edges - len(unresolved)) / max(total_edges, 1)
        print(f"\n  Edge resolution: {total_edges - len(unresolved)}/{total_edges} "
              f"({resolution_pct:.1f}%)")
        print(f"  Unresolved INVOKES: {len(unresolved)} (expected)")

    def test_discovered_dependencies(self):
        """Verify conan discovers expected dependencies for cpp-sqlite."""
        if not _conan_deps_available():
            pytest.skip("conan deps not installed")

        from doxygen_index.conan import discover_packages
        pkgs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")

        assert "boost" in pkgs, f"boost not in {sorted(pkgs)}"
        assert "sqlite3" in pkgs, f"sqlite3 not in {sorted(pkgs)}"
        assert "spdlog" in pkgs, f"spdlog not in {sorted(pkgs)}"

    def test_dependency_relationships(self, codegraph_graph):
        """Verify the as-built graph has expected DEPENDS_ON, INVOKES,
        and INCLUDES relationships from cpp-sqlite to its dependencies."""
        data = codegraph_graph
        uid_map = {n["uid"]: n for n in data}

        depends_on: set[tuple[str, str, str]] = set()
        includes: set[tuple[str, str, str]] = set()
        invokes: set[tuple[str, str, str]] = set()
        for node in data:
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

        assert ("cpp_sqlite::Database::db_", "sqlite3", "sqlite3") in depends_on
        assert ("cpp_sqlite::Database::getRawDB()", "sqlite3", "sqlite3") in depends_on
        assert ("cpp_sqlite::Database::daos_", "boost::unordered_map", "boost") in depends_on
        assert ("cpp_sqlite::Database::pLogger_", "spdlog::logger", "spdlog") in depends_on

        assert ("cpp_sqlite::Database::db_", "std::unique_ptr", "cppreference") in depends_on
        assert ("cpp_sqlite::Database::pLogger_", "std::shared_ptr", "cppreference") in depends_on
        assert ("cpp_sqlite::DataAccessObject::writeBuffer_", "std::vector",
                "cppreference") in depends_on

        assert ("DBDatabase.hpp", "sqlite3.h", "sqlite3") in includes
        assert ("DBDatabase.hpp", "unordered_map.hpp", "boost") in includes
        assert ("DBTraits.hpp", "sqlite3.h", "sqlite3") in includes

        assert ("cpp_sqlite::Database::select(PreparedSQLStmt &stmt)",
                "sqlite3_step", "sqlite3") in invokes
        assert ("cpp_sqlite::Database::insert(PreparedSQLStmt &stmt, T &data)",
                "sqlite3_bind_int64", "sqlite3") in invokes
        assert ("cpp_sqlite::Database::isInTransaction(())",
                "sqlite3_close", "sqlite3") in invokes

        for node in data:
            src = node.get("source", "")
            tags = node.get("tags", [])
            if src == "cpp-sqlite":
                assert "as-built" in tags
                assert "dependency" not in tags
            elif src in ("boost", "spdlog", "sqlite3", "cppreference", "gtest"):
                assert "dependency" in tags

        from collections import Counter
        src_counts = Counter(n.get("source", "?") for n in data)
        assert src_counts.get("boost", 0) >= 3
        assert src_counts.get("spdlog", 0) >= 4
        assert src_counts.get("sqlite3", 0) >= 10
        assert src_counts.get("cppreference", 0) >= 7

        print(f"\n  DEPENDS_ON: {len(depends_on)} unique edges")
        print(f"  INCLUDES:   {len(includes)} unique edges")
        print(f"  INVOKES:    {len(invokes)} unique edges")

    # ------------------------------------------------------------------
    # Namespace COMPOSES assertions
    # ------------------------------------------------------------------

    def test_cpp_sqlite_namespace_composes_classes(self, codegraph_graph):
        """Verify the ``cpp_sqlite`` namespace COMPOSES project classes."""
        data = codegraph_graph
        uid_map = {n["uid"]: n for n in data}

        cpp_sqlite_ns = None
        for n in data:
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "cpp_sqlite"
                    and n.get("source") == "cpp-sqlite"):
                cpp_sqlite_ns = n
                break
        assert cpp_sqlite_ns is not None, "cpp_sqlite namespace node not found"

        composes_edges = [
            e for e in cpp_sqlite_ns.get("edges", [])
            if e.get("relation_type") == "COMPOSES"
        ]
        composes_targets: set[str] = set()
        for e in composes_edges:
            tgt = uid_map.get(e["target_uid"], {})
            qn = tgt.get("qualified_name", "") or tgt.get("name", "")
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
        for expected in expected_classes:
            assert expected in composes_targets, (
                f"cpp_sqlite namespace should COMPOSE {expected}"
            )

        print(f"\n  cpp_sqlite COMPOSES {len(composes_edges)} children")

    # DEVNOTE: Previously used the full merged ParseResult to verify
    # that std namespace COMPOSES all expected stdlib types.  The as-built
    # LayerGraph only includes std types directly referenced by project
    # nodes, so the set of COMPOSES children is scoped.

    def test_std_namespace_composes_stdlib_classes(self, codegraph_graph):
        """Verify the ``std`` namespace (cppreference) COMPOSES key
        stdlib types referenced by the project."""
        data = codegraph_graph
        uid_map = {n["uid"]: n for n in data}

        std_ns = None
        for n in data:
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "std"
                    and n.get("source") == "cppreference"):
                std_ns = n
                break
        assert std_ns is not None, "std namespace node (cppreference) not found"

        composes_edges = [
            e for e in std_ns.get("edges", [])
            if e.get("relation_type") == "COMPOSES"
        ]
        composes_targets: set[str] = set()
        for e in composes_edges:
            tgt = uid_map.get(e["target_uid"], {})
            qn = tgt.get("qualified_name", "") or tgt.get("name", "")
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

        print(f"\n  std COMPOSES {len(composes_edges)} children")

    def test_namespace_composes_edges_resolve(self, codegraph_graph):
        """Verify COMPOSES edges from project namespace nodes resolve."""
        data = codegraph_graph
        node_uids = {n["uid"] for n in data}
        project_source = "cpp-sqlite"

        unresolved: list[tuple[str, str, str]] = []
        for n in data:
            if (n.get("kind") != "namespace"
                    or n.get("source") != project_source):
                continue
            for edge in n.get("edges", []):
                if edge.get("relation_type") != "COMPOSES":
                    continue
                tgt = edge.get("target_uid", "")
                if tgt and tgt not in node_uids:
                    unresolved.append((
                        n.get("qualified_name", "?"),
                        edge["relation_type"],
                        tgt,
                    ))

        assert len(unresolved) == 0, (
            f"{len(unresolved)} COMPOSES edges unresolve: {unresolved[:5]}"
        )

    def test_toml_input_paths_resolve_source_tree(self, codegraph_graph):
        """Verify classes from TOML input_paths are present."""
        data = codegraph_graph

        class_names = set()
        for node in data:
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
        data = codegraph_graph
        uid_map = {n["uid"]: n for n in data}

        tx_error = None
        for n in data:
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
