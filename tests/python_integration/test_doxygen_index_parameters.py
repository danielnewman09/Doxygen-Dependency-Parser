"""Full-pipeline parameter type+signature verification for the
doxygen-index dogfood graph.

Uses the session-scoped ``codegraph_graph`` fixture (indexes this
repository's own Python source: AST → codegraph nodes → backend →
LayerGraph → serialized JSON) to prove the ParameterNode serialization
shape end-to-end:

- Every ParameterNode dict carries the Python annotation string in
  ``type`` AND the ``node_type`` discriminator (previously the
  discriminator clobbered the type — the Phase 0 fix).
- Known functions' structured parameters have correct types, names,
  defaults, and positions after the full round-trip.
- The structured parameter data is consistent with the member's
  ``argsstring`` — the "correct type+signature in the argstring" check.

Requirements: only the ``doxygen-index`` CLI on PATH (no doxygen, no
Conan — the Python parser uses ``ast``).
"""

from __future__ import annotations


def _find_function(uid_map: dict, qualified_name: str) -> dict | None:
    """Locate a FunctionNode dict by qualified name."""
    for node in uid_map.values():
        if (
            node.get("type") == "FunctionNode"
            and node.get("qualified_name") == qualified_name
        ):
            return node
    return None


def _params(uid_map: dict, fn: dict) -> list[dict]:
    """Resolve a function's ParameterNode dicts via HAS_PARAMETER edges."""
    param_refs = [
        e for e in fn.get("edges", [])
        if e["relation_type"] == "HAS_PARAMETER"
    ]
    params = [uid_map[e["target_uid"]] for e in param_refs if e["target_uid"] in uid_map]
    return sorted(params, key=lambda p: p.get("position", 0))


class TestParameterNodeSerializationShape:
    """ParameterNode dicts carry both discriminator and Python type."""

    def test_parameter_nodes_use_node_type_discriminator(self, codegraph_graph):
        _, uid_map = codegraph_graph
        params = [n for n in uid_map.values() if n.get("node_type") == "ParameterNode"]
        assert params, "expected ParameterNodes in the graph"

        for param in params:
            # discriminator is under node_type; "type" is the Python
            # annotation string
            assert param.get("node_type") == "ParameterNode"
            assert param.get("type") != "ParameterNode", (
                f"annotation clobbered by discriminator: {param.get('name')!r}"
            )

    def test_all_parameters_have_positions(self, codegraph_graph):
        """Position ordering is preserved through the round-trip."""
        _, uid_map = codegraph_graph
        for param in uid_map.values():
            if param.get("node_type") != "ParameterNode":
                continue
            assert isinstance(param.get("position"), int), (
                f"param missing int position: {param.get('name')!r}"
            )


class TestParseXmlDirParameters:
    """The parser entry point's structured parameters survive the
    full round-trip."""

    def test_parse_xml_dir_has_expected_parameter_types(self, codegraph_graph):
        _, uid_map = codegraph_graph
        fn = _find_function(uid_map, "doxygen_index.parser.parse_xml_dir")
        assert fn is not None, "parse_xml_dir not found"

        params = _params(uid_map, fn)
        assert len(params) == 5, f"expected 5 params, got {len(params)}"

        xml_dir, source, progress_interval, layer, language_parser = params
        assert xml_dir["name"] == "xml_dir"
        assert xml_dir["type"] == "Path"
        assert xml_dir["position"] == 0

        assert source["name"] == "source"
        assert source["type"] == "str"
        assert source["default_value"] == "'msd'"
        assert source["position"] == 1

        assert progress_interval["name"] == "progress_interval"
        assert progress_interval["type"] == "int"
        assert progress_interval["default_value"] == "50"
        assert progress_interval["position"] == 2

        assert layer["name"] == "layer"
        assert layer["type"] == "str"
        assert layer["default_value"] == "'dependency'"
        assert layer["position"] == 3

        assert language_parser["name"] == "language_parser"
        assert language_parser["type"] == "LanguageParser | None"
        assert language_parser["default_value"] == "None"
        assert language_parser["position"] == 4

    def test_parameter_types_consistent_with_argsstring(self, codegraph_graph):
        """Correct type+signature: every structured param type and name
        appears in the member's argsstring."""
        _, uid_map = codegraph_graph
        fn = _find_function(uid_map, "doxygen_index.parser.parse_xml_dir")
        assert fn is not None

        argsstring = fn.get("argsstring", "")
        assert argsstring, "parse_xml_dir should carry an argsstring"

        for param in _params(uid_map, fn):
            assert param["type"] in argsstring, (
                f"param type {param['type']!r} not in argsstring {argsstring!r}"
            )
            assert param["name"] in argsstring, (
                f"param name {param['name']!r} not in argsstring {argsstring!r}"
            )


class TestDiscoverPackagesParameters:
    """Type-level dependencies (Path | str, Optional[set[str]]) survive
    the round-trip too."""

    def test_discover_packages_has_expected_parameter_types(self, codegraph_graph):
        _, uid_map = codegraph_graph
        fn = _find_function(uid_map, "doxygen_index.conan.discover_packages")
        assert fn is not None, "discover_packages not found"

        params = _params(uid_map, fn)
        assert len(params) == 3, f"expected 3 params, got {len(params)}"

        project_dir, build_type, only = params
        assert project_dir["name"] == "project_dir"
        assert project_dir["type"] == "Path | str"
        assert project_dir["default_value"] == "'.'"

        assert build_type["name"] == "build_type"
        assert build_type["type"] == "str"
        assert build_type["default_value"] == "'Debug'"

        assert only["name"] == "only"
        assert only["type"] == "Optional[set[str]]"
        assert only["default_value"] == "None"
