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
    """Parse a brief or detailed description element."""
    if desc_elem is None:
        return ""
    return get_text(desc_elem)


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