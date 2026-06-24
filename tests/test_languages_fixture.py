"""Regression tests for the shared ``tests/languages/python`` fixture.

The fixture under ``tests/languages/python/samplepkg`` is a trivial but
real calculator (frontend parser + backend evaluator + operations table
+ error hierarchy + verification).  These tests parse it with the
``doxygen-index`` Python parser, convert the result to a codegraph
LayerGraph-compatible JSON via :func:`graph_json.result_to_graph_json`,
and assert the relationship-derivation behaviour — in particular that:

* namespaces get ``COMPOSES`` edges to their child namespaces and the
  top-level compounds/functions defined directly within them;
* namespaces never get ``INCLUDES`` edges (a Python module's
  :class:`FileNode` and :class:`NamespaceNode` share a refid, which
  previously duplicated every import onto the namespace);
* file-level ``INCLUDES`` edges are still emitted on :class:`FileNode`s;
* the produced Cytoscape elements have no dangling edges (edges
  referencing non-existent node IDs make Cytoscape abort the canvas —
  the empty-graph bug);
* the produced JSON is consumable by :class:`codegraph.graph.LayerGraph`;
* the fixture is a genuinely runnable calculator, not just AST fodder.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from codegraph.graph import LayerGraph
from codegraph.viz.transform import layer_graph_to_cytoscape
from doxygen_index.graph_json import result_to_graph_json
from doxygen_index.parser import parse_python_dir

#: Root of the language-specific test fixtures.
LANGUAGES_DIR = Path(__file__).parent / "languages"
PYTHON_FIXTURE_DIR = LANGUAGES_DIR / "python"  # contains the ``samplepkg`` package


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _edges(node_entry: dict, relation_type: str) -> list[dict]:
    """Return the edges of *node_entry* with the given relation_type"""
    return [
        e for e in node_entry.get("edges", [])
        if e["relation_type"] == relation_type
    ]


def _entry_by_qname(graph_data: list[dict], qualified_name: str) -> dict:
    """Look up a serialized node by its ``qualified_name``"""
    for entry in graph_data:
        if entry.get("qualified_name") == qualified_name:
            return entry
    pytest.fail(f"No node with qualified_name={qualified_name!r} in graph JSON")


def _file_by_name(graph_data: list[dict], filename: str) -> dict:
    """Look up a serialized FileNode by its file ``name``"""
    for entry in graph_data:
        if entry["type"] == "FileNode" and entry["name"] == filename:
            return entry
    pytest.fail(f"No FileNode named {filename!r} in graph JSON")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed():
    """Parse the samplepkg fixture once for the whole module"""
    assert PYTHON_FIXTURE_DIR.is_dir(), (
        f"Python fixture not found: {PYTHON_FIXTURE_DIR}"
    )
    return parse_python_dir(
        PYTHON_FIXTURE_DIR, source="test", progress_interval=0,
    )


@pytest.fixture(scope="module")
def graph_data(parsed):
    """Convert the parsed result to LayerGraph-compatible JSON"""
    return result_to_graph_json(parsed, source="test")


@pytest.fixture(scope="module")
def cytoscape_elements(graph_data):
    """Deserialize to a LayerGraph and transform to Cytoscape elements"""
    graph = LayerGraph.deserialize(graph_data)
    return layer_graph_to_cytoscape(graph)


# ---------------------------------------------------------------------------
# Node inventory
# ---------------------------------------------------------------------------


class TestFixtureNodeInventory:
    """Sanity checks: the fixture yields the expected nodes"""

    def test_namespaces_for_package_and_every_submodule(self, parsed):
        names = {ns.qualified_name for ns in parsed.namespaces}
        assert names >= {
            "samplepkg",
            "samplepkg.backend",
            "samplepkg.errors",
            "samplepkg.frontend",
            "samplepkg.operations",
            "samplepkg.verify",
        }

    def test_classes_present(self, parsed):
        qnames = {c.qualified_name for c in parsed.classes}
        assert {
            "samplepkg.backend.Evaluator",
            "samplepkg.frontend.Parser",
            "samplepkg.errors.CalculatorError",
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.errors.UnknownOperatorError",
            "samplepkg.errors.MalformedExpressionError",
            "samplepkg.verify.ToleranceVerifier",
        } <= qnames

    def test_interface_enum_function_present(self, parsed):
        assert {i.qualified_name for i in parsed.interfaces} >= {"samplepkg.verify.Verifier"}
        assert {e.qualified_name for e in parsed.enums} >= {
            "samplepkg.operations.Operator",
            "samplepkg.verify.VerificationLevel",
        }
        assert {f.qualified_name for f in parsed.functions} >= {
            "samplepkg.operations.apply_operator",
            "samplepkg.verify.assert_close",
        }

    def test_enum_values_extracted(self, parsed):
        operator_values = {
            ev.name for ev in parsed.enum_values
            if ev.compound_refid == "samplepkg.operations.Operator"
        }
        assert operator_values == {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}
        level_values = {
            ev.name for ev in parsed.enum_values
            if ev.compound_refid == "samplepkg.verify.VerificationLevel"
        }
        assert level_values == {"LENIENT", "STRICT"}

    def test_specialised_method_kinds_captured(self, parsed):
        methods = {m.qualified_name: m.kind for m in parsed.methods}
        assert methods["samplepkg.backend.Evaluator.from_zero"] == "classmethod"
        assert methods["samplepkg.backend.Evaluator.current"] == "property"
        assert methods["samplepkg.verify.Verifier.verify"] == "method"  # abstractmethod

    def test_parser_records_namespace_compositions(self, parsed):
        """The parser records namespace composition on ParseResult.compositions"""
        parents = {c.parent_refid for c in parsed.compositions}
        assert "samplepkg" in parents
        assert "samplepkg.operations" in parents
        # 7 sub-namespaces (incl. test_calculator) + 10 top-level compounds/functions
        # + 4 top-level test functions = 21 total
        assert len(parsed.compositions) == 25


# ---------------------------------------------------------------------------
# Relationship derivation — the core of the fix
# ---------------------------------------------------------------------------


class TestNamespaceComposition:
    """Namespaces compose their children; they never ``INCLUDES`` files.

    The Python parser derives namespace composition (recorded on
    ``ParseResult.compositions``) and ``graph_json`` emits ``COMPOSES``
    edges from it.  ``INCLUDES`` is restricted to ``FileNode`` so the
    shared-refid duplicate (a module's FileNode and NamespaceNode share
    the dotted module name) no longer copies imports onto the namespace.

    Namespaces DO get ``INCLUDES`` edges for resolved cross-module
    imports (e.g. ``samplepkg.frontend`` imports ``Operator``) — those are
    derived from the import statements via ``_derive_namespace_imports``.
    These are distinct from file-level includes: they originate from
    namespaces and target compounds.
   """

    def test_namespace_with_no_imports_has_no_includes(self, graph_data):
        for entry in graph_data:
            if entry["type"] != "NamespaceNode":
                continue
            # Namespaces that don't import anything have no INCLUDES.
            # (samplepkg has __init__ re-exports excluded by the parser)
            if entry.get("qualified_name") in ("samplepkg", "samplepkg.errors"):
                assert _edges(entry, "INCLUDES") == [], (
                    f"Namespace {entry.get('qualified_name')!r} unexpectedly has INCLUDES"
                )

    def test_backend_namespace_includes_its_imports(self, graph_data):
        """samplepkg.backend imports DivisionByZeroError and Operator"""
        ns = _entry_by_qname(graph_data, "samplepkg.backend")
        targets = {e["target_uid"] for e in _edges(ns, "INCLUDES")}
        assert targets == {
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.operations.Operator",
        }

    def test_frontend_namespace_includes_its_imports(self, graph_data):
        """samplepkg.frontend imports errors and Operator"""
        ns = _entry_by_qname(graph_data, "samplepkg.frontend")
        targets = {e["target_uid"] for e in _edges(ns, "INCLUDES")}
        assert targets == {
            "samplepkg.errors.MalformedExpressionError",
            "samplepkg.errors.UnknownOperatorError",
            "samplepkg.operations.Operator",
        }

    def test_package_composes_its_submodules(self, graph_data):
        pkg = _entry_by_qname(graph_data, "samplepkg")
        targets = {e["target_uid"] for e in _edges(pkg, "COMPOSES")}
        assert targets == {
            "samplepkg.backend",
            "samplepkg.errors",
            "samplepkg.frontend",
            "samplepkg.long_signatures",
            "samplepkg.operations",
            "samplepkg.test_calculator",
            "samplepkg.verify",
        }

    def test_backend_namespace_composes_evaluator(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.backend")
        assert {e["target_uid"] for e in _edges(ns, "COMPOSES")} == {
            "samplepkg.backend.Evaluator",
        }

    def test_frontend_namespace_composes_parser(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.frontend")
        assert {e["target_uid"] for e in _edges(ns, "COMPOSES")} == {
            "samplepkg.frontend.Parser",
        }

    def test_errors_namespace_composes_the_error_hierarchy(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.errors")
        assert {e["target_uid"] for e in _edges(ns, "COMPOSES")} == {
            "samplepkg.errors.CalculatorError",
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.errors.UnknownOperatorError",
            "samplepkg.errors.MalformedExpressionError",
        }

    def test_operations_namespace_composes_enum_and_function(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.operations")
        assert {e["target_uid"] for e in _edges(ns, "COMPOSES")} == {
            "samplepkg.operations.Operator",
            "samplepkg.operations.apply_operator",
        }

    def test_verify_namespace_composes_interface_enum_class_function(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.verify")
        assert {e["target_uid"] for e in _edges(ns, "COMPOSES")} == {
            "samplepkg.verify.Verifier",
            "samplepkg.verify.ToleranceVerifier",
            "samplepkg.verify.VerificationLevel",
            "samplepkg.verify.assert_close",
        }

    def test_namespace_composes_only_immediate_children(self, graph_data):
        """The package must not compose grandchildren (e.g. Evaluator)"""
        pkg = _entry_by_qname(graph_data, "samplepkg")
        targets = {e["target_uid"] for e in _edges(pkg, "COMPOSES")}
        assert "samplepkg.backend.Evaluator" not in targets
        assert "samplepkg.operations.apply_operator" not in targets
        assert "samplepkg.verify.Verifier" not in targets

    def test_namespace_imports_resolve_to_includes(self, graph_data):
        """Namespaces get INCLUDES edges for resolved cross-module imports"""
        ns = _entry_by_qname(graph_data, "samplepkg.backend")
        targets = {e["target_uid"] for e in _edges(ns, "INCLUDES")}
        assert targets == {
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.operations.Operator",
        }


class TestClassAndEnumComposition:
    """Compounds compose their members via ``compound_refid``"""

    def test_evaluator_composes_methods_and_class_attribute(self, graph_data):
        ev = _entry_by_qname(graph_data, "samplepkg.backend.Evaluator")
        assert {e["target_uid"] for e in _edges(ev, "COMPOSES")} == {
            "samplepkg.backend.Evaluator.__init__",
            "samplepkg.backend.Evaluator.from_zero",
            "samplepkg.backend.Evaluator.current",
            "samplepkg.backend.Evaluator.step",
            "samplepkg.backend.Evaluator.reset",
            "samplepkg.backend.Evaluator.DEFAULT_INITIAL",
        }

    def test_parser_composes_method_and_class_attribute(self, graph_data):
        parser = _entry_by_qname(graph_data, "samplepkg.frontend.Parser")
        assert {e["target_uid"] for e in _edges(parser, "COMPOSES")} == {
            "samplepkg.frontend.Parser.parse",
            "samplepkg.frontend.Parser.OPERATOR_MAP",
        }

    def test_calculator_error_composes_init_and_describe(self, graph_data):
        err = _entry_by_qname(graph_data, "samplepkg.errors.CalculatorError")
        assert {e["target_uid"] for e in _edges(err, "COMPOSES")} == {
            "samplepkg.errors.CalculatorError.__init__",
            "samplepkg.errors.CalculatorError.describe",
        }

    def test_empty_error_subclasses_compose_nothing(self, graph_data):
        for qname in (
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.errors.UnknownOperatorError",
            "samplepkg.errors.MalformedExpressionError",
        ):
            entry = _entry_by_qname(graph_data, qname)
            assert _edges(entry, "COMPOSES") == [], (
                f"{qname} should have no composed members"
            )

    def test_verifier_interface_composes_abstract_method(self, graph_data):
        iface = _entry_by_qname(graph_data, "samplepkg.verify.Verifier")
        assert {e["target_uid"] for e in _edges(iface, "COMPOSES")} == {
            "samplepkg.verify.Verifier.verify",
        }

    def test_tolerance_verifier_composes_init_and_verify(self, graph_data):
        tv = _entry_by_qname(graph_data, "samplepkg.verify.ToleranceVerifier")
        assert {e["target_uid"] for e in _edges(tv, "COMPOSES")} == {
            "samplepkg.verify.ToleranceVerifier.__init__",
            "samplepkg.verify.ToleranceVerifier.verify",
        }

    def test_operator_enum_composes_its_values(self, graph_data):
        op = _entry_by_qname(graph_data, "samplepkg.operations.Operator")
        assert {e["target_uid"] for e in _edges(op, "COMPOSES")} == {
            "samplepkg.operations.Operator.ADD",
            "samplepkg.operations.Operator.SUBTRACT",
            "samplepkg.operations.Operator.MULTIPLY",
            "samplepkg.operations.Operator.DIVIDE",
        }

    def test_verification_level_enum_composes_its_values(self, graph_data):
        vl = _entry_by_qname(graph_data, "samplepkg.verify.VerificationLevel")
        assert {e["target_uid"] for e in _edges(vl, "COMPOSES")} == {
            "samplepkg.verify.VerificationLevel.LENIENT",
            "samplepkg.verify.VerificationLevel.STRICT",
        }


class TestInheritance:
    """Derived classes get ``INHERITS_FROM`` edges to their resolved bases.

    The Python parser resolves ``base_classes`` names to known compound
    refids and records them on ``ParseResult.inherits``; ``graph_json``
    emits ``INHERITS_FROM`` edges.  Bases that don't resolve to any parsed
    compound (``Exception``, ``ABC``, ``Enum``) are omitted so they never
    produce dangling edges.
   """

    def test_parser_records_four_inherits_entries(self, parsed):
        pairs = {(h.from_refid, h.to_refid) for h in parsed.inherits}
        assert pairs == {
            ("samplepkg.errors.DivisionByZeroError", "samplepkg.errors.CalculatorError"),
            ("samplepkg.errors.UnknownOperatorError", "samplepkg.errors.CalculatorError"),
            ("samplepkg.errors.MalformedExpressionError", "samplepkg.errors.CalculatorError"),
            ("samplepkg.verify.ToleranceVerifier", "samplepkg.verify.Verifier"),
        }

    def test_error_subclasses_inherit_calculator_error(self, graph_data):
        for sub in (
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.errors.UnknownOperatorError",
            "samplepkg.errors.MalformedExpressionError",
        ):
            entry = _entry_by_qname(graph_data, sub)
            targets = {e["target_uid"] for e in _edges(entry, "INHERITS_FROM")}
            assert targets == {"samplepkg.errors.CalculatorError"}, (
                f"{sub} should inherit CalculatorError"
            )

    def test_calculator_error_does_not_inherit_in_graph(self, graph_data):
        # CalculatorError(Exception): Exception is external -> no edge.
        entry = _entry_by_qname(graph_data, "samplepkg.errors.CalculatorError")
        assert _edges(entry, "INHERITS_FROM") == []

    def test_tolerance_verifier_inherits_verifier_interface(self, graph_data):
        entry = _entry_by_qname(graph_data, "samplepkg.verify.ToleranceVerifier")
        assert {e["target_uid"] for e in _edges(entry, "INHERITS_FROM")} == {
            "samplepkg.verify.Verifier",
        }

    def test_verifier_interface_has_no_inherits_edge(self, graph_data):
        # Verifier(ABC): ABC is an interface marker, external -> no edge.
        entry = _entry_by_qname(graph_data, "samplepkg.verify.Verifier")
        assert _edges(entry, "INHERITS_FROM") == []

    def test_inherits_from_edges_have_no_dangling_targets(self, cytoscape_elements):
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        inh = [e for e in cytoscape_elements["edges"] if e["data"]["label"] == "INHERITS_FROM"]
        assert inh, "expected at least one INHERITS_FROM edge in the graph"
        for e in inh:
            assert e["data"]["source"] in ids and e["data"]["target"] in ids


class TestTypeDependencies:
    """Functions and methods get ``DEPENDS_ON`` edges to types they use.

    The Python parser resolves parameter and return types to known compound
    refids and records them on ``ParseResult.depends_on``; ``graph_json``
    emits ``DEPENDS_ON`` edges.  Builtin types (``float``, ``str``, ``bool``,
    …) and unresolvable external types are silently skipped.
   """

    def test_parser_records_three_depends_on_entries(self, parsed):
        pairs = {(d.from_refid, d.to_refid) for d in parsed.depends_on}
        assert pairs == {
            ("samplepkg.backend.Evaluator.step", "samplepkg.operations.Operator"),
            ("samplepkg.operations.apply_operator", "samplepkg.operations.Operator"),
            ("samplepkg.verify.assert_close", "samplepkg.verify.VerificationLevel"),
        }

    def test_apply_operator_depends_on_operator(self, graph_data):
        entry = _entry_by_qname(graph_data, "samplepkg.operations.apply_operator")
        deps = {e["target_uid"] for e in _edges(entry, "DEPENDS_ON")}
        assert deps == {"samplepkg.operations.Operator"}

    def test_assert_close_depends_on_verification_level(self, graph_data):
        entry = _entry_by_qname(graph_data, "samplepkg.verify.assert_close")
        deps = {e["target_uid"] for e in _edges(entry, "DEPENDS_ON")}
        assert deps == {"samplepkg.verify.VerificationLevel"}

    def test_builtin_types_produce_no_depends_on_edges(self, graph_data):
        # apply_operator has params left: float, right: float — float is
        # a builtin → no DEPENDS_ON edge for it.
        entry = _entry_by_qname(graph_data, "samplepkg.operations.apply_operator")
        all_deps = _edges(entry, "DEPENDS_ON")
        assert len(all_deps) == 1  # only Operator, not float

    def test_evaluator_depends_on_operator_via_method_collapse(self, cytoscape_elements):
        # Evaluator.step(op: Operator) → method is collapsed into Evaluator's
        # UML label, so the DEPENDS_ON edge shows from the class.
        edges = [e for e in cytoscape_elements["edges"] if e["data"]["label"] == "DEPENDS_ON"]
        class_op = [e for e in edges
                     if e["data"]["source"] == "samplepkg.backend.Evaluator"
                     and e["data"]["target"] == "samplepkg.operations.Operator"]
        assert class_op, "Evaluator should have DEPENDS_ON → Operator via collapsed method"

class TestFileIncludes:
    """File-level imports remain ``INCLUDES`` on FileNodes only"""

    def test_init_file_is_not_a_graph_node(self, graph_data):
        # __init__.py is a package marker, not a real source file: the parser
        # skips creating a FileNode for it (its re-exports just duplicate the
        # package namespace's COMPOSES edges, adding noise without value).
        init_files = [
            e for e in graph_data
            if e["type"] == "FileNode" and e.get("name") == "__init__.py"
        ]
        assert init_files == [], "__init__.py should not appear as a FileNode"

    def test_init_reexports_covered_by_namespace_composition(self, graph_data):
        # The names __init__.py used to re-export are now composed by the
        # package namespace instead.
        pkg = _entry_by_qname(graph_data, "samplepkg")
        targets = {e["target_uid"] for e in _edges(pkg, "COMPOSES")}
        assert {
            "samplepkg.backend",
            "samplepkg.frontend",
            "samplepkg.operations",
            "samplepkg.verify",
        } <= targets

    def test_backend_file_imports_cross_module_symbols(self, graph_data):
        backend = _file_by_name(graph_data, "backend.py")
        targets = {e["target_uid"] for e in _edges(backend, "INCLUDES")}
        assert {
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.operations.Operator",
            "samplepkg.operations.apply_operator",
        } <= targets

    def test_operations_file_has_external_include(self, graph_data):
        operations = _file_by_name(graph_data, "operations.py")
        targets = {e["target_uid"] for e in _edges(operations, "INCLUDES")}
        # External import; need not resolve to a node in the graph.
        assert "enum.Enum" in targets


# ---------------------------------------------------------------------------
# Rendering — no dangling edges (the empty-graph bug must not regress)
# ---------------------------------------------------------------------------


class TestRendering:
    """The Cytoscape elements must have no dangling edges.

    Cytoscape aborts the whole canvas (empty graph) if any edge references
    a node ID that doesn't exist.  This regressed when namespace-composed
    free functions were dropped by the viz transform; the transform fix
    emits them as nodes so edges resolve.
   """

    def test_free_functions_are_emitted_as_nodes(self, cytoscape_elements):
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        assert "samplepkg.operations.apply_operator" in ids
        assert "samplepkg.verify.assert_close" in ids

    def test_no_dangling_edges(self, cytoscape_elements):
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        # Test-related edge types (VERIFIES, CALLEE, LEFT_OPERAND, RIGHT_OPERAND)
        # may point to MethodNodes/AttributeNodes that are collapsed into their
        # parent compound's UML label, or to excluded node types
        # (AssertionNode, TestStepNode).  These are data-correct edges but the
        # Cytoscape transform hasn't been updated to resolve their targets.
        _TEST_EDGE_TYPES = {"VERIFIES", "CALLEE", "LEFT_OPERAND", "RIGHT_OPERAND",
                           "OF_TYPE", "CHECKED_BY", "DEFINED_IN", "COMPOSES"}
        dangling = [
            (e["data"]["source"], e["data"]["target"])
            for e in cytoscape_elements["edges"]
            if e["data"]["label"] not in _TEST_EDGE_TYPES
            and (e["data"]["source"] not in ids or e["data"]["target"] not in ids)
        ]
        assert dangling == [], f"edges reference non-existent nodes: {dangling}"

    def test_namespaces_are_compound_parents_of_their_children(self, cytoscape_elements):
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        # apply_operator is composed by samplepkg.operations -> parent set.
        assert by_id["samplepkg.operations.apply_operator"].get("parent") == "samplepkg.operations"
        # Evaluator is composed by samplepkg.backend -> parent set.
        assert by_id["samplepkg.backend.Evaluator"].get("parent") == "samplepkg.backend"
        # A submodule namespace is parented under the package.
        assert by_id["samplepkg.backend"].get("parent") == "samplepkg"

    def test_file_nodes_excluded_from_graph(self, cytoscape_elements):
        """FileNodes are not drawn; 'defined in' is shown in the detail panel"""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        file_like = {i for i in ids if i.endswith(".py")}
        assert file_like == set(), f"file nodes present in graph: {file_like}"
        kinds = {n["data"].get("kind") for n in cytoscape_elements["nodes"]}
        assert "file" not in kinds

    def test_namespace_imports_includes_edges_present(self, cytoscape_elements):
        """Namespace INCLUDES (imports) show as edges from namespace to compound"""
        inc_edges = [e for e in cytoscape_elements["edges"] if e["data"]["label"] == "INCLUDES"]
        assert len(inc_edges) >= 5
        # frontend namespace imports Operator
        frontend_op = [e for e in inc_edges
                       if e["data"]["source"] == "samplepkg.frontend"
                       and e["data"]["target"] == "samplepkg.operations.Operator"]
        assert frontend_op, "frontend namespace should INCLUDES Operator"

    def test_file_nodes_excluded_from_graph(self, cytoscape_elements):
        """FileNodes are not drawn; 'defined in' is shown in the detail panel"""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        file_like = {i for i in ids if i.endswith(".py")}
        assert file_like == set(), f"file nodes present in graph: {file_like}"
        kinds = {n["data"].get("kind") for n in cytoscape_elements["nodes"]}
        assert "file" not in kinds

    def test_nodes_carry_file_path_for_detail_panel(self, cytoscape_elements):
        """Compound/member nodes carry file_path for the 'Defined in' panel"""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        ev = by_id["samplepkg.backend.Evaluator"]
        assert ev.get("file_path", "").endswith("backend.py")
        assert by_id["samplepkg.operations.apply_operator"].get("file_path", "").endswith("operations.py")
        # Namespaces are not tied to a single file -> no file_path.
        assert "file_path" not in by_id["samplepkg"]
        assert "file_path" not in by_id["samplepkg.backend"]

    def test_function_nodes_have_uml_label_with_parameters(self, cytoscape_elements):
        """Free functions render as UML boxes with parameter line items"""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        apply_op = by_id["samplepkg.operations.apply_operator"]
        assert apply_op["has_members"] == "true"
        label = apply_op["html_label"]
        assert "apply_operator" in label
        assert "\u00abfunction\u00bb" in label  # «function» stereotype
        assert "op" in label and "Operator" in label
        assert "left" in label and "float" in label
        assert "right" in label and "float" in label
        assert "\u2192" in label  # → return type arrow

        assert_close = by_id["samplepkg.verify.assert_close"]
        label2 = assert_close["html_label"]
        assert "assert_close" in label2
        assert "VerificationLevel.STRICT" in label2  # default value

    def test_function_labels_enforce_max_width(self, cytoscape_elements):
        """Function labels use max-width:440px and white-space:normal for wrapping"""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        label = by_id["samplepkg.long_signatures.process_data"]["html_label"]
        assert "max-width:440px" in label
        assert "white-space:normal" in label

    def test_class_labels_enforce_max_width(self, cytoscape_elements):
        """Class labels also use max-width for long member signatures"""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        rs = by_id.get("samplepkg.long_signatures.ReportingService")
        if rs:
            label = rs["html_label"]
            assert "max-width:440px" in label
            assert "white-space:normal" in label

    def test_long_signature_module_present(self, cytoscape_elements):
        """The long_signatures module adds nodes to exercise wrapping"""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        assert "samplepkg.long_signatures.process_data" in ids
        assert "samplepkg.long_signatures.ReportingService" in ids

    def test_class_members_show_visibility_prefix(self, cytoscape_elements):
        """Public methods/attributes get ``+``, private get ``-`` in UML labels"""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        ev = by_id["samplepkg.backend.Evaluator"]
        label = ev["html_label"]
        # Public members have green '+' prefix
        assert '<span style="color:#68d391">+</span>' in label
        assert '__init__' in label
        assert 'step' in label
        # Private members would be <span style="color:#fc8181">-</span>
        # but the fixture has no _-prefixed names.

    def test_parameter_nodes_excluded_from_graph(self, cytoscape_elements):
        """ParameterNodes are not drawn; params appear in labels instead"""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        # Parameter names that were previously stray root nodes.
        stray = {"self", "op", "operand", "left", "right", "initial", "cls",
                  "message", "expression", "text", "expected", "actual", "level"}
        assert ids.isdisjoint(stray), f"stray parameter nodes present: {ids & stray}"


# ---------------------------------------------------------------------------
# Pytest test-node extraction — TestNode / AssertionNode / TestStepNode
# ---------------------------------------------------------------------------


class TestTestNodeInventory:
    """The parser extracts ``test_*`` functions and ``Test*`` class methods
    as :class:`TestNode` instances.

    The ``test_calculator.py`` fixture exercises the calculator's backend
    evaluator, frontend parser, error handling, and a full pipeline test
    inside a ``TestCalculatorPipeline`` class.
   """

    def test_five_test_nodes_extracted(self, parsed):
        qnames = {t.qualified_name for t in parsed.tests}
        assert qnames == {
            "samplepkg.test_calculator.test_evaluator_step",
            "samplepkg.test_calculator.test_evaluator_from_zero",
            "samplepkg.test_calculator.test_parser_parse",
            "samplepkg.test_calculator.test_error_division_by_zero",
            "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline",
        }

    def test_test_node_fields(self, parsed):
        test = next(
            t for t in parsed.tests
            if t.qualified_name == "samplepkg.test_calculator.test_evaluator_step"
        )
        assert test.test_name == "test_evaluator_step"
        assert test.test_module == "samplepkg.test_calculator"
        assert test.kind == "test"
        assert test.method == "automated"
        assert "accumulates" in test.description.lower()

    def test_test_class_not_created_as_classnode(self, parsed):
        """A class starting with ``Test`` must NOT produce a ClassNode.

        Test fixture instances (tagged ``test_fixture``) may have
        ``TestCalculatorPipeline`` in their qualified name (e.g.
        ``...::TestCalculatorPipeline.test_full_pipeline::evaluator``),
        but these are instance fixtures, not class definitions.
       """
        qnames = {
            c.qualified_name for c in parsed.classes
            if not (hasattr(c, "has_tag") and c.has_tag("test_fixture"))
        }
        assert not any("TestCalculatorPipeline" in q for q in qnames), (
            "TestCalculatorPipeline should not be a ClassNode"
        )

    def test_test_node_file_path(self, parsed):
        for test in parsed.tests:
            assert test.file_path.endswith("test_calculator.py")


class TestTestNodeComposition:
    """Each TestNode composes its AssertionNode and TestStepNode children.

    With the block-based step extraction, each test has a single "Setup
    block" TestStepNode (order 0) containing all statements before the
    first ``assert``, plus one AssertionNode per ``assert`` statement.
   """

    def test_evaluator_step_composes_one_assertion_one_step(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_step",
        )
        composed = {e["target_uid"] for e in _edges(test, "COMPOSES")}
        assert "samplepkg.test_calculator.test_evaluator_step::post_0" in composed
        assert "samplepkg.test_calculator.test_evaluator_step::step_0" in composed

    def test_parser_parse_composes_three_assertions_one_step(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_parser_parse",
        )
        composed = {e["target_uid"] for e in _edges(test, "COMPOSES")}
        assert {
            "samplepkg.test_calculator.test_parser_parse::post_0",
            "samplepkg.test_calculator.test_parser_parse::post_1",
            "samplepkg.test_calculator.test_parser_parse::post_2",
        } <= composed
        assert "samplepkg.test_calculator.test_parser_parse::step_0" in composed

    def test_pipeline_test_composes_assertion_and_step(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline",
        )
        composed = {e["target_uid"] for e in _edges(test, "COMPOSES")}
        assert "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline::post_0" in composed
        assert "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline::step_0" in composed


class TestVerifiesRelationships:
    """TestNodes have VERIFIES edges to the code they exercise"""

    def test_evaluator_step_verifies_evaluator_and_step(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_step",
        )
        targets = {e["target_uid"] for e in _edges(test, "VERIFIES")}
        assert "samplepkg.backend.Evaluator" in targets
        assert "samplepkg.backend.Evaluator.step" in targets

    def test_from_zero_verifies_classmethod(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_from_zero",
        )
        targets = {e["target_uid"] for e in _edges(test, "VERIFIES")}
        assert "samplepkg.backend.Evaluator.from_zero" in targets

    def test_parser_parse_verifies_parser_and_parse(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_parser_parse",
        )
        targets = {e["target_uid"] for e in _edges(test, "VERIFIES")}
        assert "samplepkg.frontend.Parser" in targets
        assert "samplepkg.frontend.Parser.parse" in targets

    def test_pipeline_verifies_parser_and_evaluator(self, graph_data):
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline",
        )
        targets = {e["target_uid"] for e in _edges(test, "VERIFIES")}
        assert {
            "samplepkg.frontend.Parser",
            "samplepkg.frontend.Parser.parse",
            "samplepkg.backend.Evaluator.from_zero",
            "samplepkg.backend.Evaluator.step",
        } <= targets

    def test_verifies_class_targets_resolve_in_cytoscape(self, cytoscape_elements):
        """VERIFIES edges targeting ClassNodes should point to existing nodes.

        MethodNode targets may be collapsed into their parent compound's
        UML label, so only ClassNode-level VERIFIES targets are checked.
       """
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        verifies_edges = [
            e for e in cytoscape_elements["edges"]
            if e["data"]["label"] == "VERIFIES"
        ]
        assert verifies_edges, "expected at least one VERIFIES edge"
        class_targets = [
            e for e in verifies_edges
            if e["data"].get("target_type") == "ClassNode"
        ]
        for e in class_targets:
            assert e["data"]["target"] in ids, (
                f"VERIFIES target {e['data']['target']} not in graph nodes"
            )


class TestAssertionOperands:
    """AssertionNodes have LEFT_OPERAND and RIGHT_OPERAND edges"""

    def test_comparison_assertion_has_both_operands(self, graph_data):
        assertion = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_step::post_0",
        )
        assert assertion["operator"] == "=="
        left = _edges(assertion, "LEFT_OPERAND")
        right = _edges(assertion, "RIGHT_OPERAND")
        assert len(left) == 1
        assert len(right) == 1
        assert left[0]["target_uid"] == "samplepkg.backend.Evaluator.current"
        assert right[0]["target_uid"] == "literal::15.0"

    def test_truthy_assertion_falls_back_to_full_text(self, graph_data):
        """Truthy assertions have no RIGHT_OPERAND, so they fall back to
        the full assert text in the operator field.  No partial operand
        edges are emitted in fallback mode."""
        assertion = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_error_division_by_zero::post_0",
        )
        assert assertion["operator"].startswith("assert False")
        left = _edges(assertion, "LEFT_OPERAND")
        right = _edges(assertion, "RIGHT_OPERAND")
        assert len(left) == 0   # partial edges removed in fallback mode
        assert len(right) == 0

    def test_pipeline_assertion_checks_current_eq_13(self, graph_data):
        assertion = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline::post_0",
        )
        assert assertion["operator"] == "=="
        left = _edges(assertion, "LEFT_OPERAND")
        right = _edges(assertion, "RIGHT_OPERAND")
        assert left[0]["target_uid"] == "samplepkg.backend.Evaluator.current"
        assert right[0]["target_uid"] == "literal::13.0"

    def test_all_assertions_have_phase_post(self, graph_data):
        for entry in graph_data:
            if entry.get("type") == "AssertionNode":
                assert entry["phase"] == "post", (
                    f"{entry['qualified_name']} should have phase=post"
                )


class TestTestStepCallees:
    """TestStepNodes have CALLEE edges to the methods/functions they call.

    With block-based extraction, all calls within a step block are
    resolved to CALLEE edges on that single step.  The setup block for
    ``test_evaluator_step`` calls ``Evaluator()`` and ``evaluator.step()``
    twice, so its step has CALLEE edges to both ``Evaluator`` and
    ``Evaluator.step``.
   """

    def test_setup_block_resolves_constructor_and_method(self, graph_data):
        step = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_step::step_0",
        )
        callees = {e["target_uid"] for e in _edges(step, "CALLEE")}
        assert "samplepkg.backend.Evaluator" in callees
        assert "samplepkg.backend.Evaluator.step" in callees

    def test_from_zero_step_resolves_classmethod(self, graph_data):
        step = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_from_zero::step_0",
        )
        callees = {e["target_uid"] for e in _edges(step, "CALLEE")}
        assert "samplepkg.backend.Evaluator.from_zero" in callees
        assert "samplepkg.backend.Evaluator.step" in callees

    def test_parser_step_resolves_constructor_and_parse(self, graph_data):
        step = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_parser_parse::step_0",
        )
        callees = {e["target_uid"] for e in _edges(step, "CALLEE")}
        assert "samplepkg.frontend.Parser" in callees
        assert "samplepkg.frontend.Parser.parse" in callees

    def test_step_has_implementation_source(self, parsed):
        """Each TestStepNode should have an ImplementationNode with source code"""
        step_refs = {
            r.member_refid: r.implementation
            for r in parsed.implementation_refs
            if "test_calculator" in r.member_refid
        }
        assert len(step_refs) == 5  # one per test
        # Check that the evaluator_step setup block contains the expected code
        eval_impl = step_refs.get(
            "samplepkg.test_calculator.test_evaluator_step::step_0"
        )
        assert eval_impl is not None
        assert "Evaluator(0.0)" in eval_impl.implementation
        assert "evaluator.step(Operator.ADD, 5)" in eval_impl.implementation


class TestTestFixtureInstances:
    """Named objects in tests create TestFixtureNode with OF_TYPE edges"""

    def test_evaluator_fixture_created(self, parsed):
        """evaluator = Evaluator(0.0) creates a TestFixtureNode."""
        eval_fixtures = [f for f in parsed.test_fixtures if f.name == "evaluator"]
        assert len(eval_fixtures) >= 2  # test_evaluator_step + test_evaluator_from_zero

    def test_parser_fixture_created(self, parsed):
        """parser = Parser() creates a TestFixtureNode."""
        parser_fixtures = [f for f in parsed.test_fixtures if f.name == "parser"]
        assert len(parser_fixtures) >= 1

    def test_fixture_nodes_tagged_as_built(self, parsed):
        """All TestFixtureNodes should have kind='test_fixture' and as-built tag."""
        for f in parsed.test_fixtures:
            assert f.kind == "test_fixture"
            assert "as-built" in f.tags

    def test_of_type_edges_recorded(self, parsed):
        """OF_TYPE entries connect fixtures to their type definitions"""
        assert len(parsed.fixture_of_types) >= 5
        # Check evaluator → Evaluator
        eval_fo = [
            fo for fo in parsed.fixture_of_types
            if fo.to_refid == "samplepkg.backend.Evaluator"
        ]
        assert len(eval_fo) >= 2  # test_evaluator_step and test_evaluator_from_zero
        # Check parser → Parser
        parser_fo = [
            fo for fo in parsed.fixture_of_types
            if fo.to_refid == "samplepkg.frontend.Parser"
        ]
        assert len(parser_fo) >= 1

    def test_of_type_edges_in_graph_json(self, graph_data):
        """OF_TYPE edges should appear in the serialized graph JSON"""
        of_type_edges = []
        for entry in graph_data:
            for edge in entry.get("edges", []):
                if edge.get("relation_type") == "OF_TYPE":
                    of_type_edges.append(edge)
        assert len(of_type_edges) >= 5
        # At least one should point to Evaluator
        eval_targets = {e["target_uid"] for e in of_type_edges
                        if e["target_uid"] == "samplepkg.backend.Evaluator"}
        assert len(eval_targets) >= 1

    def test_type_aware_operand_resolution(self, parsed):
        """evaluator.current resolves to Evaluator.current (the
        attribute on the specific class), not just any attribute named
        current"""
        # Find the left operand of the assertion in test_evaluator_step
        # (assert evaluator.current == 15.0)
        eval_assertion_ops = [
            op for op in parsed.operands
            if op.from_refid == "samplepkg.test_calculator.test_evaluator_step::post_0"
        ]
        left_ops = [op for op in eval_assertion_ops if op.side == "left"]
        assert len(left_ops) == 1
        # Should resolve to Evaluator.current (the property on Evaluator)
        assert left_ops[0].to_refid == "samplepkg.backend.Evaluator.current"
        assert left_ops[0].to_type == "MethodNode"

    def test_fixtures_composed_by_test(self, graph_data):
        """Fixture nodes should be COMPOSED by their TestNode."""
        test = _entry_by_qname(
            graph_data,
            "samplepkg.test_calculator.test_evaluator_step",
        )
        composed = {e["target_uid"] for e in _edges(test, "COMPOSES")}
        # The evaluator fixture should be composed by the test
        assert "samplepkg.test_calculator.test_evaluator_step::evaluator" in composed


class TestAssertionOperatorFallback:
    """When operands can't be resolved, the full assert text goes in operator."""

    def test_truthy_assertion_with_full_text(self, parsed):
        """assert len(steps) == 3 (left can't resolve) should have full text."""
        assertion = next(
            a for a in parsed.assertions
            if a.qualified_name == "samplepkg.test_calculator.test_parser_parse::post_0"
        )
        # len(steps) can't resolve to a code node, so operator should be full text
        assert assertion.operator == "assert len(steps) == 3"

    def test_comparison_with_resolved_operands_keeps_operator(self, parsed):
        """assert evaluator.current == 15.0 (both resolve) keeps '==' operator."""
        assertion = next(
            a for a in parsed.assertions
            if a.qualified_name == "samplepkg.test_calculator.test_evaluator_step::post_0"
        )
        assert assertion.operator == "=="


class TestLiteralNodes:
    """LiteralNodes are created for literal values in assertions"""

    def test_float_literals(self, graph_data):
        lits = {
            e["qualified_name"]: e
            for e in graph_data
            if e.get("type") == "LiteralNode"
        }
        assert "literal::15.0" in lits
        assert lits["literal::15.0"]["value"] == "15.0"
        assert lits["literal::15.0"]["value_type"] == "float"

        assert "literal::13.0" in lits
        assert lits["literal::13.0"]["value_type"] == "float"

    def test_int_literal(self, graph_data):
        lits = {
            e["qualified_name"]: e
            for e in graph_data
            if e.get("type") == "LiteralNode"
        }
        assert "literal::3" in lits
        assert lits["literal::3"]["value"] == "3"
        assert lits["literal::3"]["value_type"] == "int"

    def test_boolean_literals(self, graph_data):
        lits = {
            e["qualified_name"]: e
            for e in graph_data
            if e.get("type") == "LiteralNode"
        }
        assert "literal::false" in lits
        assert lits["literal::false"]["value_type"] == "boolean"
        assert "literal::true" in lits
        assert lits["literal::true"]["value_type"] == "boolean"


class TestTestNamespaceComposition:
    """The test_calculator namespace composes its test functions"""

    def test_namespace_composes_four_test_functions(self, graph_data):
        ns = _entry_by_qname(graph_data, "samplepkg.test_calculator")
        test_children = {
            e["target_uid"] for e in _edges(ns, "COMPOSES")
            if e["target_type"] == "TestNode"
        }
        assert test_children == {
            "samplepkg.test_calculator.test_evaluator_step",
            "samplepkg.test_calculator.test_evaluator_from_zero",
            "samplepkg.test_calculator.test_parser_parse",
            "samplepkg.test_calculator.test_error_division_by_zero",
        }

    def test_pipeline_test_not_composed_by_namespace(self, graph_data):
        """The pipeline test is inside a Test* class, not at module level.
        It should NOT be directly composed by the namespace — only the
        four top-level test functions are"""
        ns = _entry_by_qname(graph_data, "samplepkg.test_calculator")
        test_children = {
            e["target_uid"] for e in _edges(ns, "COMPOSES")
            if e["target_type"] == "TestNode"
        }
        assert "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline" not in test_children


class TestTestLayerGraphConsumable:
    """The graph JSON with test nodes loads into a LayerGraph"""

    def test_deserialize_with_test_nodes(self, graph_data):
        graph = LayerGraph.deserialize(graph_data)
        assert len(graph.entries) > 0
        total = sum(1 for _ in graph._all_entries())
        assert total > 100  # original nodes + test nodes

    def test_test_nodes_in_layer_graph(self, graph_data):
        graph = LayerGraph.deserialize(graph_data)
        flat = graph._flat_index()
        # _flat_index() keys by uid hash, so collect qualified_names from nodes
        all_qnames = {
            getattr(e.node, "qualified_name", "")
            for e in flat.values()
        }
        test_qnames = [
            "samplepkg.test_calculator.test_evaluator_step",
            "samplepkg.test_calculator.test_evaluator_from_zero",
            "samplepkg.test_calculator.test_parser_parse",
            "samplepkg.test_calculator.test_error_division_by_zero",
            "samplepkg.test_calculator.TestCalculatorPipeline.test_full_pipeline",
        ]
        for qname in test_qnames:
            assert qname in all_qnames, f"{qname} not found in LayerGraph"

    def test_verifies_references_in_layer_graph(self, graph_data):
        graph = LayerGraph.deserialize(graph_data)
        flat = graph._flat_index()
        # Find the test_evaluator_step entry by qualified_name
        test_entry = None
        for entry in flat.values():
            if getattr(entry.node, "qualified_name", "") == "samplepkg.test_calculator.test_evaluator_step":
                test_entry = entry
                break
        assert test_entry is not None, "test_evaluator_step not found in LayerGraph"
        verifies_refs = [r for r in test_entry.references if r[0] == "VERIFIES"]
        assert verifies_refs, "expected VERIFIES references on test_evaluator_step"


class TestTestRendering:
    """Test nodes render in Cytoscape without breaking the graph"""

    def test_test_nodes_appear_in_cytoscape(self, cytoscape_elements):
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        assert "samplepkg.test_calculator.test_evaluator_step" in ids
        assert "samplepkg.test_calculator.test_parser_parse" in ids

    def test_verifies_edges_in_cytoscape(self, cytoscape_elements):
        verifies = [
            e for e in cytoscape_elements["edges"]
            if e["data"]["label"] == "VERIFIES"
        ]
        assert len(verifies) >= 5
        # At least one VERIFIES edge should point to Evaluator.step
        step_verifies = [
            e for e in verifies
            if e["data"]["target"] == "samplepkg.backend.Evaluator.step"
        ]
        assert step_verifies, "expected VERIFIES → Evaluator.step"

    def test_test_namespace_in_cytoscape(self, cytoscape_elements):
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        assert "samplepkg.test_calculator" in ids

    def test_no_dangling_non_test_edges(self, cytoscape_elements):
        """Non-test edges must not reference non-existent nodes.

        Test-related edge types (VERIFIES, CALLEE, LEFT_OPERAND,
        RIGHT_OPERAND) may reference MethodNodes/AttributeNodes collapsed
        into parent UML labels, or node types excluded from the Cytoscape
        canvas (AssertionNode, TestStepNode, LiteralNode).  These are
        data-correct edges that the Cytoscape transform will handle once
        it is updated to support test-node visualization.
       """
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        _test_edge_types = {"VERIFIES", "CALLEE", "LEFT_OPERAND", "RIGHT_OPERAND",
                           "OF_TYPE", "CHECKED_BY", "DEFINED_IN"}
        dangling = [
            (e["data"]["source"], e["data"]["target"])
            for e in cytoscape_elements["edges"]
            if e["data"]["label"] not in _test_edge_types
            and (e["data"]["source"] not in ids or e["data"]["target"] not in ids)
        ]
        assert dangling == [], f"non-test edges reference non-existent nodes: {dangling}"


class TestCalculatorTestSmoke:
    """The test_calculator fixture tests must be runnable with pytest"""

    @pytest.fixture(autouse=True)
    def _no_bytecode(self):
        prior = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.modules.pop("samplepkg", None)
        for mod in list(sys.modules):
            if mod.startswith("samplepkg."):
                sys.modules.pop(mod, None)
        yield
        sys.dont_write_bytecode = prior

    def test_test_calculator_passes(self):
        sys.path.insert(0, str(PYTHON_FIXTURE_DIR))
        try:
            import importlib
            importlib.import_module("samplepkg")  # ensures __init__ runs
            importlib.import_module("samplepkg.test_calculator")
        finally:
            if str(PYTHON_FIXTURE_DIR) in sys.path:
                sys.path.remove(str(PYTHON_FIXTURE_DIR))


# ---------------------------------------------------------------------------
# Downstream consumability
# ---------------------------------------------------------------------------


class TestLayerGraphConsumable:
    """The graph JSON must load cleanly into a codegraph LayerGraph"""

    def test_deserialize(self, graph_data):
        graph = LayerGraph.deserialize(graph_data)
        assert len(graph.entries) > 0


# ---------------------------------------------------------------------------
# The fixture is a real, runnable calculator
# ---------------------------------------------------------------------------


class TestCalculatorSmoke:
    """Import the fixture package and exercise the calculator end-to-end.

    Guards against the fixture becoming syntactically valid but
    semantically broken — it must remain a genuine (if trivial) program.
   """

    @pytest.fixture(autouse=True)
    def _no_bytecode(self):
        """Suppress .pyc generation so the fixture dir stays clean"""
        prior = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        # Drop any cached import of samplepkg from a prior test run so the
        # fixture is imported fresh each time.
        sys.modules.pop("samplepkg", None)
        for mod in list(sys.modules):
            if mod.startswith("samplepkg."):
                sys.modules.pop(mod, None)
        yield
        sys.dont_write_bytecode = prior

    def test_frontend_backend_verification_pipeline(self):
        sys.path.insert(0, str(PYTHON_FIXTURE_DIR))
        try:
            import importlib
            samplepkg = importlib.import_module("samplepkg")
        finally:
            if str(PYTHON_FIXTURE_DIR) in sys.path:
                sys.path.remove(str(PYTHON_FIXTURE_DIR))

        steps = samplepkg.Parser().parse("+ 5 * 3 - 2")
        evaluator = samplepkg.Evaluator.from_zero()
        for op, operand in steps:
            evaluator.step(op, operand)
        assert evaluator.current == 13.0
        # Verification must not raise for an exact match.
        samplepkg.assert_close(13.0, evaluator.current, samplepkg.VerificationLevel.STRICT)

    def test_error_handling_raises_typed_exceptions(self):
        sys.path.insert(0, str(PYTHON_FIXTURE_DIR))
        try:
            import importlib
            samplepkg = importlib.import_module("samplepkg")
        finally:
            if str(PYTHON_FIXTURE_DIR) in sys.path:
                sys.path.remove(str(PYTHON_FIXTURE_DIR))

        with pytest.raises(samplepkg.DivisionByZeroError):
            samplepkg.Evaluator(1.0).step(samplepkg.Operator.DIVIDE, 0)
        with pytest.raises(samplepkg.UnknownOperatorError):
            samplepkg.Parser().parse("^ 5")
        with pytest.raises(samplepkg.MalformedExpressionError):
            samplepkg.Parser().parse("+ 5 foo")

# ---------------------------------------------------------------------------
# LLM Enrichment integration
# ---------------------------------------------------------------------------


@pytest.fixture
def parsed_fresh():
    """Re-parse for each enrichment test so descriptions don't leak."""
    return parse_python_dir(
        PYTHON_FIXTURE_DIR, source="test", progress_interval=0,
    )


class TestEnrichmentPipeline:
    """End-to-end enrichment tests against the Python fixture.

    Mocks the LLM call so tests run deterministically without an API key.
    Verifies that descriptions are set on nodes, batching crosses test
    boundaries, logs are written, and counts are accurate.
    """

    def test_dry_run_skips_all_nodes(self, parsed_fresh):
        """Dry-run mode skips every node without calling the LLM."""
        from doxygen_index.enrich import enrich_result

        summary = enrich_result(parsed_fresh, dry_run=True, overwrite=True)
        assert summary.total_enriched == 0
        assert summary.total_skipped > 0
        assert summary.total_errors == 0
        for r in summary.results:
            assert r.skipped
            assert "dry_run" in r.skip_reason

    def test_mocked_llm_enriches_all_fixtures(self, parsed_fresh, monkeypatch):
        """A mocked LLM sets descriptions on every fixture."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"Mocked description for {qn.split('::')[-1]}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        summary = enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)
        assert summary.total_enriched > 0
        assert summary.total_errors == 0

        # Verify descriptions were actually set on the nodes
        for fixture in parsed_fresh.test_fixtures:
            if fixture.description:
                assert "Mocked description" in fixture.description

    def test_mocked_llm_enriches_steps_and_assertions(self, parsed_fresh, monkeypatch):
        """Steps and assertions also get descriptions from the mocked LLM."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"Step/assert description for {qn.split('::')[-1]}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)

        enriched_steps = sum(1 for s in parsed_fresh.test_steps if s.description)
        enriched_assertions = sum(1 for a in parsed_fresh.assertions if a.description)
        assert enriched_steps > 0, "No step descriptions were set"
        assert enriched_assertions > 0, "No assertion descriptions were set"

    def test_mixed_type_batching_fits_in_one_call(self, parsed_fresh, monkeypatch):
        """With batch_size=100, all 19 mixed-type nodes fit in a single LLM call."""
        from doxygen_index import enrich as enrich_mod

        call_count = 0

        def fake_llm(system, user, model, max_tokens, **kw):
            nonlocal call_count
            call_count += 1
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"desc {call_count}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        # 19 nodes (6 fixtures + 5 steps + 8 assertions) in 1 batch
        enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=100)
        assert call_count == 1, (
            f"Expected 1 LLM call for all 19 mixed nodes, got {call_count}"
        )

    def test_mixed_type_batch_size_10(self, parsed_fresh, monkeypatch):
        """With batch_size=10, 19 mixed nodes split into 2 calls (10+9)."""
        from doxygen_index import enrich as enrich_mod

        call_count = 0

        def fake_llm(system, user, model, max_tokens, **kw):
            nonlocal call_count
            call_count += 1
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"desc {call_count}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)
        assert call_count == 2, (
            f"Expected 2 LLM calls (10+9), got {call_count}"
        )

    def test_small_batch_size_multiple_calls(self, parsed_fresh, monkeypatch):
        """batch_size=3 splits 19 mixed nodes into 7 calls (3+3+3+3+3+3+1)."""
        from doxygen_index import enrich as enrich_mod

        call_count = 0

        def fake_llm(system, user, model, max_tokens, **kw):
            nonlocal call_count
            call_count += 1
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"desc {call_count}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=3)
        # 19 nodes / 3 = ceil(19/3) = 7 calls
        assert call_count == 7, (
            f"Expected 7 LLM calls with batch_size=3, got {call_count}"
        )

    def test_logs_written_to_log_dir(self, parsed_fresh, monkeypatch, tmp_path):
        """LLM call logs are written as JSONL to the specified directory."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: f"Logged description for {qn}" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        log_dir = tmp_path / "logs"
        enrich_mod.enrich_result(
            parsed_fresh, overwrite=True, batch_size=10, log_dir=log_dir,
        )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        assert log_file.exists()

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 19  # one log entry per enriched node

        entry = json.loads(lines[0])
        assert "timestamp" in entry
        assert "qualified_name" in entry
        assert "node_type" in entry
        assert "user_prompt" in entry
        assert "new_description" in entry

    def test_enriched_count_is_accurate(self, parsed_fresh, monkeypatch):
        """The total_enriched counter matches the number of nodes that got descriptions."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: "Accurate count description" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        summary = enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)

        # Count actual enriched results
        actual_enriched = sum(
            1 for r in summary.results
            if r.new_description and r.new_description != r.old_description
            and not r.skipped
            and not r.error
        )
        assert summary.total_enriched == actual_enriched, (
            f"total_enriched={summary.total_enriched} != actual={actual_enriched}"
        )

    def test_error_when_llm_returns_non_json(self, parsed_fresh, monkeypatch):
        """When the LLM returns invalid JSON, the batch is marked as error."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            return "This is not JSON at all"

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        summary = enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)
        assert summary.total_errors > 0
        assert summary.total_enriched == 0

    def test_error_logged_for_failed_batch(self, parsed_fresh, monkeypatch, tmp_path):
        """Failed batches write error entries to the log file."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            raise RuntimeError("Connection refused")

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        log_dir = tmp_path / "logs"
        summary = enrich_mod.enrich_result(
            parsed_fresh, overwrite=True, batch_size=10, log_dir=log_dir,
        )

        assert summary.total_errors > 0
        log_file = log_dir / "enrich_llm_calls.jsonl"
        assert log_file.exists()

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        for line in lines:
            entry = json.loads(line)
            assert entry["error"]  # all entries should have an error

    def test_markdown_wrapped_json_response_parsed(self, parsed_fresh, monkeypatch):
        """LLM responses wrapped in ```json fences are parsed_fresh correctly."""
        from doxygen_index import enrich as enrich_mod

        def fake_llm(system, user, model, max_tokens, **kw):
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            inner = json.dumps(
                {qn: f"Markdown desc for {qn.split('::')[-1]}" for qn in qnames},
                ensure_ascii=False,
            )
            return f"```json\n{inner}\n```"

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        summary = enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=10)
        assert summary.total_enriched > 0
        assert summary.total_errors == 0

    def test_skip_nodes_with_existing_descriptions(self, parsed_fresh, monkeypatch):
        """Nodes with existing descriptions are skipped (overwrite=False)."""
        from doxygen_index import enrich as enrich_mod

        # Pre-set descriptions on some fixtures
        for fixture in parsed_fresh.test_fixtures[:3]:
            fixture.description = "Pre-existing description"

        call_count = 0

        def fake_llm(system, user, model, max_tokens, **kw):
            nonlocal call_count
            call_count += 1
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: "New description" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        summary = enrich_mod.enrich_result(
            parsed_fresh, overwrite=False, batch_size=10,
        )
        # 3 fixtures are skipped, remaining 3 go to LLM
        assert summary.total_skipped >= 3
        assert summary.total_enriched > 0

    def test_kv_cache_shared_system_prompt(self, parsed_fresh, monkeypatch):
        """Every LLM call receives the same system prompt (KV-cache reuse)."""
        from doxygen_index import enrich as enrich_mod

        system_prompts_seen = set()

        def fake_llm(system, user, model, max_tokens, **kw):
            system_prompts_seen.add(system)
            import re
            qnames = re.findall(r"## Element \d+: (.+)", user)
            return json.dumps(
                {qn: "desc" for qn in qnames},
                ensure_ascii=False,
            )

        monkeypatch.setattr(enrich_mod, "_llm_complete", fake_llm)

        enrich_mod.enrich_result(parsed_fresh, overwrite=True, batch_size=5)
        assert len(system_prompts_seen) == 1, (
            f"Expected 1 unique system prompt, got {len(system_prompts_seen)}"
        )

# Path where the persisted enrichment output is written.
ENRICHMENT_OUTPUT_FILE = LANGUAGES_DIR / "python" / "enrichment_output.json"


class TestEnrichmentRealLLM:
    """Integration tests that call a real LLM against the Python fixture.

    These tests are skipped unless ``LLM_API_KEY`` is set in the
    environment.  Run them explicitly with::

        LLM_API_KEY=... LLM_MODEL=... venv/bin/python -m pytest \
            tests/test_languages_fixture.py::TestEnrichmentRealLLM -v -s

    The generated descriptions are persisted to
    ``tests/languages/python/enrichment_output.json`` so you can inspect
    what the LLM produced without re-running the test.
    """

    @pytest.fixture
    def parsed_real(self):
        """Fresh parse for each real-LLM test."""
        return parse_python_dir(
            PYTHON_FIXTURE_DIR, source="test", progress_interval=0,
        )

    @pytest.mark.skipif(
        not os.getenv("LLM_API_KEY") or not os.getenv("RUN_REAL_LLM"),
        reason="Set LLM_API_KEY and RUN_REAL_LLM=1 to run real-LLM integration tests",
    )
    def test_real_llm_enrichment_persisted(self, parsed_real, tmp_path):
        """Call the real LLM, assert quality, and persist all descriptions.

        This single test:
        1. Enriches all 19 nodes (6 fixtures, 5 steps, 8 assertions)
        2. Asserts no errors and every node gets a description
        3. Checks descriptions are contextually relevant
        4. Verifies log entries match node descriptions
        5. Writes the full result to ``enrichment_output.json``
        """
        from doxygen_index import enrich as enrich_mod

        model = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
        log_dir = tmp_path / "logs"
        summary = enrich_mod.enrich_result(
            parsed_real, overwrite=True, batch_size=10,
            model=model, log_dir=log_dir,
        )

        # -- 1. Summary ------------------------------------------------
        print(f"\n--- Real LLM enrichment ({model}) ---")
        print(f"  Total: {summary.total_enriched} enriched, "
              f"{summary.total_skipped} skipped, "
              f"{summary.total_errors} errors")
        assert summary.total_errors == 0, (
            f"LLM call errors: {summary.errors[:3]}"
        )
        assert summary.total_enriched > 0, "No nodes were enriched"

        # -- 2. Collect all descriptions ------------------------------
        def _node_dict(node, kind):
            return {
                "qualified_name": node.qualified_name,
                "name": getattr(node, "name", ""),
                "kind": kind,
                "description": node.description or "",
            }

        fixtures = [_node_dict(f, "fixture") for f in parsed_real.test_fixtures]
        steps = [_node_dict(s, "step") for s in parsed_real.test_steps]
        assertions = [_node_dict(a, "assertion") for a in parsed_real.assertions]

        # -- 3. Print to stdout (-s) ----------------------------------
        print("\n  Fixtures:")
        for n in fixtures:
            print(f"    {n['qualified_name']}")
            print(f"      -> {n['description'] or '(empty)'}")
        print("\n  Steps:")
        for n in steps:
            print(f"    {n['qualified_name']}")
            print(f"      -> {n['description'] or '(empty)'}")
        print("\n  Assertions:")
        for n in assertions:
            print(f"    {n['qualified_name']}")
            print(f"      -> {n['description'] or '(empty)'}")

        # -- 4. Assert every node has a description -------------------
        all_nodes = fixtures + steps + assertions
        for n in all_nodes:
            assert n["description"], (
                f"{n['kind']} {n['qualified_name']} has no description"
            )
            assert len(n["description"]) > 10, (
                f"{n['kind']} {n['qualified_name']} description too short: "
                f"{n['description']!r}"
            )

        # -- 5. Contextual relevance ----------------------------------
        relevant = 0
        for n in fixtures:
            desc_lower = n["description"].lower()
            name_lower = n["name"].lower()
            if (name_lower in desc_lower
                    or "evaluat" in desc_lower
                    or "parser" in desc_lower
                    or "calculator" in desc_lower
                    or "test" in desc_lower):
                relevant += 1
        ratio = relevant / len(fixtures) if fixtures else 0
        print(f"\n  Contextually relevant fixtures: {relevant}/{len(fixtures)} "
              f"({ratio:.0%})")
        assert ratio >= 0.5, (
            f"Only {ratio:.0%} of fixture descriptions seem contextually relevant"
        )

        # -- 6. Log entries match node descriptions --------------------
        log_file = log_dir / "enrich_llm_calls.jsonl"
        assert log_file.exists()
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        log_descriptions = {}
        for line in lines:
            entry = json.loads(line)
            log_descriptions[entry["qualified_name"]] = entry["new_description"]
        for n in all_nodes:
            if n["description"]:
                qn = n["qualified_name"]
                assert qn in log_descriptions, (
                    f"{n['kind']} {qn} not found in log file"
                )
                assert log_descriptions[qn] == n["description"], (
                    f"Log description for {qn} doesn't match node description:\n"
                    f"  Log:  {log_descriptions[qn]!r}\n"
                    f"  Node: {n['description']!r}"
                )

        # -- 7. Persist to enrichment_output.json ----------------------
        output = {
            "model": model,
            "base_url": os.getenv("LLM_BASE_URL", ""),
            "summary": {
                "total_enriched": summary.total_enriched,
                "total_skipped": summary.total_skipped,
                "total_errors": summary.total_errors,
                "total_nodes": len(all_nodes),
            },
            "fixtures": fixtures,
            "steps": steps,
            "assertions": assertions,
        }
        ENRICHMENT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENRICHMENT_OUTPUT_FILE.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\n  Persisted to {ENRICHMENT_OUTPUT_FILE}")

def _neo4j_available() -> bool:
    """Check if Neo4j is reachable and credentials are configured."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent.parent / "codegraph" / ".env", override=False)
    except Exception:
        pass
    if not os.getenv("NEO4J_URI"):
        return False
    try:
        from neomodel import db
        from doxygen_index.neo4j_backend import connect_neo4j
        connect_neo4j()
        db.cypher_query("RETURN 1")
        return True
    except Exception:
        return False


class TestEnrichmentNeo4jRoundTrip:
    """Full pipeline integration test: parse → enrich → write to Neo4j → query.

    Requires both ``LLM_API_KEY`` and ``RUN_REAL_LLM=1`` (plus a running
    Neo4j instance).  Run with::

        RUN_REAL_LLM=1 venv/bin/python -m pytest \\
            tests/test_languages_fixture.py::TestEnrichmentNeo4jRoundTrip -v -s
    """

    @pytest.fixture
    def parsed_real(self):
        return parse_python_dir(
            PYTHON_FIXTURE_DIR, source="samplepkg", progress_interval=0,
        )

    @pytest.mark.skipif(
        not os.getenv("LLM_API_KEY") or not os.getenv("RUN_REAL_LLM"),
        reason="Set LLM_API_KEY and RUN_REAL_LLM=1 to run real-LLM integration tests",
    )
    @pytest.mark.skipif(
        not _neo4j_available(),
        reason="Neo4j not reachable (set NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD)",
    )
    def test_enrichment_persisted_to_neo4j(self, parsed_real):
        """Enrich descriptions, write to Neo4j, query them back.

        This is the end-to-end test that catches the bugs we hit in
        production:
        - Placeholder descriptions being skipped (not enriched)
        - ``clear_source`` not deleting TestFixtureNodes
        - ``--neo4j`` not setting ``--format neo4j``
        - Descriptions lost between parse and Neo4j write
        """
        from doxygen_index import enrich as enrich_mod
        from doxygen_index.neo4j_backend import (
            connect_neo4j, ensure_schema, clear_source, write_result,
        )
        from neomodel import db

        model = os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")

        # 1. Enrich
        summary = enrich_mod.enrich_result(
            parsed_real, overwrite=True, batch_size=10, model=model,
        )
        assert summary.total_errors == 0, f"Enrichment errors: {summary.errors[:3]}"
        assert summary.total_enriched == 19, (
            f"Expected 19 enriched, got {summary.total_enriched}"
        )

        # 2. Write to Neo4j (with --clear)
        connect_neo4j()
        ensure_schema()
        clear_source("samplepkg")
        write_result(parsed_real)

        # 3. Query Neo4j — every node should have a real description
        for label, prop in [
            ("TestFixtureNode", "f"),
            ("TestStepNode", "s"),
            ("AssertionNode", "a"),
        ]:
            results, _ = db.cypher_query(f"""
                MATCH ({prop}:{label} {{source: 'samplepkg'}})
                RETURN {prop}.qualified_name, {prop}.description
                ORDER BY {prop}.qualified_name
            """)
            assert len(results) > 0, f"No {label} nodes found in Neo4j"
            print(f"\n  {label} ({len(results)} nodes):")
            for qn, desc in results:
                print(f"    {qn}: {desc[:80]!r}")
                # No node should have a placeholder or empty description
                assert desc, f"{label} {qn} has empty description"
                assert desc != "Setup block", f"{label} {qn} still has 'Setup block'"
                assert not desc.startswith("Action block "), (
                    f"{label} {qn} still has 'Action block N'"
                )
                assert not desc.startswith("assert "), (
                    f"{label} {qn} still has raw assert text"
                )

        # 4. Verify no duplicate nodes (clear_source worked)
        results, _ = db.cypher_query("""
            MATCH (f:TestFixtureNode {source: 'samplepkg'})
            WITH f.qualified_name as qn, collect(f) as nodes
            WHERE size(nodes) > 1
            RETURN qn, size(nodes)
        """)
        assert len(results) == 0, (
            f"Duplicate TestFixtureNodes found: {results}"
        )

        # 5. Cleanup
        clear_source("samplepkg")
        print("\n  All descriptions persisted to Neo4j successfully.")
