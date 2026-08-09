"""Fixtures for cpp-sqlite full-integration tests.

Session-scoped ``codegraph_graph`` fixture indexes cpp-sqlite into the
active backend (sqlite by default — no Docker; ``CODEGRAPH_BACKEND=neo4j``
opt-in) once, then returns ``(serialized, uid_map)``.  All test modules
in this directory share the same indexing run.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import os as _os
import json as _json

import pytest

_HERE = Path(__file__).resolve().parent
_PARENT_TESTS = _HERE.parent
_FIXTURE_DIR = _PARENT_TESTS / "fixtures" / "cpp-sqlite"
_CODEGRAPH_OUTPUT = _PARENT_TESTS / "codegraph_output"

#: Gitignored directory for generated test data (serialized JSON, …).
#: Kept out of git — the serialization is large and fully reproducible
#: from the fixture sources.
_UNIT_TEST_DATA = _PARENT_TESTS / "unit_test_data"

# Mirror the credentials from tests/conftest.py (via docker-compose.yml).
_TEST_BOLT_PORT = 7689
_TEST_USER = "neo4j"
_TEST_PASSWORD = "doxygen-index-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doxygen_available() -> bool:
    return shutil.which("doxygen") is not None


def _conan_deps_available() -> bool:
    """Return True if conan deps for cpp-sqlite are installed."""
    try:
        from doxygen_index.conan import discover_packages
        pkgs = discover_packages(project_dir=str(_FIXTURE_DIR), build_type="Debug")
        return "boost" in pkgs and "sqlite3" in pkgs
    except Exception:
        return False


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
        uid_map[node["uid"]] = node
        stack.extend(node.get("composes", []))
    return uid_map


# ---------------------------------------------------------------------------
# Backend selection — the full-pipeline ingest runs ``doxygen-index --neo4j``
# (``--neo4j`` is a compat alias for "write to the active backend"; the
# write path is backend-agnostic since Phase 2 of the decoupling plan).
# sqlite is the default (no Docker); CODEGRAPH_BACKEND=neo4j opt-in.
# ---------------------------------------------------------------------------


def _backend_name() -> str:
    return _os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower()


#: Shared sqlite database file for the ingest subprocess + this process.
#: (The main conftest defaults ``SQLITE_PATH=:memory:``; the integration
#: subprocess is a separate process, so it needs a real file.)
_SQLITE_PATH = _CODEGRAPH_OUTPUT / "cpp_sqlite_integration.sqlite3"


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

    1. Runs ``doxygen-index codegraph --neo4j`` on the cpp-sqlite fixture
       (``--neo4j`` = "write to the active backend"; sqlite by default,
       Neo4j opt-in).
    2. Retrieves the as-built LayerGraph from the backend.
    3. Saves serialized JSON and self-contained HTML.

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

    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    if not _conan_deps_available():
        pytest.skip("conan deps not installed")

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
    # ingest is diagnosable live: tail tests/codegraph_output/ingest.log
    ingest_log = _CODEGRAPH_OUTPUT / "ingest.log"
    with ingest_log.open("w", encoding="utf-8") as _logf:
        result = subprocess.run(
            [
                "doxygen-index", "codegraph",
                "--project-dir", str(_FIXTURE_DIR),
                "--output-dir", str(_CODEGRAPH_OUTPUT),
                "--cppreference",
                "--neo4j",
                "--clear",
                "--yes",
                "--only", "sqlite3,boost,spdlog",
            ],
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
        pytest.fail(f"doxygen-index codegraph failed (rc={result.returncode})")

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
    json_output = _UNIT_TEST_DATA / "cpp_sqlite_one_hop.json"
    json_output.write_text(
        _json.dumps(serialized, indent=2, default=str),
        encoding="utf-8",
    )
    _dbg("json written")

    # ── Step 4: Export self-contained HTML ─────────────────
    from codegraph.export.viz import export_html_from_json
    _dbg("export_html_from_json...")
    html_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.html"
    export_html_from_json(
        str(json_output), str(html_output), title="cpp-sqlite as-built"
    )
    _dbg("html export done")

    # ── Step 5: Export PlantUML (full + collapsed + public-only) ───────
    from codegraph.export.plantuml import export_plantuml, GraphView
    _dbg("export_plantuml full...")
    puml_text = export_plantuml(graph, fields="all")
    _dbg("export_plantuml full done")
    puml_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.puml"
    puml_output.write_text(puml_text, encoding="utf-8")

    # Collapsed: external deps (std, boost, spdlog, sqlite3) → packages,
    # no file nodes / file edges.
    _dbg("export_plantuml collapsed...")
    puml_collapsed = export_plantuml(
        graph, fields="all", view=GraphView.COLLAPSED,
    )
    _dbg("export_plantuml collapsed done")
    puml_collapsed_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop_collapsed.puml"
    puml_collapsed_output.write_text(puml_collapsed, encoding="utf-8")

    # Public API only: collapsed deps + hidden private members + no
    # concept nodes + no file nodes.
    _dbg("export_plantuml public...")
    puml_public = export_plantuml(
        graph, fields="all",
        view=GraphView.PUBLIC_API,
    )
    _dbg("export_plantuml public done")
    puml_public_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop_public.puml"
    puml_public_output.write_text(puml_public, encoding="utf-8")

    # Render all to SVG
    plantuml_bin = shutil.which("plantuml")
    if plantuml_bin:
        _dbg("plantuml SVG renders (3x)...")
        for puml_name in ("cpp_sqlite_one_hop.puml",
                          "cpp_sqlite_one_hop_collapsed.puml",
                          "cpp_sqlite_one_hop_public.puml"):
            subprocess.run(
                [plantuml_bin, "-tsvg", puml_name],
                cwd=str(_CODEGRAPH_OUTPUT),
                env=env, timeout=120,
            )
        _dbg("plantuml SVG renders done")
        svg_ok = (_CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.svg").exists()
    else:
        svg_ok = False

    uid_map = _flat_uid_map(serialized)
    print(f"\n  LayerGraph as-built: {len(uid_map)} nodes")
    print(f"  JSON: {json_output} ({json_output.stat().st_size:,} bytes)")
    print(f"  HTML: {html_output} ({html_output.stat().st_size:,} bytes)")
    print(f"  PUML: {puml_output} ({puml_output.stat().st_size:,} bytes)")
    print(f"  SVG:  {'✓' if svg_ok else '✗ (plantuml not found)'}")
    return (serialized, uid_map)
