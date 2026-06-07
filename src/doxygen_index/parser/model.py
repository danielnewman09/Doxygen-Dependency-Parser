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
    invokes: list[InvokeEntry] = field(default_factory=list)
    invoked_by: list[InvokeEntry] = field(default_factory=list)
    template_param_refs: list[TemplateParamRef] = field(default_factory=list)
    specializes_refs: list[SpecializesRef] = field(default_factory=list)
    implementations: list[ImplementationNode] = field(default_factory=list)
    implementation_refs: list[ImplementationRef] = field(default_factory=list)

    @property
    def compounds(self) -> list:
        """Aggregate all compound-type nodes for backward compat."""
        return self.classes + self.enums + self.unions + self.interfaces + self.concepts

    @property
    def members(self) -> list:
        """Aggregate all member-type nodes for backward compat."""
        return self.methods + self.attributes + self.enum_values + self.defines + self.functions