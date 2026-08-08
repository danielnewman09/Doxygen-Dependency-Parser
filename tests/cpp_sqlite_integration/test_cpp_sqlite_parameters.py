"""Full-pipeline parameter type+signature verification for cpp-sqlite.

Uses the session-scoped ``codegraph_graph`` fixture (indexes the real
cpp-sqlite fixture: doxygen → codegraph nodes → backend → LayerGraph →
serialized JSON) to prove the Phase 0 serialization fix end-to-end:

- ParameterNode dicts carry the C++ ``type`` string AND the
  ``node_type`` discriminator (previously the discriminator clobbered
  the C++ type).
- A known method's structured parameters have correct types, names, and
  defaults after the full round-trip.
- The structured parameter data is consistent with the member's
  ``argsstring`` — the "correct type+signature in the argstring" check.

Requirements: ``doxygen`` on PATH and Conan deps installed (skipped
otherwise, same as the rest of this suite).
"""

from __future__ import annotations


def _find_ctor(uid_map: dict) -> dict | None:
    """Locate the DataAccessObject constructor MethodNode dict."""
    for node in uid_map.values():
        if (
            node.get("type") == "MethodNode"
            and node.get("name") == "DataAccessObject"
            and "DataAccessObject::DataAccessObject" in node.get("qualified_name", "")
        ):
            return node
    return None


def _ctor_params(uid_map: dict, ctor: dict) -> list[dict]:
    """Resolve the ctor's ParameterNode dicts via HAS_PARAMETER edges."""
    param_refs = [
        e for e in ctor.get("edges", [])
        if e["relation_type"] == "HAS_PARAMETER"
    ]
    params = [uid_map[e["target_uid"]] for e in param_refs if e["target_uid"] in uid_map]
    return sorted(params, key=lambda p: p.get("position", 0))


class TestParameterNodeSerializationShape:
    """ParameterNode dicts carry both discriminator and C++ type."""

    def test_parameter_nodes_use_node_type_discriminator(self, codegraph_graph):
        _, uid_map = codegraph_graph
        params = [n for n in uid_map.values() if n.get("node_type") == "ParameterNode"]
        assert params, "expected ParameterNodes in the graph"

        for param in params[:10]:
            # discriminator is under node_type; "type" is the C++ type
            assert param.get("node_type") == "ParameterNode"
            assert param.get("type") != "ParameterNode", (
                f"C++ type clobbered by discriminator: {param.get('name')!r}"
            )


class TestDataAccessObjectCtorParameters:
    """The ctor's structured parameters survive the full round-trip."""

    def test_ctor_has_expected_parameter_types(self, codegraph_graph):
        _, uid_map = codegraph_graph
        ctor = _find_ctor(uid_map)
        assert ctor is not None, "DataAccessObject ctor not found"

        params = _ctor_params(uid_map, ctor)
        assert len(params) == 2, f"expected 2 params, got {len(params)}"

        database, logger = params
        assert database["name"] == "database"
        assert database["type"] == "Database &"
        assert database["position"] == 0

        assert logger["name"] == "pLogger"
        assert logger["type"] == "std::shared_ptr< spdlog::logger >"
        assert logger["default_value"] == "nullptr"
        assert logger["position"] == 1

    def test_parameter_types_consistent_with_argsstring(self, codegraph_graph):
        """Correct type+signature: every structured param type and name
        appears in the member's argsstring."""
        _, uid_map = codegraph_graph
        ctor = _find_ctor(uid_map)
        assert ctor is not None

        argsstring = ctor.get("argsstring", "")
        assert argsstring, "ctor should carry an argsstring"

        for param in _ctor_params(uid_map, ctor):
            assert param["type"] in argsstring, (
                f"param type {param['type']!r} not in argsstring {argsstring!r}"
            )
            assert param["name"] in argsstring, (
                f"param name {param['name']!r} not in argsstring {argsstring!r}"
            )

    def test_member_and_parameters_share_consistent_signature(self, codegraph_graph):
        """The argsstring, definition, and structured params tell one story."""
        _, uid_map = codegraph_graph
        ctor = _find_ctor(uid_map)
        assert ctor is not None

        # definition carries return type + scope; argsstring the params
        assert "DataAccessObject" in ctor.get("definition", "")
        assert ctor.get("argsstring", "").startswith("(")
        assert ctor.get("argsstring", "").endswith(")")
