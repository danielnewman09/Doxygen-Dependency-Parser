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
    """Return the edges of *node_entry* with the given relation_type."""
    return [
        e for e in node_entry.get("edges", [])
        if e["relation_type"] == relation_type
    ]


def _entry_by_qname(graph_data: list[dict], qualified_name: str) -> dict:
    """Look up a serialized node by its ``qualified_name``."""
    for entry in graph_data:
        if entry.get("qualified_name") == qualified_name:
            return entry
    pytest.fail(f"No node with qualified_name={qualified_name!r} in graph JSON")


def _file_by_name(graph_data: list[dict], filename: str) -> dict:
    """Look up a serialized FileNode by its file ``name``."""
    for entry in graph_data:
        if entry["type"] == "FileNode" and entry["name"] == filename:
            return entry
    pytest.fail(f"No FileNode named {filename!r} in graph JSON")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed():
    """Parse the samplepkg fixture once for the whole module."""
    assert PYTHON_FIXTURE_DIR.is_dir(), (
        f"Python fixture not found: {PYTHON_FIXTURE_DIR}"
    )
    return parse_python_dir(
        PYTHON_FIXTURE_DIR, source="test", progress_interval=0,
    )


@pytest.fixture(scope="module")
def graph_data(parsed):
    """Convert the parsed result to LayerGraph-compatible JSON."""
    return result_to_graph_json(parsed, source="test")


@pytest.fixture(scope="module")
def cytoscape_elements(graph_data):
    """Deserialize to a LayerGraph and transform to Cytoscape elements."""
    graph = LayerGraph.deserialize(graph_data)
    return layer_graph_to_cytoscape(graph)


# ---------------------------------------------------------------------------
# Node inventory
# ---------------------------------------------------------------------------


class TestFixtureNodeInventory:
    """Sanity checks: the fixture yields the expected nodes."""

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
        """The parser records namespace composition on ParseResult.compositions."""
        parents = {c.parent_refid for c in parsed.compositions}
        assert "samplepkg" in parents
        assert "samplepkg.operations" in parents
        # 6 sub-namespaces + 10 top-level compounds/functions = ... total
        assert len(parsed.compositions) == 20


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
        """samplepkg.backend imports DivisionByZeroError and Operator."""
        ns = _entry_by_qname(graph_data, "samplepkg.backend")
        targets = {e["target_uid"] for e in _edges(ns, "INCLUDES")}
        assert targets == {
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.operations.Operator",
        }

    def test_frontend_namespace_includes_its_imports(self, graph_data):
        """samplepkg.frontend imports errors and Operator."""
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
        """The package must not compose grandchildren (e.g. Evaluator)."""
        pkg = _entry_by_qname(graph_data, "samplepkg")
        targets = {e["target_uid"] for e in _edges(pkg, "COMPOSES")}
        assert "samplepkg.backend.Evaluator" not in targets
        assert "samplepkg.operations.apply_operator" not in targets
        assert "samplepkg.verify.Verifier" not in targets

    def test_namespace_imports_resolve_to_includes(self, graph_data):
        """Namespaces get INCLUDES edges for resolved cross-module imports."""
        ns = _entry_by_qname(graph_data, "samplepkg.backend")
        targets = {e["target_uid"] for e in _edges(ns, "INCLUDES")}
        assert targets == {
            "samplepkg.errors.DivisionByZeroError",
            "samplepkg.operations.Operator",
        }


class TestClassAndEnumComposition:
    """Compounds compose their members via ``compound_refid``."""

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
    """File-level imports remain ``INCLUDES`` on FileNodes only."""

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
        dangling = [
            (e["data"]["source"], e["data"]["target"])
            for e in cytoscape_elements["edges"]
            if e["data"]["source"] not in ids or e["data"]["target"] not in ids
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
        """FileNodes are not drawn; 'defined in' is shown in the detail panel."""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        file_like = {i for i in ids if i.endswith(".py")}
        assert file_like == set(), f"file nodes present in graph: {file_like}"
        kinds = {n["data"].get("kind") for n in cytoscape_elements["nodes"]}
        assert "file" not in kinds

    def test_namespace_imports_includes_edges_present(self, cytoscape_elements):
        """Namespace INCLUDES (imports) show as edges from namespace to compound."""
        inc_edges = [e for e in cytoscape_elements["edges"] if e["data"]["label"] == "INCLUDES"]
        assert len(inc_edges) >= 5
        # frontend namespace imports Operator
        frontend_op = [e for e in inc_edges
                       if e["data"]["source"] == "samplepkg.frontend"
                       and e["data"]["target"] == "samplepkg.operations.Operator"]
        assert frontend_op, "frontend namespace should INCLUDES Operator"

    def test_file_nodes_excluded_from_graph(self, cytoscape_elements):
        """FileNodes are not drawn; 'defined in' is shown in the detail panel."""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        file_like = {i for i in ids if i.endswith(".py")}
        assert file_like == set(), f"file nodes present in graph: {file_like}"
        kinds = {n["data"].get("kind") for n in cytoscape_elements["nodes"]}
        assert "file" not in kinds

    def test_nodes_carry_file_path_for_detail_panel(self, cytoscape_elements):
        """Compound/member nodes carry file_path for the 'Defined in' panel."""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        ev = by_id["samplepkg.backend.Evaluator"]
        assert ev.get("file_path", "").endswith("backend.py")
        assert by_id["samplepkg.operations.apply_operator"].get("file_path", "").endswith("operations.py")
        # Namespaces are not tied to a single file -> no file_path.
        assert "file_path" not in by_id["samplepkg"]
        assert "file_path" not in by_id["samplepkg.backend"]

    def test_function_nodes_have_uml_label_with_parameters(self, cytoscape_elements):
        """Free functions render as UML boxes with parameter line items."""
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
        """Function labels use max-width:440px and white-space:normal for wrapping."""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        label = by_id["samplepkg.long_signatures.process_data"]["html_label"]
        assert "max-width:440px" in label
        assert "white-space:normal" in label

    def test_class_labels_enforce_max_width(self, cytoscape_elements):
        """Class labels also use max-width for long member signatures."""
        by_id = {n["data"]["id"]: n["data"] for n in cytoscape_elements["nodes"]}
        rs = by_id.get("samplepkg.long_signatures.ReportingService")
        if rs:
            label = rs["html_label"]
            assert "max-width:440px" in label
            assert "white-space:normal" in label

    def test_long_signature_module_present(self, cytoscape_elements):
        """The long_signatures module adds nodes to exercise wrapping."""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        assert "samplepkg.long_signatures.process_data" in ids
        assert "samplepkg.long_signatures.ReportingService" in ids

    def test_class_members_show_visibility_prefix(self, cytoscape_elements):
        """Public methods/attributes get ``+``, private get ``-`` in UML labels."""
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
        """ParameterNodes are not drawn; params appear in labels instead."""
        ids = {n["data"]["id"] for n in cytoscape_elements["nodes"]}
        # Parameter names that were previously stray root nodes.
        stray = {"self", "op", "operand", "left", "right", "initial", "cls",
                  "message", "expression", "text", "expected", "actual", "level"}
        assert ids.isdisjoint(stray), f"stray parameter nodes present: {ids & stray}"


# ---------------------------------------------------------------------------
# Downstream consumability
# ---------------------------------------------------------------------------


class TestLayerGraphConsumable:
    """The graph JSON must load cleanly into a codegraph LayerGraph."""

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
        """Suppress .pyc generation so the fixture dir stays clean."""
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