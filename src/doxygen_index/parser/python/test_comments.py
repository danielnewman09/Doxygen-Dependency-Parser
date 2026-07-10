"""Bidirectional sync of enriched test descriptions as source-file comments.

This module closes the loop between the **codegraph** (Neo4j / ParseResult)
and the **test source files** for test-element descriptions:

* :func:`read_test_comments` — *source → graph*: parse the tagged
  ``# codegraph:test-desc <qualified_name>`` comment blocks out of a
  ``.py`` file and return a ``{qualified_name: description}`` map.  The
  test parser (:mod:`tests`) applies this map to populate each node's
  ``description`` field, overriding the parser-generated placeholders
  ("Setup block", "assert …", etc.) and the test-function docstring
  brief.

* :func:`write_test_comments` — *graph → source*: take the enriched
  ``description`` fields on a :class:`ParseResult`'s test-related nodes
  and write them back as tagged comment blocks in the ``.py`` files,
  anchored above each element (test function, step block, fixture
  assignment, assert statement).  Existing tagged blocks for managed
  nodes are replaced idempotently.

The **qualified_name is the bidirectional mapping key.**  It is
deterministic (derived from the module path, function name, and child
ordering) and stable across re-parses, so a comment written above a
step block maps unambiguously back to that ``TestStepNode``'s
``description`` field — even if lines shift above it.

Comment convention
------------------

A tagged block is a ``# codegraph:test-desc <qualified_name>`` *tag
line* followed by one or more ``# <text>`` *continuation lines*::

    # codegraph:test-desc samplepkg.test_calculator.test_evaluator_step::step_0
    # Sets up an Evaluator and applies two operations to verify
    # that accumulation across steps produces the expected total.

The block ends at the first line that is not a ``#`` comment (or at the
next tag line).  **Continuation lines are reflowed:** consecutive
non-blank ``#`` lines form one paragraph (joined with a single space
on read) and a bare ``#`` line is a paragraph break.  This means a
wrapped paragraph round-trips back to its original single-line form,
so line wrapping on write is lossless and idempotent.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doxygen_index.parser.model import ParseResult

# Maximum line length (characters) for written comment blocks, matching
# Black's default line length.  The tag line (``# codegraph:test-desc <qn>``)
# is never wrapped — it is a structural identifier and must stay on one line
# so the read regex can match it; only the ``# …`` description continuations
# are wrapped to this width.
MAX_COMMENT_WIDTH = 88


# ══════════════════════════════════════════════════════════════════════════
# Comment convention
# ══════════════════════════════════════════════════════════════════════════

#: The marker that opens a description comment block.
TAG = "codegraph:test-desc"

_TAG_RE = re.compile(r"^\s*#\s*" + re.escape(TAG) + r"\s+(\S+)\s*$")
_CONT_RE = re.compile(r"^\s*#\s?(.*)$")


# ══════════════════════════════════════════════════════════════════════════
# Placeholder detection (mirrors doxygen_index.enrich / neo4j_backend)
# ══════════════════════════════════════════════════════════════════════════

_PLACEHOLDER_PATTERNS = [
    re.compile(r"^Setup block$"),
    re.compile(r"^Action block \d+$"),
    re.compile(r"^assert .+$"),
]


def _is_placeholder_description(desc: str | None) -> bool:
    """Return True if *desc* is a parser-generated placeholder.

    Placeholders are the auto-generated labels the parser writes before
    enrichment: ``"Setup block"``, ``"Action block 3"``, raw assert
    text like ``"assert x == 1"``, and the empty string.
    """
    if not desc or not desc.strip():
        return True
    stripped = desc.strip()
    return any(p.match(stripped) for p in _PLACEHOLDER_PATTERNS)


# ══════════════════════════════════════════════════════════════════════════
# Read: source → graph
# ══════════════════════════════════════════════════════════════════════════


def read_test_comments(file_path: str | Path) -> dict[str, str]:
    """Parse tagged description comments from a Python source file.

    Scans the file's lines for ``# codegraph:test-desc <qn>`` tag lines
    and collects the following ``# …`` continuation lines as the
    description body.  Returns a map of ``qualified_name → description``
    suitable for applying to parsed test nodes.

    The mapping is **by qualified name only** — line positions are
    irrelevant, so the map stays valid when lines are added or removed
    above a block.

    Args:
        file_path: Path to a ``.py`` test file.

    Returns:
        Dict mapping each tagged block's qualified name to its
        description string (continuation lines joined with ``\\n``).
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    lines = text.splitlines()
    result: dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        m = _TAG_RE.match(lines[i])
        if not m:
            i += 1
            continue
        qn = m.group(1)
        desc_lines: list[str] = []
        j = i + 1
        while j < n:
            if _TAG_RE.match(lines[j]):
                break  # next block starts here
            cm = _CONT_RE.match(lines[j])
            if cm is None:
                break  # not a comment line — block ends
            desc_lines.append(cm.group(1))
            j += 1
        # Reflow: consecutive non-blank "# " lines are one paragraph
        # (joined with a single space); a bare "#" line is a paragraph
        # break.  This makes wrapped descriptions round-trip losslessly.
        paragraphs: list[str] = []
        cur: list[str] = []
        for text in desc_lines:
            if text == "":
                if cur:
                    paragraphs.append(" ".join(cur))
                    cur = []
            else:
                cur.append(text)
        if cur:
            paragraphs.append(" ".join(cur))
        result[qn] = "\n\n".join(paragraphs).strip()
        i = j
    return result


# ══════════════════════════════════════════════════════════════════════════
# Write: graph → source
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class CommentWriteReport:
    """Summary of a :func:`write_test_comments` run.

    Attributes:
        files_changed: Paths of files that were modified on disk.
        files_unchanged: Paths that needed no edits.
        nodes_written: Count of description blocks written.
        nodes_skipped_placeholder: Count of nodes skipped because their
            description was a parser placeholder.
        nodes_skipped_docstring: Count of test nodes skipped because their
            description equals the existing docstring brief.
        errors: Per-file error messages.
    """

    files_changed: list[str] = field(default_factory=list)
    files_unchanged: list[str] = field(default_factory=list)
    nodes_written: int = 0
    nodes_scaffolded: int = 0
    nodes_skipped_placeholder: int = 0
    nodes_skipped_docstring: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "files_changed": self.files_changed,
            "files_unchanged": self.files_unchanged,
            "nodes_written": self.nodes_written,
            "nodes_scaffolded": self.nodes_scaffolded,
            "nodes_skipped_placeholder": self.nodes_skipped_placeholder,
            "nodes_skipped_docstring": self.nodes_skipped_docstring,
            "errors": self.errors,
        }


def _indent_of(line: str) -> str:
    """Return the leading whitespace of *line*."""
    return line[: len(line) - len(line.lstrip())]


def _docstring_briefs_and_anchors(text: str) -> tuple[dict[int, str], dict[int, tuple[int, str]]]:
    """Return ``{def_lineno: docstring_brief}`` and ``{def_lineno: (anchor_line, indent)}``.

    The *anchor_line* for a function is the first decorator line when
    decorators are present, otherwise the ``def`` line — so that an
    inserted comment block sits above the decorators rather than between
    a decorator and the ``def``.  *indent* is the leading whitespace of
    the anchor line.
    """
    briefs: dict[int, str] = {}
    anchors: dict[int, tuple[int, str]] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return briefs, anchors
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        ds = ast.get_docstring(node, clean=True) or ""
        briefs[node.lineno] = ds.split("\n")[0] if ds else ""
        if node.decorator_list:
            anchor_line = min(d.lineno for d in node.decorator_list)
        else:
            anchor_line = node.lineno
        indent = _indent_of(lines[anchor_line - 1]) if 1 <= anchor_line <= len(lines) else ""
        anchors[node.lineno] = (anchor_line, indent)
    return briefs, anchors


def _comment_block(
    qn: str, desc: str, indent: str, *, width: int = MAX_COMMENT_WIDTH,
) -> list[str]:
    """Build the tagged comment-block lines for *qn* / *desc* at *indent*.

    An empty *desc* produces a bare tag line — a **slot** to be filled in
    by hand (scaffold mode).  A non-empty description produces the tag line
    followed by ``# …`` continuation lines wrapped to *width*: consecutive
    non-blank lines reflow into one paragraph on read, and a bare ``#``
    line separates paragraphs, so wrapping is lossless and idempotent.

    The tag line itself is never wrapped (it is a structural identifier
    the read regex must match on one line); only the ``# …`` description
    continuations are wrapped.
    """
    block = [f"{indent}# {TAG} {qn}"]
    if not desc:
        return block
    prefix = f"{indent}# "
    avail = max(10, width - len(prefix))
    # Split into paragraphs on any run of newlines; reflow each paragraph's
    # own lines with a space before wrapping so write/read stay symmetric.
    paragraphs = [p.strip() for p in re.split(r"\n+", desc) if p.strip()]
    first = True
    for para in paragraphs:
        if not first:
            block.append(f"{indent}#")  # paragraph break
        first = False
        wrapped = textwrap.wrap(
            para, width=avail, break_long_words=False, break_on_hyphens=False,
        ) or [""]
        for wl in wrapped:
            block.append(f"{prefix}{wl}" if wl else f"{indent}#")
    return block


def write_test_comments(
    result: "ParseResult",
    *,
    dry_run: bool = False,
    descriptions: dict[str, str] | None = None,
    files: set[str] | None = None,
    scaffold: bool = False,
    width: int = MAX_COMMENT_WIDTH,
) -> CommentWriteReport:
    """Write enriched descriptions from *result* back into test source files.

    For each test-related node (TestNode, TestStepNode, TestFixtureNode,
    AssertionNode) that carries a non-placeholder ``description`` and a
    known source location, a tagged comment block is (re)written into the
    node's source file immediately above its anchor line:

    * TestNode — above the ``def`` (above any decorators).
    * TestStepNode — above the first line of the step block
      (``body_start``).
    * TestFixtureNode — above the assignment line.
    * AssertionNode — above the ``assert`` statement.

    Existing tagged blocks for nodes managed by this *result* are removed
    first, so the operation is idempotent: re-running replaces blocks in
    place rather than duplicating them.  TestNode descriptions that match
    the function's existing docstring brief are skipped (no redundant
    comment).

    **Scaffold mode** (``scaffold=True``) lets you add comment slots
    *without enriching first*: a bare ``# codegraph:test-desc <qn>`` tag
    line is written for every test element whose description is still a
    parser placeholder (or, for a TestNode, equal to its docstring brief).
    Fill the slot in by hand by adding ``# …`` description lines directly
    beneath the tag; the next parse picks them up and maps them onto the
    node's ``description`` field.  Elements that already carry a real
    description are written normally.

    Args:
        result: A parsed project result carrying test nodes with their
            enriched ``description`` fields and source locations.
        dry_run: If True, compute the edits but do not write to disk.
        descriptions: Optional ``{qualified_name: description}`` override.
            When provided, these descriptions take precedence over the
            nodes' own ``description`` attributes (e.g. when feeding
            values read from Neo4j).  Placeholders in the override are
            still skipped (or scaffolded, in scaffold mode).
        files: Optional set of file paths to restrict writing to.
        scaffold: If True, insert empty comment slots for every test
            element that does not yet have a real description, instead
            of skipping it.

    Returns:
        A :class:`CommentWriteReport` describing what was written.
    """
    report = CommentWriteReport()
    override = descriptions or {}

    # ------------------------------------------------------------------
    # 1. Gather managed nodes + descriptions, grouped by source file.
    # ------------------------------------------------------------------
    # Each entry: (insertion_anchor, qualified_name, description, indent)
    # plus the test def-lineno (for docstring-brief comparison) when known.
    per_file_items: dict[str, list[tuple]] = defaultdict(list)
    per_file_managed_qns: dict[str, set[str]] = defaultdict(set)

    def _node_desc(qn: str, node) -> str:
        d = override.get(qn)
        if d is None:
            d = getattr(node, "description", "") or ""
        return d

    def _anchor_line(node, attr: str) -> int:
        v = getattr(node, attr, 0) or 0
        return int(v) if v else 0

    # Test nodes (skip when description matches the docstring brief —
    # that comparison is done per-file once we have the AST).
    for t in result.tests:
        fp = getattr(t, "file_path", "") or ""
        qn = getattr(t, "qualified_name", "") or ""
        if not fp or not qn:
            continue
        per_file_managed_qns[fp].add(qn)
        desc = _node_desc(qn, t)
        is_ph = _is_placeholder_description(desc)
        def_ln = int(getattr(t, "line_number", 0) or 0)
        if scaffold:
            per_file_items[fp].append(("test", def_ln, qn, desc, is_ph))
        elif is_ph:
            report.nodes_skipped_placeholder += 1
        else:
            per_file_items[fp].append(("test", def_ln, qn, desc, is_ph))

    for s in result.test_steps:
        fp = getattr(s, "file_path", "") or ""
        qn = getattr(s, "qualified_name", "") or ""
        if not fp or not qn:
            continue
        per_file_managed_qns[fp].add(qn)
        desc = _node_desc(qn, s)
        is_ph = _is_placeholder_description(desc)
        if scaffold:
            per_file_items[fp].append(("step", _anchor_line(s, "body_start"), qn, desc, is_ph))
        elif is_ph:
            report.nodes_skipped_placeholder += 1
        else:
            per_file_items[fp].append(("step", _anchor_line(s, "body_start"), qn, desc, is_ph))

    for f in result.test_fixtures:
        fp = getattr(f, "file_path", "") or ""
        qn = getattr(f, "qualified_name", "") or ""
        if not fp or not qn:
            continue
        per_file_managed_qns[fp].add(qn)
        desc = _node_desc(qn, f)
        is_ph = _is_placeholder_description(desc)
        if scaffold:
            per_file_items[fp].append(("fixture", _anchor_line(f, "line_number"), qn, desc, is_ph))
        elif is_ph:
            report.nodes_skipped_placeholder += 1
        else:
            per_file_items[fp].append(("fixture", _anchor_line(f, "line_number"), qn, desc, is_ph))

    for a in result.assertions:
        fp = getattr(a, "file_path", "") or ""
        qn = getattr(a, "qualified_name", "") or ""
        if not fp or not qn:
            continue
        per_file_managed_qns[fp].add(qn)
        desc = _node_desc(qn, a)
        is_ph = _is_placeholder_description(desc)
        if scaffold:
            per_file_items[fp].append(("assertion", _anchor_line(a, "line_number"), qn, desc, is_ph))
        elif is_ph:
            report.nodes_skipped_placeholder += 1
        else:
            per_file_items[fp].append(("assertion", _anchor_line(a, "line_number"), qn, desc, is_ph))

    # ------------------------------------------------------------------
    # 2. For each file: strip managed tagged blocks, then re-insert.
    # ------------------------------------------------------------------
    for fp, raw_items in per_file_items.items():
        if files is not None and fp not in files:
            continue
        path = Path(fp)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            report.errors.append(f"{fp}: read failed: {exc}")
            continue

        lines = text.splitlines()
        line_indent = [_indent_of(ln) for ln in lines]

        briefs, test_anchors = _docstring_briefs_and_anchors(text)
        managed_qns = per_file_managed_qns[fp]

        # Build the concrete insertion list: (insertion_anchor, qn, desc, indent)
        insertions: list[tuple[int, str, str, str]] = []
        for kind, anchor_src, qn, desc, is_ph in raw_items:
            is_slot = False
            if kind == "test":
                def_ln = anchor_src
                if not def_ln:
                    continue
                brief = briefs.get(def_ln)
                anchor_line, indent = test_anchors.get(
                    def_ln, (def_ln, line_indent[def_ln - 1] if 1 <= def_ln <= len(line_indent) else ""),
                )
                if scaffold:
                    # Empty slot when still a placeholder or just the docstring.
                    if is_ph or desc == brief:
                        desc, is_slot = "", True
                else:
                    # Skip when the description just duplicates the docstring.
                    if desc == brief:
                        report.nodes_skipped_docstring += 1
                        continue
            else:
                anchor_line = anchor_src
                indent = line_indent[anchor_line - 1] if 1 <= anchor_line <= len(line_indent) else ""
                if scaffold and is_ph:
                    desc, is_slot = "", True
            if not anchor_line:
                continue
            insertions.append((anchor_line, qn, desc, indent))
            if is_slot:
                report.nodes_scaffolded += 1

        # Group insertions by anchor line (multiple nodes can share one).
        by_anchor: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        for anchor_line, qn, desc, indent in insertions:
            by_anchor[anchor_line].append((qn, desc, indent))

        # Delete set: indices of existing tagged blocks whose qn we manage.
        delete: set[int] = set()  # 0-based line indices
        i = 0
        n = len(lines)
        while i < n:
            m = _TAG_RE.match(lines[i])
            if m and m.group(1) in managed_qns:
                delete.add(i)
                j = i + 1
                while j < n and _CONT_RE.match(lines[j]) and not _TAG_RE.match(lines[j]):
                    delete.add(j)
                    j += 1
                i = j
            else:
                i += 1

        # Single pass: emit insertions before the anchor line, skip deletes.
        out: list[str] = []
        for idx0, line in enumerate(lines):
            lineno = idx0 + 1
            if lineno in by_anchor:
                # Stable order within a shared anchor: by qn for determinism.
                for qn, desc, indent in sorted(by_anchor[lineno]):
                    out.extend(_comment_block(qn, desc, indent, width=width))
                    report.nodes_written += 1
            if idx0 in delete:
                continue
            out.append(line)

        new_text = "\n".join(out)
        if text.endswith("\n"):
            new_text += "\n"

        if new_text == text:
            report.files_unchanged.append(fp)
            continue

        report.files_changed.append(fp)
        if dry_run:
            continue
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            report.errors.append(f"{fp}: write failed: {exc}")

    return report