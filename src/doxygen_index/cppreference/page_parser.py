"""
Parse individual cppreference HTML pages into data-model entries.

Each ``parse_*`` function accepts a :class:`PageInfo` and a BeautifulSoup
object, returning one or more data-model entries from
:mod:`doxygen_index.parser`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from codegraph import ClassNode, FileNode, MethodNode, FunctionNode, ParameterNode

from doxygen_index.parser import normalize_argsstring, derive_module, derive_source_type

from .classifier import PageInfo
from .html_helpers import (
    extract_declarations,
    extract_member_list,
    extract_page_description,
    extract_page_title,
    extract_parameters,
    extract_base_classes,
    parse_declaration_signature,
    strip_html_to_text,
)

SOURCE = "cppreference"


# ---------------------------------------------------------------------------
# Qualified-name helpers
# ---------------------------------------------------------------------------

# Map URL path segments to C++ namespace components
_PATH_TO_NS: dict[str, str] = {
    "container": "std",
    "algorithm": "std",
    "string": "std",
    "io": "std",
    "filesystem": "std::filesystem",
    "thread": "std",
    "atomic": "std",
    "memory": "std",
    "regex": "std",
    "chrono": "std::chrono",
    "utility": "std",
    "locale": "std",
    "error": "std",
    "numeric": "std",
    "ranges": "std::ranges",
    "iterator": "std",
    "header": "",
    "named_req": "",
    "concept": "std",
    "coroutine": "std",
}


def _qualified_name_from_title(title: str, info: PageInfo) -> str:
    """Build a qualified name from the page title.

    cppreference titles are usually already qualified (e.g. ``std::vector``).
    If the title doesn't start with ``std::``, infer the namespace from the
    URL path.
    """
    # Fix whitespace around :: that BS4 text extraction introduces
    title = re.sub(r"\s*::\s*", "::", title)
    title = title.strip()

    if "::" in title:
        return title

    # Infer namespace from the URL path
    parts = info.relative.replace("en/cpp/", "").split("/")
    if parts:
        ns = _PATH_TO_NS.get(parts[0], "std")
        if ns:
            return f"{ns}::{title}"
    return title


def _short_name(qualified_name: str) -> str:
    """Extract the short name from a qualified name."""
    return qualified_name.split("::")[-1] if "::" in qualified_name else qualified_name


# ---------------------------------------------------------------------------
# Header pages
# ---------------------------------------------------------------------------

def parse_header_page(info: PageInfo, soup) -> tuple[FileNode, list[str]]:
    """Parse a header reference page (e.g. ``<vector>``).

    Returns:
        A tuple of (FileNode, list of symbol refids declared in this header).
        The refids can be used to create DEFINED_IN relationships.
    """
    from posixpath import normpath

    file_entry = FileNode(
        refid=info.refid,
        name=info.header_name,
        path=info.relative,
        language="C++",
        source=SOURCE,
    )

    # Extract symbols listed on this header page and resolve their links
    # to cppreference refids so we can create DEFINED_IN edges.
    symbol_refids: list[str] = []
    base_dir = info.relative.rsplit("/", 1)[0]  # "en/cpp/header"

    for item in extract_member_list(soup):
        if not item.link:
            continue
        # Resolve relative link: "../memory/unique_ptr.html" relative to
        # "en/cpp/header" → "en/cpp/memory/unique_ptr.html"
        resolved = normpath(f"{base_dir}/{item.link}")
        # Convert to refid: strip en/ prefix and .html suffix
        if resolved.startswith("en/"):
            resolved = resolved[3:]
        if resolved.endswith(".html"):
            resolved = resolved[:-5]
        symbol_refids.append(f"cppreference:{resolved}")

    return file_entry, symbol_refids


# ---------------------------------------------------------------------------
# Class / struct pages
# ---------------------------------------------------------------------------

def parse_class_page(
    info: PageInfo, soup,
) -> tuple[ClassNode, list[MethodNode]]:
    """Parse a class/struct page.

    Returns the compound entry and stub members extracted from the
    ``t-dsc`` member-list tables.  Stubs have brief descriptions but
    no full signatures — those come from parsing the member sub-pages.
    """
    title = extract_page_title(soup)
    qualified_name = _qualified_name_from_title(title, info)
    name = _short_name(qualified_name)
    brief, detailed = extract_page_description(soup)
    bases = extract_base_classes(soup)

    compound = ClassNode(
        refid=info.refid,
        kind="class",
        name=name,
        qualified_name=qualified_name,
        file_path="",
        line_number=None,
        brief_description=brief,
        detailed_description=detailed,
        base_classes=bases,
        is_final=False,
        is_abstract=False,
        source=SOURCE,
        source_type="header",
        definition="",
        module=derive_module(qualified_name),
        layer="dependency",
    )

    # Extract member stubs from t-dsc tables
    stubs: list[MethodNode] = []
    for item in extract_member_list(soup):
        member_name = item.name
        # Clean up member names: "(constructor)", "(destructor)", "operator=" etc.
        member_name = re.sub(r"^\((.+)\)$", r"\1", member_name)
        member_qn = f"{qualified_name}::{member_name}"
        # Build a refid from the link if available
        if item.link:
            # link is relative like "vector/push_back.html"
            link_path = item.link.rstrip("/")
            if link_path.endswith(".html"):
                link_path = link_path[:-5]
            stub_refid = f"cppreference:cpp/{_strip_leading_path(info.relative)}/{link_path}".rstrip("/")
        else:
            stub_refid = f"{info.refid}/{member_name}"

        stubs.append(MethodNode(
            refid=stub_refid,
            compound_refid=info.refid,
            kind="function",
            name=member_name,
            qualified_name=member_qn,
            type_signature="",
            definition="",
            argsstring="",
            file_path="",
            line_number=None,
            brief_description=item.brief,
            detailed_description="",
            protection="public",
            is_static=False,
            is_const=False,
            is_constexpr=False,
            is_virtual=False,
            is_inline=False,
            is_explicit=False,
            source=SOURCE,
            layer="dependency",
        ))

    return compound, stubs


def _strip_leading_path(relative: str) -> str:
    """Strip 'en/cpp/' and filename from a relative path, leaving the directory.

    ``en/cpp/container/vector.html`` → ``container/vector``
    """
    path = relative.replace("en/cpp/", "")
    if path.endswith(".html"):
        path = path[:-5]
    return path


# ---------------------------------------------------------------------------
# Member pages
# ---------------------------------------------------------------------------

def parse_member_page(
    info: PageInfo, soup,
) -> list[tuple[MethodNode, list[ParameterNode]]]:
    """Parse a member function page (e.g. ``std::vector::push_back``).

    Returns one ``(MethodNode, [ParameterNode, ...])`` tuple per overload.
    """
    title = extract_page_title(soup)
    brief, detailed = extract_page_description(soup)
    declarations = extract_declarations(soup)
    param_docs = extract_parameters(soup)

    results: list[tuple[MethodNode, list[ParameterNode]]] = []

    if not declarations:
        # No declarations found — create a single entry from the title
        qualified_name = _qualified_name_from_title(title, info)
        name = _short_name(qualified_name)
        member = _make_method(
            refid=f"{info.refid}#0",
            compound_refid=info.parent_refid,
            name=name,
            qualified_name=qualified_name,
            brief=brief,
            detailed=detailed,
        )
        results.append((member, []))
        return results

    for decl in declarations:
        ret_type, func_name, argsstring, sig_params = parse_declaration_signature(decl.text)

        # Use the parsed name, or fall back to the title
        if not func_name:
            qualified_name = _qualified_name_from_title(title, info)
            func_name = _short_name(qualified_name)
        else:
            # If the parsed name is already qualified, use it; otherwise qualify it
            if "::" in func_name:
                qualified_name = func_name
            else:
                # Derive namespace from parent class
                qualified_name = _qualified_name_from_title(func_name, info)
                # If we have a parent, use parent's qualified name
                if info.parent_refid:
                    parent_qn = _qn_from_refid(info.parent_refid)
                    if parent_qn:
                        qualified_name = f"{parent_qn}::{func_name}"

        # Append normalized argsstring for overload safety
        normalized_args = normalize_argsstring(argsstring)

        refid = f"{info.refid}#{decl.overload_index}"
        definition = decl.text.rstrip(";").strip()

        member = _make_method(
            refid=refid,
            compound_refid=info.parent_refid,
            name=func_name,
            qualified_name=f"{qualified_name}{normalized_args}",
            ret_type=ret_type,
            definition=definition,
            argsstring=argsstring,
            brief=brief,
            detailed=detailed,
        )

        # Build ParameterNode list — merge signature params with docs
        params: list[ParameterNode] = []
        doc_map = {p.name: p.description for p in param_docs}
        for i, (ptype, pname, pdefault) in enumerate(sig_params):
            desc = doc_map.get(pname, "")
            params.append(ParameterNode(
                member_refid=refid,
                position=i,
                name=pname,
                type=ptype,
                default_value=pdefault or "",
            ))
        # If no sig params but docs exist, create entries from docs alone
        if not params and param_docs:
            for i, p in enumerate(param_docs):
                params.append(ParameterNode(
                    member_refid=refid,
                    position=i,
                    name=p.name,
                    type="",
                    default_value="",
                ))

        results.append((member, params))

    return results


def _qn_from_refid(refid: str) -> str:
    """Best-effort qualified name from a cppreference refid.

    ``cppreference:cpp/container/vector`` → ``std::vector``
    """
    path = refid.replace("cppreference:", "")
    parts = path.split("/")
    if len(parts) < 2:
        return ""
    category = parts[1] if len(parts) > 1 else ""
    ns = _PATH_TO_NS.get(category, "std")
    entity = parts[-1] if parts else ""
    if ns:
        return f"{ns}::{entity}"
    return entity


def _make_method(
    refid: str,
    compound_refid: str,
    name: str,
    qualified_name: str,
    ret_type: str = "",
    definition: str = "",
    argsstring: str = "",
    brief: str = "",
    detailed: str = "",
) -> MethodNode:
    return MethodNode(
        refid=refid,
        compound_refid=compound_refid,
        kind="function",
        name=name,
        qualified_name=qualified_name,
        type_signature=ret_type,
        definition=definition,
        argsstring=argsstring,
        file_path="",
        line_number=None,
        brief_description=brief,
        detailed_description=detailed,
        protection="public",
        is_static=False,
        is_const=False,
        is_constexpr=False,
        is_virtual=False,
        is_inline=False,
        is_explicit=False,
        source=SOURCE,
        layer="dependency",
    )


# ---------------------------------------------------------------------------
# Free function pages
# ---------------------------------------------------------------------------

def parse_free_function_page(
    info: PageInfo, soup,
) -> list[tuple[FunctionNode, list[ParameterNode]]]:
    """Parse a free function page (e.g. ``std::sort``).

    Uses FunctionNode with qualified_name = ::name(args).
    """
    original_parent = info.parent_refid
    info.parent_refid = ""
    try:
        results = parse_member_page(info, soup)
    finally:
        info.parent_refid = original_parent

    # Convert MethodNode results to FunctionNode
    cleaned: list[tuple[FunctionNode, list[ParameterNode]]] = []
    for member, params in results:
        fn = FunctionNode(
            refid=member.refid,
            kind="function",
            name=member.name,
            qualified_name=f"::{member.name}{normalize_argsstring(member.argsstring)}",
            type_signature=member.type_signature,
            definition=member.definition,
            argsstring=member.argsstring,
            file_path=member.file_path,
            line_number=member.line_number,
            brief_description=member.brief_description,
            detailed_description=member.detailed_description,
            source=SOURCE,
            layer="dependency",
        )
        # Update parameter refids to point to the FunctionNode
        for p in params:
            p.member_refid = fn.refid
        cleaned.append((fn, params))
    return cleaned
