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
# Session-scoped fixture — runs ONCE for this directory
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def codegraph_graph():
    """Full reindex + Neo4j ingest + LayerGraph retrieval.

    1. Runs ``doxygen-index codegraph --neo4j`` on the cpp-sqlite fixture.
    2. Retrieves the as-built LayerGraph from Neo4j.
    3. Saves serialized JSON and self-contained HTML.

    Session-scoped — runs once for the entire test session.  The
    serialized JSON is committed so downstream consumers can work
    without Neo4j.
    """
    if not _doxygen_available():
        pytest.skip("doxygen not found on PATH")
    if not _conan_deps_available():
        pytest.skip("conan deps not installed")

    _CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Index into Neo4j (--clear handles stale data) ──
    env = {**_os.environ,
           "NEO4J_URI": f"bolt://localhost:{_TEST_BOLT_PORT}",
           "NEO4J_USER": _TEST_USER,
           "NEO4J_PASSWORD": _TEST_PASSWORD}

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
    )
    if result.returncode != 0:
        pytest.fail(f"doxygen-index codegraph failed (rc={result.returncode})")

    # ── Step 2: Retrieve LayerGraph from Neo4j ──────────────
    from codegraph.graph import LayerGraph
    graph = LayerGraph.from_neo4j("as-built")
    serialized = graph.serialize(fields="all")

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
