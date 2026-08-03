"""Fixtures for cpp-sqlite full-integration tests.

Session-scoped ``codegraph_graph`` fixture indexes cpp-sqlite into
Neo4j once, then returns ``(serialized, uid_map)``.  All test modules
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
# Backend gate — the full-pipeline ingest runs ``doxygen-index --neo4j``
# (raw Cypher in neo4j_backend).  Under the default sqlite backend these
# tests skip; they run only with CODEGRAPH_BACKEND=neo4j.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _require_neo4j_backend():
    """Skip the whole directory unless the Neo4j backend is selected."""
    if _os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower() != "neo4j":
        pytest.skip(
            "cpp-sqlite full-pipeline tests run the `--neo4j` ingest subprocess "
            "(raw Cypher); requires CODEGRAPH_BACKEND=neo4j"
        )
    yield


# ---------------------------------------------------------------------------
# Override the global clear_db — cpp_sqlite_integration tests are
# read-only; they consume data indexed once by codegraph_graph.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_db(request):
    """No-op: preserve the indexed graph across read-only tests."""
    print(f"\n  [clear_db] cpp_sqlite_integration no-op for {request.node.name}")
    yield


# ---------------------------------------------------------------------------
# Session-scoped fixture — runs ONCE for this directory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def codegraph_graph():
    """Full reindex + backend ingest + LayerGraph retrieval.

    1. Runs ``doxygen-index codegraph --neo4j`` on the cpp-sqlite fixture.
    2. Retrieves the as-built LayerGraph from the backend.
    3. Saves serialized JSON and self-contained HTML.

    Session-scoped — runs once for the entire test session.  The
    serialized JSON is committed so downstream consumers can work
    without the backend.
    """
    # The ingest subprocess runs ``--neo4j`` (raw Cypher in
    # neo4j_backend) — neo4j-backend only.  Skipping here (inside the
    # session fixture) covers the ordering gap where a session-scoped
    # fixture sets up before function-scoped autouse guards run.
    if _os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower() != "neo4j":
        pytest.skip(
            "cpp-sqlite full-pipeline tests run the `--neo4j` ingest subprocess "
            "(raw Cypher); requires CODEGRAPH_BACKEND=neo4j"
        )

    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    if not _conan_deps_available():
        pytest.skip("conan deps not installed")

    _CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Index into Neo4j (--clear handles stale data) ──
    # The subprocess needs credentials in its environment because
    # get_backend() auto-configures from env vars.  This is the only
    # legitimate env-var reference — the subprocess is a separate
    # process that cannot use set_backend().
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
        capture_output=True, text=True,
    )
    print(f"\n  [subprocess] rc={result.returncode}")
    print(f"  [subprocess] stdout tail:\n{result.stdout[-3000:]}")
    if result.returncode != 0:
        print(f"  [subprocess] stderr tail:\n{result.stderr[-3000:]}")
        pytest.fail(f"doxygen-index codegraph failed (rc={result.returncode})")

    # ── Step 2: Configure backend in *this* process ──────
    # The subprocess configured neomodel in ITS process, not ours.
    # set_backend + execute_raw triggers ensure_driver() →
    # db.set_connection() so that LayerGraph.from_neo4j() can use
    # neomodel's db.cypher_query internally.
    from codegraph.backends import set_backend
    from codegraph.backends.neo4j import Neo4jBackend, Neo4jConfig

    set_backend(Neo4jBackend(Neo4jConfig(
        uri=f"bolt://localhost:{_TEST_BOLT_PORT}",
        user=_TEST_USER,
        password=_TEST_PASSWORD,
    )))
    from codegraph import get_backend
    backend = get_backend()
    # Force a fresh driver connection — ensure_driver() would no-op if
    # db.driver was already set by a previous fixture to a different DB.
    import neomodel
    backend.close()
    neomodel.db.driver = None  # close() doesn't null this; ensure_driver() needs it
    backend.execute_raw("RETURN 1")  # triggers ensure_driver() → fresh connection

    # Diagnostic: verify data is reachable
    results, _ = backend.execute_raw(
        "MATCH (n) WHERE 'as-built' IN n.tags RETURN count(n) AS cnt"
    )
    as_built_count = results[0]["cnt"] if results else 0
    print(f"\n  [diag] as-built nodes in Neo4j: {as_built_count}")

    from codegraph.graph import LayerGraph
    graph = LayerGraph.from_neo4j("as-built")
    serialized = graph.serialize(fields="all")
    print(f"  [diag] serialized entries: {len(serialized)}")

    if not serialized:
        pytest.fail("LayerGraph is empty — indexing produced no nodes")

    # ── Step 3: Save serialization ─────────────────────────
    json_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.json"
    json_output.write_text(
        _json.dumps(serialized, indent=2, default=str),
        encoding="utf-8",
    )

    # ── Step 4: Export self-contained HTML ─────────────────
    from codegraph.export.viz import export_html_from_json
    html_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.html"
    export_html_from_json(
        str(json_output), str(html_output), title="cpp-sqlite as-built"
    )

    # ── Step 5: Export PlantUML (full + collapsed + public-only) ───────
    from codegraph.export.plantuml import export_plantuml, GraphView
    puml_text = export_plantuml(graph, fields="all")
    puml_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop.puml"
    puml_output.write_text(puml_text, encoding="utf-8")

    # Collapsed: external deps (std, boost, spdlog, sqlite3) → packages,
    # no file nodes / file edges.
    puml_collapsed = export_plantuml(
        graph, fields="all", view=GraphView.COLLAPSED,
    )
    puml_collapsed_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop_collapsed.puml"
    puml_collapsed_output.write_text(puml_collapsed, encoding="utf-8")

    # Public API only: collapsed deps + hidden private members + no
    # concept nodes + no file nodes.
    puml_public = export_plantuml(
        graph, fields="all",
        view=GraphView.PUBLIC_API,
    )
    puml_public_output = _CODEGRAPH_OUTPUT / "cpp_sqlite_one_hop_public.puml"
    puml_public_output.write_text(puml_public, encoding="utf-8")

    # Render all to SVG
    plantuml_bin = shutil.which("plantuml")
    if plantuml_bin:
        for puml_name in ("cpp_sqlite_one_hop.puml",
                          "cpp_sqlite_one_hop_collapsed.puml",
                          "cpp_sqlite_one_hop_public.puml"):
            subprocess.run(
                [plantuml_bin, "-tsvg", puml_name],
                cwd=str(_CODEGRAPH_OUTPUT),
                env=env, timeout=120,
            )
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
