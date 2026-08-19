"""Tag-integrity tests for the doxygen-index dogfood graph.

Verifies the provenance-tag invariant that downstream consumers rely
on for the single-source dogfood graph (this repository's own
``src/doxygen_index``):

- Every node whose ``source`` is the project (``doxygen-index``) must
  carry the ``as-built`` tag — otherwise it would be invisible to
  ``LayerGraph.from_backend(..., "as-built")`` and to every view that
  selects project code by tag.
- Every node carrying the ``as-built`` tag must have the project
  ``source`` — there are no dependency sources in this graph, so the
  two sets must be *equal* (stronger than the cpp-sqlite suite, which
  must allow dependency-tagged one-hop neighbours).

These tests run against the fresh ingest produced by the
session-scoped ``codegraph_graph`` fixture, so they validate exactly
what the build writes to the backend (sqlite by default, Neo4j
opt-in via ``CODEGRAPH_BACKEND``).
"""

from __future__ import annotations

import pytest

#: Source label the ingest assigns to the project's own code.  Derived
#: from the repo-root config name; must match what
#: ``tag_nodes_by_source`` uses as the project source.
PROJECT_SOURCE = "doxygen-index"


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

        project_uids = {n.canonical_key for n in project_nodes}
        as_built_uids = set(backend.graph.find_uids_by_tag("as-built"))
        assert as_built_uids, "expected as-built-tagged nodes after ingest"

        return project_uids, as_built_uids

    def test_all_project_source_nodes_are_as_built_tagged(
        self, as_built_sets,
    ):
        """Every ``doxygen-index``-source node carries the ``as-built`` tag.

        A project node without the tag would exist in the backend but
        never appear in any as-built LayerGraph or tag-selected view.
        """
        project_uids, as_built_uids = as_built_sets
        untagged = project_uids - as_built_uids
        assert not untagged, (
            f"{len(untagged)} project nodes missing the as-built tag"
        )

    def test_all_as_built_tagged_nodes_have_project_source(
        self, as_built_sets,
    ):
        """Every ``as-built``-tagged node is project source.

        Unlike cpp-sqlite (whose one-hop view mixes in dependency-tagged
        neighbours), the dogfood graph is single-source — a non-project
        node carrying ``as-built`` would be a tagging leak.
        """
        project_uids, as_built_uids = as_built_sets
        leaked = as_built_uids - project_uids
        assert not leaked, (
            f"{len(leaked)} as-built-tagged nodes with non-project source"
        )

    def test_single_source_graph_sets_are_equal(self, as_built_sets):
        """The graph is single-source: project uids == as-built uids."""
        project_uids, as_built_uids = as_built_sets
        assert project_uids == as_built_uids, (
            f"project={len(project_uids)} vs as-built={len(as_built_uids)} — "
            "the dogfood graph must contain no untagged and no "
            "non-project nodes"
        )

    def test_serialized_fixture_carries_as_built_tags(self, codegraph_graph):
        """The serialized LayerGraph entries carry the tag too."""
        serialized, uid_map = codegraph_graph
        for node in uid_map.values():
            assert "as-built" in node.get("tags", []), (
                f"entry missing as-built tag: {node.get('qualified_name')}"
            )
            assert "dependency" not in node.get("tags", [])
