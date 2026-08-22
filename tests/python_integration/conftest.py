"""Fixtures for the Python full-integration tests.

Session-scoped ``codegraph_graph`` fixture indexes THIS library's own
Python source (``src/doxygen_index`` — the dogfooding fixture) into the
active backend (sqlite by default — no Docker; ``CODEGRAPH_BACKEND=neo4j``
opt-in) once, then returns ``(serialized, uid_map)``.  All test modules
in this directory share the same indexing run.

The fixture project is the repository itself: the root
``.doxygen-index.toml`` declares ``language = "python"`` and
``input_paths = ["src"]``, so the full pipeline (AST parse → codegraph
nodes → backend → LayerGraph) runs against real, non-trivial Python:
cross-module imports, a class hierarchy (``LanguageParser`` interface →
``CppParser``/``PythonParser``), enums, free functions with rich
INVOKES edges, and structured parameters.

Requirements: the ``doxygen-index`` CLI on PATH (no doxygen, no Conan —
the Python parser needs only ``ast``).

The generated backend database (sqlite backend only) is archived to
``tests/unit_test_data/python_integration.sqlite3`` alongside the
serialized JSON so external tooling can validate against the exact
database the suite exercised.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import os as _os
import json as _json

import pytest

_HERE = Path(__file__).resolve().parent
_PARENT_TESTS = _HERE.parent
#: The fixture project: this repository's own Python source.
_REPO_ROOT = _PARENT_TESTS.parent
_CODEGRAPH_OUTPUT = _PARENT_TESTS / "codegraph_output"

#: Gitignored directory for generated test data (serialized JSON,
#: archived sqlite database, …).  Kept out of git — the artifacts are
#: large and fully reproducible from the fixture sources.
_UNIT_TEST_DATA = _PARENT_TESTS / "unit_test_data"

#: Source label the ingest assigns to the project's own code.  Derived
#: from the repo-root config name (tests/.doxygen-index.toml is not used
#: — the fixture project config lives at the repo root).
PROJECT_SOURCE = "doxygen-index"

# Mirror the credentials from tests/conftest.py (via docker-compose.yml).
_TEST_BOLT_PORT = 7689
_TEST_USER = "neo4j"
_TEST_PASSWORD = "doxygen-index-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refresh_fixture_report(
    json_path: Path,
    db_path: Path,
    out_path: Path,
    title: str,
    test_cpp: Path | None = None,
) -> None:
    """Regenerate the markdown fixture report from the archived artifacts.

    Loads ``scripts/export_fixture_report.py`` (repo-relative) and runs
    it against the just-archived JSON + sqlite db.  Best-effort: a
    failure prints a warning rather than failing the suite.
    """
    try:
        import importlib.util

        scripts_dir = _PARENT_TESTS.parent / "scripts"
        spec = importlib.util.spec_from_file_location(
            "export_fixture_report", scripts_dir / "export_fixture_report.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        report = mod.build_report(
            json_path, db_path, test_cpp, title,
        )
        out_path.write_text(report + "\n", encoding="utf-8")
        print(f"  [report] {out_path.name} refreshed "
              f"({out_path.stat().st_size:,} bytes)")
    except Exception as e:  # noqa: BLE001 — best-effort artifact refresh
        print(f"  [report] fixture report not refreshed: {e}")


def _cli_command() -> list[str]:
    """Return the command that runs the ``doxygen-index`` CLI.

    Prefers a ``doxygen-index`` entry point on PATH; falls back to
    ``python -m doxygen_index.cli`` so the suite also runs when only the
    venv's python is on PATH.
    """
    exe = shutil.which("doxygen-index")
    if exe:
        return [exe]
    return [sys.executable, "-m", "doxygen_index.cli"]


def _archive_sqlite(source_path: Path, dest_path: Path) -> None:
    """Snapshot a live WAL-mode sqlite database into *dest_path*.

    The backend keeps the database open in WAL journal mode, so a plain
    ``shutil.copy2`` races the ``-wal`` sidecar and can silently drop
    committed-but-uncheckpointed frames.  SQLite's online backup API
    merges WAL content into the snapshot.  The destination is switched
    to rollback-journal mode so the archived file is fully
    self-contained (no ``-wal``/``-shm`` sidecars for external tools).
    """
    import sqlite3

    for sidecar in (dest_path,
                    Path(str(dest_path) + "-wal"),
                    Path(str(dest_path) + "-shm")):
        sidecar.unlink(missing_ok=True)
    src = sqlite3.connect(str(source_path), timeout=30)
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()


def _wait_for_neo4j(
    uri: str,
    user: str,
    password: str,
    timeout: int = 60,
) -> None:
    """Poll Neo4j until it accepts connections or *timeout* seconds elapse."""
    import time
    from neo4j import GraphDatabase

    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            with GraphDatabase.driver(uri, auth=(user, password)) as driver:
                driver.verify_connectivity()
            return
        except Exception as e:
            last_err = str(e)
            time.sleep(2)
    pytest.fail(f"Neo4j not reachable at {uri} after {timeout}s: {last_err}")


def _flat_uid_map(serialized: list[dict]) -> dict[str, dict]:
    """Build a flat {uid: node_dict} map by walking the nested ``composes`` tree."""
    uid_map: dict[str, dict] = {}
    stack = list(serialized)
    while stack:
        node = stack.pop()
        uid_map[node["canonical_key"]] = node
        stack.extend(node.get("composes", []))
    return uid_map


# ---------------------------------------------------------------------------
# Backend selection — the full-pipeline ingest runs ``doxygen-index project
# --format neo4j`` (write to the active backend; the write path is
# backend-agnostic).  sqlite is the default (no Docker);
# CODEGRAPH_BACKEND=neo4j opt-in.
# ---------------------------------------------------------------------------


def _backend_name() -> str:
    return _os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower()


#: Shared sqlite database file for the ingest subprocess + this process.
#: (The main conftest defaults ``SQLITE_PATH=:memory:``; the integration
#: subprocess is a separate process, so it needs a real file.)
_SQLITE_PATH = _CODEGRAPH_OUTPUT / "python_integration.sqlite3"


@pytest.fixture(autouse=True)
def clear_db(request):
    """No-op: preserve the indexed graph across read-only tests."""
    yield


# ---------------------------------------------------------------------------
# Session-scoped fixture — runs ONCE for this directory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def codegraph_graph():
    """Full reindex + backend ingest + LayerGraph retrieval.

    1. Runs ``doxygen-index project <repo-root> --format neo4j --clear``
       on this repository's own Python source (``src/doxygen_index``).
    2. Retrieves the as-built LayerGraph from the backend.
    3. Saves serialized JSON and self-contained HTML.
    4. Exports PlantUML (full + collapsed + public) and validates
       SVG renders.

    Session-scoped — runs once for the entire test session.  The
    serialized JSON is committed so downstream consumers can work
    without the backend.
    """
    import time as _time
    _T0 = _time.time()

    def _dbg(msg):
        print(f"  [ingest +{_time.time() - _T0:6.1f}s] {msg}", flush=True)

    _dbg("codegraph_graph fixture: start")
    backend_name = _backend_name()

    config_file = _REPO_ROOT / ".doxygen-index.toml"
    if not config_file.exists():
        pytest.fail(
            f"fixture project config not found: {config_file} — the Python "
            f"integration suite indexes this repository itself"
        )
    if not (_REPO_ROOT / "src" / "doxygen_index").is_dir():
        pytest.fail(f"fixture source tree not found: {_REPO_ROOT / 'src'}")

    _CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)
    _UNIT_TEST_DATA.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Index into the active backend (--clear handles stale data) ──
    # The subprocess is a separate process — it cannot use set_backend();
    # get_backend() auto-configures from env vars, so pass them through.
    if backend_name == "neo4j":
        env = {**_os.environ,
               "NEO4J_URI": f"bolt://localhost:{_TEST_BOLT_PORT}",
               "NEO4J_USER": _TEST_USER,
               "NEO4J_PASSWORD": _TEST_PASSWORD}
        # ── Wait for Neo4j to be healthy ──
        _wait_for_neo4j(
            uri=f"bolt://localhost:{_TEST_BOLT_PORT}",
            user=_TEST_USER,
            password=_TEST_PASSWORD,
            timeout=60,
        )
    else:
        # sqlite (default): a real file shared between the subprocess
        # (which writes it) and this process (which reads it back).
        if _SQLITE_PATH.exists():
            _SQLITE_PATH.unlink()
        env = {**_os.environ,
               "CODEGRAPH_BACKEND": "sqlite",
               "SQLITE_PATH": str(_SQLITE_PATH)}

    _dbg("launching ingest subprocess...")
    # Stream subprocess output to a log file (not captured) so a hung
    # ingest is diagnosable live: tail tests/codegraph_output/ingest-python.log
    ingest_log = _CODEGRAPH_OUTPUT / "ingest-python.log"
    with ingest_log.open("w", encoding="utf-8") as _logf:
        result = subprocess.run(
            _cli_command()
            + [
                "project", str(_REPO_ROOT),
                "--format", "sqlite" if backend_name == "sqlite" else "neo4j",
                "--clear", "--yes",
                "--output-dir", str(_CODEGRAPH_OUTPUT / "python"),
            ],
            cwd=str(_REPO_ROOT),
            env=env, timeout=600,
            stdout=_logf, stderr=subprocess.STDOUT,
            text=True,
        )
    _dbg(f"ingest subprocess done rc={result.returncode} (log: {ingest_log})")
    _sub_out = ingest_log.read_text(encoding="utf-8")
    print(f"\n  [subprocess] rc={result.returncode}")
    print(f"  [subprocess] stdout tail:\n{_sub_out[-3000:]}")
    if result.returncode != 0:
        print(f"  [subprocess] stderr tail:\n{_sub_out[-3000:]}")
        pytest.fail(f"doxygen-index project failed (rc={result.returncode})")

    # ── Step 2: Configure backend in *this* process ──────
    from codegraph.backends import set_backend
    if backend_name == "neo4j":
        from codegraph.backends.neo4j import Neo4jBackend, Neo4jConfig
        set_backend(Neo4jBackend(Neo4jConfig(
            uri=f"bolt://localhost:{_TEST_BOLT_PORT}",
            user=_TEST_USER,
            password=_TEST_PASSWORD,
        )))
        from codegraph import get_backend
        backend = get_backend()
        # Force a fresh driver connection — ensure_driver() would no-op
        # if db.driver was already set by a previous fixture to a
        # different DB.
        import neomodel
        backend.close()
        neomodel.db.driver = None  # close() doesn't null this; ensure_driver() needs it
        backend.execute_raw("RETURN 1")  # triggers ensure_driver() → fresh connection
    else:
        from codegraph.backends.sqlite import SqliteBackend, SqliteConfig
        set_backend(SqliteBackend(SqliteConfig(path=str(_SQLITE_PATH))))
        from codegraph import get_backend
        backend = get_backend()

    # Diagnostic: verify data is reachable (backend-agnostic)
    _dbg("diag count query...")
    as_built_count = len(backend.graph.find_uids_by_tag("as-built"))
    print(f"\n  [diag] as-built nodes in backend: {as_built_count}")

    from codegraph.graph import LayerGraph
    _dbg("LayerGraph.from_backend('as-built')...")
    graph = LayerGraph.from_backend(backend, "as-built")
    _dbg("from_backend done")
    serialized = graph.serialize(fields="all")
    _dbg(f"serialize done: {len(serialized)} entries")

    if not serialized:
        pytest.fail("LayerGraph is empty — indexing produced no nodes")

    # ── Step 3: Save serialization ─────────────────────────
    _dbg("writing json...")
    json_output = _UNIT_TEST_DATA / "doxygen_index_one_hop.json"
    json_output.write_text(
        _json.dumps(serialized, indent=2, default=str),
        encoding="utf-8",
    )
    _dbg("json written")

    # ── Step 3b: Save sqlite reference artifact ─────────────
    # The generated backend database is archived into unit_test_data
    # alongside the serialized JSON so external tooling can open the
    # exact database the integration suite validated against.
    # (sqlite backend only — Neo4j has no single file to archive.)
    if backend_name == "sqlite":
        _dbg("archiving sqlite reference artifact...")
        sqlite_output = _UNIT_TEST_DATA / "python_integration.sqlite3"
        _archive_sqlite(_SQLITE_PATH, sqlite_output)
        _dbg("sqlite reference artifact archived")

        # ── Step 3c: Refresh the markdown fixture report ─────
        # Keep the human-readable completeness report in sync with the
        # archived artifacts.
        _dbg("refreshing markdown fixture report...")
        _refresh_fixture_report(
            json_path=json_output,
            db_path=sqlite_output,
            out_path=_UNIT_TEST_DATA / "python_fixture_report.md",
            title="doxygen-index (dogfood) as-built fixture report",
        )

    # HTML export was removed from codegraph.  JSON remains the interchange
    # artifact; PlantUML is the supported visualization export.

    # ── Step 4: Export PlantUML (full + collapsed + public-only) ───────
    from codegraph.export.plantuml import export_plantuml, GraphView
    _dbg("export_plantuml full...")
    puml_text = export_plantuml(graph, fields="all")
    _dbg("export_plantuml full done")
    puml_output = _CODEGRAPH_OUTPUT / "doxygen_index_one_hop.puml"
    puml_output.write_text(puml_text, encoding="utf-8")

    # Collapsed: namespace packages only — no file nodes / file edges.
    _dbg("export_plantuml collapsed...")
    puml_collapsed = export_plantuml(
        graph, fields="all", view=GraphView.COLLAPSED,
    )
    _dbg("export_plantuml collapsed done")
    puml_collapsed_output = _CODEGRAPH_OUTPUT / "doxygen_index_one_hop_collapsed.puml"
    puml_collapsed_output.write_text(puml_collapsed, encoding="utf-8")

    # Public API only: hidden private members + no file nodes.
    _dbg("export_plantuml public...")
    puml_public = export_plantuml(
        graph, fields="all",
        view=GraphView.PUBLIC_API,
    )
    _dbg("export_plantuml public done")
    puml_public_output = _CODEGRAPH_OUTPUT / "doxygen_index_one_hop_public.puml"
    puml_public_output.write_text(puml_public, encoding="utf-8")

    # Render all to SVG — each render is validated.  PlantUML exits 0
    # even on syntax errors (it emits an error-page SVG instead of
    # failing), so a broken diagram would otherwise ship silently as
    # a ``<svg>`` file that renders nothing.
    plantuml_bin = shutil.which("plantuml")
    if plantuml_bin:
        from codegraph.export.plantuml import render_plantuml_to_svg
        _dbg("plantuml SVG renders (3x, validated)...")
        probe = subprocess.run(
            [plantuml_bin, "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert probe.returncode == 0

        for puml_name in ("doxygen_index_one_hop.puml",
                          "doxygen_index_one_hop_collapsed.puml",
                          "doxygen_index_one_hop_public.puml"):
            render_plantuml_to_svg(
                _CODEGRAPH_OUTPUT / puml_name,
                plantuml_bin=plantuml_bin,
                env=env, timeout=120,
            )
        _dbg("plantuml SVG renders done")
        svg_ok = (_CODEGRAPH_OUTPUT / "doxygen_index_one_hop.svg").exists()
    else:
        svg_ok = False

    uid_map = _flat_uid_map(serialized)
    print(f"\n  LayerGraph as-built: {len(uid_map)} nodes")
    print(f"  JSON: {json_output} ({json_output.stat().st_size:,} bytes)")
    if backend_name == "sqlite":
        print(f"  SQLite: {sqlite_output} ({sqlite_output.stat().st_size:,} bytes)")
    print(f"  PUML: {puml_output} ({puml_output.stat().st_size:,} bytes)")
    print(f"  SVG:  {'✓' if svg_ok else '✗ (plantuml not found)'}")
    return (serialized, uid_map)
