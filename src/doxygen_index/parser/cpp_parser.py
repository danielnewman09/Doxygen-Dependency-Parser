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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from doxygen_index.parser.model import InheritsEntry, DependsOnEntry, CompositionEntry
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
    ConceptConstraintEntry,
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




def _qualified_name_parent(qualified_name: str) -> str:
    """Return the parent namespace/module of *qualified_name*.

    Splits at the last ``::`` (or ``.``) that is NOT inside angle
    brackets, so that ``IsVector< std::vector< T > >`` correctly
    resolves its parent to ``cpp_sqlite`` (not ``IsVector< std``).
    """
    depth = 0
    last_sep = -1
    sep = None
    # Walk backwards to find the last scope separator
    for i in range(len(qualified_name) - 1, 0, -1):
        ch = qualified_name[i]
        if ch == '>':
            depth += 1
        elif ch == '<':
            depth = max(0, depth - 1)
        elif depth == 0:
            if qualified_name[i:i + 2] == '::':
                sep = '::'
                last_sep = i
                break
            if ch == '.' and qualified_name[i - 1] != '.':
                sep = '.'
                last_sep = i
                break
    if last_sep < 0:
        return ""
    return qualified_name[:last_sep]


def derive_module(qualified_name: str) -> str:
    """Extract the namespace prefix from a C++ qualified name.

    Only splits on ``::`` that are *outside* angle brackets so that
    template specialisations like ``IsVector< std::vector< T > >``
    don't produce a fake parent ``IsVector< std``.
    """
    return _qualified_name_parent(qualified_name)




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
    source: str,
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
            source=source,
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
        total = len(compounds)

        # Collect XML files that exist
        xml_files = [
            source_dir / f"{refid}.xml"
            for refid, _kind in compounds
        ]
        xml_files = [f for f in xml_files if f.exists()]

        if not xml_files:
            return

        # Parse in parallel — each file is independent and list.append
        # is GIL-atomic in CPython, so concurrent mutation of *result*
        # is safe.
        max_workers = min(32, (len(xml_files) // 10) + 1)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(self.parse_compound_file, f, source, result, layer): f
                for f in xml_files
            }
            completed = 0
            for future in as_completed(futures):
                completed += 1
                if progress_interval and completed % progress_interval == 0:
                    print(f"  Parsed {completed}/{total} XML files...")
                # Re-raise any exceptions from workers
                future.result()

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
            # Record InheritsEntry for graph JSON edge emission.
            # to_refid: Doxygen's internal refid (primary resolution within
            #   the same parse).
            # to_name: qualified name (fallback for cross-parse resolution,
            #   e.g. when ``std::runtime_error`` comes from cppreference).
            base_refid = baseref.get("refid", "")
            if base_name:
                result.inherits.append(InheritsEntry(
                    from_refid=fields["refid"],
                    to_refid=base_refid,
                    to_name=base_name,
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
            # Extract refs — the referenced entity constrains the
            # concept that references it (referenced → referencer).
            for ref in init_elem.findall(".//ref"):
                ref_refid = ref.get("refid", "")
                if ref_refid and ref_refid != fields["refid"]:
                    kindref = ref.get("kindref", "compound")
                    # Map Doxygen kindref to codegraph node type.
                    to_type = "CompoundNode"
                    if kindref == "concept":
                        to_type = "ConceptNode"
                    result.concept_constraints.append(ConceptConstraintEntry(
                        from_refid=ref_refid,    # referenced entity
                        to_refid=fields["refid"],  # concept doing the referencing
                        to_type=to_type,
                    ))

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
        _add_parameter_refs(memberdef, refid, result, source)

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
            visibility=fields["prot"],
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
            visibility=fields["prot"],
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
            visibility=fields["prot"],
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

        Resolves type_constraint text in template parameter refs to concept
        qualified names, derives namespace composition edges, and extracts
        implementation source code.
        """
        _resolve_concept_constraints(result)
        _derive_namespace_compositions(result)
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

    Also resolves concept-to-concept references in ``<initializer>``
    text that Doxygen doesn't emit as ``<ref>`` elements.  A concept
    like ``TransferObject`` referenced in another concept's constraint
    expression (e.g. ``DefaultConstructibleTransferObject``) produces
    a CONSTRAINS edge from the referencing concept to the referenced one.
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

    # --- CONSTRAINS edges from template constraints ---
    # When a template param like ``template<ValidTransferObject T>`` constrains
    # a compound (e.g. RepeatedFieldTransferObject), the concept (ValidTransferObject)
    # CONSTRAINS that compound.  Emit a ConceptConstraintEntry with the concept
    # as the ``from`` and the constrained compound as the ``to``.
    concept_qname_to_refid: dict[str, str] = {}
    for c in result.concepts:
        if hasattr(c, 'refid') and c.refid and hasattr(c, 'qualified_name'):
            concept_qname_to_refid[c.qualified_name] = c.refid
    for tp in result.template_param_refs:
        if not tp.concept_qualified_name or not tp.from_refid:
            continue
        concept_refid = concept_qname_to_refid.get(tp.concept_qualified_name)
        if not concept_refid:
            continue
        pair = (concept_refid, tp.from_refid)
        result.concept_constraints.append(
            ConceptConstraintEntry(
                from_refid=concept_refid,
                to_refid=tp.from_refid,
                to_type="CompoundNode",
            )
        )

    # --- Concept-to-concept references from initializer text ---
    # Doxygen emits ``<ref>`` for compounds referenced in template
    # arguments but NOT for concept names appearing as constraints
    # (e.g. ``TransferObject<T>`` in ``DefaultConstructibleTransferObject``
    # is plain text, not a ``<ref>``).  Scan each concept's initializer
    # for known concept qualified names and emit CONSTRAINS edges.
    #
    # Build qualified_name → refid lookups.
    concept_refids: dict[str, str] = {}
    for c in result.concepts:
        if hasattr(c, 'refid') and c.refid and hasattr(c, 'qualified_name'):
            concept_refids[c.qualified_name] = c.refid

    # Track already-recorded pairs so we don't duplicate.
    existing_pairs: set[tuple[str, str]] = {
        (cc.from_refid, cc.to_refid) for cc in result.concept_constraints
    }

    # Reverse map: short name → set of qualified_names (for disambiguation).
    short_to_qns: dict[str, set[str]] = {}
    for qn in concept_names:
        short = qn.rsplit("::", 1)[-1] if "::" in qn else qn
        short_to_qns.setdefault(short, set()).add(qn)

    for concept in result.concepts:
        initializer = getattr(concept, 'initializer', '') or ''
        if not initializer:
            continue
        from_refid = getattr(concept, 'refid', None)
        if not from_refid:
            continue
        from_qn = getattr(concept, 'qualified_name', '')

        for target_qn in concept_names:
            if target_qn == from_qn:
                continue  # skip self

            # Search for the qualified name OR the short name in the
            # initializer text.  Use word-boundary matches (the concept
            # name must appear as a standalone identifier, not as a
            # substring of a longer name, e.g. ``TransferObject``
            # matching inside ``DefaultConstructibleTransferObject``).
            short = target_qn.rsplit("::", 1)[-1] if "::" in target_qn else target_qn

            # Qualified name match (precise).
            if target_qn in initializer:
                pass  # found
            elif short in initializer:
                # Verify it's a word-boundary match (not embedded in a
                # longer identifier).
                import re as _re
                pattern = _re.compile(r'\b' + _re.escape(short) + r'\b')
                if not pattern.search(initializer):
                    continue
                # Disambiguate: if multiple concepts share the same short
                # name, prefer the one in the same namespace.
                candidates = short_to_qns.get(short, {target_qn})
                if len(candidates) > 1:
                    from_ns = from_qn.rsplit("::", 1)[0] if "::" in from_qn else ""
                    same_ns = [qn for qn in candidates
                               if qn.rsplit("::", 1)[0] == from_ns]
                    if same_ns:
                        target_qn = same_ns[0]
                    # else keep the original (first in set — non-deterministic
                    # but rare in practice).
            else:
                continue

            target_refid = concept_refids.get(target_qn)
            if not target_refid:
                continue

            pair = (target_refid, from_refid)
            if pair in existing_pairs:
                continue
            existing_pairs.add(pair)

            result.concept_constraints.append(
                ConceptConstraintEntry(
                    from_refid=target_refid,     # referenced concept
                    to_refid=from_refid,          # concept doing the referencing
                    to_type="ConceptNode",
                )
            )

    # --- Template-parameter CONSTRAINS edges ---
    # When a compound's template parameter is constrained by a concept
    # (e.g. ``template<ValidTransferObject T> struct RepeatedFieldTransferObject``),
    # the concept constrains the compound itself.
    #
    # Build refid → from_refid (owning compound) lookup from template param refs.
    tp_from_to_concept: dict[str, tuple[str, str]] = {}
    for tp in result.template_param_refs:
        if tp.concept_qualified_name and tp.from_refid:
            concept_refid = concept_refids.get(tp.concept_qualified_name)
            if concept_refid:
                tp_from_to_concept[tp.from_refid] = (concept_refid, tp.concept_qualified_name)

    for tp_from_refid, (concept_refid, concept_qn) in tp_from_to_concept.items():
        pair = (concept_refid, tp_from_refid)
        if pair in existing_pairs:
            continue
        existing_pairs.add(pair)
        result.concept_constraints.append(
            ConceptConstraintEntry(
                from_refid=concept_refid,    # the concept
                to_refid=tp_from_refid,      # the compound it constrains
                to_type="CompoundNode",
            )
        )


def _derive_namespace_compositions(result: ParseResult) -> None:
    """Record namespace ``COMPOSES`` relationships on ``result.compositions``.

    A namespace composes its *immediate* direct children: child namespaces
    and top-level classes / interfaces / enums / unions / structs / concepts /
    functions defined directly within it (i.e. whose parent qualified name —
    everything before the last ``::`` — equals the namespace's qualified name).

    Matching is scoped by (qualified_name, source) so that, e.g., a
    ``std`` namespace from one Doxygen parse run doesn't accidentally compose
    classes from another source whose names happen to start with ``std::``.

    When a child's parent namespace doesn't exist in ``result.namespaces``
    (e.g. ``boost::unordered_map`` when ``boost`` has no Doxygen namespace
    compound), a synthetic NamespaceNode is created so the child still gets
    a proper parent.  Nested missing namespaces (``spdlog::spdlog_ex`` when
    neither exists) are handled recursively.
    """
    # Map (qualified_name, source) → refid for all namespaces.
    ns_refid_by_key: dict[tuple[str, str], str] = {}
    for ns in result.namespaces:
        qname = getattr(ns, "qualified_name", None)
        src = getattr(ns, "source", "") or ""
        refid = getattr(ns, "refid", None)
        if qname and refid:
            ns_refid_by_key[(qname, src)] = refid

    # Build a set of all known child qualified_names (for children that
    # happen to also be namespaces declared in the source).  We use this
    # to avoid creating a synthetic when a real one already exists.
    known_child_qnames: set[tuple[str, str]] = set()
    child_sources = (
        result.namespaces + result.classes + result.interfaces
        + result.enums + result.unions + result.concepts + result.functions
    )
    for child in child_sources:
        cq = getattr(child, "qualified_name", None)
        cs = getattr(child, "source", "") or ""
        if cq:
            known_child_qnames.add((cq, cs))

    def _ensure_namespace(ns_qname: str, source: str) -> str | None:
        """Return the refid for *ns_qname*, creating a synthetic
        NamespaceNode (and any intermediate ancestors) into
        ``result.namespaces`` if it doesn't already exist."""
        key = (ns_qname, source)
        existing = ns_refid_by_key.get(key)
        if existing:
            return existing

        # Derive the parent namespace of this one (e.g. spdlog for spdlog::spdlog_ex).
        parent_ns = derive_module(ns_qname)

        # Normalise: if this synthetic namespace has the same (qname, source)
        # as a *real* child node, we should NOT create a duplicate — the
        # real one will be created when that compound is parsed.  However,
        # if the real one hasn't been created yet (because its Doxygen
        # compound comes later), we still create a synthetic.  On merge
        # in Neo4j the real one will replace the synthetic's properties
        # while the uid stays the same (both computed from source + qname).

        # Recursively ensure the parent exists first.
        parent_refid: str | None = None
        if parent_ns:
            parent_refid = _ensure_namespace(parent_ns, source)

        # Create a synthetic NamespaceNode.
        synthetic_refid = f"synthetic-ns:{ns_qname}"
        synthetic_name = ns_qname.split("::")[-1]
        ns_node = NamespaceNode(
            refid=synthetic_refid,
            name=synthetic_name,
            qualified_name=ns_qname,
            source=source,
            layer="as-built",
        )
        result.namespaces.append(ns_node)
        ns_refid_by_key[key] = synthetic_refid

        # Compose this synthetic namespace under its parent.
        if parent_refid and parent_ns:
            result.compositions.append(CompositionEntry(
                parent_refid=parent_refid,
                child_refid=synthetic_refid,
                child_type="NamespaceNode",
            ))

        return synthetic_refid

    if not ns_refid_by_key and not known_child_qnames:
        return

    for child in child_sources:
        child_qname = getattr(child, "qualified_name", None)
        child_refid = getattr(child, "refid", None)
        child_source = getattr(child, "source", "") or ""
        if not child_qname or not child_refid:
            continue

        # Derive parent: everything before the last "::".
        parent_qname = derive_module(child_qname)
        if not parent_qname:
            continue

        # Ensure the parent namespace exists (synthesizing if needed).
        parent_refid = _ensure_namespace(parent_qname, child_source)
        if not parent_refid:
            continue

        # Skip self-composition.
        if parent_refid == child_refid:
            continue

        result.compositions.append(CompositionEntry(
            parent_refid=parent_refid,
            child_refid=child_refid,
            child_type=type(child).__name__,
        ))


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