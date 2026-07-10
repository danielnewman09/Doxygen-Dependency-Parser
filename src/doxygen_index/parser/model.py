"""
Data model — backend-agnostic representation of parsed Doxygen output.

These dataclasses are language-agnostic: they represent the structural
information that Doxygen extracts regardless of which programming language
was originally parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
)
from codegraph.models.test import TestNode, AssertionNode, TestStepNode, TestFixtureNode
from codegraph.models.literal import LiteralNode


# ---------------------------------------------------------------------------
# Relationship / auxiliary entries
# ---------------------------------------------------------------------------


@dataclass
class IncludeEntry:
    file_refid: str
    included_file: str
    included_refid: str
    is_local: bool


@dataclass
class CompositionEntry:
    """A ``COMPOSES`` relationship from a parent namespace to a child.

    Recorded by the language parser (e.g. the Python parser derives it
    from qualified-name containment) and consumed by ``graph_json`` to
    emit ``COMPOSES`` edges.  This keeps namespace composition in the
    parser while leaving ``graph_json`` as a thin consumer.
    """
    parent_refid: str
    child_refid: str
    child_type: str


@dataclass
class InheritsEntry:
    """An ``INHERITS_FROM`` relationship from a derived compound to a base.

    Recorded by the language parser (e.g. the Python parser resolves
    ``base_classes`` names to known compound refids) and consumed by
    ``graph_json`` to emit ``INHERITS_FROM`` edges.  Bases that don't
    resolve to any parsed compound (e.g. ``Exception``, ``ABC``, ``Enum``)
    are simply omitted, so they never produce dangling edges.
    """
    from_refid: str
    to_refid: str
    to_type: str


@dataclass
class DependsOnEntry:
    """A ``DEPENDS_ON`` relationship from a function/method to a type it uses.

    Recorded by the language parser: parameter types and return types are
    resolved to known compound refids (skipping builtins).  Consumed by
    ``graph_json`` to emit ``DEPENDS_ON`` edges.
    """
    from_refid: str
    to_refid: str
    to_type: str


@dataclass
class TemplateParamEntry:
    """A single template parameter extracted from <templateparamlist>."""
    type_constraint: str = ""
    declname: str = ""
    defname: str = ""
    defval: str = ""


@dataclass
class TemplateParamRef:
    """A TEMPLATE_PARAM relationship from a compound to its type parameter.

    This is a relationship-style entry (like IncludeEntry or InvokeEntry)
    that will be written as a TEMPLATE_PARAM edge in the graph.
    The target node (a ClassNode with kind='type_parameter') will be
    created on-the-fly during Neo4j ingestion.

    If the type_constraint matches a known ConceptNode qualified name,
    an ENFORCES_CONCEPT edge will also be created from the type-parameter
    node to that concept.
    """
    from_refid: str
    position: int
    type_constraint: str = ""
    declname: str = ""
    defname: str = ""
    defval: str = ""
    concept_qualified_name: str = ""
    """Qualified name of the Concept that constrains this parameter.
    Empty string if the constraint is just 'typename' (unconstrained) or
    if the constraint text doesn't match any known concept."""


@dataclass
class SpecializesRef:
    """A SPECIALIZES relationship from a specialization to its primary template."""
    from_refid: str
    from_qualified_name: str
    primary_template_qualified_name: str


@dataclass
class InvokeEntry:
    from_refid: str
    to_refid: str
    to_name: str


@dataclass
class ImplementationRef:
    """Association between a member and its extracted implementation source.

    Links the member's refid to the ImplementationNode so that
    write_result() can create the HAS_IMPLEMENTATION relationship
    after persisting both the member and the implementation node.
    """
    member_refid: str
    implementation: ImplementationNode


@dataclass
class VerifiesEntry:
    """A VERIFIES relationship from a TestNode to the code it tests."""
    from_refid: str
    to_refid: str
    to_type: str


@dataclass
class OperandEntry:
    """A LEFT_OPERAND or RIGHT_OPERAND from AssertionNode to a code node."""
    from_refid: str
    to_refid: str
    to_type: str
    side: str  # "left" or "right"


@dataclass
class CalleeEntry:
    """A CALLEE relationship from TestStepNode to a called method/function."""
    from_refid: str
    to_refid: str
    to_type: str


@dataclass
class TestCompositionEntry:
    """COMPOSES from TestNode to its AssertionNode/TestStepNode children."""
    parent_refid: str
    child_refid: str
    child_type: str


@dataclass
class FixtureOfTypeEntry:
    """An OF_TYPE relationship from a TestFixtureNode to its type definition.

    Recorded by the Python parser when it detects a variable assignment
    like ``evaluator = Evaluator(0.0)`` inside a test.  The fixture is a
    :class:`TestFixtureNode`; ``to_refid`` points to the type definition
    node (e.g. the ``Evaluator`` ClassNode).  Consumed by ``graph_json``
    to emit ``OF_TYPE`` edges and by ``neo4j_backend`` to persist them.
    """
    from_refid: str   # TestFixtureNode refid
    to_refid: str     # type definition node refid
    to_type: str      # type node type (e.g. "ClassNode", "EnumNode")


@dataclass
class FixtureCheckedByEntry:
    """A CHECKED_BY relationship from a TestFixtureNode to an AssertionNode.

    Recorded when a fixture variable appears anywhere in an assert
    expression (as a bare name, attribute base, subscript base, or
    call argument).  The direction is fixture → assertion.
    """
    from_refid: str   # TestFixtureNode refid
    to_refid: str     # AssertionNode refid


@dataclass
class FixtureDefinedInEntry:
    """A DEFINED_IN relationship from a TestFixtureNode to a TestStepNode.

    Recorded when a fixture variable is assigned within a step block.
    The direction is fixture → step.
    """
    from_refid: str   # TestFixtureNode refid
    to_refid: str     # TestStepNode refid


# ---------------------------------------------------------------------------
# Aggregate result
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """Complete parsed output from a Doxygen XML directory."""
    files: list[FileNode] = field(default_factory=list)
    namespaces: list[NamespaceNode] = field(default_factory=list)
    classes: list[ClassNode] = field(default_factory=list)
    enums: list[EnumNode] = field(default_factory=list)
    unions: list[UnionNode] = field(default_factory=list)
    interfaces: list[InterfaceNode] = field(default_factory=list)
    concepts: list[ConceptNode] = field(default_factory=list)
    methods: list[MethodNode] = field(default_factory=list)
    attributes: list[AttributeNode] = field(default_factory=list)
    enum_values: list[EnumValueNode] = field(default_factory=list)
    defines: list[DefineNode] = field(default_factory=list)
    functions: list[FunctionNode] = field(default_factory=list)
    parameters: list[ParameterNode] = field(default_factory=list)
    includes: list[IncludeEntry] = field(default_factory=list)
    compositions: list[CompositionEntry] = field(default_factory=list)
    inherits: list[InheritsEntry] = field(default_factory=list)
    depends_on: list[DependsOnEntry] = field(default_factory=list)
    # Resolved namespace-level imports: namespace → imported compound.
    # Derived from ``result.includes`` by resolving relative import names
    # to full qualified refids; emitted as INCLUDES edges on NamespaceNode.
    namespace_includes: list[IncludeEntry] = field(default_factory=list)
    invokes: list[InvokeEntry] = field(default_factory=list)
    invoked_by: list[InvokeEntry] = field(default_factory=list)
    template_param_refs: list[TemplateParamRef] = field(default_factory=list)
    specializes_refs: list[SpecializesRef] = field(default_factory=list)
    implementations: list[ImplementationNode] = field(default_factory=list)
    implementation_refs: list[ImplementationRef] = field(default_factory=list)
    # Test-related nodes and relationships
    tests: list = field(default_factory=list)
    assertions: list = field(default_factory=list)
    test_steps: list = field(default_factory=list)
    test_fixtures: list = field(default_factory=list)
    literals: list = field(default_factory=list)
    verifies: list[VerifiesEntry] = field(default_factory=list)
    operands: list[OperandEntry] = field(default_factory=list)
    callees: list[CalleeEntry] = field(default_factory=list)
    test_compositions: list[TestCompositionEntry] = field(default_factory=list)
    fixture_of_types: list[FixtureOfTypeEntry] = field(default_factory=list)
    fixture_checked_by: list[FixtureCheckedByEntry] = field(default_factory=list)
    fixture_defined_in: list[FixtureDefinedInEntry] = field(default_factory=list)
    pending_calls: list[tuple[str, str, int]] = field(default_factory=list)
    """Raw call data collected during AST walk: ``(caller_refid, callee_text, lineno)``.

    Populated by :func:`~doxygen_index.parser.python.functions.visit_function`
    for every ``ast.Call`` found in a function/method body.  Consumed by
    :func:`~doxygen_index.parser.python.postprocess.derive_invokes` which
    resolves *callee_text* to known function/method refids and emits
    :class:`InvokeEntry` entries.
    """

    @property
    def compounds(self) -> list:
        """Aggregate all compound-type nodes for backward compat."""
        return self.classes + self.enums + self.unions + self.interfaces + self.concepts

    @property
    def members(self) -> list:
        """Aggregate all member-type nodes for backward compat."""
        return self.methods + self.attributes + self.enum_values + self.defines + self.functions