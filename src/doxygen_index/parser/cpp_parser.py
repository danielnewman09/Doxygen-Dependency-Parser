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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import xml.etree.ElementTree as ET

from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, DefineNode,
    ImplementationNode, ParameterNode, FunctionNode,
    FileNode, NamespaceNode,
    SourceFragmentNode,
)
from codegraph.constants import normalize_language

from doxygen_index.parser.base import LanguageParser
from doxygen_index.parser.model import InheritsEntry, DependsOnEntry, CompositionEntry
from doxygen_index.parser.helpers import parse_index
from doxygen_index.parser.helpers import (
    get_text,
    parse_description,
    parse_location,
    parse_template_params,
)
from doxygen_index.parser.cpp_tests import (
    is_gtest_test_member,
    parse_gtest_test,
)
from doxygen_index.parser.model import (
    ParseResult,
    TemplateParamRef,
    SpecializesRef,
    ConceptConstraintEntry,
    CatchClauseEntry,
    InvokeEntry,
    ImplementationRef,
    IncludeEntry,
    CalleeEntry,
    VerifiesEntry,
)


# ---------------------------------------------------------------------------
# C++-specific utilities
# ---------------------------------------------------------------------------


def extract_include_guard(file_path: str | Path | None) -> str:
    """Return a header's conventional ``#ifndef``/``#define`` macro.

    Doxygen does not reliably retain include guards in its XML because they
    are preprocessing structure rather than semantic declarations. Reading
    this small, typed file attribute at the source boundary keeps codegen from
    inventing a path-derived guard during an as-built round trip.
    """
    if not file_path:
        return ""
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return ""
    ifndef: str | None = None
    for line in lines[:100]:
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = re.fullmatch(r"#\s*ifndef\s+([A-Za-z_]\w*)", stripped)
        if match:
            ifndef = match.group(1)
            continue
        if ifndef is not None:
            match = re.fullmatch(r"#\s*define\s+([A-Za-z_]\w*)", stripped)
            return ifndef if match and match.group(1) == ifndef else ""
        if not stripped.startswith(("/*", "*", "*/", "#pragma")):
            return ""
    return ""


def extract_include_directives(file_path: str | Path | None) -> list[str]:
    """Return ordered include operands and their blank-line groups.

    Empty strings represent a separator after an include group. This is source
    structure, not decoration: clang-format with ``IncludeBlocks: Preserve``
    retains it when canonicalizing generated files.
    """
    if not file_path:
        return []
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return []
    includes: list[str] = []
    pending_separator = False
    for line in lines:
        match = re.match(r"^\s*#\s*include\s*([<\"].*[>\"])\s*(?://.*)?$", line)
        if match:
            if pending_separator and includes:
                includes.append("")
            includes.append(match.group(1))
            pending_separator = False
        elif includes and not line.strip():
            pending_separator = True
        elif line.strip() and not line.lstrip().startswith("//"):
            pending_separator = False
    return includes


def extract_namespace_padding(file_path: str | Path | None) -> tuple[int, int]:
    """Return blank-line counts just inside a simple top-level namespace."""
    if not file_path:
        return 0, 0
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return 0, 0
    open_index = next(
        (index for index, line in enumerate(lines) if re.match(r"^\s*namespace\s+\w+\s*$", line)),
        None,
    )
    if open_index is None or open_index + 1 >= len(lines):
        return 0, 0
    brace_index = open_index + 1
    if lines[brace_index].strip() != "{":
        return 0, 0
    leading = 0
    for line in lines[brace_index + 1:]:
        if line.strip():
            break
        leading += 1
    close_index = next(
        (
            index for index in range(len(lines) - 1, brace_index, -1)
            if re.match(r"^\s*}\s*(?://\s*namespace.*)?$", lines[index])
        ),
        None,
    )
    if close_index is None:
        return leading, 0
    trailing = 0
    for line in reversed(lines[brace_index + 1:close_index]):
        if line.strip():
            break
        trailing += 1
    return leading, trailing


def extract_guard_padding(file_path: str | Path | None) -> int:
    """Return blank lines immediately preceding a conventional ``#endif``."""
    if not file_path:
        return 0
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return 0
    endif_index = next(
        (index for index in range(len(lines) - 1, -1, -1)
         if re.match(r"^\s*#\s*endif\b", lines[index])),
        None,
    )
    if endif_index is None:
        return 0
    padding = 0
    for line in reversed(lines[:endif_index]):
        if line.strip():
            break
        padding += 1
    return padding


# ---------------------------------------------------------------------------
# Source-span ownership — lossless residual subtraction
#
# After structured nodes are indexed, every remaining meaningful source span
# in a file must be represented either by a structured node or by a
# ``SourceFragmentNode``.  The pipeline below is deliberately generic: it has
# no macro-name, casing, or library-specific rules.  ``build_owned_spans``
# assembles the complete ordered ownership map for a file, and
# ``extract_residual_source_fragments`` subtracts that map from the raw
# source text and emits one fragment per remaining non-whitespace run.
# ---------------------------------------------------------------------------


@dataclass
class OwnedSpan:
    """An inclusive, 1-based source span claimed by a structured owner.

    Attributes:
        start: First line (1-based, inclusive).
        end: Last line (1-based, inclusive).
        owner: Qualified name of the owning entity (node qualified name or
            file path for boilerplate spans).
        owner_type: Node type name (``"ClassNode"``, ``"MethodNode"``, ...)
            or ``"FileNode"``/``"NamespaceNode"`` for boilerplate spans.
        kind: ``"node"`` for structured-node spans; ``"boilerplate"`` for
            includes/guards; ``"ns-open"``/``"ns-close"`` for namespace
            boundary lines.
    """

    start: int
    end: int
    owner: str = ""
    owner_type: str = ""
    kind: str = "node"

    def __post_init__(self) -> None:
        if self.end < self.start:
            self.end = self.start


_NS_OPEN_RE = re.compile(r"^\s*namespace\s+([\w:]+)\s*\{?\s*(?://.*)?$")


def _read_source_lines(file_path: str | Path | None) -> list[str] | None:
    """Return source lines (with endings) or None when unreadable."""
    if not file_path:
        return None
    try:
        return Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
    except (OSError, UnicodeError):
        return None


def _strip_for_scan(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Strip strings, char literals, ``//`` and ``/* */`` comments from a line.

    Block-comment state is threaded across lines so braces inside multi-line
    comments never skew span-end derivation.  Returns ``(clean, state)``.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if in_block_comment:
            if c == "*" and i + 1 < n and line[i + 1] == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if c == "/" and i + 1 < n and line[i + 1] == "*":
            in_block_comment = True
            i += 2
            continue
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out), in_block_comment


def _derive_span_end(lines: list[str], start_line: int) -> int:
    """Derive the inclusive end line of the construct opening at *start_line*.

    1-based.  Walks forward tracking brace/paren depth (ignoring strings and
    comments) until a line whose stripped content terminates the construct:
    a ``;`` or ``}`` at net zero depth, the end of a preprocessor block, or
    EOF.  Only returns when the boundary is unambiguous — an unterminated
    construct ends at EOF rather than guessing.
    """
    n = len(lines)
    if n == 0:
        return start_line
    start = max(1, start_line)
    if start - 1 >= n:
        return start_line
    first_clean, _state = _strip_for_scan(lines[start - 1], False)
    if first_clean.strip().startswith("#"):
        # Preprocessor line: owns its ``\`` continuations and nothing else.
        end = start
        while end < n and lines[end - 1].rstrip().endswith("\\"):
            end += 1
        return end
    paren = brace = 0
    in_block = False
    for idx in range(start - 1, n):
        clean, in_block = _strip_for_scan(lines[idx], in_block)
        stripped = clean.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue  # preprocessor lines open/close no source blocks
        paren += stripped.count("(") - stripped.count(")")
        brace += stripped.count("{") - stripped.count("}")
        if paren == 0 and brace == 0 and stripped.endswith((";", "}")):
            return idx + 1
    return n


def _doc_comment_span(
    lines: list[str], doc_text: str, decl_line: int
) -> tuple[int, int] | None:
    """Locate *doc_text* (byte-exact) in the source region above *decl_line*.

    Returns the inclusive 1-based line span of the comment block, or None
    when the text cannot be found unambiguously.

    The search runs *backward* from the declaration: when two declarations
    carry identical documentation, the comment immediately above the
    declaration is the one that belongs to it (``extract_preceding_doc_comment``
    only returns text found directly above, modulo blank/template lines), so
    an earlier unrelated copy of the same text is never selected.
    """
    if not doc_text:
        return None
    target = doc_text[:-1] if doc_text.endswith("\n") else doc_text
    if not target:
        return None
    prefix = "".join(lines[: decl_line - 1])
    offset = prefix.rfind(target)
    if offset < 0:
        return None
    start_line = prefix[:offset].count("\n") + 1
    end_line = start_line + target.count("\n")
    return start_line, end_line


def _template_prefix_start(lines: list[str], decl_line: int) -> int:
    """Extend an owned span start up to the first ``template<...>`` line.

    The template parameter list is part of the compound/member declaration;
    without owning it, the ``template<...>`` line would be re-emitted as a
    fragment alongside the rendered declaration.  Handles single- and
    multi-line template lists.  Returns the (1-based) start line.
    """
    start = decl_line
    j = decl_line - 2  # 0-based line above the declaration
    open_template = False
    while j >= 0:
        stripped = lines[j].strip()
        if not stripped:
            break
        is_template = open_template or stripped.startswith("template")
        if not is_template:
            break
        start = j + 1
        if not stripped.endswith(">"):
            open_template = True
            j -= 1
            continue
        # Completed template list — keep walking up only if the line above
        # is itself another template list (``template <...> template<...>``).
        j -= 1
        if j >= 0 and lines[j].strip().startswith("template"):
            open_template = True
            continue
        break
    return start


def _source_span(
    file_path: str | Path | None,
    decl_line: int | None,
    *,
    body_file: str = "",
    body_start: int = 0,
    body_end: int = 0,
    doc_comment: str = "",
) -> tuple[int, int] | None:
    """Return the inclusive (start_line, end_line) span a node owns.

    The span runs from the node's doc comment (or template prefix, or the
    declaration line) through its declaration end — or its body end when
    the body lives in the same file.  Returns None when the span cannot be
    determined unambiguously.
    """
    if not file_path or not decl_line:
        return None
    lines = _read_source_lines(file_path)
    if lines is None:
        return None
    if decl_line < 1 or decl_line > len(lines):
        return None
    start = decl_line
    if doc_comment:
        span = _doc_comment_span(lines, doc_comment, decl_line)
        if span is not None:
            start = span[0]
    else:
        template_start = _template_prefix_start(lines, decl_line)
        if template_start < start:
            start = template_start
    if body_end and body_file in ("", str(file_path)) and body_start:
        # A body may only extend the *contiguous* owned span.  In-class
        # bodies open immediately at/after the declaration (the closing
        # brace is already found by ``_derive_span_end``); an out-of-line
        # definition in the same file lives elsewhere and is owned
        # separately by ``build_owned_spans`` (body_file routing).  Gluing
        # a distant body onto the declaration span would swallow unrelated
        # code (e.g. ``withTransaction`` [406-471] crossing the enclosing
        # class end at 442).
        if body_start <= max(start, _derive_span_end(lines, start)) + 1:
            end = max(start, body_end)
        else:
            end = max(start, _derive_span_end(lines, start))
    else:
        end = max(start, _derive_span_end(lines, start))
    return start, end


def _file_boilerplate_spans(
    file_path: str | Path, lines: list[str]
) -> list[OwnedSpan]:
    """Structural boilerplate spans: includes, guard lines, namespace lines.

    These spans classify source lines by their preprocessor/namespace
    structure, never by identifier spelling.  Include directives and the
    include-guard lines are owned by the file; namespace open/close lines are
    owned by the namespace (by name from the source line).
    """
    spans: list[OwnedSpan] = []
    path = str(file_path)
    guard = extract_include_guard(file_path)
    endif_idx: int | None = None
    for idx, line in enumerate(lines):
        lineno = idx + 1
        stripped = line.strip()
        if re.match(r"^#\s*include\b", stripped):
            spans.append(OwnedSpan(
                lineno, lineno, owner=path, owner_type="FileNode",
                kind="boilerplate",
            ))
            continue
        if guard:
            match = re.match(r"^#\s*(ifndef|define)\s+([A-Za-z_]\w*)", stripped)
            if match and match.group(2) == guard:
                spans.append(OwnedSpan(
                    lineno, lineno, owner=path, owner_type="FileNode",
                    kind="boilerplate",
                ))
                continue
            if re.match(r"^#\s*endif\b", stripped):
                endif_idx = idx
    if guard and endif_idx is not None:
        spans.append(OwnedSpan(
            endif_idx + 1, endif_idx + 1, owner=path, owner_type="FileNode",
            kind="boilerplate",
        ))

    for open_idx, name in _namespace_open_lines(lines):
        spans.append(OwnedSpan(
            open_idx + 1, open_idx + 1, owner=name, owner_type="NamespaceNode",
            kind="ns-open",
        ))
        if open_idx + 1 < len(lines) and lines[open_idx + 1].strip() == "{":
            spans.append(OwnedSpan(
                open_idx + 2, open_idx + 2, owner=name, owner_type="NamespaceNode",
                kind="boilerplate",
            ))
        close_idx = _namespace_close_line(lines, open_idx)
        if close_idx is not None:
            spans.append(OwnedSpan(
                close_idx + 1, close_idx + 1, owner=name,
                owner_type="NamespaceNode", kind="ns-close",
            ))
    return spans


def _namespace_open_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(0-based index, name)`` for every ``namespace`` open line."""
    result: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _NS_OPEN_RE.match(line)
        if match:
            result.append((idx, match.group(1)))
    return result


def _top_level_namespace_name(file_path: str | Path | None) -> str:
    """Name of the first top-level namespace in *file_path* ("" if none).

    Used to preserve an otherwise-empty namespace shell (a ``.cpp`` whose
    namespace contains only blank lines) in as-built generation.
    """
    if not file_path:
        return ""
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return ""
    opens = _namespace_open_lines(lines)
    return opens[0][1] if opens else ""


def extract_leading_blank_lines(file_path: str | Path | None) -> int:
    """Blank lines before the first non-blank line of the file."""
    if not file_path:
        return 0
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return 0
    count = 0
    for line in lines:
        if line.strip():
            break
        count += 1
    return count


def extract_namespace_regions(
    file_path: str | Path | None,
) -> list[dict]:
    """Ordered top-level namespace regions with their blank layout.

    Each region: ``{name, open_line, close_line, leading_blank_lines,
    trailing_blank_lines}`` (1-based inclusive lines).  Blank lines directly
    inside a region's braces belong to the region (they are excluded from
    residuals by the ownership model, so the region must render them).
    """
    if not file_path:
        return []
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return []
    regions: list[dict] = []
    for open_idx, name in _namespace_open_lines(lines):
        close_idx = _namespace_close_line(lines, open_idx)
        open_line = open_idx + 1
        brace_line = open_idx + 2 if (
            open_idx + 1 < len(lines) and lines[open_idx + 1].strip() == "{"
        ) else open_line + 1
        close_line = close_idx + 1 if close_idx is not None else len(lines)
        leading = 0
        for line in lines[brace_line: close_line - 1]:
            if line.strip():
                break
            leading += 1
        trailing = 0
        for line in reversed(lines[brace_line: close_line - 1]):
            if line.strip():
                break
            trailing += 1
        regions.append({
            "name": name,
            "open_line": open_line,
            "close_line": close_line,
            "leading_blank_lines": leading,
            "trailing_blank_lines": trailing,
        })
    return regions


def extract_include_directive_lines(file_path: str | Path | None) -> list[int]:
    """1-based source lines parallel to ``extract_include_directives``.

    A 0 entry marks a separator (blank-line group boundary).  Only include
    directives that appear in the directive list are recorded; a directive
    whose spelling appears multiple times keeps each occurrence's line.
    """
    if not file_path:
        return []
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except (OSError, UnicodeError):
        return []
    spellings: list[str] = []
    include_lines: list[int] = []
    for idx, line in enumerate(lines):
        match = re.match(r"^\s*#\s*include\s*([<\"].*[>\"])\s*(?://.*)?$", line)
        if match:
            spellings.append(match.group(1))
            include_lines.append(idx + 1)
    # ``extract_include_directives`` inserts ``""`` separators; mirror them.
    out: list[int] = []
    pending_separator = False
    for idx, line in enumerate(lines):
        match = re.match(r"^\s*#\s*include\s*([<\"].*[>\"])\s*(?://.*)?$", line)
        if match:
            if pending_separator and out:
                out.append(0)
            out.append(idx + 1)
            pending_separator = False
        elif out and not line.strip():
            pending_separator = True
        elif line.strip() and not line.lstrip().startswith("//"):
            pending_separator = False
    return out


def _namespace_close_line(lines: list[str], open_idx: int) -> int | None:
    """Brace-match from a namespace open line to its closing ``}`` line.

    Returns the 0-based index of the close line, or None when the braces do
    not balance (source error or exotic construct).
    """
    depth = 0
    in_block = False
    for idx in range(open_idx, len(lines)):
        raw = lines[idx]
        clean, in_block = _strip_for_scan(raw, in_block)
        opens = clean.count("{")
        closes = clean.count("}")
        if idx == open_idx and not opens:
            # ``namespace x`` on its own line; the ``{`` is on the next line.
            continue
        depth += opens - closes
        if depth <= 0 and closes:
            return idx
    return None


def _namespace_ranges(spans: list[OwnedSpan]) -> list[tuple[int, int, str]]:
    """Pair ns-open/ns-close boundary spans into ``(open, close, name)``.

    Names are fully qualified: for traditional nested declarations
    (``namespace outer { namespace inner { ... } }``) the stack tracks the
    enclosing qualified names so a fragment inside ``inner`` receives
    ``outer::inner``, not just ``inner``.  C++17 qualified syntax
    (``namespace outer::inner { ... }``) is already qualified by the source
    scan; stacking it under an enclosing namespace prepends that scope
    correctly.  Sibling namespaces stay independent because closes pair with
    opens in LIFO source order.
    """
    markers = sorted(
        (s.start, s.kind, s.owner)
        for s in spans
        if s.owner_type == "NamespaceNode" and s.kind in ("ns-open", "ns-close")
    )
    ranges: list[tuple[int, int, str]] = []
    stack: list[tuple[int, str]] = []  # (open_line, qualified_name)
    for line, kind, name in markers:
        if kind == "ns-open":
            parent = stack[-1][1] if stack else ""
            qualified = f"{parent}::{name}" if parent else name
            stack.append((line, qualified))
        elif stack:
            open_line, qualified = stack.pop()
            ranges.append((open_line, line, qualified))
    return ranges


def _placement_for(ranges: list[tuple[int, int, str]], line: int) -> str:
    """Innermost namespace range containing *line*, else ""."""
    best: tuple[int, str] | None = None
    for open_line, close_line, name in ranges:
        if open_line <= line <= close_line:
            if best is None or open_line > best[0]:
                best = (open_line, name)
    return best[1] if best else ""


def _legacy_placement(lines: list[str], start_0based: int) -> str:
    """Fallback placement from source lines (namespace open above the run)."""
    namespace = ""
    for prefix in lines[:start_0based]:
        match = re.match(r"^\s*namespace\s+([\w:]+)\s*$", prefix)
        if match:
            namespace = match.group(1)
    return namespace


def build_owned_spans(
    file_path: str | Path | None,
    result: ParseResult,
) -> tuple[list[OwnedSpan], list[str]]:
    """Return the complete ordered source-ownership map for *file_path*.

    The map includes:

    * structured node spans for that file — compounds, members, and the
      bodies of members whose implementation lives in this file;
    * file boilerplate spans — include directives, include-guard lines, and
      namespace open/closing lines;

    Identical and nested spans are merged safely; overlapping non-nested
    spans are REPORTED (second return value) rather than silently resolved.
    No identifier-spelling or macro-name heuristics are used.
    """
    spans: list[OwnedSpan] = []
    lines = _read_source_lines(file_path)
    if lines is not None:
        spans.extend(_file_boilerplate_spans(file_path, lines))

    path = str(file_path) if file_path else ""
    compound_nodes = (
        result.classes + result.enums + result.unions
        + result.interfaces + result.concepts
    )
    member_nodes = (
        result.methods + result.attributes + result.enum_values
        + result.defines + result.functions
    )
    for node in compound_nodes + member_nodes:
        qualified_name = getattr(node, "qualified_name", "") or ""
        node_type = type(node).__name__
        if getattr(node, "file_path", "") == path:
            start = int(getattr(node, "start_line", 0) or 0)
            end = int(getattr(node, "end_line", 0) or 0)
            if start and end:
                spans.append(OwnedSpan(
                    start, end, owner=qualified_name, owner_type=node_type,
                ))
        # Implementation body declared elsewhere, defined in this file.
        body_file = getattr(node, "body_file", "") or ""
        body_start = int(getattr(node, "body_start", 0) or 0)
        body_end = int(getattr(node, "body_end", 0) or 0)
        if body_file == path and body_start and body_end:
            spans.append(OwnedSpan(
                body_start, body_end, owner=qualified_name, owner_type=node_type,
            ))
    return _normalize_ownership(spans)


def _normalize_ownership(
    spans: list[OwnedSpan],
) -> tuple[list[OwnedSpan], list[str]]:
    """Sort, merge identical spans, and report overlapping non-nested spans.

    Uses an interval sweep: each span is checked against every still-active
    prior span (``end >= start``), not just its sorted neighbour, so a
    nested span can never hide a crossing overlap between two non-adjacent
    spans.  Identical spans merge; fully nested spans are allowed silently;
    crossing (partially overlapping, non-nested) spans are reported with
    both owners and ranges.
    """
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    merged: list[OwnedSpan] = []
    for span in ordered:
        if merged and merged[-1].start == span.start and merged[-1].end == span.end:
            continue
        merged.append(span)
    problems: list[str] = []
    active: list[OwnedSpan] = []  # prior spans still overlapping the sweep point
    for span in merged:
        active = [prev for prev in active if prev.end >= span.start]
        for prev in active:
            if prev.end >= span.end:
                continue  # prev fully contains span — nested, safe
            problems.append(
                f"{span.owner_type} '{span.owner}' [{span.start}-{span.end}] overlaps "
                f"{prev.owner_type} '{prev.owner}' [{prev.start}-{prev.end}]"
            )
        active.append(span)
    return merged, problems


def extract_residual_source_fragments(
    file_path: str | Path | None,
    owned_spans: list[tuple[int, int] | OwnedSpan],
    *,
    source: str = "",
    layer: str = "as-built",
) -> list[SourceFragmentNode]:
    """Return source spans not claimed by any structured owner.

    ``owned_spans`` is the complete ownership map (from
    :func:`build_owned_spans`) — 1-based inclusive spans, either as
    ``(start, end)`` tuples or :class:`OwnedSpan` instances.  The map's spans
    are subtracted from the raw source text and the remaining runs are
    coalesced (attaching adjacent plain comments and whitespace) into one
    :class:`SourceFragmentNode` per non-whitespace span.

    This is pure subtraction: it never inspects macro names, casing, or
    library families.  Fragments are emitted in source order.
    """
    lines = _read_source_lines(file_path)
    if lines is None:
        return []
    spans = [
        s if isinstance(s, OwnedSpan) else OwnedSpan(start=s[0], end=s[1])
        for s in owned_spans
    ]
    owned: set[int] = set()
    for span in spans:
        for line in range(max(1, span.start), span.end + 1):
            owned.add(line)
    ns_ranges = _namespace_ranges(spans)
    total = len(lines)

    # Namespace boundary blank lines are owned by the FileNode layout
    # metadata (``namespace_leading/trailing_blank_lines``), never by a
    # residual: a fragment glued across a namespace open/close would
    # double-count the same blank lines at render time.  Blank lines
    # directly before a closing ``#endif`` likewise belong to
    # ``guard_leading_blank_lines``.
    layout_boundary_blanks: set[int] = set()
    for span in spans:
        if getattr(span, "owner_type", "") != "NamespaceNode":
            continue
        if span.kind == "ns-open":
            idx = span.start + 1  # 1-based line after the open line
            if idx <= total and lines[idx - 1].strip() == "{":
                idx += 1  # brace on its own line is owned boilerplate
            while idx <= total and not lines[idx - 1].strip():
                layout_boundary_blanks.add(idx)
                idx += 1
        elif span.kind == "ns-close":
            idx = span.start - 1  # 1-based line before the close line
            while idx >= 1 and not lines[idx - 1].strip():
                layout_boundary_blanks.add(idx)
                idx -= 1
    for idx in range(total - 1, -1, -1):
        if re.match(r"^\s*#\s*endif\b", lines[idx]):
            probe = idx - 1
            while probe >= 0 and not lines[probe].strip():
                layout_boundary_blanks.add(probe + 1)
                probe -= 1
            break

    fragments: list[SourceFragmentNode] = []
    index = 0
    while index < total:
        line_number = index + 1
        if line_number in owned:
            index += 1
            continue
        # Start of an unowned run: extend upward over adjacent plain ``//``
        # comments, then at most one blank line (never into a namespace
        # boundary blank owned by the FileNode layout metadata).
        start = index
        while start > 0:
            lineno = start  # 1-based line number of lines[start - 1]
            if lineno in owned:
                break
            if not lines[start - 1].lstrip().startswith("//"):
                break
            start -= 1
        if start > 0:
            lineno = start
            if (
                lineno not in owned
                and lineno not in layout_boundary_blanks
                and not lines[start - 1].strip()
            ):
                start -= 1
        # Extend downward over every unowned line (stopping before a
        # namespace-boundary blank owned by the FileNode metadata).
        end = start
        while end < total and (end + 1) not in owned:
            if (end + 1) in layout_boundary_blanks:
                break
            end += 1
        body = "".join(lines[start:end])
        if body.strip():
            placement = ""
            if ns_ranges:
                # File-level runs between re-opened namespace regions must
                # stay file-level — only ranges the parser actually paired
                # may claim a run.  ``_legacy_placement`` is a last-resort
                # fallback for sources where range pairing produced nothing.
                placement = _placement_for(ns_ranges, start + 1)
            else:
                placement = _legacy_placement(lines, start)
            fragments.append(SourceFragmentNode(
                qualified_name=f"{file_path}#{start + 1}-{end}",
                file_path=str(file_path),
                start_line=start + 1,
                end_line=end,
                placement=placement,
                text=body,
                source=source,
                layer=layer,
                tags=[layer],
            ))
        # Always advance past the current run — a run of nothing but
        # namespace-boundary blanks (owned by FileNode layout metadata) must
        # not pin ``index`` forever.
        index = max(end, index + 1)
    return fragments

def extract_preceding_doc_comment(
    file_path: str | Path | None, line_number: int | None
) -> str:
    """Return the contiguous comment block directly before a declaration.

    Accepts Doxygen blocks (``/*!``/``/**``/``///``/``//!``) and plain
    single-line ``//`` comments.  Only a comment directly above the
    declaration (modulo blank/template lines) is attached — a floating
    comment separated by code stays unowned and becomes a residual.
    """
    if not file_path or not line_number or line_number < 2:
        return ""
    try:
        lines = Path(file_path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
    except (OSError, UnicodeError):
        return ""
    index = line_number - 2
    # Skip template prefixes only — a comment must be *directly* above the
    # declaration (or its template prefix) to count as attached.
    while index >= 0 and lines[index].lstrip().startswith("template"):
        index -= 1
    if index < 0:
        return ""
    stripped = lines[index].lstrip()
    if not stripped:
        # Blank line between the comment and the declaration: only an
        # explicit Doxygen marker (``/*!``/``/**``/``///``/``//!``) may span
        # it.  A plain ``//`` section header separated by a blank is an
        # ordinary comment — it stays a residual, never a doc comment.
        probe = index
        while probe >= 0 and not lines[probe].strip():
            probe -= 1
        if probe < 0 or not lines[probe].lstrip().startswith(
            ("/*!", "/**", "///", "//!")
        ):
            return ""
        index = probe
        stripped = lines[index].lstrip()
    if not stripped.startswith(("*/", "/*!", "/**", "///", "//!", "//")):
        return ""
    end = index
    if stripped.startswith("*/"):
        while index >= 0 and "/*!" not in lines[index] and "/**" not in lines[index]:
            index -= 1
        if index < 0:
            return ""
    elif stripped.startswith(("///", "//!", "//")):
        while index >= 0 and lines[index].lstrip().startswith("//"):
            index -= 1
        index += 1
    return "".join(lines[index:end + 1])


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


def _read_body(body_file: str | None, body_start: int | None, body_end: int | None) -> str:
    """Extract a member's implementation body text from its source file.

    Doxygen reports the body's line range (``bodystart``/``bodyend`` on
    ``<location>`` — 1-indexed, inclusive, and including the signature
    line for out-of-line definitions).  The body is read from the
    source file so the implementation survives into the graph as
    structured data (the codegen raw material for out-of-line and
    inline definitions).

    Returns ``""`` when there is no body or the file cannot be read.
    """
    if not body_file or not body_start or not body_end:
        return ""
    try:
        lines = Path(body_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if body_start < 1 or body_end > len(lines):
        return ""
    return "\n".join(lines[body_start - 1: body_end])


def _declaration_span(file_path: str | Path | None, line_number: int | None) -> str:
    """Return the verbatim source declaration starting at *line_number*.

    Walks from the declaration line until a ``;`` at net-zero brace depth
    (or EOF).  Used for attributes whose specifiers doxygen drops (e.g.
    ``inline`` on a static member) — the source spelling is the only
    faithful record for as-built generation.
    """
    if not file_path or not line_number:
        return ""
    lines = _read_source_lines(file_path)
    if lines is None or line_number < 1 or line_number > len(lines):
        return ""
    depth = 0
    in_block = False
    out: list[str] = []
    for idx in range(line_number - 1, len(lines)):
        raw = lines[idx]
        clean, in_block = _strip_for_scan(raw, in_block)
        out.append(raw)
        stripped = clean.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0 and stripped.endswith(";"):
            break
    return "".join(out)


def _source_base_specifiers(
    file_path: str | Path | None, line_number: int | None
) -> list[str]:
    """Source-spelled base list of a class/struct declaration.

    Doxygen's ``<basecompoundref>`` list for a template *specialization*
    includes bases inherited from the primary template (e.g. ``IsVector<
    std::vector<T, Allocator>>`` reports both ``std::false_type`` and
    ``std::true_type``) with no attribute distinguishing them.  The source
    declaration is authoritative: the bases written after the top-level
    ``:`` and before the body ``{`` are the faithful list.

    Returns the raw comma-separated base spellings (access/virtual
    qualifiers included), or ``[]`` when the declaration has no bases or
    the span cannot be read unambiguously.
    """
    if not file_path or not line_number:
        return []
    lines = _read_source_lines(file_path)
    if lines is None or line_number < 1 or line_number > len(lines):
        return []
    parts: list[str] = []
    angle = 0
    paren = 0
    seen_colon = False
    for idx in range(line_number - 1, len(lines)):
        clean, _in_block = _strip_for_scan(lines[idx], False)
        stripped = clean.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Skip template prefixes (``template <...>`` on their own lines).
        if not seen_colon and stripped.startswith("template"):
            continue
        i = 0
        n = len(stripped)
        while i < n:
            ch = stripped[i]
            if ch == "<":
                angle += 1
            elif ch == ">":
                angle = max(0, angle - 1)
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren = max(0, paren - 1)
            elif (
                ch == ":" and angle == 0 and paren == 0
                and i + 1 < n and stripped[i + 1] != ":"
            ):
                seen_colon = True
                rest = stripped[i + 1:]
                if "{" in rest:
                    rest = rest.split("{", 1)[0]
                if rest.strip():
                    parts.append(rest.strip())
                return _split_base_specifiers("".join(parts))
            elif ch == "{" and angle == 0 and paren == 0:
                # Body opened before any colon — no bases.
                return _split_base_specifiers("".join(parts))
            i += 1
        if seen_colon:
            # Continue accumulating continuation lines until the ``{``.
            if "{" in stripped:
                return _split_base_specifiers("".join(parts))
            parts.append(" ")
    return []


def _split_base_specifiers(text: str) -> list[str]:
    """Split a base-clause string on top-level commas (template args safe)."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [b for b in out if b]


def _source_initializer(
    file_path: str | Path | None, line_number: int | None
) -> str:
    """Return the verbatim source definition of a concept/alias declaration.

    Walks from *line_number* (the declaration line — e.g. ``concept X = ...``
    or ``using Y = ...``) through the terminating ``;`` at net-zero brace
    depth.  Doxygen collapses multiline requires-blocks and embedded
    comments; the source spelling is the faithful record for as-built
    generation.  Returns "" when the span cannot be read.
    """
    if not file_path or not line_number:
        return ""
    lines = _read_source_lines(file_path)
    if lines is None or line_number < 1 or line_number > len(lines):
        return ""
    depth = 0
    paren = 0
    in_block = False
    out: list[str] = []
    for idx in range(line_number - 1, len(lines)):
        raw = lines[idx]
        clean, in_block = _strip_for_scan(raw, in_block)
        out.append(raw)
        stripped = clean.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth += stripped.count("{") - stripped.count("}")
        paren += stripped.count("(") - stripped.count(")")
        if depth <= 0 and paren <= 0 and stripped.endswith(";"):
            break
    return "".join(out)


def _is_under_test_dir(loc_file: str | None, test_dirs: list[Path] | None) -> bool:
    """Return True if *loc_file* lives under one of the resolved test dirs.

    Doxygen records ``location file`` attributes relative to its run
    directory (or absolute); both forms are checked against the
    (absolute) test dirs.
    """
    if not loc_file or not test_dirs:
        return False
    try:
        p = Path(loc_file).resolve()
    except OSError:
        return False
    return any(
        td in p.parents or p == td for td in test_dirs
    )


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
    # Ordered source declarations for member template parameters
    # (``template <ValidTransferObject T>`` above an in-class method).
    template_declarations = [
        " ".join(
            part for part in (parameter.type_constraint, parameter.declname)
            if part
        ) + (f" = {parameter.defval}" if parameter.defval else "")
        for parameter in parse_template_params(
            memberdef.find("templateparamlist")
        )
    ]
    # Explicit value expression for enum values (e.g. ``1 << 3``).
    # Captured raw from the ``<initializer>`` element so the value is
    # preserved for round-trip code generation; rendering (e.g. whether
    # a leading ``=`` is emitted) is a template concern.
    initializer = memberdef.findtext("initializer", "")

    loc = memberdef.find("location")
    file_path, line_number, body_start, body_end = parse_location(loc)
    body_file = loc.get("bodyfile") if loc is not None else None

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
        "initializer": initializer,
        "template_declarations": template_declarations,
        "body": _read_body(body_file, body_start, body_end),
        "body_file": body_file or "",
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
        test_source_dirs: list[Path] | None = None,
    ) -> None:
        """Parse all Doxygen XML in *source_dir* and populate *result*.

        *source_dir* must contain a ``index.xml`` file produced by
        Doxygen.  Each compound XML file is parsed to extract classes,
        functions, etc.

        ``test_source_dirs`` marks the project's test directories (from
        ``test_paths`` in ``.doxygen-index.toml``): compounds defined in
        those files contribute test nodes only — their non-test symbols
        are skipped so test scaffolding never pollutes the project API
        graph.
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

        # Resolve test dirs once so per-file membership checks are cheap.
        test_dirs = [Path(d).resolve() for d in (test_source_dirs or [])]

        # Parse in parallel — each file is independent and list.append
        # is GIL-atomic in CPython, so concurrent mutation of *result*
        # is safe.
        max_workers = min(32, (len(xml_files) // 10) + 1)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    self.parse_compound_file, f, source, result, layer,
                    test_dirs,
                ): f
                for f in xml_files
            }
            completed = 0
            for future in as_completed(futures):
                completed += 1
                if progress_interval and completed % progress_interval == 0:
                    print(f"  Parsed {completed}/{total} XML files...")
                # Re-raise any exceptions from workers
                future.result()
        self._extract_residual_fragments(result, source=source, layer=layer)

    @staticmethod
    def _extract_residual_fragments(
        result: ParseResult,
        *,
        source: str,
        layer: str,
    ) -> None:
        """Populate generic residuals after all Doxygen-owned spans are known.

        For every indexed file, the complete source-ownership map
        (:func:`build_owned_spans`) is subtracted from the raw source text;
        every remaining meaningful span becomes a :class:`SourceFragmentNode`.
        """
        for file_node in result.files:
            path = file_node.path
            if not path:
                continue
            spans, problems = build_owned_spans(path, result)
            for problem in problems:
                print(
                    f"  Warning: ownership overlap in {path}: {problem}",
                    file=sys.stderr,
                )
            result.source_fragments.extend(extract_residual_source_fragments(
                path, spans, source=source, layer=layer,
            ))

    # ------------------------------------------------------------------
    # Doxygen XML compound file parsing
    # ------------------------------------------------------------------

    def parse_compound_file(
        self,
        xml_path: Path,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
        test_dirs: list[Path] | None = None,
    ) -> None:
        """Parse a single Doxygen compound XML file.

        For each ``<compounddef>``:
        * ``file`` and ``namespace`` kinds are handled directly
          (language-agnostic).
        * All other kinds are delegated to :meth:`parse_compound`.
          If it returns a qualified name, members within
          ``<sectiondef>`` elements are then processed via
          :meth:`parse_member`.

        Compounds defined in a test directory (``test_dirs``) contribute
        test nodes only: gtest macros become TestNode elements and the
        file/FileNode is kept for DEFINED_IN edges, but the file's
        non-test symbols (test-local structs, helper functions, macro
        noise) are skipped entirely.
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

            # Whether this compound is defined in a test source file.
            loc = compounddef.find("location")
            loc_file = loc.get("file") if loc is not None else None
            is_test_file = _is_under_test_dir(loc_file, test_dirs)

            # --- Files (language-agnostic) ---
            if kind == "file":
                loc = compounddef.find("location")
                file_path = loc.get("file") if loc is not None else None
                language = normalize_language(compounddef.get("language", ""))
                namespace_leading, namespace_trailing = extract_namespace_padding(file_path)
                file_lines = _read_source_lines(file_path)
                namespace_name = _top_level_namespace_name(file_path)
                result.files.append(FileNode(
                    refid=refid, name=compoundname,
                    path=file_path or "", language=language, source=source,
                    include_guard=extract_include_guard(file_path),
                    include_directives=extract_include_directives(file_path),
                    include_directive_lines=extract_include_directive_lines(file_path),
                    leading_blank_lines=extract_leading_blank_lines(file_path),
                    namespace_regions=extract_namespace_regions(file_path),
                    namespace_leading_blank_lines=namespace_leading,
                    namespace_trailing_blank_lines=namespace_trailing,
                    namespace_name=namespace_name,
                    guard_leading_blank_lines=extract_guard_padding(file_path),
                    start_line=1 if file_lines else 0,
                    end_line=len(file_lines) if file_lines else 0,
                ))
                for inc in compounddef.findall("includes"):
                    result.includes.append(IncludeEntry(
                        file_refid=refid,
                        included_file=inc.text or "",
                        included_refid=inc.get("refid") or "",
                        is_local=inc.get("local") == "yes",
                    ))
                # Parse file-level members: typedefs, functions, variables.
                # Test files contribute only their gtest TestNodes —
                # everything else (helper functions, BOOST_DESCRIBE_STRUCT
                # macro noise, test-local variables) is skipped.
                for sectiondef in compounddef.findall("sectiondef"):
                    for memberdef in sectiondef.findall("memberdef"):
                        fields = _extract_common_member_fields(memberdef)
                        member_kind = memberdef.get("kind", "")
                        if is_test_file and member_kind != "function":
                            continue
                        if member_kind in ("typedef", "variable"):
                            self._parse_variable_member(
                                memberdef, fields, refid, "", source, result, layer)
                        elif member_kind == "function":
                            if is_gtest_test_member(memberdef, fields):
                                # GoogleTest TEST_F/TEST/TEST_P macros —
                                # Doxygen records them as file-level
                                # functions named after the macro.  Convert
                                # to TestNode + assertions/steps.
                                file_stem = Path(
                                    fields.get("file_path") or ""
                                ).stem
                                parse_gtest_test(
                                    memberdef, fields, file_stem,
                                    source, result, layer,
                                )
                            elif not is_test_file:
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
                # Skip catch-clause scanning for test files — test-local
                # exception usage is not part of the project's dependency
                # surface.
                if not is_test_file:
                    _scan_file_catch_clauses(compounddef, result)
                continue

            # --- Namespaces (language-agnostic) ---
            if kind == "namespace":
                if is_test_file:
                    continue  # test-local namespaces (e.g. my_app) are skipped
                name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
                ns_loc = compounddef.find("location")
                ns_file = ns_loc.get("file") if ns_loc is not None else None
                ns_line = (
                    int(ns_loc.get("line") or 0)
                    if ns_loc is not None else 0
                )
                ns_span = _source_span(ns_file, ns_line) if ns_line else None
                result.namespaces.append(NamespaceNode(
                    refid=refid, name=name,
                    qualified_name=compoundname, source=source, layer=layer,
                    file_path=ns_file or "",
                    start_line=ns_span[0] if ns_span else 0,
                    end_line=ns_span[1] if ns_span else 0,
                ))
                continue

            # --- Language-specific type compound ---
            if is_test_file:
                # Test-local structs/classes (Vertex3D, RigidBody, the
                # DatabaseTest gtest fixture, ...) stay out of the graph.
                continue

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
        template_parameters = parse_template_params(
            compounddef.find("templateparamlist")
        )
        for idx, parameter in enumerate(template_parameters):
            result.template_param_refs.append(TemplateParamRef(
                from_refid=refid,
                position=idx,
                type_constraint=parameter.type_constraint,
                declname=parameter.declname,
                defname=parameter.defname,
                defval=parameter.defval,
            ))
        fields["template_declarations"] = [
            " ".join(
                part for part in (parameter.type_constraint, parameter.declname)
                if part
            ) + (f" = {parameter.defval}" if parameter.defval else "")
            for parameter in template_parameters
        ]

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
        base_specifiers = _source_base_specifiers(
            fields["file_path"], fields["line_number"]
        )
        is_struct = fields["kind"] == "struct"
        for baseref in compounddef.findall("basecompoundref"):
            base_name = (baseref.text or "").strip()
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
        if not base_specifiers and base_classes:
            # Fallback: derive source spelling from the XML access/virtual
            # qualifiers (a struct's default public access is unspelled).
            for baseref in compounddef.findall("basecompoundref"):
                base_name = (baseref.text or "").strip()
                prot = baseref.get("prot", "public")
                virt = baseref.get("virt") == "virtual"
                parts: list[str] = []
                if virt:
                    parts.append("virtual")
                if prot != "public" or not is_struct:
                    parts.append(prot)
                base_specifiers.append(
                    " ".join(parts + [base_name]) if parts else base_name
                )

        is_final = compounddef.get("final") == "yes"
        is_abstract = compounddef.get("abstract") == "yes"

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            doc_comment=source_documentation,
        )

        result.classes.append(ClassNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            definition=fields["definition"],
            template_declarations=fields.get("template_declarations", []),
            module=fields["module"],
            base_classes=base_classes,
            base_specifiers=base_specifiers,
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
        # The Doxygen initializer collapses multiline requires-blocks and
        # embedded comments onto one line.  The source is authoritative:
        # capture the verbatim definition (``concept X = ...;``) from the
        # declaration file so as-built generation reproduces it exactly.
        source_init = _source_initializer(fields["file_path"], fields["line_number"])
        if source_init:
            initializer = source_init

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            doc_comment=source_documentation,
        )
        result.concepts.append(ConceptNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            template_declarations=fields.get("template_declarations", []),
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
        span = _source_span(fields["file_path"], fields["line_number"])
        result.enums.append(EnumNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
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
        span = _source_span(fields["file_path"], fields["line_number"])
        result.unions.append(UnionNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
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
        span = _source_span(fields["file_path"], fields["line_number"])
        result.interfaces.append(InterfaceNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=fields["name"],
            qualified_name=fields["qualified_name"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
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
        "enum": "_parse_enum_member",
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
        is_nodiscard = memberdef.get("nodiscard") == "yes"

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            body_file=fields.get("body_file", "") or "",
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            doc_comment=source_documentation,
        )

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
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            body=fields.get("body", "") or "",
            body_file=fields.get("body_file", "") or "",
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            template_declarations=fields.get("template_declarations", []),
            protection=fields["prot"],
            visibility=fields["prot"],
            is_static=is_static,
            is_const=is_const,
            is_constexpr=is_constexpr,
            is_virtual=is_virtual,
            is_inline=is_inline,
            is_explicit=is_explicit,
            is_nodiscard=is_nodiscard,
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
        is_constexpr = memberdef.get("constexpr") == "yes"
        is_nodiscard = memberdef.get("nodiscard") == "yes"

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            body_file=fields.get("body_file", "") or "",
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            doc_comment=source_documentation,
        )

        result.attributes.append(AttributeNode(
            refid=fields["refid"],
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            type_signature=fields["type_str"],
            initializer=fields.get("initializer") or "",
            declaration=_declaration_span(
                fields["file_path"], fields["line_number"]
            ),
            definition=fields["definition"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            protection=fields["prot"],
            visibility=fields["prot"],
            is_static=is_static,
            is_const=is_const,
            is_constexpr=is_constexpr,
            is_nodiscard=is_nodiscard,
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

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            body_file=fields.get("body_file", "") or "",
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            doc_comment=source_documentation,
        )

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
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            body=fields.get("body", "") or "",
            body_file=fields.get("body_file", "") or "",
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            protection=fields["prot"],
            visibility=fields["prot"],
            source=source,
            layer=layer,
            tags=[layer],
        ))

    @staticmethod
    def _parse_enum_member(
        memberdef: ET.Element,
        fields: dict,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Handle a nested enum declared inside a class/struct.

        Doxygen records nested enums as ``<memberdef kind="enum">`` inside
        the owning compound's sectiondef.  The enum is composed by the
        owning compound (not a namespace), keeps source order/visibility,
        and carries its underlying type (``: uint8_t``) when present.
        """
        name = fields["name"]
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        refid = fields["refid"]

        source_documentation = extract_preceding_doc_comment(
            fields["file_path"], fields["line_number"]
        )
        span = _source_span(
            fields["file_path"], fields["line_number"],
            body_file=fields.get("body_file", "") or "",
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            doc_comment=source_documentation,
        )

        result.enums.append(EnumNode(
            refid=refid,
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            underlying_type=fields.get("type_str") or "",
            enum_class=memberdef.get("strong") == "yes",
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            brief_description=fields["brief"],
            detailed_description=fields["detailed"],
            source_documentation=source_documentation,
            definition=fields["definition"],
            protection=fields["prot"],
            visibility=fields["prot"],
            module=derive_module(qname),
            source=source,
            source_type=fields["source_type"],
            layer=layer,
            tags=[layer],
        ))

        # Enumerators are composed by the nested enum itself.
        for value_el in memberdef.findall("enumvalue"):
            value_name = value_el.findtext("name", "") or ""
            if not value_name:
                continue
            value_fields = _extract_common_member_fields(value_el)
            value_span = _source_span(
                value_fields["file_path"], value_fields["line_number"],
                doc_comment=extract_preceding_doc_comment(
                    value_fields["file_path"], value_fields["line_number"]
                ),
            )
            result.enum_values.append(EnumValueNode(
                refid=value_fields["refid"],
                compound_refid=refid,
                kind=value_fields["kind"],
                name=value_name,
                qualified_name=f"{qname}::{value_name}",
                file_path=value_fields["file_path"] or "",
                line_number=value_fields["line_number"],
                start_line=value_span[0] if value_span else 0,
                end_line=value_span[1] if value_span else 0,
                initializer=value_fields.get("initializer") or "",
                brief_description=value_fields["brief"],
                detailed_description=value_fields["detailed"],
                protection=value_fields["prot"],
                visibility=value_fields["prot"],
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

        span = _source_span(
            fields["file_path"], fields["line_number"],
            doc_comment=extract_preceding_doc_comment(
                fields["file_path"], fields["line_number"]
            ),
        )

        result.enum_values.append(EnumValueNode(
            refid=fields["refid"],
            compound_refid=compound_refid,
            kind=fields["kind"],
            name=name,
            qualified_name=qname,
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            initializer=fields.get("initializer") or "",
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

        span = _source_span(
            fields["file_path"], fields["line_number"],
            body_file=fields.get("body_file", "") or "",
            body_start=fields["body_start"] or 0,
            body_end=fields["body_end"] or 0,
            doc_comment=extract_preceding_doc_comment(
                fields["file_path"], fields["line_number"]
            ),
        )

        result.defines.append(DefineNode(
            refid=fields["refid"],
            kind=fields["kind"],
            name=name,
            qualified_name=name,
            definition=fields["definition"],
            file_path=fields["file_path"] or "",
            line_number=fields["line_number"],
            start_line=span[0] if span else 0,
            end_line=span[1] if span else 0,
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
        _resolve_catch_clauses(result)
        extract_implementations(result)
        _resolve_test_calls(result)


# ---------------------------------------------------------------------------
# Post-processing helpers (C++-specific)
# ---------------------------------------------------------------------------


def _resolve_test_calls(result: ParseResult) -> None:
    """Resolve deferred C++ test-body call sites into CALLEE / VERIFIES.

    The test parser records every call site inside a step/assertion body
    as a :class:`PendingTestCall` while parsing (file parse order is
    nondeterministic — the class files may not be parsed yet).  Here,
    against the fully-populated result:

    * step calls → ``CALLEE`` (step → code) + ``VERIFIES`` (test → code)
    * assertion-body calls → ``VERIFIES`` only (assertions are not steps)

    Unresolved names (lambdas, local helpers, unparsed symbols) are
    silently dropped so no dangling edges are emitted.
    """
    from doxygen_index.parser.cpp_tests import _resolve_callee

    if not result.pending_test_calls:
        return

    resolved = 0
    dropped = 0
    for pc in result.pending_test_calls:
        hit = _resolve_callee(result, pc.callee_text)
        if hit is None:
            dropped += 1
            continue
        callee_refid, callee_type = hit
        if not pc.is_assert:
            result.callees.append(CalleeEntry(
                from_refid=pc.from_refid,
                to_refid=callee_refid,
                to_type=callee_type,
            ))
        result.verifies.append(VerifiesEntry(
            from_refid=pc.test_refid,
            to_refid=callee_refid,
            to_type=callee_type,
        ))
        resolved += 1

    print(f"  Test call resolution: {resolved} resolved, {dropped} dropped")


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
    for c in sorted(result.concepts, key=lambda item: (item.qualified_name, item.refid)):
        short = c.qualified_name.rsplit("::", 1)[-1] if "::" in c.qualified_name else c.qualified_name
        if short not in concept_short_names:
            concept_short_names[short] = c.qualified_name

    existing_pairs: set[tuple[str, str]] = {
        (cc.from_refid, cc.to_refid) for cc in result.concept_constraints
    }
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
        if pair not in existing_pairs:
            existing_pairs.add(pair)
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

    for concept in sorted(result.concepts, key=lambda item: (item.qualified_name, item.refid)):
        initializer = getattr(concept, 'initializer', '') or ''
        if not initializer:
            continue
        from_refid = getattr(concept, 'refid', None)
        if not from_refid:
            continue
        from_qn = getattr(concept, 'qualified_name', '')

        for target_qn in sorted(concept_names):
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
                    same_ns = sorted(
                        qn for qn in candidates
                        if qn.rsplit("::", 1)[0] == from_ns
                    )
                    if same_ns:
                        target_qn = same_ns[0]
                    # Otherwise the sorted outer iteration remains the stable
                    # choice for an ambiguous short name.
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
        # Nested compounds (compound_refid set — e.g. a class-scoped enum)
        # are composed by their owning compound, never by a synthetic
        # namespace derived from their ``Parent::Child`` qualified name.
        if getattr(child, "compound_refid", "") or "":
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


# ---------------------------------------------------------------------------
# Catch-clause exception type extraction
# ---------------------------------------------------------------------------

#: Matches a catch statement: the ``catch`` keyword followed by ``(``.
#: Doxygen collapses inter-token whitespace in ``<programlisting>`` text,
#: so ``catch (...`` always appears as ``catch(...``.
_CATCH_CLAUSE_RE = re.compile(r"\bcatch\s*\(")

#: Doxygen ``<ref>`` kindref values that denote a caught exception type
#: (a compound/class/struct/enum/union).  ``member`` refs on a catch line
#: would be calls inside the handler — not the caught type.
_CATCH_TYPE_KINDREFS = ("compound", "class", "struct", "enum", "union")


def _strip_strings_comments(text: str) -> str:
    """Remove comments and string/char literals from a source line.

    ``<programlisting>`` codelines are single lines, so ``//`` comments
    run to the end of the line.  Used for brace counting so format strings
    like ``"{} {{}}"`` and comments cannot skew body-range detection.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            break
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _brace_delta(clean: str) -> int:
    """Net brace depth change of a comment/string-stripped line."""
    return clean.count("{") - clean.count("}")


def _scan_file_catch_clauses(
    compounddef: ET.Element,
    result: ParseResult,
) -> None:
    """Scan a file compound's ``<programlisting>`` for catch clauses.

    Doxygen puts the full source of every indexed file (headers *and*
    ``.cpp`` files) in the file compound's ``<programlisting>``, with one
    ``<codeline>`` per line (carrying a ``lineno``) and ``<ref>`` elements
    for referenced symbols.  Function *definition* lines carry a
    ``kindref="member"`` ref pointing at the member being defined — which
    is the only reliable way to attribute a ``.cpp``-defined method body
    (Doxygen's ``<location>`` for those points at the header declaration).

    This walks the listing with brace matching, tracks the enclosing member
    via definition lines, and records a :class:`CatchClauseEntry` for every
    catch clause found.  Catches inside macros or unreferenced internal
    functions end up with a refid that resolves to no parsed member — the
    downstream edge builder drops those, so no spurious edges are created.
    """
    pl = compounddef.find("programlisting")
    if pl is None:
        return

    owner_refid: str | None = None    # member whose body we are inside
    pending_refid: str | None = None  # signature seen, awaiting its '{'
    depth = 0

    for codeline in pl.findall("codeline"):
        raw = "".join(codeline.itertext())
        stripped = raw.strip()
        if stripped.startswith("#"):
            # Preprocessor lines (and macro bodies) open no source blocks.
            continue
        clean = _strip_strings_comments(raw)
        refs = [
            (r.get("refid", ""), r.get("kindref", ""), "".join(r.itertext()))
            for r in codeline.findall(".//ref")
        ]
        member_refs = [r for r in refs if r[1] == "member"]
        braces = _brace_delta(clean)

        if owner_refid is not None:
            depth += braces
            if _CATCH_CLAUSE_RE.search(clean):
                _record_catch_clause(codeline, refs, owner_refid, result)
            if depth <= 0:
                owner_refid = None
        elif pending_refid is not None:
            depth += braces
            if depth > 0:
                owner_refid = pending_refid
                pending_refid = None
            elif stripped.endswith(";"):
                # Signature ended without a body (declaration or call).
                pending_refid = None
            elif "{" in clean:
                # Multi-line signature whose body opens on this line (or a
                # one-liner body with net-zero braces, e.g. ``void f() { x(); }``).
                owner_refid = pending_refid
                pending_refid = None
        elif (
            member_refs
            and "(" in raw
            and "->" not in raw
            and not stripped.endswith(";")
        ):
            # Definition/signature candidate: a member ref on a line that
            # opens a function call but is not itself a call (no ``->``, no
            # trailing ``;``).  Multi-line signatures stay pending until the
            # opening brace appears.
            pending_refid = member_refs[0][0]
            depth = braces
            if depth > 0 or "{" in clean:
                owner_refid = pending_refid
                pending_refid = None


def _record_catch_clause(
    codeline: ET.Element,
    refs: list[tuple[str, str, str]],
    owner_refid: str,
    result: ParseResult,
) -> None:
    """Record one catch clause under its owning member."""
    type_ref = next(
        (r for r in refs if r[1] in _CATCH_TYPE_KINDREFS),
        None,
    )
    result.catch_clauses.append(CatchClauseEntry(
        from_refid=owner_refid,
        type_text=_catch_type_text("".join(codeline.itertext())),
        to_refid=type_ref[0] if type_ref else "",
        to_type=type_ref[1] if type_ref else "ClassNode",
    ))


def _catch_type_text(raw: str) -> str:
    """Extract the caught type text from a catch line.

    Handles doxygen's whitespace-collapsed text (``catch(conststd::exception&ex)``)
    and returns the type as written (``conststd::exception&``).  Returns
    ``""`` for catch-all clauses (``catch(...)``).
    """
    m = _CATCH_CLAUSE_RE.search(raw)
    if not m:
        return ""
    inner = raw[m.end():]
    depth = 0
    end = len(inner)
    for i, ch in enumerate(inner):
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                end = i
                break
            depth -= 1
    inner = inner[:end].strip()
    if not inner or "..." in inner:
        return ""
    return inner


def _normalize_catch_type(text: str) -> str:
    """Normalize a caught-type string to a qualified name for resolution.

    Strips qualifiers and the exception parameter name from doxygen's
    whitespace-collapsed text, e.g. ``conststd::exception&ex`` →
    ``std::exception``.  Returns ``""`` when the text is not a resolvable
    type name (catch-all, function pointers, ambiguous bare names).
    """
    name = text.strip()
    name = re.sub(r"^(const|volatile|struct|class|enum)", "", name).strip()
    # Trim a trailing reference/pointer suffix plus the parameter name
    # (``&ex`` / ``*err``).  After this the remainder is the type itself.
    name = re.sub(r"[&*][^&*]*$", "", name).strip()
    if not name:
        return ""
    # A bare ``catch(MyErr err)`` (no ``&``/``*``) collapses to
    # ``MyErrerr`` — ambiguous without whitespace, so leave it
    # unresolved rather than guess (resolution fails closed).
    return name


def _resolve_catch_clauses(result: ParseResult) -> None:
    """Resolve catch-clause entries to ``DEPENDS_ON`` edges.

    Types carrying a programlisting ``<ref>`` are used directly.  Ref-less
    types are resolved by qualified name (then short name) against the
    parsed compounds; standard-library exceptions merged in by a later
    phase (e.g. cppreference) don't resolve here and are silently skipped,
    so unresolved catches never produce dangling edges.
    """
    compounds_by_qn: dict[str, object] = {}
    short_to_qn: dict[str, str] = {}
    for compound in result.compounds:
        qn = getattr(compound, "qualified_name", "") or ""
        if not qn:
            continue
        compounds_by_qn.setdefault(qn, compound)
        short = qn.rsplit("::", 1)[-1]
        short_to_qn.setdefault(short, qn)

    for cc in result.catch_clauses:
        if cc.to_refid:
            result.depends_on.append(DependsOnEntry(
                from_refid=cc.from_refid,
                to_refid=cc.to_refid,
                to_type=cc.to_type,
            ))
            continue
        name = _normalize_catch_type(cc.type_text)
        if not name:
            continue
        target = compounds_by_qn.get(name)
        if target is None and name in short_to_qn:
            target = compounds_by_qn.get(short_to_qn[name])
        if target is None:
            continue
        result.depends_on.append(DependsOnEntry(
            from_refid=cc.from_refid,
            to_refid=target.refid,
            to_type="ClassNode",
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
