"""
C++ language parser for Doxygen XML output.

Implements :class:`~doxygen_index.parser.base.LanguageParser` with C++-specific
logic for parsing classes, structs, concepts, enums, unions, interfaces,
methods, attributes, enum values, and defines.  Each compound and member
kind has its own focused handler method, making it easy to understand,
test, and extend.

C++-specific utilities (``normalize_argsstring``, ``derive_module``, etc.)
are exposed as static methods so they can be reused by other C++-aware
modules (e.g. the cppreference page parser).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import xml.etree.ElementTree as ET

from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, DefineNode,
    ImplementationNode, ParameterNode, FunctionNode,
    FileNode, NamespaceNode,
)

from doxygen_index.parser.base import LanguageParser
from doxygen_index.parser.model import InheritsEntry, DependsOnEntry
from doxygen_index.parser.helpers import parse_index
from doxygen_index.parser.helpers import (
    get_text,
    parse_description,
    parse_location,
    parse_template_params,
)
from doxygen_index.parser.model import (
    ParseResult,
    TemplateParamRef,
    SpecializesRef,
    InvokeEntry,
    ImplementationRef,
    IncludeEntry,
)


# ---------------------------------------------------------------------------
# C++-specific utilities
# ---------------------------------------------------------------------------


def normalize_argsstring(argsstring: str) -> str:
    """Strip parameter names from argsstring, keeping types only.

    ``(int x, const char* str)`` → ``(int, const char*)``
    ``(void)`` → ``()``
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




def derive_module(qualified_name: str) -> str:
    """Extract the namespace prefix from a C++ qualified name."""
    if "::" not in qualified_name:
        return ""
    return qualified_name.rsplit("::", 1)[0]




def derive_source_type(file_path: str) -> str:
    """Derive source type from a C++ file extension."""
    if not file_path:
        return ""
    ext = Path(file_path).suffix.lower()
    if ext in (".h", ".hpp", ".hxx", ".h++"):
        return "header"
    if ext in (".c", ".cpp", ".cxx", ".cc", ".c++"):
        return "source"
    return ""




def detect_template_specialization(qualified_name: str) -> tuple[bool, str]:
    """Detect if a qualified name is a C++ template specialization.

    Returns ``(is_specialization, primary_template_name)``.
    A name like ``Foo<Bar>`` is a specialization of ``Foo``.

    Correctly handles nested angle brackets and qualified names inside
    template arguments (e.g. ``IsVector<std::vector<T>>``).
    """
    if "<" not in qualified_name or not qualified_name.endswith(">"):
        return False, ""

    # Find the position of the first '<' that opens the outer template arg list.
    depth = 0
    first_angle = -1
    for i, ch in enumerate(qualified_name):
        if ch == "<":
            if depth == 0 and first_angle == -1:
                first_angle = i
            depth += 1
        elif ch == ">":
            depth -= 1

    if first_angle == -1:
        return False, ""

    primary_qn = qualified_name[:first_angle].rstrip()
    if not primary_qn:
        return False, ""

    return True, primary_qn




# ---------------------------------------------------------------------------
# Common field extraction
# ---------------------------------------------------------------------------


def _extract_common_member_fields(
    memberdef: ET.Element,
) -> dict:
    """Extract fields shared by all C++ member kinds.

    Returns a dict with keys: refid, kind, prot, name, type_str,
    definition, argsstring, file_path, line_number, body_start, body_end,
    brief, detailed, source_type.
    """
    refid = memberdef.get("id", "")
    kind = memberdef.get("kind", "")
    prot = memberdef.get("prot", "public")

    name = memberdef.findtext("name", "")
    type_str = get_text(memberdef.find("type"))
    # Extract Doxygen <ref> elements from the <type> tag — these
    # are the ground-truth type references that become DEPENDS_ON
    # edges to dependency nodes.
    type_refs: list[dict] = []
    type_el = memberdef.find("type")
    if type_el is not None:
        for ref_el in type_el.iter("ref"):
            tr_refid = ref_el.get("refid", "")
            tr_kindref = ref_el.get("kindref", "")
            if tr_refid and tr_kindref:
                type_refs.append({"refid": tr_refid, "kindref": tr_kindref})
    definition = memberdef.findtext("definition", "")
    argsstring = memberdef.findtext("argsstring", "")

    loc = memberdef.find("location")
    file_path, line_number, body_start, body_end = parse_location(loc)

    brief = parse_description(memberdef.find("briefdescription"))
    detailed = parse_description(memberdef.find("detaileddescription"))

    source_type = derive_source_type(file_path or "")

    return {
        "refid": refid,
        "kind": kind,
        "prot": prot,
        "name": name,
        "type_str": type_str,
        "type_refs": type_refs,
        "definition": definition,
        "argsstring": argsstring,
        "file_path": file_path,
        "line_number": line_number,
        "body_start": body_start,
        "body_end": body_end,
        "brief": brief,
        "detailed": detailed,
        "source_type": source_type,
    }


def _extract_common_compound_fields(
    compounddef: ET.Element,
) -> dict:
    """Extract fields shared by all C++ type compound kinds.

    Returns a dict with keys: refid, kind, compoundname, name,
    qualified_name, file_path, line_number, brief, detailed, definition,
    module, source_type.
    """
    refid = compounddef.get("id", "")
    kind = compounddef.get("kind", "")
    compoundname = compounddef.findtext("compoundname", "")

    name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
    qualified_name = compoundname

    loc = compounddef.find("location")
    file_path, line_number, _, _ = parse_location(loc)

    brief = parse_description(compounddef.find("briefdescription"))
    detailed = parse_description(compounddef.find("detaileddescription"))
    definition = compounddef.findtext("definition", "")

    module = derive_module(qualified_name)
    source_type = derive_source_type(file_path or "")

    return {
        "refid": refid,
        "kind": kind,
        "compoundname": compoundname,
        "name": name,
        "qualified_name": qualified_name,
        "file_path": file_path,
        "line_number": line_number,
        "brief": brief,
        "detailed": detailed,
        "definition": definition,
        "module": module,
        "source_type": source_type,
    }


def _add_template_param_refs(
    element: Optional[ET.Element],
    from_refid: str,
    result: ParseResult,
) -> None:
    """Add TemplateParamRef entries from a <templateparamlist>."""
    tpl_params = parse_template_params(element)
    for idx, tp in enumerate(tpl_params):
        result.template_param_refs.append(TemplateParamRef(
            from_refid=from_refid,
            position=idx,
            type_constraint=tp.type_constraint,
            declname=tp.declname,
            defname=tp.defname,
            defval=tp.defval,
        ))


def _add_parameter_refs(
    memberdef: ET.Element,
    refid: str,
    result: ParseResult,
) -> None:
    """Add ParameterNode entries from <param> children."""
    for i, param in enumerate(memberdef.findall("param")):
        param_name = param.findtext("declname", "")
        param_type = get_text(param.find("type"))
        default_value = param.findtext("defval")
        result.parameters.append(ParameterNode(
            member_refid=refid,
            position=i,
            name=param_name or "",
            type=param_type,
            default_value=default_value or "",
        ))


def _add_invoke_refs(
    memberdef: ET.Element,
    refid: str,
    result: ParseResult,
) -> None:
    """Add InvokeEntry entries from <references>/<referencedby> children."""
    for ref in memberdef.findall("references"):
        result.invokes.append(InvokeEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))

    for ref in memberdef.findall("referencedby"):
        result.invoked_by.append(InvokeEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))


# ---------------------------------------------------------------------------
# CppParser
# ---------------------------------------------------------------------------


class CppParser(LanguageParser):
    """Language parser for C/C++ Doxygen XML output.

    Handles compound kinds: class, struct, concept, enum, union, interface.
    Handles member kinds: function (method), variable, typedef, enumvalue,
    define.

    Each compound and member kind is handled by a dedicated ``_parse_*``
    method, making the dispatch logic a thin routing layer rather than a
    monolithic function.
    """

    # ------------------------------------------------------------------
    # LanguageParser interface
    # ------------------------------------------------------------------

    def parse_source_dir(
        self,
        source_dir: Path,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
        progress_interval: int = 0,
    ) -> None:
        """Parse all Doxygen XML in *source_dir* and populate *result*.

        *source_dir* must contain a ``index.xml`` file produced by
        Doxygen.  Each compound XML file is parsed to extract classes,
        functions, etc.
        """
        source_dir = Path(source_dir)
        index_path = source_dir / "index.xml"
        if not index_path.exists():
            raise FileNotFoundError(f"index.xml not found in {source_dir}")

        compounds = parse_index(index_path)

        for i, (refid, kind) in enumerate(compounds):
            xml_file = source_dir / f"{refid}.xml"
            if xml_file.exists():
                self.parse_compound_file(xml_file, source, result, layer)

            if progress_interval and (i + 1) % progress_interval == 0:
                print(f"  Parsed {i + 1}/{len(compounds)} XML files...")

    # ------------------------------------------------------------------
    # Doxygen XML compound file parsing
    # ------------------------------------------------------------------

    def parse_compound_file(
        self,
        xml_path: Path,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> None:
        """Parse a single Doxygen compound XML file.

        For each ``<compounddef>``:
        * ``file`` and ``namespace`` kinds are handled directly
          (language-agnostic).
        * All other kinds are delegated to :meth:`parse_compound`.
          If it returns a qualified name, members within
          ``<sectiondef>`` elements are then processed via
          :meth:`parse_member`.
        """
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"Warning: Could not parse {xml_path}: {e}", file=sys.stderr)
            return

        for compounddef in root.findall(".//compounddef"):
            refid = compounddef.get("id", "")
            kind = compounddef.get("kind", "")
            compoundname = compounddef.findtext("compoundname", "")

            # --- Files (language-agnostic) ---
            if kind == "file":
                loc = compounddef.find("location")
                file_path = loc.get("file") if loc is not None else None
                language = compounddef.get("language", "")
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
                # Parse file-level members: typedefs, functions, variables.
                for sectiondef in compounddef.findall("sectiondef"):
                    for memberdef in sectiondef.findall("memberdef"):
                        fields = _extract_common_member_fields(memberdef)
                        member_kind = memberdef.get("kind", "")
                        if member_kind in ("typedef", "variable"):
                            self._parse_variable_member(
                                memberdef, fields, refid, "", source, result, layer)
                        elif member_kind == "function":
                            self._parse_file_function(
                                memberdef, fields, refid, source, result, layer)
                        elif member_kind == "define":
                            pass
                        for tr in fields.get("type_refs", []):
                            result.depends_on.append(DependsOnEntry(
                                from_refid=fields["refid"],
                                to_refid=tr["refid"],
                                to_type=tr["kindref"],
                            ))
                continue

            # --- Namespaces (language-agnostic) ---
            if kind == "namespace":
                name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
                result.namespaces.append(NamespaceNode(
                    refid=refid, name=name,
                    qualified_name=compoundname, source=source, layer=layer,
                ))
                continue

            # --- Language-specific type compound ---
            qualified_name = self.parse_compound(compounddef, source, result, layer)
            if qualified_name is None:
                print(
                    f"Warning: Unknown compound kind '{kind}' for refid={refid}, skipping",
                    file=sys.stderr,
                )
                continue

            # --- Parse members (shared across compound types) ---
            for sectiondef in compounddef.findall("sectiondef"):
                for memberdef in sectiondef.findall("memberdef"):
                    self.parse_member(
                        memberdef, refid, qualified_name, source, result, layer,
                    )


    # ------------------------------------------------------------------
    # Compound dispatch
    # ------------------------------------------------------------------

    # Map of compound kind → handler method name
    _COMPOUND_HANDLERS: dict[str, str] = {
        "class": "_parse_class_compound",
        "struct": "_parse_class_compound",
        "concept": "_parse_concept_compound",
        "enum": "_parse_enum_compound",
        "union": "_parse_union_compound",
        "interface": "_parse_interface_compound",
    }

    def parse_compound(
        self,
        compounddef: ET.Element,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> str | None:
        """Parse a C++ type compound and add entries to *result*.

        Delegates to a per-kind handler (e.g. ``_parse_class_compound``).
        Also handles template parameters and specialization detection
        (shared across all type compounds).

        Returns the qualified name of the compound, or None if the kind
        is not handled.
        """
        kind = compounddef.get("kind", "")
        handler_name = self._COMPOUND_HANDLERS.get(kind)
        if handler_name is None:
            return None

        fields = _extract_common_compound_fields(compounddef)
        refid = fields["refid"]

        # --- Template parameters (compound-level) ---
        _add_template_param_refs(
            compounddef.find("templateparamlist"), refid, result,
        )

        # --- Template specialization detection ---
        is_spec, primary_template = detect_template_specialization(fields["qualified_name"])
        if is_spec and primary_template:
            result.specializes_refs.append(SpecializesRef(
                from_refid=refid,
                from_qualified_name=fields["qualified_name"],
                primary_template_qualified_name=primary_template,
            ))

        # --- Delegate to kind-specific handler ---
        handler = getattr(self, handler_name)
        handler(compounddef, fields, source, result, layer)

        return fields["qualified_name"]

    # ------------------------------------------------------------------
    # Compound handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_class_compound(
        compounddef: ET.Element,
        fields: dict,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle class/struct compounds."""
        base_classes = []
        for baseref in compounddef.findall("basecompoundref"):
            base_name = baseref.text or ""
            base_classes.append(base_name)
            # Record InheritsEntry for graph JSON edge emission
            base_refid = baseref.get("refid", "")
            if base_refid and base_name:
                result.inherits.append(InheritsEntry(
                    from_refid=fields["refid"],
                    to_refid=base_refid,
                    to_type="ClassNode",
                ))

        is_final = compounddef.get("final") == "yes"
        is_abstract = compounddef.get("abstract") == "yes"

        result.classes.append(ClassNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            definition=fields["definition"],
            module=fields["module"],
            base_classes=base_classes,
            is_final=is_final,
            is_abstract=is_abstract,
            source=source,
            source_type=fields["source_type"],
            layer=layer,
            tags=[layer],
        ))

    @staticmethod
    def _parse_concept_compound(
        compounddef: ET.Element,
        fields: dict,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle C++20 concept compounds."""
        initializer = ""
        init_elem = compounddef.find("initializer")
        if init_elem is not None:
            initializer = get_text(init_elem)

        result.concepts.append(ConceptNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            definition=fields["definition"],
            module=fields["module"],
            source=source,
            source_type=fields["source_type"],
            layer=layer,
            initializer=initializer,
        ))

    @staticmethod
    def _parse_enum_compound(
        compounddef: ET.Element,
        fields: dict,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle enum compounds."""
        result.enums.append(EnumNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            definition=fields["definition"],
            module=fields["module"],
            source=source,
            source_type=fields["source_type"],
            layer=layer,
        ))

    @staticmethod
    def _parse_union_compound(
        compounddef: ET.Element,
        fields: dict,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle union compounds."""
        result.unions.append(UnionNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            definition=fields["definition"],
            module=fields["module"],
            source=source,
            source_type=fields["source_type"],
            layer=layer,
        ))

    @staticmethod
    def _parse_interface_compound(
        compounddef: ET.Element,
        fields: dict,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle interface compounds."""
        result.interfaces.append(InterfaceNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            definition=fields["definition"],
            module=fields["module"],
            source=source,
            source_type=fields["source_type"],
            layer=layer,
        ))

    # ------------------------------------------------------------------
    # Member dispatch
    # ------------------------------------------------------------------

    # Map of member kind → handler method name
    _MEMBER_HANDLERS: dict[str, str] = {
        "function": "_parse_function_member",
        "variable": "_parse_variable_member",
        "typedef": "_parse_variable_member",
        "enumvalue": "_parse_enumvalue_member",
        "define": "_parse_define_member",
    }

    def parse_member(
        self,
        memberdef: ET.Element,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> None:
        """Parse a C++ member definition and add entries to *result*.

        Delegates to a per-kind handler (e.g. ``_parse_function_member``).
        Template parameters, function parameters, and invoke references
        are handled uniformly for all member kinds that produce nodes.
        """
        kind = memberdef.get("kind", "")
        handler_name = self._MEMBER_HANDLERS.get(kind)

        if handler_name is None:
            refid = memberdef.get("id", "")
            name = memberdef.findtext("name", "")
            print(
                f"Warning: Unknown member kind '{kind}' for refid={refid}, name={name}, skipping",
                file=sys.stderr,
            )
            return

        fields = _extract_common_member_fields(memberdef)
        refid = fields["refid"]

        # --- Template parameters (shared) ---
        _add_template_param_refs(
            memberdef.find("templateparamlist"), refid, result,
        )

        # --- Delegate to kind-specific handler ---
        handler = getattr(self, handler_name)
        handler(memberdef, fields, compound_refid, parent_qualified_name, source, result, layer)

        # --- Type references (shared) ---
        # Doxygen <ref> elements inside <type> give us ground-truth
        # type dependencies (e.g. Database::db_ → sqlite3).
        for tr in fields.get("type_refs", []):
            result.depends_on.append(DependsOnEntry(
                from_refid=fields["refid"],
                to_refid=tr["refid"],
                to_type=tr["kindref"],
            ))

        # --- Parameters (shared, for all member kinds that produce nodes) ---
        _add_parameter_refs(memberdef, refid, result)

        # --- Invoke references (shared) ---
        _add_invoke_refs(memberdef, refid, result)

    # ------------------------------------------------------------------
    # Member handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_function_member(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle a function (method) member."""
        if not compound_refid:
            # Function without a compound — skip (shouldn't happen in valid C++ XML)
            return

        name = fields["name"]
        normalized_args = normalize_argsstring(fields["argsstring"])
        qname = f"{parent_qualified_name}::{name}{normalized_args}"

        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"
        is_constexpr = memberdef.get("constexpr") == "yes"
        is_virtual = memberdef.get("virt") in ("virtual", "pure-virtual")
        is_inline = memberdef.get("inline") == "yes"
        is_explicit = memberdef.get("explicit") == "yes"

        result.methods.append(MethodNode(
            refid=fields["refid"],
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            type_signature=fields["type_str"],
            definition=fields["definition"],
            argsstring=fields["argsstring"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            protection=fields["prot"],
            is_static=is_static,
            is_const=is_const,
            is_constexpr=is_constexpr,
            is_virtual=is_virtual,
            is_inline=is_inline,
            is_explicit=is_explicit,
            source=source,
            source_type=fields["source_type"],
            layer=layer,
            tags=[layer],
        ))

    @staticmethod
    def _parse_variable_member(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle a variable or typedef member."""
        name = fields["name"]
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"

        result.attributes.append(AttributeNode(
            refid=fields["refid"],
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            type_signature=fields["type_str"],
            definition=fields["definition"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            protection=fields["prot"],
            is_static=is_static,
            is_const=is_const,
            source=source,
            layer=layer,
        ))

    @staticmethod
    def _parse_file_function(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle a file-level function (not a class method)."""
        name = fields["name"]
        qname = name  # file-level, no enclosing class/namespace

        result.functions.append(FunctionNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            type_signature=fields["type_str"],
            definition=fields["definition"],
            argsstring=fields["argsstring"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            protection=fields["prot"],
            source=source,
            layer=layer,
            tags=[layer],
        ))

    @staticmethod
    def _parse_enumvalue_member(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle an enum value member."""
        name = fields["name"]
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name

        result.enum_values.append(EnumValueNode(
            refid=fields["refid"],
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source=source,
            layer=layer,
        ))

    @staticmethod
    def _parse_define_member(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle a #define macro member."""
        name = fields["name"]

        result.defines.append(DefineNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=name,
            qualified_name=name,
            definition=fields["definition"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source=source,
            layer=layer,
        ))

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def post_process(self, result: ParseResult) -> None:
        """Resolve C++-specific cross-references after all compounds are parsed.

        Currently resolves type_constraint text in template parameter refs
        to concept qualified names.
        """
        _resolve_concept_constraints(result)

        # Extract implementation source code from source files
        extract_implementations(result)


# ---------------------------------------------------------------------------
# Post-processing helpers (C++-specific)
# ---------------------------------------------------------------------------


def _resolve_concept_constraints(result: ParseResult) -> None:
    """Resolve type_constraint text to concept qualified names.

    After all compounds are parsed, we know which concepts exist.
    For each TemplateParamRef, if the type_constraint matches a known
    concept qualified name (either with or without namespace prefix),
    set concept_qualified_name on the ref so that an ENFORCES_CONCEPT
    edge can be created during ingestion.
    """
    concept_names = {c.qualified_name for c in result.concepts}
    # Also include short names (after ::) for prefix-less matches
    concept_short_names: dict[str, str] = {}
    for c in result.concepts:
        short = c.qualified_name.rsplit("::", 1)[-1] if "::" in c.qualified_name else c.qualified_name
        if short not in concept_short_names:
            concept_short_names[short] = c.qualified_name

    for tp in result.template_param_refs:
        if not tp.type_constraint:
            continue
        # Strip leading "typename " prefix — it's not a constraint
        constraint = tp.type_constraint
        if constraint.startswith("typename "):
            continue
        # Exact match against concept qualified names
        if constraint in concept_names:
            tp.concept_qualified_name = constraint
            continue
        # Try short name match
        if constraint in concept_short_names:
            tp.concept_qualified_name = concept_short_names[constraint]


def extract_implementations(
    result: ParseResult,
    source_base: Path | str | None = None,
) -> None:
    """Extract implementation source code from source files using body_start/body_end.

    For each member with body_start > 0 and body_end > 0, reads the
    source file and extracts lines body_start..body_end (inclusive),
    creates an ImplementationNode, and records the association.

    Members without implementation bodies (body_start == 0, body_end == 0,
    or missing source file) are skipped.

    Args:
        result: The ParseResult to augment with implementations.
        source_base: Optional base directory for resolving relative file paths.
            If None, file_path values must be absolute paths.
    """
    if source_base is not None:
        source_base = Path(source_base)

    # Cache for file contents to avoid re-reading the same file
    file_cache: dict[str, list[str] | None] = {}

    def _read_lines(file_path: str) -> list[str] | None:
        """Read file lines from cache or disk. Returns None if file not found."""
        if file_path in file_cache:
            return file_cache[file_path]

        path = Path(file_path)
        if not path.is_absolute() and source_base is not None:
            path = source_base / path

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            file_cache[file_path] = lines
            return lines
        except FileNotFoundError:
            print(f"  Warning: Source file not found for implementation extraction: {path}",
                  file=sys.stderr)
            file_cache[file_path] = None  # Cache the miss
            return None

    # Collect all members that have body locations
    members_with_bodies: list[tuple[object, str]] = []
    for m in result.methods:
        if m.body_start > 0 and m.body_end > 0 and m.file_path:
            members_with_bodies.append((m, m.refid))
    for f in result.functions:
        if f.body_start > 0 and f.body_end > 0 and f.file_path:
            members_with_bodies.append((f, f.refid))
    for d in result.defines:
        if d.body_start > 0 and d.body_end > 0 and d.file_path:
            members_with_bodies.append((d, d.refid))

    if not members_with_bodies:
        return

    impl_count = 0
    skip_count = 0

    for member, refid in members_with_bodies:
        lines = _read_lines(member.file_path)
        if lines is None:
            skip_count += 1
            continue

        # Doxygen bodystart/bodyend are 1-based line numbers, inclusive
        start = member.body_start - 1  # Convert to 0-based index
        end = member.body_end            # 1-based inclusive, so slice end is this value

        if start < 0 or end > len(lines) or start >= end:
            skip_count += 1
            continue

        source_text = "".join(lines[start:end]).rstrip("\n")

        if not source_text.strip():
            skip_count += 1
            continue

        impl_node = ImplementationNode(
            qualified_name=member.qualified_name,
            kind="implementation",
            implementation=source_text,
            impl_embedding=[],  # Embeddings deferred to a later phase
            source=member.source if hasattr(member, 'source') else "",
            layer=member.layer if hasattr(member, 'layer') else "dependency",
        )

        result.implementations.append(impl_node)
        result.implementation_refs.append(ImplementationRef(
            member_refid=refid,
            implementation=impl_node,
        ))
        impl_count += 1

    print(f"  Implementations extracted: {impl_count} (skipped: {skip_count})")