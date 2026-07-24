"""Integration tests for the Doxygen XML → graph JSON pipeline.

Two fixture levels:
* ``cpp_sqlite_minimal`` — self-contained 132-line header, no external
  deps.  Used for fast unit tests (class discovery, method extraction,
  CSV export, tagging).
* ``cpp-sqlite`` (real) — the actual cpp-sqlite project with Conan
  dependencies (boost, sqlite3, spdlog).  Uses ``doxygen-index codegraph``
  (the CLI's own unified Doxygen pipeline) for indexing and caching.
  The CLI writes a pickle cache to ``<output-dir>/<project>_unified.pkl``;
  ``TestFullGraphExport`` loads it to produce ``cpp_sqlite_one_hop.json``.

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


# ---------------------------------------------------------------------------
# Helpers
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
#   ``TestDepIndexing`` runs the unified parse once (slow) and pickles
#   the ParseResult.  ``TestFullGraphExport`` loads the cached result
#   (fast) and produces ``cpp_sqlite_one_hop.json``.

_REAL_FIXTURE = FIXTURE_DIR.parent / "cpp-sqlite"

# The CLI's ``codegraph`` subcommand caches the unified ParseResult at
# ``<output-dir>/<project>_unified.pkl``.  Tests load from there instead
# of maintaining a separate pickle cache.
CODEGRAPH_OUTPUT = FIXTURE_DIR.parent / "codegraph_output"
_UNIFIED_CACHE = CODEGRAPH_OUTPUT / "cpp-sqlite_unified.pkl"


def _conan_deps_available() -> bool:
    """Return True if conan deps for cpp-sqlite are installed."""
    from doxygen_index.conan import discover_packages
    try:
        pkgs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")
        return "boost" in pkgs and "sqlite3" in pkgs
    except Exception:
        return False


@pytest.fixture(scope="module")
def merged_result():
    """Load the cached unified ParseResult produced by the CLI.

    Populated by ``TestDepIndexing`` on first run.  Subsequent runs are
    instant (no Doxygen needed).
    """
    import pickle
    if not _UNIFIED_CACHE.exists():
        pytest.skip("unified parse cache not populated — run TestDepIndexing first")
    with open(_UNIFIED_CACHE, "rb") as f:
        result = pickle.load(f)
    node_count = (
        len(result.classes) + len(result.methods)
        + len(result.attributes) + len(result.functions)
        + len(result.namespaces) + len(result.enums)
        + len(result.concepts) + len(result.defines)
    )
    print(f"\n  Loaded unified parse from CLI cache: {node_count} nodes")
    return result


class TestDepIndexing:
    """Run ``doxygen-index codegraph`` (the CLI's own unified Doxygen
    pipeline) which handles discovery, parsing, tagging, caching, and
    optional cppreference merge — all in one command.

    The CLI writes a pickle cache to
    ``<output-dir>/<project>_unified.pkl``.  Subsequent tests load it
    via :func:`merged_result` — no separate test pickle cache needed.
    """

    def test_index_unified_and_cache(self):
        """Run ``doxygen-index codegraph`` on the cpp-sqlite fixture
        with cppreference enabled.

        The CLI runs one Doxygen parse covering project source + all
        Conan dependency include directories, producing cross-references
        (INVOKES edges) from project code to dependency symbols.
        """
        if not _doxygen_available():
            pytest.skip("doxygen not found on PATH")
        if not _conan_deps_available():
            pytest.skip("conan deps not installed")

        CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [
                "doxygen-index", "codegraph",
                "--project-dir", str(_REAL_FIXTURE),
                "--output-dir", str(CODEGRAPH_OUTPUT),
                "--cppreference",
                "--force",
            ],
            capture_output=True, text=True, timeout=900,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            pytest.fail(f"doxygen-index codegraph failed (rc={result.returncode})")

        assert _UNIFIED_CACHE.exists(), (
            f"CLI did not produce expected pickle at {_UNIFIED_CACHE}"
        )

        import pickle
        import os
        with open(_UNIFIED_CACHE, "rb") as f:
            parsed = pickle.load(f)

        node_count = (
            len(parsed.classes) + len(parsed.methods)
            + len(parsed.attributes) + len(parsed.functions)
            + len(parsed.namespaces) + len(parsed.enums)
            + len(parsed.concepts) + len(parsed.defines)
        )
        size_bytes = os.path.getsize(_UNIFIED_CACHE)
        print(f"\n  Unified parse: {node_count} nodes")
        print(f"  Pickle size: {size_bytes:,} bytes")
        assert node_count > 100, (
            f"Expected >100 nodes in unified parse, got {node_count}"
        )


def _generate_one_hop_json(merged_result) -> dict:
    """Generate the one-hop filtered graph JSON from *merged_result*.

    Writes ``cpp_sqlite_one_hop.json`` to ``CODEGRAPH_OUTPUT`` and
    returns the filtered node list.  Idempotent — callers that only
    need the JSON on disk can ignore the return value.
    """
    from doxygen_index.graph_json import result_to_graph_json
    import json

    full_graph = result_to_graph_json(merged_result, source="cpp-sqlite")

    # Identify project-owned nodes by source field.
    project_source = "cpp-sqlite"
    project_uids: set[str] = set()
    for node in full_graph:
        if node.get("source", "") == project_source:
            uid = node.get("uid", "")
            if uid:
                project_uids.add(uid)

    # Collect one-hop neighbours: dep nodes targeted by project edges.
    neighbour_uids: set[str] = set()
    for node in full_graph:
        if node.get("uid", "") not in project_uids:
            continue
        for edge in node.get("edges", []):
            target = edge.get("target_uid", "")
            if target and target not in project_uids:
                neighbour_uids.add(target)

    # Also pull in namespace parents of neighbour nodes.  A dependency
    # type (e.g. ``std::shared_ptr``) appears as a neighbour because
    # project code has a DEPENDS_ON edge to it.  Its parent namespace
    # (``std``) composes it, so the namespace node itself isn't targeted
    # by any project edge — but we want it in the one-hop graph for
    # structural context.
    # We do this BEFORE building the keep set so that the parent
    # namespace's own COMPOSES edges (to other siblings the project
    # doesn't directly depend on) are also included.
    parent_ns_uids: set[str] = set()
    for node in full_graph:
        if node.get("kind") != "namespace":
            continue
        for edge in node.get("edges", []):
            if edge.get("relation_type") == "COMPOSES" and edge.get("target_uid", "") in neighbour_uids:
                parent_ns_uids.add(node.get("uid", ""))

    keep_uids = project_uids | neighbour_uids | parent_ns_uids
    filtered = [n for n in full_graph if n.get("uid", "") in keep_uids]

    CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)
    output = CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.json"
    output.write_text(json.dumps(filtered, indent=2, default=str),
                      encoding="utf-8")

    print(f"\n  Filtered graph: {len(filtered)} nodes "
          f"({len(project_uids)} project + {len(neighbour_uids)} neighbours)")
    print(f"  Output: {output}")
    print(f"  Size: {output.stat().st_size:,} bytes")
    return filtered


@pytest.fixture(scope="module")
def one_hop_json(merged_result) -> list[dict]:
    """Return the one-hop filtered graph JSON, regenerating only when
    the pickle cache is newer than the JSON on disk.

    Module-scoped — runs once regardless of test order.  When the
    JSON is fresh the fixture returns instantly from disk.
    """
    import json
    output = CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.json"
    if output.exists() and _UNIFIED_CACHE.exists():
        pickle_mtime = _UNIFIED_CACHE.stat().st_mtime
        json_mtime = output.stat().st_mtime
        if json_mtime >= pickle_mtime:
            print(f"\n  Loading cached one-hop JSON ({output.stat().st_size:,} bytes)")
            return json.loads(output.read_text(encoding="utf-8"))
    return _generate_one_hop_json(merged_result)


class TestFullGraphExport:
    """Export the real cpp-sqlite codebase as a LayerGraph-compatible JSON.

    Loads the cached ParseResult produced by ``doxygen-index codegraph``
    and writes ``cpp_sqlite_one_hop.json``.  The fixture is committed
    so downstream consumers (visualisation, graph analysis) can work
    with a realistic C++ project without running Doxygen.

    ``one_hop_json`` is a module-scoped fixture — it runs once and is
    shared by all tests in this class, so running any single test
    produces a fresh JSON without imposing ordering.
    """

    def test_export_json_with_one_hop(self, one_hop_json):
        """Parse cpp-sqlite + cached deps and write one-hop graph JSON."""
        filtered = one_hop_json
        assert len(filtered) > 50, f"Expected >50 nodes, got {len(filtered)}"

        # Basic structural assertions
        all_edges = []
        for node in filtered:
            all_edges.extend(node.get("edges", []))
        edge_types = {e["relation_type"] for e in all_edges}
        assert "COMPOSES" in edge_types, f"Expected COMPOSES in {edge_types}"
        assert "INVOKES" in edge_types, f"Expected INVOKES in {edge_types}"

        # Verify key classes from the real codebase are present
        node_names = {n.get("name", "") for n in filtered}
        assert "Database" in node_names
        assert "DAOBase" in node_names
        assert "DataAccessObject" in node_names
        assert "Transaction" in node_names

        # Verify dep type nodes appear (one-hop pull-in)
        dep_sources = {n.get("source", "") for n in filtered}
        print(f"  Edge types: {sorted(edge_types)}")
        print(f"  Sources present: {sorted(dep_sources)}")

    def test_all_edges_resolve_to_nodes(self, merged_result):
        """Verify edge target resolution quality.

        Doxygen generates ``<references>`` to symbols it can resolve via
        ``#include`` but doesn't always fully document (e.g. macros,
        extern functions, implementation-detail symbols).  These appear
        as INVOKES edges whose targets don't exist in the graph.

        This test asserts that NON-INVOKES edges all resolve and that
        the overall resolution rate is reasonable.
        """
        from doxygen_index.graph_json import result_to_graph_json

        full_graph = result_to_graph_json(merged_result, source="cpp-sqlite")
        node_uids = {n["uid"] for n in full_graph}

        total_edges = 0
        unresolved: list[dict] = []
        for node in full_graph:
            for edge in node.get("edges", []):
                total_edges += 1
                if edge["target_uid"] not in node_uids:
                    unresolved.append(edge)

        # INVOKES to undocumented symbols are expected.
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

    def test_discovered_dependencies(self, merged_result):
        """Verify conan discovers expected dependencies for cpp-sqlite."""
        if not _conan_deps_available():
            pytest.skip("conan deps not installed")

        from doxygen_index.conan import discover_packages
        pkgs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")

        # Core deps should be present
        assert "boost" in pkgs, f"boost not in {sorted(pkgs)}"
        assert "sqlite3" in pkgs, f"sqlite3 not in {sorted(pkgs)}"
        assert "spdlog" in pkgs, f"spdlog not in {sorted(pkgs)}"

    def test_dependency_relationships(self, one_hop_json):
        """Verify the one-hop JSON has expected DEPENDS_ON, INVOKES,
        and INCLUDES relationships from cpp-sqlite to its dependencies."""
        import json

        data = one_hop_json
        uid_map = {n["uid"]: n for n in data}

        # Gather all DEPENDS_ON edges as (from_qn, to_qn, to_source) triples.
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

        # ── External DEPENDS_ON (project → conan dep) ──
        assert ("cpp_sqlite::Database::db_", "sqlite3", "sqlite3") in depends_on, \
            "Database::db_ should DEPENDS_ON sqlite3"
        assert ("cpp_sqlite::Database::getRawDB()", "sqlite3", "sqlite3") in depends_on, \
            "Database::getRawDB should DEPENDS_ON sqlite3"
        assert ("cpp_sqlite::Database::daos_", "boost::unordered_map", "boost") in depends_on, \
            "Database::daos_ should DEPENDS_ON boost::unordered_map"
        assert ("cpp_sqlite::Database::pLogger_", "spdlog::logger", "spdlog") in depends_on, \
            "Database::pLogger_ should DEPENDS_ON spdlog::logger"

        # ── Stdlib DEPENDS_ON (project → cppreference) ──
        assert ("cpp_sqlite::Database::db_", "std::unique_ptr", "cppreference") in depends_on, \
            "Database::db_ should DEPENDS_ON std::unique_ptr"
        assert ("cpp_sqlite::Database::pLogger_", "std::shared_ptr", "cppreference") in depends_on, \
            "Database::pLogger_ should DEPENDS_ON std::shared_ptr"
        assert ("cpp_sqlite::DataAccessObject::writeBuffer_", "std::vector",
                "cppreference") in depends_on, \
            "DataAccessObject::writeBuffer_ should DEPENDS_ON std::vector"

        # ── INCLUDES (project → dep headers) ──
        assert ("DBDatabase.hpp", "sqlite3.h", "sqlite3") in includes, \
            "DBDatabase.hpp should INCLUDE sqlite3.h"
        assert ("DBDatabase.hpp", "unordered_map.hpp", "boost") in includes, \
            "DBDatabase.hpp should INCLUDE unordered_map.hpp"
        assert ("DBTraits.hpp", "sqlite3.h", "sqlite3") in includes, \
            "DBTraits.hpp should INCLUDE sqlite3.h"

        # ── INVOKES (project → dep functions) ──
        # Database::select calls sqlite3 C API functions.
        assert ("cpp_sqlite::Database::select(PreparedSQLStmt &stmt)",
                "sqlite3_step", "sqlite3") in invokes, \
            "Database::select should INVOKE sqlite3_step"
        assert ("cpp_sqlite::Database::insert(PreparedSQLStmt &stmt, T &data)",
                "sqlite3_bind_int64", "sqlite3") in invokes, \
            "Database::insert should INVOKE sqlite3_bind_int64"
        assert ("cpp_sqlite::Database::isInTransaction(())",
                "sqlite3_close", "sqlite3") in invokes, \
            "Database::isInTransaction should INVOKE sqlite3_close"

        # ── Source provenance: project nodes are tagged "as-built",
        #    dependency nodes are tagged "dependency".
        #    ``tag_nodes_by_source`` in doxygen.py sets both ``source``
        #    and ``tags``.
        for node in data:
            src = node.get("source", "")
            tags = node.get("tags", [])
            if src == "cpp-sqlite":
                assert "as-built" in tags, \
                    f"cpp-sqlite node {node.get('qualified_name','')} should be as-built"
                assert "dependency" not in tags, \
                    f"cpp-sqlite node {node.get('qualified_name','')} should NOT be dependency"
            elif src in ("boost", "spdlog", "sqlite3", "cppreference", "gtest"):
                assert "dependency" in tags, \
                    f"{src} node {node.get('qualified_name','')} should be dependency"

        # ── Dep node counts by source ──
        from collections import Counter
        src_counts = Counter(n.get("source", "?") for n in data)
        assert src_counts.get("boost", 0) >= 3, f"Expected >=3 boost nodes, got {src_counts.get('boost', 0)}"
        assert src_counts.get("spdlog", 0) >= 4, f"Expected >=4 spdlog nodes, got {src_counts.get('spdlog', 0)}"
        assert src_counts.get("sqlite3", 0) >= 10, f"Expected >=10 sqlite3 nodes, got {src_counts.get('sqlite3', 0)}"
        assert src_counts.get("cppreference", 0) >= 7, f"Expected >=7 cppreference nodes, got {src_counts.get('cppreference', 0)}"

        print(f"\n  DEPENDS_ON: {len(depends_on)} unique edges")
        print(f"  INCLUDES:   {len(includes)} unique edges")
        print(f"  INVOKES:    {len(invokes)} unique edges")

    # ------------------------------------------------------------------
    # Namespace COMPOSES assertions
    # ------------------------------------------------------------------

    def test_cpp_sqlite_namespace_composes_classes(self, one_hop_json):
        """Verify that the ``cpp_sqlite`` namespace node has COMPOSES
        edges to the project's top-level classes and structs."""
        data = one_hop_json
        uid_map = {n["uid"]: n for n in data}

        # Locate the cpp_sqlite namespace node (source=cpp-sqlite).
        cpp_sqlite_ns = None
        for n in data:
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "cpp_sqlite"
                    and n.get("source") == "cpp-sqlite"):
                cpp_sqlite_ns = n
                break
        assert cpp_sqlite_ns is not None, "cpp_sqlite namespace node not found"

        # Gather all COMPOSES targets from this namespace.
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

        # Expected top-level cpp_sqlite classes and structs.
        # Note: DAOBase is defined in the global namespace, not cpp_sqlite.
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

        print(f"\n  cpp_sqlite COMPOSES {len(composes_edges)} children:")
        for qn in sorted(composes_targets)[:10]:
            print(f"    {qn}")
        if len(composes_targets) > 10:
            print(f"    ... and {len(composes_targets) - 10} more")

    def test_std_namespace_composes_stdlib_classes(self, merged_result):
        """Verify that the ``std`` namespace node (from cppreference)
        has COMPOSES edges to stdlib class nodes like ``std::vector``
        and ``std::shared_ptr``.

        Uses the full graph (from ``merged_result``) rather than the
        one-hop filtered graph, because the one-hop filter strips
        namespace nodes that are parents of dependency types.
        """
        from doxygen_index.graph_json import result_to_graph_json

        full_graph = result_to_graph_json(merged_result, source="cpp-sqlite")
        uid_map = {n["uid"]: n for n in full_graph}

        # Locate the std namespace from cppreference.
        std_ns = None
        for n in full_graph:
            if (n.get("kind") == "namespace"
                    and n.get("qualified_name") == "std"
                    and n.get("source") == "cppreference"):
                std_ns = n
                break
        assert std_ns is not None, "std namespace node (cppreference) not found"

        # Gather all COMPOSES targets from std.
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

        print(f"\n  std COMPOSES {len(composes_edges)} children:")
        for qn in sorted(composes_targets)[:10]:
            print(f"    {qn}")
        if len(composes_targets) > 10:
            print(f"    ... and {len(composes_targets) - 10} more")

    def test_namespace_composes_edges_resolve(self, one_hop_json):
        """Verify that COMPOSES edges from *project* namespace nodes
        resolve to nodes present in the graph.

        Dependency namespaces (e.g. ``std::optional`` from cppreference)
        may compose children that are not pulled into the one-hop
        graph, so only project-owned namespaces are checked here.
        """
        data = one_hop_json
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
            f"{len(unresolved)} namespace COMPOSES edges fail to resolve: {unresolved[:5]}"
        )
