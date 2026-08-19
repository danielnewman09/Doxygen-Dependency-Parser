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
            uids1 = [r["canonical_key:ID"] for r in csv.DictReader(f)]
        with open(n2, newline="", encoding="utf-8") as f:
            uids2 = [r["canonical_key:ID"] for r in csv.DictReader(f)]

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

