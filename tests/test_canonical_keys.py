"""Regression tests for the canonical-key graph identity contract."""

from __future__ import annotations

from codegraph import ClassNode, MethodNode, NamespaceNode

from doxygen_index.graph_json import result_to_graph_json
from doxygen_index.parser.model import CompositionEntry, ParseResult


def _graph(*, source="demo", **lists):
    return result_to_graph_json(
        ParseResult(**lists), source=source, text_scan=False,
    )


def _node(graph, qualified_name, *, source=None):
    matches = [n for n in graph if n.get("qualified_name") == qualified_name]
    if source is not None:
        matches = [n for n in matches if n.get("source") == source]
    assert len(matches) == 1
    return matches[0]


class TestCanonicalKeys:
    def test_nodes_are_exported_with_canonical_keys(self):
        graph = _graph(classes=[ClassNode(
            refid="class-widget", name="Widget",
            qualified_name="demo::Widget", source="demo",
        )])

        node = _node(graph, "demo::Widget")
        assert node["canonical_key"].startswith("cg:v1:repository:")
        assert ":class:qualified_name=" in node["canonical_key"]
        assert "uid" not in node

    def test_canonical_keys_are_deterministic(self):
        def make_result():
            return [ClassNode(
                refid="class-widget", name="Widget",
                qualified_name="demo::Widget", source="demo",
            )]

        first = _graph(classes=make_result())
        second = _graph(classes=make_result())
        assert _node(first, "demo::Widget")["canonical_key"] == _node(
            second, "demo::Widget"
        )["canonical_key"]

    def test_source_scope_distinguishes_same_qualified_name(self):
        graph = _graph(
            source="project",
            namespaces=[
                NamespaceNode(
                    refid="project-std", name="std", qualified_name="std",
                    source="project",
                ),
                NamespaceNode(
                    refid="cppreference-std", name="std", qualified_name="std",
                    source="cppreference", tags=["dependency"],
                ),
            ],
        )

        project = _node(graph, "std", source="project")
        dependency = _node(graph, "std", source="cppreference")
        assert project["canonical_key"] != dependency["canonical_key"]
        assert ":project%2Fproject:namespace:" in project["canonical_key"]
        assert ":project%2Fcppreference:namespace:" in dependency["canonical_key"]

    def test_overloads_have_distinct_canonical_keys(self):
        graph = _graph(methods=[
            MethodNode(
                refid="method-1", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(int value)", source="demo",
            ),
            MethodNode(
                refid="method-2", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(const char* value)", source="demo",
            ),
        ])

        keys = [n["canonical_key"] for n in graph]
        assert len(keys) == 2
        assert len(set(keys)) == 2

    def test_edges_reference_canonical_keys(self):
        namespace = NamespaceNode(
            refid="cppreference-std", name="std", qualified_name="std",
            source="cppreference", tags=["dependency"],
        )
        vector = ClassNode(
            refid="cppreference-vector", name="vector",
            qualified_name="std::vector", source="cppreference",
            tags=["dependency"],
        )
        graph = _graph(
            namespaces=[namespace], classes=[vector],
            compositions=[CompositionEntry(
                parent_refid=namespace.refid,
                child_refid=vector.refid,
                child_type="ClassNode",
            )],
            source="project",
        )

        parent = _node(graph, "std", source="cppreference")
        child = _node(graph, "std::vector", source="cppreference")
        edge = next(e for e in parent["edges"] if e["relation_type"] == "COMPOSES")
        assert edge["target_key"] == child["canonical_key"]
        assert "target_uid" not in edge
