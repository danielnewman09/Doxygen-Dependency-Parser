"""
Low-level extraction functions for cppreference HTML pages.

All functions accept a BeautifulSoup ``Tag`` or ``BeautifulSoup`` object
and return plain dataclasses / tuples.  No file I/O or side-effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class Declaration:
    """A single C++ declaration extracted from a ``t-dcl`` row."""
    text: str
    overload_index: int = 0
    since_cxx: str = ""
    until_cxx: str = ""


@dataclass
class MemberListItem:
    """An entry from a ``t-dsc`` member-list table."""
    name: str
    link: str          # relative href (may be empty)
    brief: str


@dataclass
class ParamInfo:
    """A parameter from a ``t-par`` table."""
    name: str
    description: str


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def strip_html_to_text(element) -> str:
    """Recursively extract visible text from a BS4 element.

    Normalises whitespace to single spaces and strips leading/trailing.
    """
    if element is None:
        return ""
    text = element.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Declaration tables  (table.t-dcl-begin > tr.t-dcl)
# ---------------------------------------------------------------------------

_CXX_VERSION_RE = re.compile(r"t-(?:since|until)-cxx(\d+)")


def _extract_version_markers(row) -> tuple[str, str]:
    """Return (since_cxx, until_cxx) from CSS classes on a ``t-dcl`` row."""
    classes = row.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()
    since = ""
    until = ""
    for cls in classes:
        m = _CXX_VERSION_RE.match(cls)
        if m:
            version = m.group(1)
            if cls.startswith("t-since"):
                since = f"C++{version}"
            else:
                until = f"C++{version}"
    return since, until


def extract_declarations(soup) -> list[Declaration]:
    """Extract C++ declarations from all ``t-dcl-begin`` tables on a page."""
    declarations: list[Declaration] = []
    idx = 0
    for table in soup.find_all("table", class_="t-dcl-begin"):
        for row in table.find_all("tr", class_="t-dcl"):
            cells = row.find_all("td")
            if not cells:
                continue
            # The first cell contains the declaration code
            decl_text = strip_html_to_text(cells[0])
            if not decl_text:
                continue
            since, until = _extract_version_markers(row)
            declarations.append(Declaration(
                text=decl_text,
                overload_index=idx,
                since_cxx=since,
                until_cxx=until,
            ))
            idx += 1
    return declarations


# ---------------------------------------------------------------------------
# Member-list tables  (table.t-dsc-begin > tr.t-dsc)
# ---------------------------------------------------------------------------

def extract_member_list(soup) -> list[MemberListItem]:
    """Extract member names and briefs from ``t-dsc-begin`` tables."""
    items: list[MemberListItem] = []
    for table in soup.find_all("table", class_="t-dsc-begin"):
        for row in table.find_all("tr", class_="t-dsc"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            name_cell, desc_cell = cells[0], cells[1]

            # Extract name — prefer the <a> link text inside t-dsc-member-div
            link_tag = name_cell.find("a")
            name = ""
            link = ""
            if link_tag:
                name = strip_html_to_text(link_tag)
                link = link_tag.get("href", "")
            if not name:
                name = strip_html_to_text(name_cell)

            brief = strip_html_to_text(desc_cell)
            if name:
                items.append(MemberListItem(name=name, link=link, brief=brief))
    return items


# ---------------------------------------------------------------------------
# Parameter tables  (table.t-par-begin > tr.t-par)
# ---------------------------------------------------------------------------

def extract_parameters(soup) -> list[ParamInfo]:
    """Extract parameter docs from ``t-par-begin`` tables."""
    params: list[ParamInfo] = []
    for table in soup.find_all("table", class_="t-par-begin"):
        for row in table.find_all("tr", class_="t-par"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            # cppreference uses: name | - | description
            name = strip_html_to_text(cells[0])
            desc = strip_html_to_text(cells[2])
            if name:
                params.append(ParamInfo(name=name, description=desc))
    return params


# ---------------------------------------------------------------------------
# Page description
# ---------------------------------------------------------------------------

def extract_page_title(soup) -> str:
    """Extract the qualified name from the page title."""
    # Try the specific cppreference title span first
    title_span = soup.find("span", class_="mw-page-title-main")
    if title_span:
        return strip_html_to_text(title_span)
    # Fallback to <h1>
    h1 = soup.find("h1")
    if h1:
        return strip_html_to_text(h1)
    title_tag = soup.find("title")
    if title_tag:
        text = strip_html_to_text(title_tag)
        # Strip " - cppreference.com" suffix
        return re.sub(r"\s*[-–—]\s*cppreference\.com.*$", "", text)
    return ""


def extract_page_description(soup) -> tuple[str, str]:
    """Extract (brief, detailed) description from a cppreference page.

    Brief is the first content paragraph.  Detailed is subsequent paragraphs
    up to the first section header or declaration table.
    """
    # Find the main content area
    content = soup.find("div", class_="mw-parser-output")
    if content is None:
        content = soup.find("div", id="mw-content-text")
    if content is None:
        content = soup

    paragraphs: list[str] = []
    for child in content.children:
        tag_name = getattr(child, "name", None)
        if tag_name is None:
            continue
        # Stop at headers, declaration tables, or TOC
        if tag_name in ("h2", "h3", "h4"):
            break
        if tag_name == "table" and child.get("class"):
            classes = child["class"]
            if isinstance(classes, str):
                classes = classes.split()
            if "t-dcl-begin" in classes or "t-dsc-begin" in classes:
                break
        if tag_name == "div" and child.get("id") == "toc":
            break
        if tag_name == "p":
            text = strip_html_to_text(child)
            if text:
                paragraphs.append(text)

    brief = paragraphs[0] if paragraphs else ""
    detailed = " ".join(paragraphs[1:]) if len(paragraphs) > 1 else ""
    return brief, detailed


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------

def extract_base_classes(soup) -> list[str]:
    """Extract base class names from an inheritance section.

    cppreference shows base classes in an inheritance diagram — an SVG
    image with an HTML ``<map>`` containing ``<area>`` elements whose
    ``title`` attribute gives the base class refid path (e.g.
    ``cpp/error/exception``). We extract the last path segment as the
    base class name.
    """
    bases: list[str] = []
    # Primary source: <area> tags inside the inheritance diagram
    diagram_div = soup.find("div", class_="t-inheritance-diagram")
    if diagram_div:
        for area in diagram_div.find_all("area"):
            title = area.get("title", "")
            if title:
                # title is like "cpp/error/exception" — take last segment
                base_name = title.rsplit("/", 1)[-1]
                if base_name:
                    bases.append(base_name)
    # Fallback: t-dsc rows with "inherits from" / "derived from" text
    if not bases:
        for row in soup.find_all("tr", class_="t-dsc"):
            text = strip_html_to_text(row)
            if "inherits from" in text.lower() or "derived from" in text.lower():
                link = row.find("a")
                if link:
                    bases.append(strip_html_to_text(link))
    return bases


# ---------------------------------------------------------------------------
# Declaration signature parser
# ---------------------------------------------------------------------------

# Matches: optional_template return_type name(args) qualifiers
_FUNC_SIG_RE = re.compile(
    r"^"
    r"(?:template\s*<[^>]*>\s*)?"     # optional template<...>
    r"(.*?)"                           # return type (non-greedy)
    r"\b(\w+(?:::\w+)*)"              # function name (possibly qualified)
    r"\s*(\([^)]*\))"                 # argument list in parens
    r"(.*?)$",                         # trailing qualifiers (const, noexcept, etc.)
    re.DOTALL,
)

_PARAM_SPLIT_RE = re.compile(
    r",(?![^<>]*>)"  # split on commas not inside angle brackets
)

_PARAM_PARSE_RE = re.compile(
    r"^(.*?)\s+(\w+)\s*(?:=\s*(.*))?$"  # type name [= default]
)


def parse_declaration_signature(
    text: str,
) -> tuple[str, str, str, list[tuple[str, str, str]]]:
    """Parse a C++ declaration string into components.

    Returns:
        (return_type, name, argsstring, [(param_type, param_name, default), ...])
    """
    text = re.sub(r"\s+", " ", text).strip()
    # Remove trailing semicolons and version markers like (1) (since C++11)
    text = re.sub(r"\s*;\s*$", "", text)
    text = re.sub(r"\s*\(\d+\)\s*$", "", text)
    text = re.sub(r"\s*\((?:since|until|deprecated in)\s+C\+\+\d+\)\s*$", "", text)

    m = _FUNC_SIG_RE.match(text)
    if not m:
        # Could be a type alias, variable, or unparseable
        return ("", "", "", [])

    return_type = m.group(1).strip()
    name = m.group(2).strip()
    argsstring = m.group(3).strip()
    # qualifiers = m.group(4).strip()

    # Parse parameters from argsstring
    params: list[tuple[str, str, str]] = []
    inner = argsstring[1:-1].strip()  # strip parens
    if inner and inner != "void":
        for part in _PARAM_SPLIT_RE.split(inner):
            part = part.strip()
            if not part:
                continue
            pm = _PARAM_PARSE_RE.match(part)
            if pm:
                params.append((pm.group(1).strip(), pm.group(2), pm.group(3) or ""))
            else:
                # Couldn't split type/name — store whole thing as type
                params.append((part, "", ""))

    return (return_type, name, argsstring, params)
