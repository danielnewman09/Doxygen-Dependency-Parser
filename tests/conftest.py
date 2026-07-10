"""Pytest fixtures for Neo4j integration tests.

Session lifecycle
-----------------
1. ``test_neo4j_container`` — starts a dedicated ``neo4j-doxygen-index-test``
   Docker container on port 7689 via ``docker compose``, waits for it
   to be healthy, and tears it down after the session.

2. ``setup_neomodel`` — connects to the test container using the
   credentials baked into ``tests/docker-compose.yml`` (port 7689,
   password ``doxygen-index-test``).  Drops stale constraints/indexes,
   installs fresh labels, and wipes the database once before the
   session.

3. ``clear_db`` — wipes the database after every test function so each
   test starts with a clean slate.

Design notes
------------
The single source of truth for credentials is
``tests/docker-compose.yml``, and the conftest mirrors those values
in hardcoded defaults.  This avoids conflicts with VS Code's
``python.envFile`` setting, which automatically loads the project
``.env`` before pytest runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_COMPOSE_FILE = _HERE / "docker-compose.yml"

# Mirror the credentials baked into tests/docker-compose.yml.
# These are the authoritative defaults for the test session.
_TEST_BOLT_PORT = 7689
_TEST_USER = "neo4j"
_TEST_PASSWORD = "doxygen-index-test"


# ---------------------------------------------------------------------------
# Helpers
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

    Always starts the container regardless of ``NEO4J_URI`` because
    VS Code's Python extension may pre-load the project ``.env`` file,
    which sets ``NEO4J_URI`` to the *development* container's port.
    Checking ``NEO4J_URI`` would therefore cause the fixture to skip
    starting the test container, and ``setup_neomodel`` would then try
    to connect to a dead port.

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
def setup_neomodel(test_neo4j_container):
    """Configure neomodel, install labels, and wipe the database once
    before the test session starts.

    Connects to the test container launched by
    :func:`test_neo4j_container` using the credentials baked into
    ``tests/docker-compose.yml``.

    If you need to use an external Neo4j instance instead, set
    ``DOXYGEN_INDEX_TEST_SKIP_CONTAINER=1`` and provide your own
    ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD`` environment
    variables.
    """
    from neomodel import db, get_config

    uri = f"bolt://localhost:{_TEST_BOLT_PORT}"
    user = _TEST_USER
    password = _TEST_PASSWORD

    host = uri.replace("bolt://", "")
    config = get_config()
    config.database_url = f"bolt://{user}:{password}@{host}"

    # Drop ALL existing constraints and indexes so that a schema change
    # doesn't collide with stale constraints from a previous session.
    try:
        results, _ = db.cypher_query(
            "SHOW CONSTRAINTS YIELD name RETURN name"
        )
        for r in results:
            db.cypher_query(f"DROP CONSTRAINT {r[0]} IF EXISTS")
        results, _ = db.cypher_query(
            'SHOW INDEXES YIELD name, type WHERE type <> "LOOKUP" RETURN name'
        )
        for r in results:
            db.cypher_query(f"DROP INDEX {r[0]} IF EXISTS")
    except Exception:
        pass  # best-effort — ignore if Neo4j is empty/fresh

    # Install labels (creates constraints/indexes)
    db.install_all_labels()

    # Wipe the database once before the session
    db.cypher_query("MATCH (n) DETACH DELETE n")


@pytest.fixture(autouse=True)
def clear_db():
    """Clear the Neo4j database after each test.

    Ensures that tests with explicit unique identifiers don't collide
    with data from previous tests.
    """
    yield
    from neomodel import db
    db.cypher_query("MATCH (n) DETACH DELETE n")
