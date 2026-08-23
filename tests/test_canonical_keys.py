"""Regression tests for the canonical-key graph identity contract."""

from __future__ import annotations

import csv

from codegraph import (
    ClassNode,
    FileNode,
    ImplementationNode,
    MethodNode,
    NamespaceNode,
    ParameterNode,
)
from codegraph.identity import CanonicalIdentity
from codegraph.models.test import TestNode, TestStepNode

from doxygen_index.graph_json import result_to_graph_json
from doxygen_index.parser.model import (
    CompositionEntry,
    ImplementationRef,
    ParseResult,
    TestCompositionEntry as CompositionForTest,
)


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
    def test_portable_contract_is_canonical_only(self):
        graph = _graph(classes=[ClassNode(
            refid="class-widget", name="Widget",
            qualified_name="demo::Widget", source="demo",
        )])

        nodes = {node["canonical_key"] for node in graph}
        assert nodes
        for node in graph:
            assert node.get("canonical_key")
            CanonicalIdentity.from_key(node["canonical_key"])
            assert not (set(node) & {
                "uid", "refid", "compound_refid", "member_refid",
                "parent_refid", "child_refid", "from_refid", "to_refid",
            })
            for edge in node.get("edges", []):
                assert edge.get("target_key") or edge.get("target_ref")
                assert "target_uid" not in edge
                assert not (set(edge) & {
                    "uid", "refid", "from_refid", "to_refid",
                })
                if edge.get("target_key"):
                    CanonicalIdentity.from_key(edge["target_key"])

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

    def test_parser_locator_changes_do_not_change_canonical_identity(self):
        def make_result(method_refids, file_refid):
            methods = [
                MethodNode(
                    refid=method_refids[0], name="run",
                    qualified_name="demo::Widget::run",
                    argsstring="(int value)", source="demo",
                ),
                MethodNode(
                    refid=method_refids[1], name="run",
                    qualified_name="demo::Widget::run",
                    argsstring="(const char* value)", source="demo",
                ),
            ]
            return ParseResult(
                files=[FileNode(
                    refid=file_refid, name="widget.hpp",
                    path="include/widget.hpp", source="demo",
                )],
                methods=methods,
                parameters=[
                    ParameterNode(
                        member_refid=method_refids[0], position=0,
                        type="int", source="demo",
                    ),
                    ParameterNode(
                        member_refid=method_refids[1], position=0,
                        type="const char*", source="demo",
                    ),
                ],
            )

        first = result_to_graph_json(
            make_result(("old-int", "old-str"), "old-file"),
            "demo", text_scan=False,
        )
        second = result_to_graph_json(
            make_result(("new-int", "new-str"), "new-file"),
            "demo", text_scan=False,
        )

        def keys(graph, node_type):
            return {
                node["canonical_key"]
                for node in graph
                if (node.get("node_type") or node.get("type")) == node_type
            }

        assert keys(first, "FileNode") == keys(second, "FileNode")
        assert keys(first, "MethodNode") == keys(second, "MethodNode")
        first_params = keys(first, "ParameterNode")
        second_params = keys(second, "ParameterNode")
        assert len(first_params) == len(second_params) == 2
        assert first_params == second_params
        assert len({
            dict(CanonicalIdentity.from_key(key).values)["parent_callable_key"]
            for key in first_params
        }) == 2

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

    def test_implementation_keys_and_edges_are_stable(self):
        methods = [
            MethodNode(
                refid="method-int", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(int value)", source="demo",
            ),
            MethodNode(
                refid="method-str", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(const char* value)", source="demo",
            ),
        ]
        implementations = [
            ImplementationNode(
                name="run", qualified_name="demo::Widget::run",
                implementation="return value + 1;", source="demo",
            ),
            ImplementationNode(
                name="run", qualified_name="demo::Widget::run",
                implementation="return strlen(value);", source="demo",
            ),
        ]
        result = ParseResult(
            methods=methods,
            implementations=implementations,
            implementation_refs=[
                ImplementationRef("method-int", implementations[0]),
                ImplementationRef("method-str", implementations[1]),
            ],
        )

        first_export = result_to_graph_json(result, "demo", text_scan=False)
        first_keys = [node.canonical_key for node in implementations]
        repeated_export = result_to_graph_json(result, "demo", text_scan=False)
        repeated_keys = [node.canonical_key for node in implementations]

        assert len(set(first_keys)) == 2
        assert repeated_keys == first_keys
        method_entries = [n for n in first_export if n["type"] == "MethodNode"]
        implementation_entries = {
            n["canonical_key"] for n in first_export
            if n["type"] == "ImplementationNode"
        }
        targets = {
            edge["target_key"]
            for node in method_entries
            for edge in node.get("edges", [])
            if edge["relation_type"] == "HAS_IMPLEMENTATION"
        }
        assert targets == implementation_entries == set(first_keys)
        assert all("refid" not in node for node in first_export)
        assert all("refid" not in node for node in repeated_export)

    def test_test_step_implementation_uses_resolved_step_key(self):
        test = TestNode(
            refid="test-case", name="test_case",
            qualified_name="demo.test_case", source="demo",
        )
        step = TestStepNode(
            refid="test-case::step_0", name="step_0",
            qualified_name="demo.test_case::step_0", source="demo",
            order=0,
        )
        implementation = ImplementationNode(
            name="step_0", qualified_name=step.qualified_name,
            implementation="value = Widget()", source="demo",
        )
        result = ParseResult(
            tests=[test],
            test_steps=[step],
            implementations=[implementation],
            implementation_refs=[ImplementationRef(step.refid, implementation)],
            test_compositions=[CompositionForTest(
                parent_refid=test.refid,
                child_refid=step.refid,
                child_type="TestStepNode",
            )],
        )

        graph = result_to_graph_json(result, "demo", text_scan=False)
        step_entry = next(n for n in graph if n["type"] == "TestStepNode")
        impl_entry = next(n for n in graph if n["type"] == "ImplementationNode")
        edge = next(
            edge for edge in step_entry["edges"]
            if edge["relation_type"] == "HAS_IMPLEMENTATION"
        )

        assert edge["target_key"] == impl_entry["canonical_key"]
        identity = CanonicalIdentity.from_key(impl_entry["canonical_key"])
        assert dict(identity.values)["parent_callable_key"] == step_entry["canonical_key"]

    def test_csv_uses_object_canonical_keys_for_duplicate_qnames(self, tmp_path):
        from doxygen_index.csv_export import export_csv

        methods = [
            MethodNode(
                refid="method-a", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(int)", source="demo",
            ),
            MethodNode(
                refid="method-b", name="run",
                qualified_name="demo::Widget::run",
                argsstring="(str)", source="demo",
            ),
        ]
        result = ParseResult(methods=methods)
        graph = result_to_graph_json(result, "demo", text_scan=False)
        expected = {n["canonical_key"] for n in graph}

        nodes_path, _ = export_csv(result, "demo", tmp_path)
        with nodes_path.open(newline="", encoding="utf-8") as handle:
            actual = {row["canonical_key:ID"] for row in csv.DictReader(handle)}

        assert actual == expected
        assert len(actual) == 2

    def test_csv_normalization_does_not_scan_nodes_per_entry(
        self, monkeypatch, tmp_path,
    ):
        import doxygen_index.csv_export as csv_export
        import doxygen_index.graph_json as graph_json

        nodes = [
            ClassNode(
                refid=f"class-{index}", name=f"C{index}",
                qualified_name=f"demo::C{index}", source="demo",
            )
            for index in range(128)
        ]
        result = ParseResult(classes=nodes)
        node_list_calls = 0
        original_node_lists = csv_export._node_lists

        def fake_normalize(parse_result, source, **kwargs):
            entries = []
            for index, node in enumerate(parse_result.classes):
                node.canonical_key = f"cg:test:{index}"
                entries.append({
                    "type": "ClassNode",
                    "qualified_name": node.qualified_name,
                    "canonical_key": node.canonical_key,
                })
            return entries

        def counted_node_lists(parse_result):
            nonlocal node_list_calls
            node_list_calls += 1
            return original_node_lists(parse_result)

        monkeypatch.setattr(graph_json, "result_to_graph_json", fake_normalize)
        monkeypatch.setattr(csv_export, "_node_lists", counted_node_lists)
        monkeypatch.setattr(csv_export, "_export_nodes", lambda *args: 128)
        monkeypatch.setattr(csv_export, "_export_relationships", lambda *args: 0)
        monkeypatch.setattr(csv_export, "_write_load_script", lambda *args: None)

        csv_export.export_csv(result, "demo", tmp_path)

        assert node_list_calls == 1
