"""Pytest fixtures — backend-agnostic (Neo4j or SQLite via CODEGRAPH_BACKEND).

SQLite is the DEFAULT backend: an in-memory :class:`SqliteBackend`, no
Docker, no Neo4j.  Set ``CODEGRAPH_BACKEND=neo4j`` to run the legacy
Neo4j path instead (starts the ``neo4j-doxygen-index-test`` Docker
container on port 7689).

Session lifecycle
-----------------
1. ``setup_backend`` (session, autouse) — selects the backend:

   * ``sqlite`` (default) — ``set_backend(SqliteBackend(...))`` with an
     in-memory database.  No containers, no external services.
   * ``neo4j`` — start the test container (unless
     ``DOXYGEN_INDEX_TEST_SKIP_CONTAINER=1``), connect ``Neo4jBackend``,
     drop stale constraints/indexes, install fresh labels, wipe once.

2. ``clear_db`` (autouse, function) — wipes the active backend after
   every test via ``get_backend().wipe()`` (backend-agnostic).  The
   ``cpp_sqlite_integration`` subdirectory overrides this to a no-op
   (read-only tests sharing session-scoped data).

Suites that genuinely require the Neo4j backend (raw-Cypher) skip
themselves when ``CODEGRAPH_BACKEND != neo4j``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make the backend selection visible to ``pytest.mark.skipif`` markers in
# test modules (they evaluate at collection time): default to sqlite.
os.environ.setdefault("CODEGRAPH_BACKEND", "sqlite")
# Any ``get_backend()`` call at import/collection time (before the session
# fixture runs) must not create a file-backed database in the repo root.
os.environ.setdefault("SQLITE_PATH", ":memory:")
_BACKEND_NAME = os.environ.get("CODEGRAPH_BACKEND", "sqlite").lower()

_HERE = Path(__file__).resolve().parent
_COMPOSE_FILE = _HERE / "docker-compose.yml"

# Mirror the credentials baked into tests/docker-compose.yml.
# These are the authoritative defaults for the Neo4j test session.
_TEST_BOLT_PORT = 7689
_TEST_USER = "neo4j"
_TEST_PASSWORD = "doxygen-index-test"


# ---------------------------------------------------------------------------
# Helpers (Neo4j mode only)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True if the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``docker compose -f <compose_file> ...``."""
    cmd = ["docker", "compose", "-f", str(_COMPOSE_FILE), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _bolt_reachable(uri: str, user: str, password: str, timeout: int = 60) -> bool:
    """Poll until Neo4j at *uri* accepts a real Bolt handshake."""
    from neo4j import GraphDatabase

    deadline = time.monotonic() + timeout
    last_err: Exception | None = None

    while time.monotonic() < deadline:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            return True
        except Exception as exc:
            last_err = exc
            time.sleep(2)

    if last_err:
        print(f"  Bolt connectivity check failed: {last_err}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_neo4j_container():
    """Start a dedicated Neo4j Docker container for the test session.

    Only used when ``CODEGRAPH_BACKEND=neo4j``.  Always starts the
    container regardless of ``NEO4J_URI`` because VS Code's Python
    extension may pre-load the project ``.env`` file, which sets
    ``NEO4J_URI`` to the *development* container's port.

    The container is torn down automatically when the session ends.
    """
    if os.environ.get("DOXYGEN_INDEX_TEST_SKIP_CONTAINER", "").lower() in ("1", "true", "yes"):
        yield
        return

    if not _COMPOSE_FILE.exists():
        pytest.fail(
            f"docker-compose.yml not found at {_COMPOSE_FILE} — "
            "cannot start test Neo4j container."
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon is not reachable.  "
            "Set DOXYGEN_INDEX_TEST_SKIP_CONTAINER=1 and provide your own "
            "Neo4j instance to run integration tests."
        )

    print("\n  Starting test Neo4j container ...")
    try:
        _compose("up", "--detach", "--wait")
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        pytest.fail(f"Failed to start test Neo4j container:\n{exc.stderr}")

    # Double-check with a real Bolt handshake (--wait uses healthcheck
    # which can sometimes report healthy before Bolt is truly ready).
    bolt_uri = f"bolt://localhost:{_TEST_BOLT_PORT}"
    if not _bolt_reachable(bolt_uri, _TEST_USER, _TEST_PASSWORD):
        _compose("down", "--volumes", check=False)
        pytest.fail("Test Neo4j container started but Bolt is not reachable.")

    print("  Test Neo4j container is ready.\n")

    yield

    print("\n  Tearing down test Neo4j container ...")
    _compose("down", "--volumes", check=False)
    print("  Done.\n")


@pytest.fixture(scope="session", autouse=True)
def setup_backend(request):
    """Configure the active codegraph backend for the test session.

    sqlite (default): in-memory SqliteBackend via ``set_backend()`` —
    no Docker, no Neo4j.

    neo4j: connects to the test container launched by
    :func:`test_neo4j_container` using the credentials baked into
    ``tests/docker-compose.yml``, drops stale constraints/indexes,
    installs fresh labels, and wipes the database once before the
    session.
    """
    if _BACKEND_NAME == "neo4j":
        request.getfixturevalue("test_neo4j_container")

        from codegraph.backends import set_backend
        from codegraph.backends.neo4j import Neo4jBackend, Neo4jConfig

        set_backend(Neo4jBackend(Neo4jConfig(
            uri=f"bolt://localhost:{_TEST_BOLT_PORT}",
            user=_TEST_USER,
            password=_TEST_PASSWORD,
        )))

        from codegraph import get_backend
        backend = get_backend()

        # Drop ALL existing constraints and indexes so that a schema change
        # doesn't collide with stale constraints from a previous session.
        try:
            results, _ = backend.execute_raw(
                "SHOW CONSTRAINTS YIELD name RETURN name"
            )
            for r in results:
                backend.execute_raw(f"DROP CONSTRAINT {r[0]} IF EXISTS")
            results, _ = backend.execute_raw(
                'SHOW INDEXES YIELD name, type WHERE type <> "LOOKUP" RETURN name'
            )
            for r in results:
                backend.execute_raw(f"DROP INDEX {r[0]} IF EXISTS")
        except Exception:
            pass  # best-effort — ignore if Neo4j is empty/fresh

        # Install labels (creates constraints/indexes)
        from neomodel import db  # NB: install_all_labels has no codegraph equivalent yet
        db.install_all_labels()

        # Wipe the database once before the session
        backend.wipe()
    else:
        from codegraph.backends import set_backend
        from codegraph.backends.sqlite import SqliteBackend, SqliteConfig

        backend = SqliteBackend(SqliteConfig(path=":memory:"))
        backend.initialize(SqliteConfig(path=":memory:"))
        set_backend(backend)

    yield


@pytest.fixture(scope="session")
def setup_neomodel(setup_backend):
    """Backward-compatible alias for :func:`setup_backend`.

    Historical name kept so any code that requested ``setup_neomodel``
    keeps working; the backend is now backend-agnostic.
    """
    yield None


@pytest.fixture(autouse=True)
def clear_db(request):
    """Clear the active backend after each test.

    Ensures that tests with explicit unique identifiers don't collide
    with data from previous tests.  Backend-agnostic:
    ``get_backend().wipe()``.
    """
    yield
    # Skip wipe for cpp_sqlite_integration subdirectory — those tests
    # are read-only and share the session-scoped codegraph_graph data.
    if "cpp_sqlite_integration" in str(request.node.fspath):
        return
    from codegraph import get_backend
    get_backend().wipe()
