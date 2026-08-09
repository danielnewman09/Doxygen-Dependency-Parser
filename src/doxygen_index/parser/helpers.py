"""
Language-agnostic XML text extraction and utility helpers.

These functions operate on Doxygen's XML output format directly, without
any language-specific logic.  They are shared by all LanguageParser
implementations.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from doxygen_index.parser.model import (
    TemplateParamEntry,
)


# ---------------------------------------------------------------------------
# XML text extraction
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
    """Parse a brief or detailed description element, preserving line structure.

    Paragraphs (``<para>``) are rendered as separate blocks separated by a
    blank line, and Doxygen directives such as ``\\param``, ``\\return``,
    ``\\throws`` and ``\\note`` are reconstructed on their own lines.  The
    result can be re-emitted with the same line endings as the original
    comment, up to the structure Doxygen preserves in its XML output.
    """
    if desc_elem is None:
        return ""
    blocks: list[str] = []
    for child in desc_elem:
        if not isinstance(child.tag, str):
            continue
        blocks.append(_render_block(child))
    return "\n\n".join(block for block in blocks if block)


# ---------------------------------------------------------------------------
# Line-preserving description rendering
#
# Doxygen's XML output preserves structure that a naive text flattening
# throws away: paragraph boundaries (``<para>``), ``\param``/``\return``/
# ``\throws`` directives (``<parameterlist>``/``<simplesect>``), code blocks
# (``<programlisting>``), lists, and verbatim text.  ``get_text`` collapses
# all of this into a single line; these renderers keep each structural unit
# on its own line so the description can be re-emitted with the same
# formatting fidelity (line endings) as the original comment.
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset({
    "para", "parameterlist", "simplesect", "xrefsect", "programlisting",
    "verbatim", "itemizedlist", "orderedlist", "variablelist",
    "heading", "table", "blockquote",
    "sect1", "sect2", "sect3", "sect4",
})

_PARAMETERLIST_DIRECTIVES = {
    "param": "param",
    "exception": "throws",
    "retval": "retval",
}

_SIMPLESECT_DIRECTIVES = {
    "return": "return",
    "returns": "return",
    "note": "note",
    "warning": "warning",
    "see": "see",
    "author": "author",
    "version": "version",
    "since": "since",
    "pre": "pre",
    "post": "post",
    "invariant": "invariant",
    "remark": "remark",
    "par": "par",
    "rcs": "rcs",
}


def _render_block(el: ET.Element) -> str:
    """Render a block-level description element, preserving line structure."""
    tag = el.tag
    if tag == "para":
        return _render_para(el)
    if tag == "parameterlist":
        return _render_parameterlist(el)
    if tag == "simplesect":
        return _render_simplesect(el)
    if tag == "xrefsect":
        return _render_xrefsect(el)
    if tag == "programlisting":
        return _render_programlisting(el)
    if tag == "verbatim":
        return _render_verbatim(el)
    if tag in ("itemizedlist", "orderedlist"):
        return _render_list(el)
    if tag == "variablelist":
        return _render_variablelist(el)
    if tag in ("sect1", "sect2", "sect3", "sect4"):
        return _render_section(el)
    return get_text(el)


def _render_para(para: ET.Element) -> str:
    """Render a ``<para>`` element, keeping each line on its own line."""
    chunks: list[str] = []
    if para.text:
        chunks.append(para.text)
    for child in para:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag in ("linebreak", "br"):
            chunks.append("\n")
        elif tag == "sp":
            chunks.append(" ")
        elif tag in _BLOCK_TAGS:
            chunks.append("\n")
            chunks.append(_render_block(child))
        else:
            chunks.append(get_text(child))
        # Whitespace-only tails (Doxygen's XML formatting between elements)
        # are noise; meaningful tails (" ... and more") are kept.
        if child.tail and child.tail.strip():
            chunks.append(child.tail)
    lines = [
        re.sub(r"[ \t]+", " ", ln).strip()
        for ln in "".join(chunks).split("\n")
    ]
    # A single ``<para>`` maps to one block of lines: blank lines inside it
    # are XML formatting noise between sibling block elements, so drop them.
    return "\n".join(ln for ln in lines if ln)


def _render_parameterlist(el: ET.Element) -> str:
    """Render ``<parameterlist>`` (``\\param``/``\\throws``) items, one per line."""
    kind = el.get("kind", "param")
    directive = _PARAMETERLIST_DIRECTIVES.get(kind, kind)
    lines: list[str] = []
    for item in el.findall("parameteritem"):
        names = [get_text(n) for n in item.findall(".//parametername")]
        desc_para = item.find("parameterdescription/para")
        desc = _render_para(desc_para) if desc_para is not None else ""
        name = ", ".join(n for n in names if n)
        if name:
            line = f"\\{directive} {name} {desc}".rstrip()
        else:
            line = f"\\{directive} {desc}".rstrip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _render_simplesect(el: ET.Element) -> str:
    """Render ``<simplesect>`` (``\\return``/``\\note``/``\\warning``/...) items."""
    kind = el.get("kind", "")
    title = get_text(el.find("title"))
    para = el.find("para")
    body = _render_para(para) if para is not None else ""
    directive = _SIMPLESECT_DIRECTIVES.get(kind, kind)
    parts = " ".join(p for p in (title, body) if p)
    if directive:
        return f"\\{directive} {parts}".rstrip()
    return parts


def _render_xrefsect(el: ET.Element) -> str:
    """Render ``<xrefsect>`` as ``Title: description``."""
    title = get_text(el.find("xreftitle"))
    para = el.find("xrefdescription/para")
    desc = _render_para(para) if para is not None else ""
    if title and desc:
        return f"{title}: {desc}"
    return title or desc


def _render_programlisting(el: ET.Element) -> str:
    """Render ``<programlisting>`` as code lines, one ``<codeline>`` per line."""
    codelines = el.findall("codeline")
    if not codelines:
        return get_text(el)
    return "\n".join(get_text(c) for c in codelines)


def _render_verbatim(el: ET.Element) -> str:
    """Render ``<verbatim>`` raw text, preserving its line endings."""
    return (el.text or "").strip("\n")


def _render_section(el: ET.Element) -> str:
    """Render a ``<sectN>`` section: its title as a line, then its content."""
    parts: list[str] = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag == "title":
            title = get_text(child)
            if title:
                parts.append(title)
        elif tag in _BLOCK_TAGS:
            parts.append(_render_block(child))
        else:
            rendered = get_text(child)
            if rendered:
                parts.append(rendered)
    return "\n\n".join(parts)


def _render_list(el: ET.Element) -> str:
    """Render ``<itemizedlist>``/``<orderedlist>`` items, one per line."""
    lines: list[str] = []
    for idx, item in enumerate(el.findall("listitem"), start=1):
        para = item.find("para")
        text = _render_para(para) if para is not None else get_text(item)
        lines.append(f"{idx}. {text}" if el.tag == "orderedlist" else f"- {text}")
    return "\n".join(lines)


def _render_variablelist(el: ET.Element) -> str:
    """Render ``<variablelist>`` entries as ``- term: description`` lines."""
    lines: list[str] = []
    for entry in el.findall("varlistentry"):
        term = get_text(entry.find("term"))
        listitem = entry.find("listitem")
        para = listitem.find("para") if listitem is not None else None
        desc = _render_para(para) if para is not None else ""
        if term and desc:
            lines.append(f"- {term}: {desc}")
        elif term:
            lines.append(f"- {term}")
        else:
            lines.append(f"- {desc}")
    return "\n".join(lines)


def parse_location(loc_elem: Optional[ET.Element]) -> tuple[Optional[str], Optional[int], Optional[int], Optional[int]]:
    """Extract file path, line number, body start, and body end from location element.

    Returns:
        (file_path, line_number, body_start, body_end)
        body_start and body_end are None if not present or -1 (no body).
    """
    if loc_elem is None:
        return None, None, None, None
    file_path = loc_elem.get("file")
    line = loc_elem.get("line")
    bodystart = loc_elem.get("bodystart")
    bodyend = loc_elem.get("bodyend")
    body_start = int(bodystart) if bodystart and bodystart != "-1" else None
    body_end = int(bodyend) if bodyend and bodyend != "-1" else None
    return file_path, int(line) if line else None, body_start, body_end


def parse_template_params(element: Optional[ET.Element]) -> list[TemplateParamEntry]:
    """Parse a <templateparamlist> element into TemplateParamEntry items.

    Handles both compound-level and member-level template parameter lists.
    The <type> element may contain nested <ref> children that we flatten.
    """
    if element is None:
        return []
    params = []
    for param in element.findall("param"):
        type_constraint = ""
        type_elem = param.find("type")
        if type_elem is not None:
            type_constraint = get_text(type_elem)
        declname = param.findtext("declname", "") or ""
        defname = param.findtext("defname", "") or ""
        defval = param.findtext("defval", "") or ""
        params.append(TemplateParamEntry(
            type_constraint=type_constraint,
            declname=declname,
            defname=defname,
            defval=defval,
        ))
    return params


# ---------------------------------------------------------------------------
# Index parsing
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