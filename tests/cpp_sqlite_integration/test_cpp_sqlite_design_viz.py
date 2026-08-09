"""Design-layer visualisation tests for the cpp-sqlite project.

Verifies that a design layergraph (from a JSON file) can be ingested
into the active backend, retrieved as a "design"-tagged LayerGraph,
exported to PlantUML, and rendered to SVG — with expected elements
and structure.

The design graph represents a version-migration system built on top
of cpp-sqlite (MigrationManager, SchemaVersion, Migration, etc.).

Backend-agnostic: runs against the active backend (sqlite by
default, no Docker) via ``to_backend``/``from_backend``.  Does not
require the cpp-sqlite as-built data to be pre-indexed.
"""

from __future__ import annotations

import json as _json
import shutil
import subprocess
from pathlib import Path

import pytest

# This suite ingests the design LayerGraph into the active backend and
# exercises round-trips; its ``_ensure_backend`` fixture ensures the
# active backend is configured (sqlite by default, no Docker).
_HERE = Path(__file__).resolve().parent
_CODEGRAPH_OUTPUT = _HERE.parent / "codegraph_output"
_DESIGN_JSON = _HERE.parent / "data" / "design_layergraph.json"


@pytest.fixture(scope="session")
def _ensure_backend():
    """Ensure the ACTIVE backend is configured (session-scoped).

    sqlite (default): a self-contained in-memory database so the design
    graph stays isolated from the as-built cpp-sqlite index.  neo4j
    (opt-in): the test container on port 7689.
    """
    import os as _os
    from codegraph.backends import set_backend
    from codegraph import get_backend

    if _os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower() == "neo4j":
        from codegraph.backends.neo4j import Neo4jBackend, Neo4jConfig
        bolt_uri = "bolt://localhost:7689"
        set_backend(Neo4jBackend(Neo4jConfig(
            uri=bolt_uri,
            user="neo4j",
            password="doxygen-index-test",
        )))
        backend = get_backend()
        try:
            backend.execute_raw("RETURN 1")
        except Exception as e:
            pytest.skip(f"Neo4j not available on {bolt_uri}: {e}")
    else:
        from codegraph.backends.sqlite import SqliteBackend, SqliteConfig
        set_backend(SqliteBackend(SqliteConfig(path=":memory:")))


class TestDesignLayerVisualization:
    """Ingest the design layergraph JSON into the active backend,
    retrieve the "design" LayerGraph, and export PlantUML.
    Verifies the DESIGN_API
    output: architecture classes, packages, enums, members — but NOT
    test scaffolding (TestNode, AssertionNode, TestStepNode,
    TestFixtureNode, LiteralNode)."""

    @pytest.fixture(scope="class")
    def design_puml_text(self, _ensure_backend):
        """Ingest design JSON → active backend → retrieve design layer →
        export PlantUML.  Saves the PUML to codegraph_output."""
        from codegraph.graph import LayerGraph
        from codegraph.export.plantuml import export_plantuml, GraphView
        from codegraph import get_backend

        # ── Step 1: Ingest design JSON into the active backend ──
        assert _DESIGN_JSON.exists(), f"Design JSON not found at {_DESIGN_JSON}"
        data = _json.loads(_DESIGN_JSON.read_text(encoding="utf-8"))
        graph = LayerGraph.deserialize(data)
        graph.to_backend(get_backend())

        # ── Step 2: Retrieve the "design" LayerGraph ─────────
        design_graph = LayerGraph.from_backend(get_backend(), "design")

        # ── Step 3: Export design API view (architecture only,
        #            no test scaffolding) ────────────────────
        puml_text = export_plantuml(
            design_graph, fields="all", view=GraphView.DESIGN_API,
        )
        puml_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_design.puml"
        puml_output.write_text(puml_text, encoding="utf-8")

        # ── Step 4: Export public API view (design API + no
        #            private members) ────────────────────────
        puml_public = export_plantuml(
            design_graph, fields="all", view=GraphView.PUBLIC_API,
        )
        puml_public_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_design_public.puml"
        puml_public_output.write_text(puml_public, encoding="utf-8")

        # ── Step 5: Render SVGs ─────────────────────────────
        plantuml_bin = shutil.which("plantuml")
        if plantuml_bin:
            for puml_name in ("cpp_sqlite_design.puml",
                              "cpp_sqlite_design_public.puml"):
                subprocess.run(
                    [plantuml_bin, "-tsvg", puml_name],
                    cwd=str(_CODEGRAPH_OUTPUT), timeout=120,
                )
            svg_ok = (_CODEGRAPH_OUTPUT / "cpp_sqlite_design.svg").exists()
        else:
            svg_ok = False

        print(f"\n  Design PUML: {puml_output} "
              f"({puml_output.stat().st_size:,} bytes)")
        print(f"  Design SVG:  {'✓' if svg_ok else '✗ (plantuml not found)'}")
        return puml_text

    @pytest.fixture(scope="class")
    def design_lines(self, design_puml_text):
        """Non-empty, non-comment, non-skinparam PlantUML lines."""
        return [
            L for L in design_puml_text.split("\n")
            if L.strip() and not L.strip().startswith("'")
            and not L.strip().startswith("@")
            and not L.strip().startswith("skinparam")
        ]

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def test_puml_has_startuml(self, design_puml_text):
        """The PlantUML output is wrapped in @startuml / @enduml."""
        assert "@startuml" in design_puml_text
        assert "@enduml" in design_puml_text

    # ------------------------------------------------------------------
    # Package assertions
    # ------------------------------------------------------------------

    def test_has_cpp_sqlite_package(self, design_lines):
        """The cpp_sqlite namespace is a package containing design classes."""
        pkg_lines = [L for L in design_lines
                     if 'package "cpp_sqlite" as cpp_sqlite' in L]
        assert len(pkg_lines) == 1, (
            f"Expected exactly 1 cpp_sqlite package, got {len(pkg_lines)}"
        )

    # ------------------------------------------------------------------
    # Design class assertions
    # ------------------------------------------------------------------

    def test_has_key_design_classes(self, design_lines):
        """Core design classes appear in the PlantUML output.

        The design agent output contains the 8 contract classes plus
        the cpp-sqlite types the design depends on (Database,
        Transaction).  The regenerated fixture (pipeline dump) also
        carries the as-built/dependency footprint of the design;
        see ``test_design_depends_on_as_built_types`` for the edge
        assertions.
        """
        for cls_name in (
            "MigrationManager",
            "SchemaVersion",
            "Migration",
            "SchemaVerificationResult",
            "SchemaMismatch",
            "MigrationResult",
            "Transaction",
            "Database",
        ):
            assert any(
                cls_name in L and "class" in L
                for L in design_lines
            ), f"Missing design class: {cls_name}"

    def test_design_depends_on_as_built_types(self, design_puml_text):
        """The design layer carries DEPENDS_ON edges to the as-built and
        dependency types it uses.

        The regenerated design graph is the authoritative pipeline
        output: the design's MigrationManager takes a Database, its
        Migration contract operates on a Transaction, and the manager
        stores std::unique_ptr<Migration> / std::vector<int>.  These
        references surface as labelled ``..>`` arrows in the export.
        """
        for arrow in (
            "cpp_sqlite__Migration ..> cpp_sqlite__Transaction : depends_on",
            "cpp_sqlite__MigrationManager ..> cpp_sqlite__Database : depends_on",
            "cpp_sqlite__MigrationManager ..> std__unique_ptr : depends_on",
            "cpp_sqlite__MigrationManager ..> std__vector : depends_on",
        ):
            assert arrow in design_puml_text, (
                f"Missing design dependency edge: {arrow}"
            )

    def test_has_key_design_structs(self, design_lines):
        """Design value types (kind=struct) appear as PlantUML classes."""
        for struct_name in (
            "MigrationResult",
            "SchemaMismatch",
        ):
            assert any(
                struct_name in L and "class" in L
                for L in design_lines
            ), f"Missing design struct: {struct_name}"

    def test_has_enums_with_values(self, design_puml_text):
        """Design enums appear with their expected values."""
        # Enum declarations
        assert 'enum "MismatchKind"' in design_puml_text, (
            "Missing MismatchKind enum"
        )
        assert 'enum "MigrationErrorCode"' in design_puml_text, (
            "Missing MigrationErrorCode enum"
        )

        # Enum values (rendered as bare labels inside the enum block)
        enum_values = [
            "ChecksumMismatch", "ColumnDifference",
            "ExtraTable", "MissingTable",
            "NotInitialized", "VersionNotFound", "RollbackFailed",
            "MigrationFailed", "DuplicateVersion", "Success",
        ]
        for val in enum_values:
            assert val in design_puml_text, f"Missing enum value: {val}"

    # ------------------------------------------------------------------
    # Member assertions
    # ------------------------------------------------------------------

    def test_migration_manager_has_methods(self, design_lines):
        """MigrationManager exposes key methods with signatures.

        The agent output renders the full signature ``name(args):
        return_type full_declaration``, so assertions match on the
        method name + argument list (present in the name prefix).
        """
        methods = [
            "+verify():",
            "+rollback(int target_version):",
            "+apply():",
            "+register_migration(std::unique_ptr<Migration>):",
        ]
        for method in methods:
            assert any(method in L for L in design_lines), (
                f"Missing MigrationManager method: {method}"
            )
        # The constructor's argsstring is the full declaration
        # ``MigrationManager(Database &db)``, so the rendered line is
        # the method name prepended to it.
        assert any(
            L.lstrip().startswith("+MigrationManager")
            and "Database &db" in L
            for L in design_lines
        ), "Missing MigrationManager constructor (Database &db)"

    def test_migration_manager_has_private_members(self, design_lines):
        """MigrationManager's private members (migrations_, db_) appear
        with ``-`` visibility prefix."""
        for member in ("-migrations_", "-db_"):
            assert any(member in L for L in design_lines), (
                f"Missing private member: {member}"
            )

    def test_class_has_members(self, design_lines):
        """Classes with members show them inline — not as standalone
        top-level attributes."""
        # SchemaVersion should have members
        assert any(
            "+checksum: std::string" in L for L in design_lines
        ), "SchemaVersion missing checksum member"
        assert any(
            "+version: int" in L for L in design_lines
        ), "SchemaVersion missing version member"

    # ------------------------------------------------------------------
    # DESIGN_API hides test scaffolding
    # ------------------------------------------------------------------

    def test_design_view_excludes_test_nodes(self, design_puml_text):
        """DESIGN_API view hides TestNode scaffolding."""
        test_names = [
            "test_duplicate_version_rejected",
            "test_sorted_by_version",
            "test_applies_only_pending",
            "test_up_failure_rolls_back",
            "test_apply_in_order",
            "test_mismatch_detected",
            "test_consistent_schema",
            "test_rollback_to_version",
            "test_down_failure_aborts",
        ]
        for name in test_names:
            assert f'package "{name}"' not in design_puml_text, (
                f"Test node {name} should be excluded from DESIGN_API view"
            )

    def test_design_view_excludes_assertions_and_steps(self, design_puml_text):
        """DESIGN_API view hides AssertionNode and TestStepNode scaffolding."""
        assert "cond__pre__" not in design_puml_text, (
            "Precondition assertions should be excluded from DESIGN_API"
        )
        assert "cond__post__" not in design_puml_text, (
            "Postcondition assertions should be excluded from DESIGN_API"
        )
        assert "step__" not in design_puml_text, (
            "Test steps should be excluded from DESIGN_API"
        )

    def test_design_view_excludes_test_scaffolding_children(self, design_puml_text):
        """DESIGN_API view excludes test scaffolding including nested
        assertion names."""
        assert 'package "test_duplicate_version_rejected"' not in design_puml_text
        assert 'class "reg_dup_error_is_duplicate"' not in design_puml_text
        assert 'class "reg_dup_invoke_register_first"' not in design_puml_text

    # ------------------------------------------------------------------
    # Design-level attribute (fixture) assertions
    # ------------------------------------------------------------------

    def test_design_view_excludes_fixture_attributes(self, design_lines):
        """Design-level fixture attributes carry the ``"test"`` tag
        and are excluded from DESIGN_API view."""
        fixtures = [
            "error_state",
            "is_initialized",
            "registered_versions",
            "execution_order",
        ]
        for attr in fixtures:
            assert not any(
                attr in L and "<<attribute>>" in L
                for L in design_lines
            ), f"Fixture attribute {attr} should be excluded (has 'test' tag)"

    def test_design_view_excludes_assertion_edges(self, design_puml_text):
        """DESIGN_API view excludes left_operand/right_operand edges
        (they only connect assertions to fixtures/literals, and assertions
        are hidden)."""
        assert "left_operand" not in design_puml_text, (
            "left_operand edges should be excluded (assertions hidden)"
        )
        assert "right_operand" not in design_puml_text, (
            "right_operand edges should be excluded (assertions hidden)"
        )

    # ------------------------------------------------------------------
    # Public API view assertions
    # ------------------------------------------------------------------

    @pytest.fixture(scope="class")
    def public_puml_text(self):
        """Load the public-only PlantUML from the pre-generated file."""
        puml_path = _CODEGRAPH_OUTPUT / "cpp_sqlite_design_public.puml"
        assert puml_path.exists(), f"Public PUML not found at {puml_path}"
        return puml_path.read_text(encoding="utf-8")

    def test_public_view_hides_private_members(self, design_puml_text,
                                                 public_puml_text):
        """The PUBLIC_API view hides private members (-prefix)."""
        assert "-migrations_" in design_puml_text, (
            "Full view should have private members"
        )
        assert "-migrations_" not in public_puml_text, (
            "Public view should hide private members"
        )
        assert "-db_" not in public_puml_text, (
            "Public view should hide private db_ member"
        )

    def test_public_view_retains_design_classes(self, public_puml_text):
        """The PUBLIC_API view still shows the cpp_sqlite design classes."""
        assert "MigrationManager" in public_puml_text
        assert "SchemaVersion" in public_puml_text
        assert 'package "cpp_sqlite"' in public_puml_text

    def test_public_view_is_larger_than_design_api(self, design_puml_text,
                                                      public_puml_text):
        """DESIGN_API is smaller than PUBLIC_API because it strips
        test scaffolding that PUBLIC_API doesn't filter."""
        assert len(design_puml_text) < len(public_puml_text), (
            f"DESIGN_API ({len(design_puml_text)} chars) should be "
            f"smaller than PUBLIC_API ({len(public_puml_text)} chars)"
        )

    # ------------------------------------------------------------------
    # SVG rendering
    # ------------------------------------------------------------------

    def test_design_svg_generated(self):
        """The DESIGN_API PlantUML was rendered to SVG.

        DESIGN_API excludes test scaffolding, so no nested class-in-class
        — PlantUML should render cleanly."""
        svg_path = _CODEGRAPH_OUTPUT / "cpp_sqlite_design.svg"
        assert svg_path.exists(), (
            f"SVG not generated at {svg_path} — plantuml CLI may be missing"
        )
        svg_text = svg_path.read_text(encoding="utf-8")
        assert "<svg" in svg_text, "SVG file missing <svg> root element"
        assert "</svg>" in svg_text, "SVG file missing closing </svg> tag"
        assert "IllegalStateException" not in svg_text, (
            "PlantUML rendering error — check PUML syntax"
        )
        svg_size = svg_path.stat().st_size
        assert svg_size > 3000, f"SVG too small ({svg_size} bytes)"
        print(f"\n  Design SVG: {svg_size:,} bytes")

    def test_public_svg_generated(self):
        """The PUBLIC_API design PlantUML was rendered to SVG."""
        svg_path = _CODEGRAPH_OUTPUT / "cpp_sqlite_design_public.svg"
        assert svg_path.exists(), (
            f"Public SVG not generated at {svg_path} — plantuml CLI may be missing"
        )
        svg_text = svg_path.read_text(encoding="utf-8")
        assert "<svg" in svg_text
        assert "</svg>" in svg_text
        assert "IllegalStateException" not in svg_text
        svg_size = svg_path.stat().st_size
        assert svg_size > 1000, f"Public SVG too small ({svg_size} bytes)"
        print(f"\n  Public SVG: {svg_size:,} bytes")


class TestDesignLayerRoundTrip:
    """Verify that the design LayerGraph round-trips through
    PlantUML import/export without losing structure."""

    @pytest.fixture(scope="class")
    def design_roundtrip_puml(self, _ensure_backend):
        """Ingest design, retrieve, export → import → re-export."""
        import json as _json
        from codegraph.graph import LayerGraph
        from codegraph.export.plantuml import import_plantuml, export_plantuml
        from codegraph import get_backend

        # Ingest
        data = _json.loads(_DESIGN_JSON.read_text(encoding="utf-8"))
        graph = LayerGraph.deserialize(data)
        graph.to_backend(get_backend())

        # Retrieve and export
        design_graph = LayerGraph.from_backend(get_backend(), "design")
        puml1 = export_plantuml(design_graph, fields="all")

        # Import → re-export
        graph2 = import_plantuml(puml1, tags=frozenset({"design"}))
        return export_plantuml(graph2, fields="all")

    def test_roundtrip_has_startuml(self, design_roundtrip_puml):
        """Round-tripped PlantUML has valid framing."""
        assert "@startuml" in design_roundtrip_puml
        assert "@enduml" in design_roundtrip_puml

    def test_roundtrip_has_key_classes(self, design_roundtrip_puml):
        """Key design classes survive the round-trip."""
        for cls_name in (
            "MigrationManager", "SchemaVersion", "Migration",
            "Transaction", "Database",
        ):
            assert cls_name in design_roundtrip_puml, (
                f"{cls_name} missing from round-tripped PlantUML"
            )

    def test_roundtrip_has_cpp_sqlite_package(self, design_roundtrip_puml):
        """The cpp_sqlite package survives the round-trip."""
        assert 'package "cpp_sqlite"' in design_roundtrip_puml

    def test_roundtrip_has_methods(self, design_roundtrip_puml):
        """Class members survive the round-trip."""
        assert "+apply()" in design_roundtrip_puml
        assert "+verify()" in design_roundtrip_puml
        assert "+rollback(" in design_roundtrip_puml

    def test_roundtrip_reenport_is_valid(self, design_roundtrip_puml):
        """Re-importing the re-exported PlantUML succeeds."""
        from codegraph.export.plantuml import import_plantuml
        graph = import_plantuml(
            design_roundtrip_puml, tags=frozenset({"design"}),
        )
        assert len(graph.entries) > 0
        print(f"\n  Re-import entries: {len(graph.entries)}")
