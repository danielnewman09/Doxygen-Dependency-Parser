"""
Doxygen XML parser — extracts symbols, documentation, and relationships.

Parses Doxygen-generated XML files into a neutral data model (dataclasses)
that can be consumed by any backend (e.g. Neo4j).
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
)


# ---------------------------------------------------------------------------
# Data model — backend-agnostic representation of parsed Doxygen output
# ---------------------------------------------------------------------------


@dataclass
class IncludeEntry:
    file_refid: str
    included_file: str
    included_refid: str
    is_local: bool


@dataclass
class CallEntry:
    from_refid: str
    to_refid: str
    to_name: str


@dataclass
class ParseResult:
    """Complete parsed output from a Doxygen XML directory."""
    files: list[FileNode] = field(default_factory=list)
    namespaces: list[NamespaceNode] = field(default_factory=list)
    classes: list[ClassNode] = field(default_factory=list)
    enums: list[EnumNode] = field(default_factory=list)
    unions: list[UnionNode] = field(default_factory=list)
    interfaces: list[InterfaceNode] = field(default_factory=list)
    methods: list[MethodNode] = field(default_factory=list)
    attributes: list[AttributeNode] = field(default_factory=list)
    enum_values: list[EnumValueNode] = field(default_factory=list)
    defines: list[DefineNode] = field(default_factory=list)
    functions: list[FunctionNode] = field(default_factory=list)
    parameters: list[ParameterNode] = field(default_factory=list)
    includes: list[IncludeEntry] = field(default_factory=list)
    calls: list[CallEntry] = field(default_factory=list)
    called_by: list[CallEntry] = field(default_factory=list)

    @property
    def compounds(self) -> list:
        """Aggregate all compound-type nodes for backward compat."""
        return self.classes + self.enums + self.unions + self.interfaces

    @property
    def members(self) -> list:
        """Aggregate all member-type nodes for backward compat."""
        return self.methods + self.attributes + self.enum_values + self.defines + self.functions


# ---------------------------------------------------------------------------
# XML text extraction helpers
# ---------------------------------------------------------------------------

def get_text(element: Optional[ET.Element], default: str = "") -> str:
    """Extract text content from an element, handling nested elements."""
    if element is None:
        return default
    text_parts = []
    if element.text:
        text_parts.append(element.text)
    for child in element:
        text_parts.append(get_text(child))
        if child.tail:
            text_parts.append(child.tail)
    result = " ".join(text_parts)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


def parse_description(desc_elem: Optional[ET.Element]) -> str:
    """Parse a brief or detailed description element."""
    if desc_elem is None:
        return ""
    return get_text(desc_elem)


def parse_location(loc_elem: Optional[ET.Element]) -> tuple[Optional[str], Optional[int]]:
    """Extract file path and line number from location element."""
    if loc_elem is None:
        return None, None
    file_path = loc_elem.get("file")
    line = loc_elem.get("line")
    return file_path, int(line) if line else None


def _derive_module(qualified_name: str) -> str:
    """Extract the namespace prefix from a qualified name."""
    if "::" not in qualified_name:
        return ""
    return qualified_name.rsplit("::", 1)[0]


def _derive_source_type(file_path: str) -> str:
    """Derive source type from file extension."""
    if not file_path:
        return ""
    ext = Path(file_path).suffix.lower()
    if ext in (".h", ".hpp", ".hxx", ".h++"):
        return "header"
    if ext in (".c", ".cpp", ".cxx", ".cc", ".c++"):
        return "source"
    return ""


def _normalize_argsstring(argsstring: str) -> str:
    """Strip parameter names from argsstring, keeping types only.

    (int x, const char* str) → (int, const char*)
    (void) → ()
    """
    if not argsstring:
        return "()"
    inner = argsstring.strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    if not inner or inner == "void":
        return "()"
    parts = [p.strip() for p in inner.split(",")]
    normalized = []
    for part in parts:
        tokens = part.split()
        if len(tokens) > 1:
            last = tokens[-1]
            if not any(c in last for c in "<>*&::") and last.isidentifier():
                tokens = tokens[:-1]
        normalized.append(" ".join(tokens))
    return "(" + ", ".join(normalized) + ")"


# ---------------------------------------------------------------------------
# Compound and member parsing
# ---------------------------------------------------------------------------

def _parse_member(memberdef: ET.Element, compound_refid: str,
                  parent_qualified_name: str,
                  source: str, result: ParseResult) -> None:
    """Parse a member definition element into the result using kind dispatch."""
    refid = memberdef.get("id", "")
    kind = memberdef.get("kind", "")
    prot = memberdef.get("prot", "public")

    name = memberdef.findtext("name", "")
    type_str = get_text(memberdef.find("type"))
    definition = memberdef.findtext("definition", "")
    argsstring = memberdef.findtext("argsstring", "")

    loc = memberdef.find("location")
    file_path, line_number = parse_location(loc)

    brief = parse_description(memberdef.find("briefdescription"))
    detailed = parse_description(memberdef.find("detaileddescription"))

    source_type = _derive_source_type(file_path or "")

    # --- Kind dispatch ---
    if kind == "function" and compound_refid:
        # Method — belongs to a compound
        normalized_args = _normalize_argsstring(argsstring)
        qname = f"{parent_qualified_name}::{name}{normalized_args}"

        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"
        is_constexpr = memberdef.get("constexpr") == "yes"
        is_virtual = memberdef.get("virt") in ("virtual", "pure-virtual")
        is_inline = memberdef.get("inline") == "yes"
        is_explicit = memberdef.get("explicit") == "yes"

        result.methods.append(MethodNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname, type_signature=type_str,
            definition=definition, argsstring=argsstring,
            file_path=file_path or "", line_number=line_number,
            brief_description=brief, detailed_description=detailed,
            protection=prot, is_static=is_static, is_const=is_const,
            is_constexpr=is_constexpr, is_virtual=is_virtual,
            is_inline=is_inline, is_explicit=is_explicit, source=source,
            source_type=source_type, layer="dependency",
        ))

    elif kind == "function":
        # Free function — no compound parent
        normalized_args = _normalize_argsstring(argsstring)
        qname = f"::{name}{normalized_args}"

        result.functions.append(FunctionNode(
            refid=refid, kind=kind, name=name, qualified_name=qname,
            type_signature=type_str, definition=definition,
            argsstring=argsstring, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, source=source, layer="dependency",
        ))

    elif kind == "variable":
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"

        result.attributes.append(AttributeNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname, type_signature=type_str,
            definition=definition, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, protection=prot,
            is_static=is_static, is_const=is_const, source=source,
            layer="dependency",
        ))

    elif kind == "enumvalue":
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        result.enum_values.append(EnumValueNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname,
            file_path=file_path or "", line_number=line_number,
            brief_description=brief, detailed_description=detailed,
            source=source, layer="dependency",
        ))

    elif kind == "define":
        result.defines.append(DefineNode(
            refid=refid, kind=kind, name=name, qualified_name=name,
            definition=definition, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, source=source, layer="dependency",
        ))

    else:
        print(f"Warning: Unknown member kind '{kind}' for refid={refid}, name={name}, skipping",
              file=sys.stderr)
        return

    # --- Parameters (shared) ---
    for i, param in enumerate(memberdef.findall("param")):
        param_name = param.findtext("declname", "")
        param_type = get_text(param.find("type"))
        default_value = param.findtext("defval")
        result.parameters.append(ParameterNode(
            member_refid=refid, position=i, name=param_name or "",
            type=param_type, default_value=default_value or "",
        ))

    # --- Call references (shared) ---
    for ref in memberdef.findall("references"):
        result.calls.append(CallEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))

    for ref in memberdef.findall("referencedby"):
        result.called_by.append(CallEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))


def _parse_compound_file(xml_path: Path, source: str, result: ParseResult) -> None:
    """Parse a compound (class/struct/file) XML file."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"Warning: Could not parse {xml_path}: {e}", file=sys.stderr)
        return

    for compounddef in root.findall(".//compounddef"):
        refid = compounddef.get("id", "")
        kind = compounddef.get("kind", "")
        language = compounddef.get("language", "")
        compoundname = compounddef.findtext("compoundname", "")

        # --- Files ---
        if kind == "file":
            loc = compounddef.find("location")
            file_path = loc.get("file") if loc is not None else None
            result.files.append(FileNode(
                refid=refid, name=compoundname,
                path=file_path or "", language=language, source=source,
            ))

            for inc in compounddef.findall("includes"):
                result.includes.append(IncludeEntry(
                    file_refid=refid,
                    included_file=inc.text or "",
                    included_refid=inc.get("refid") or "",
                    is_local=inc.get("local") == "yes",
                ))
            continue

        # --- Namespaces ---
        if kind == "namespace":
            name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
            result.namespaces.append(NamespaceNode(
                refid=refid, name=name,
                qualified_name=compoundname, source=source,
                layer="dependency",
            ))
            continue

        # --- Common fields for all compound types ---
        name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
        qualified_name = compoundname

        loc = compounddef.find("location")
        file_path, line_number = parse_location(loc)

        brief = parse_description(compounddef.find("briefdescription"))
        detailed = parse_description(compounddef.find("detaileddescription"))
        definition = compounddef.findtext("definition", "")

        module = _derive_module(qualified_name)
        source_type = _derive_source_type(file_path or "")

        # --- Kind dispatch ---
        if kind in ("class", "struct"):
            base_classes = [
                baseref.text or ""
                for baseref in compounddef.findall("basecompoundref")
            ]
            is_final = compounddef.get("final") == "yes"
            is_abstract = compounddef.get("abstract") == "yes"

            result.classes.append(ClassNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                base_classes=base_classes, is_final=is_final,
                is_abstract=is_abstract, source=source,
                source_type=source_type, layer="dependency",
            ))

        elif kind == "enum":
            result.enums.append(EnumNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        elif kind == "union":
            result.unions.append(UnionNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        elif kind == "interface":
            result.interfaces.append(InterfaceNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        else:
            print(f"Warning: Unknown compound kind '{kind}' for refid={refid}, skipping",
                  file=sys.stderr)
            continue

        # --- Parse members (shared across compound types) ---
        for sectiondef in compounddef.findall("sectiondef"):
            for memberdef in sectiondef.findall("memberdef"):
                _parse_member(memberdef, refid, qualified_name, source, result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_index(index_path: Path) -> list[tuple[str, str]]:
    """Parse index.xml to get the list of all compound refids and kinds."""
    compounds = []
    try:
        tree = ET.parse(index_path)
        root = tree.getroot()
        for compound in root.findall("compound"):
            refid = compound.get("refid", "")
            kind = compound.get("kind", "")
            compounds.append((refid, kind))
    except ET.ParseError as e:
        print(f"Warning: Could not parse index.xml: {e}", file=sys.stderr)
    return compounds


def parse_xml_dir(xml_dir: Path, source: str = "msd",
                  progress_interval: int = 50) -> ParseResult:
    """Parse all Doxygen XML in a directory and return a ParseResult.

    Args:
        xml_dir: Directory containing Doxygen XML output (must have index.xml).
        source: Source label for provenance tracking.
        progress_interval: Print progress every N compounds (0 to disable).

    Returns:
        ParseResult with all parsed data.
    """
    index_path = xml_dir / "index.xml"
    if not index_path.exists():
        raise FileNotFoundError(f"index.xml not found in {xml_dir}")

    compounds = parse_index(index_path)
    result = ParseResult()

    for i, (refid, kind) in enumerate(compounds):
        xml_file = xml_dir / f"{refid}.xml"
        if xml_file.exists():
            _parse_compound_file(xml_file, source, result)

        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Parsed {i + 1}/{len(compounds)} XML files...")

    return result
