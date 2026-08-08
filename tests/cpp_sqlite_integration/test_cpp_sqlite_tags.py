"""Tag-integrity tests for the cpp-sqlite as-built graph.

Verifies the provenance-tag invariant that downstream consumers rely
on: when the cpp-sqlite source is indexed as the as-built graph, the
``source`` label and the ``as-built`` tag must agree *exactly*.

- Every node whose ``source`` is the project (``cpp-sqlite``) must
  carry the ``as-built`` tag — otherwise it would be invisible to
  ``LayerGraph.from_backend(..., "as-built")`` and to every view
  that selects project code by tag.
- Every node carrying the ``as-built`` tag must have the project
  ``source`` — otherwise dependency code (boost, sqlite3, spdlog,
  cppreference) would leak into the as-built view.

These tests run against the fresh ingest produced by the
session-scoped ``codegraph_graph`` fixture, so they validate exactly
what the build writes to the backend (sqlite by default, Neo4j
opt-in via ``CODEGRAPH_BACKEND``).
"""

from __future__ import annotations

import pytest

#: Source label the ingest assigns to the project's own code.  Derived
#: from the fixture project name (tests/fixtures/cpp-sqlite); must match
#: what ``tag_nodes_by_source`` uses as the project source.
PROJECT_SOURCE = "cpp-sqlite"


class TestAsBuiltTagIntegrity:
    """The ``source`` label and the ``as-built`` tag agree on every node."""

    @pytest.fixture(scope="class")
    def as_built_sets(self, codegraph_graph):
        """Query the backend directly.

        The session ``codegraph_graph`` fixture returns the *tagged*
        as-built LayerGraph — untagged project nodes never reach it —
        so the tag check must go to the repository API.
        """
        from codegraph import get_backend

        backend = get_backend()
        project_nodes = backend.graph.find_all_by_source(PROJECT_SOURCE)
        assert project_nodes, "expected project-source nodes after ingest"

        project_uids = {n._uid_value() for n in project_nodes}
        as_built_uids = set(backend.graph.find_uids_by_tag("as-built"))
        assert as_built_uids, "expected as-built-tagged nodes after ingest"

        return project_uids, as_built_uids

    def test_all_project_source_nodes_are_as_built_tagged(
        self, as_built_sets,
    ):
        """Every ``cpp-sqlite``-source node carries the ``as-built`` tag.

        A project node without the tag would exist in the backend but
        never appear in any as-built LayerGraph or tag-selected view.
        """
        project_uids, as_built_uids = as_built_sets

        missing = project_uids - as_built_uids
        assert not missing, (
            f"{len(missing)} node(s) with source={PROJECT_SOURCE!r} are "
            f"missing the 'as-built' tag (of {len(project_uids)} project "
            f"nodes)"
        )

    def test_all_as_built_tagged_nodes_are_project_source(
        self, as_built_sets,
    ):
        """No node tagged ``as-built`` comes from a dependency source.

        Dependency code (boost, sqlite3, spdlog, cppreference) must be
        tagged ``dependency`` so the as-built view never mixes library
        internals into the project diagram.
        """
        project_uids, as_built_uids = as_built_sets

        foreign = as_built_uids - project_uids
        assert not foreign, (
            f"{len(foreign)} node(s) tagged 'as-built' do not have "
            f"source={PROJECT_SOURCE!r} (of {len(as_built_uids)} tagged "
            f"nodes)"
        )
