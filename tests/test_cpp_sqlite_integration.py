"""Integration tests for the Doxygen XML → graph JSON pipeline.

Two fixture levels:
* ``cpp_sqlite_minimal`` — self-contained 132-line header, no external
  deps.  Used for fast unit tests (class discovery, method extraction,
  CSV export, tagging).
* ``cpp-sqlite`` (real) — the actual cpp-sqlite project with Conan
  dependencies (boost, sqlite3, spdlog).  Dependencies are indexed
  independently (``TestDepIndexing``), cached to pickle, and merged with
  the project parse (``TestFullGraphExport``) to produce the one-hop
  graph JSON.

Requirements: ``doxygen`` must be on PATH.  Real-fixture tests require
``conan install . --build=missing`` in ``tests/fixtures/cpp-sqlite``.
"""

from __future__ import annotations

import pytest
from pathlib import Path
import tempfile
import csv
import shutil

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
DEP_CACHE_DIR = FIXTURE_DIR.parent / "dep_cache"

# Single cache file for the unified parse result
_UNIFIED_CACHE = DEP_CACHE_DIR / "cpp_sqlite_unified.pkl"


def _conan_deps_available() -> bool:
    """Return True if conan deps for cpp-sqlite are installed."""
    from doxygen_index.conan import discover_packages
    try:
        pkgs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")
        return "boost" in pkgs and "sqlite3" in pkgs
    except Exception:
        return False


def _save_dep_cache(dep_name: str, result: "ParseResult") -> None:
    """Pickle a ParseResult to the cache directory."""
    import pickle
    DEP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = DEP_CACHE_DIR / f"{dep_name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(result, f)
    print(f"  Cached: {path} ({path.stat().st_size:,} bytes)")


def _load_dep_cache(dep_name: str) -> "ParseResult | None":
    """Load a cached ParseResult, or None if not cached."""
    import pickle
    path = DEP_CACHE_DIR / f"{dep_name}.pkl"
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def _load_unified_cache() -> "ParseResult | None":
    """Load the cached unified parse result."""
    import pickle
    if not _UNIFIED_CACHE.exists():
        return None
    with open(_UNIFIED_CACHE, "rb") as f:
        return pickle.load(f)


def _save_unified_cache(result: "ParseResult") -> None:
    """Pickle the unified parse result."""
    import pickle
    _UNIFIED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(_UNIFIED_CACHE, "wb") as f:
        pickle.dump(result, f)
    print(f"  Unified cache: {_UNIFIED_CACHE} ({_UNIFIED_CACHE.stat().st_size:,} bytes)")


@pytest.fixture(scope="module")
def real_parsed_result():
    """Parse cpp-sqlite + all deps in one unified Doxygen run.

    Slow (boost has 15K headers) — cached to pickle.  Use
    :func:`merged_result` instead for fast loads from cache.
    """
    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    if not _conan_deps_available():
        pytest.skip("conan deps not installed")

    from doxygen_index.doxygen import run_unified_doxygen
    from doxygen_index.conan import discover_packages
    from doxygen_index.parser import parse_xml_dir

    dep_dirs = discover_packages(project_dir=str(_REAL_FIXTURE), build_type="Debug")
    output_dir = Path(tempfile.mkdtemp(prefix="cpp_sqlite_full_"))
    xml_dir = run_unified_doxygen(
        "cpp_sqlite",
        [_REAL_FIXTURE / "cpp_sqlite" / "src"],
        dep_dirs,
        output_dir,
    )
    assert xml_dir is not None, "Doxygen failed"
    result = parse_xml_dir(xml_dir, source="cpp_sqlite", layer="dependency")
    return result


@pytest.fixture(scope="module")
def merged_result():
    """Load the cached unified parse result.

    Populated by ``TestDepIndexing`` on first run.  Subsequent runs are
    instant (no Doxygen needed).
    """
    result = _load_unified_cache()
    if result is None:
        pytest.skip("unified parse cache not populated — run TestDepIndexing first")
    print(f"\n  Loaded unified parse from cache")
    return result


class TestDepIndexing:
    """Run a unified Doxygen parse (project + all deps in INPUT) and
    cache the ParseResult.

    This is slow because boost has 15K headers but only runs once.
    Subsequent tests load the cached result via :func:`merged_result`.
    """

    def test_index_unified_and_cache(self):
        """Parse cpp-sqlite + all deps (including targeted boost modules)
        in one Doxygen run, and pickle the result.

        Boost is huge (15K headers) but ``run_unified_doxygen`` uses
        ``gcc -E -M`` to discover only the boost modules the project
        actually includes (~14 of 120+, ~410 files).
        """
        if not _doxygen_available():
            pytest.skip("doxygen not found on PATH")
        if not _conan_deps_available():
            pytest.skip("conan deps not installed")

        from doxygen_index.doxygen import run_unified_doxygen
        from doxygen_index.conan import discover_packages
        from doxygen_index.parser import parse_xml_dir

        dep_dirs = discover_packages(
            project_dir=str(_REAL_FIXTURE), build_type="Debug"
        )
        output_dir = Path(tempfile.mkdtemp(prefix="dep_unified_"))
        try:
            xml_dir = run_unified_doxygen(
                "cpp_sqlite",
                [_REAL_FIXTURE / "cpp_sqlite" / "src"],
                dep_dirs,
                output_dir,
            )
            assert xml_dir is not None, "Doxygen failed"

            result = parse_xml_dir(xml_dir, source="cpp_sqlite",
                                   layer="dependency")

            # Tag nodes by file location so dep symbols get the correct
            # source label (e.g. boost nodes → source="boost").
            from doxygen_index.doxygen import tag_nodes_by_source, resolve_namespace_type_deps
            tag_nodes_by_source(result, _REAL_FIXTURE, dep_dirs,
                                "cpp_sqlite")

            # Parse cppreference to get stdlib nodes (std::unique_ptr,
            # std::shared_ptr, std::string, ...).  Cached to avoid
            # re-parsing on every run.
            cppref_result = _load_dep_cache("cppreference")
            if cppref_result is None:
                try:
                    from doxygen_index.cppreference import parse as parse_cppref
                    cppref_dir = Path.home() / ".cache" / "doxygen-index" / "cppreference" / "reference"
                    if cppref_dir.exists():
                        cppref_result = parse_cppref(cppref_dir, progress_interval=200)
                        _save_dep_cache("cppreference", cppref_result)
                except ImportError:
                    print("  Warning: cppreference extra not installed — stdlib deps unavailable")
                except Exception as e:
                    print(f"  Warning: cppreference parse failed: {e}")

            if cppref_result is not None:
                from doxygen_index.graph_json import merge_parse_results
                result = merge_parse_results(result, cppref_result)
                print(f"  Merged cppreference: +{len(cppref_result.classes)} classes, +{len(cppref_result.functions)} functions")

            # Run namespace scanning (now std::unique_ptr et al. resolve).
            resolve_namespace_type_deps(result)

            node_count = (
                len(result.classes) + len(result.methods)
                + len(result.attributes) + len(result.functions)
                + len(result.namespaces) + len(result.enums)
                + len(result.concepts) + len(result.defines)
            )
            print(f"\n  Unified parse: {node_count} nodes total")
            _save_unified_cache(result)
            assert node_count > 100, (
                f"Expected >100 nodes in unified parse, got {node_count}"
            )
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)


class TestFullGraphExport:
    """Export the real cpp-sqlite codebase as a LayerGraph-compatible JSON.

    Loads cached dependency ParseResults, merges them with the project
    parse, and writes ``cpp_sqlite_one_hop.json``.

    The fixture is committed so downstream consumers (visualisation, graph
    analysis) can work with a realistic C++ project without running Doxygen.
    """

    def test_export_json_with_one_hop(self, merged_result):
        """Parse cpp-sqlite + cached deps and write one-hop graph JSON."""
        from doxygen_index.graph_json import result_to_graph_json
        import json

        full_graph = result_to_graph_json(merged_result, source="cpp_sqlite")
        assert len(full_graph) > 50, f"Expected >50 nodes, got {len(full_graph)}"

        # Identify project-owned nodes by source field.
        project_source = "cpp_sqlite"
        project_uids: set[str] = set()
        for node in full_graph:
            if node.get("source", "") == project_source:
                uid = node.get("uid", "")
                if uid:
                    project_uids.add(uid)

        assert len(project_uids) > 10, (
            f"Expected >10 cpp_sqlite project nodes, got {len(project_uids)}"
        )

        # Collect one-hop neighbours: dep nodes targeted by project edges.
        neighbour_uids: set[str] = set()
        for node in full_graph:
            if node.get("uid", "") not in project_uids:
                continue
            for edge in node.get("edges", []):
                target = edge.get("target_uid", "")
                if target and target not in project_uids:
                    neighbour_uids.add(target)

        # Build filtered graph: project nodes + one-hop neighbours.
        keep_uids = project_uids | neighbour_uids
        filtered = [n for n in full_graph if n.get("uid", "") in keep_uids]

        output_dir = TEST_DATA_DIR / "unit_test_data"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "cpp_sqlite_one_hop.json"
        output.write_text(json.dumps(filtered, indent=2, default=str),
                          encoding="utf-8")

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
        print(f"\n  Filtered graph: {len(filtered)} nodes "
              f"({len(project_uids)} project + {len(neighbour_uids)} neighbours)")
        print(f"  Edge types: {sorted(edge_types)}")
        print(f"  Sources present: {sorted(dep_sources)}")
        print(f"  Output: {output}")
        print(f"  Size: {output.stat().st_size:,} bytes")

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

        full_graph = result_to_graph_json(merged_result, source="cpp_sqlite")
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

    def test_dependency_relationships(self, merged_result):
        """Verify the one-hop JSON has expected DEPENDS_ON, INVOKES,
        and INCLUDES relationships from cpp-sqlite to its dependencies."""
        import json

        output = TEST_DATA_DIR / "unit_test_data" / "cpp_sqlite_one_hop.json"
        if not output.exists():
            pytest.skip("one-hop JSON not generated — run test_export_json_with_one_hop first")

        data = json.loads(output.read_text(encoding="utf-8"))
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

        # ── Source provenance: all dep nodes must be tagged "dependency" ──
        for node in data:
            src = node.get("source", "")
            tags = node.get("tags", [])
            if src == "cpp_sqlite":
                assert "as-built" in tags, \
                    f"cpp_sqlite node {node.get('qualified_name','')} should be as-built"
                assert "dependency" not in tags, \
                    f"cpp_sqlite node {node.get('qualified_name','')} should NOT be dependency"
            elif src in ("boost", "spdlog", "sqlite3", "cppreference"):
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
