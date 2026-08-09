#!/usr/bin/env python3
"""Export the integration-test fixture data as a reviewable markdown report.

Reads the artifacts archived by the integration suites
(``tests/unit_test_data/``):

* the serialized as-built LayerGraph JSON (``*_one_hop.json``) — the
  exact graph the suite validated,
* the archived sqlite database (``*_integration.sqlite3``) — used to
  enrich test steps with their actual source code via
  ``HAS_IMPLEMENTATION`` edges.

Produces a human-readable markdown report (``*_fixture_report.md``)
covering, for each codebase:

* graph summary (node kinds, edge types, sources),
* for cpp-sqlite: a per-test breakdown — every TEST_F, its assertions
  (operator + assert text), its steps (line range, callees, source
  body), and its VERIFIES targets — plus a completeness cross-check
  against the fixture source (TEST_F count, assert count),
* the inventory of code the tests verify (VERIFIES target frequencies).

Usage::

    python scripts/export_fixture_report.py --json tests/unit_test_data/cpp_sqlite_one_hop.json \
        --db tests/unit_test_data/cpp_sqlite_integration.sqlite3 \
        --out tests/unit_test_data/cpp_sqlite_fixture_report.md \
        --title "cpp-sqlite as-built fixture"
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixture-source completeness counters (mirror the integration tests)
# ---------------------------------------------------------------------------

_TEST_F_RE = re.compile(r"^TEST_F\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)")
_ASSERT_IN_SOURCE_RE = re.compile(
    r"^\s*(?:ASSERT|EXPECT)_[A-Z_0-9]+\s*\(", re.MULTILINE,
)


def _source_counts(test_cpp: Path) -> dict:
    """Count TEST_F cases and ASSERT_*/EXPECT_* statements in the source."""
    text = test_cpp.read_text(encoding="utf-8")
    cases = {}
    for line in text.splitlines():
        m = _TEST_F_RE.match(line.strip())
        if m:
            cases[m.group(2)] = m.group(1)
    return {
        "test_f_cases": len(cases),
        "assert_statements": len(_ASSERT_IN_SOURCE_RE.findall(text)),
    }


# ---------------------------------------------------------------------------
# Graph loading
# ---------------------------------------------------------------------------


def _flatten(serialized: list[dict]) -> dict[str, dict]:
    """Flat {uid: node} map from the nested serialized LayerGraph."""
    uid_map: dict[str, dict] = {}
    stack = list(serialized)
    while stack:
        node = stack.pop()
        uid_map[node["uid"]] = node
        stack.extend(node.get("composes", []))
    return uid_map


def _uid_to_qname(uid_map: dict[str, dict]) -> dict[str, str]:
    return {
        uid: (n.get("qualified_name") or n.get("name") or uid)
        for uid, n in uid_map.items()
    }


def _load_step_sources(db_path: Path) -> dict[str, str]:
    """Map step uid → implementation source from the archived sqlite db."""
    if not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT n1.uid, n2.properties
            FROM edges e
            JOIN nodes n1 ON n1.id = e.source_id
            JOIN nodes n2 ON n2.id = e.target_id
            WHERE e.rel_type = 'HAS_IMPLEMENTATION'
            """
        ).fetchall()
    finally:
        con.close()
    out: dict[str, str] = {}
    for step_uid, props_json in rows:
        props = json.loads(props_json)
        src = props.get("implementation") or ""
        if src.strip():
            out[step_uid] = src
    return out


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def _inline_code(text: str, max_len: int = 200) -> str:
    """Single-line code span (collapses newlines) for table cells.

    Uses double-backtick delimiters when the content itself contains a
    backtick (backslash-escaping does not work inside code spans).
    """
    flat = " ".join(text.split())
    if len(flat) > max_len:
        flat = flat[: max_len - 1] + "…"
    if "`" in flat:
        return f"``{flat}``"
    return f"`{flat}`"


def _code_block(text: str) -> str:
    return "```cpp\n" + text.rstrip("\n") + "\n```"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _short_name(qname: str) -> str:
    """Strip namespace prefixes for compact display."""
    return qname.split("::")[-1]


def build_report(
    json_path: Path,
    db_path: Path | None,
    test_cpp: Path | None,
    title: str,
) -> str:
    serialized = json.loads(json_path.read_text(encoding="utf-8"))
    uid_map = _flatten(serialized)
    qname = _uid_to_qname(uid_map)
    step_sources = _load_step_sources(db_path) if db_path else {}

    # ── Graph-wide summary ─────────────────────────────────────
    kinds = Counter(n.get("kind", "?") for n in uid_map.values())
    sources = Counter(n.get("source", "?") for n in uid_map.values())
    edge_types: Counter = Counter()
    for n in uid_map.values():
        for e in n.get("edges", []):
            edge_types[e["relation_type"]] += 1

    L: list[str] = []
    L.append(f"# {title}")
    L.append("")
    L.append(
        f"_Generated from `{json_path.name}`"
        + (f" + `{db_path.name}`" if db_path else "")
        + " — the exact data the integration suite validated._"
    )
    L.append("")
    L.append(f"- Nodes: **{len(uid_map)}** (of which `as-built`: "
             f"{sum(1 for n in uid_map.values() if 'as-built' in (n.get('tags') or []))})")
    L.append("")
    L.append("## Graph summary")
    L.append("")
    L.append("### Nodes by kind")
    L.append("")
    L.append(_md_table(["kind", "count"], [[k, str(v)] for k, v in kinds.most_common()]))
    L.append("")
    L.append("### Nodes by source")
    L.append("")
    L.append(_md_table(["source", "count"], [[k, str(v)] for k, v in sources.most_common()]))
    L.append("")
    L.append("### Edge types")
    L.append("")
    L.append(_md_table(["relation_type", "count"], [[k, str(v)] for k, v in edge_types.most_common()]))
    L.append("")

    # ── Test section (only when test nodes exist) ──────────────
    tests = [n for n in uid_map.values() if n.get("kind") == "test"]
    if tests:
        L.extend(_build_test_section(tests, uid_map, qname, step_sources, test_cpp))
    else:
        L.append("## Tests")
        L.append("")
        L.append("_No test nodes in this graph._")
        L.append("")

    # ── Verified-code inventory ────────────────────────────────
    verifies = Counter()
    for n in uid_map.values():
        for e in n.get("edges", []):
            if e["relation_type"] == "VERIFIES":
                verifies[qname.get(e["target_uid"], e["target_uid"])] += 1
    if verifies:
        L.append("## Code verified by tests (VERIFIES target frequency)")
        L.append("")
        L.append(_md_table(
            ["qualified_name", "# tests"],
            [[qn, str(c)] for qn, c in verifies.most_common()],
        ))
        L.append("")

    return "\n".join(L)


def _build_test_section(tests, uid_map, qname, step_sources, test_cpp) -> list[str]:
    L: list[str] = []

    # Group tests by suite.
    suites: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(tests, key=lambda n: n.get("qualified_name", "")):
        suites[t["qualified_name"].split("::")[1]].append(t)

    # ── Completeness cross-check vs fixture source ─────────────
    L.append("## Tests")
    L.append("")
    L.append(f"**{len(tests)} test nodes** across **{len(suites)} suites**.")
    L.append("")
    if test_cpp and test_cpp.exists():
        src = _source_counts(test_cpp)
        graph_asserts = sum(
            1 for n in uid_map.values() if n.get("kind") == "assertion"
        )
        graph_steps = sum(
            1 for n in uid_map.values() if n.get("kind") == "test_step"
        )
        L.append("### Completeness cross-check (fixture source vs graph)")
        L.append("")
        L.append(_md_table(
            ["measure", "fixture source", "graph", "status"],
            [
                ["TEST_F cases", str(src["test_f_cases"]), str(len(tests)),
                 "✅" if src["test_f_cases"] <= len(tests) else "❌"],
                ["ASSERT_*/EXPECT_* statements",
                 str(src["assert_statements"]), str(graph_asserts),
                 "✅" if src["assert_statements"] <= graph_asserts else "❌"],
                ["test steps", "—", str(graph_steps), "—"],
            ],
        ))
        L.append("")

    # ── Per-test details ───────────────────────────────────────
    for suite_name in sorted(suites):
        suite_tests = suites[suite_name]
        L.append(f"## Suite `{suite_name}` ({len(suite_tests)} tests)")
        L.append("")
        L.append(_md_table(
            ["test", "line", "#asserts", "#steps", "verifies"],
            [
                [
                    f"`{t['name']}`",
                    str(t.get("line_number", 0)),
                    str(sum(1 for c in t.get("composes", [])
                            if c.get("kind") == "assertion")),
                    str(sum(1 for c in t.get("composes", [])
                            if c.get("kind") == "test_step")),
                    str(sum(1 for e in t.get("edges", [])
                            if e["relation_type"] == "VERIFIES")),
                ]
                for t in suite_tests
            ],
        ))
        L.append("")

        for t in suite_tests:
            _build_single_test(L, t, qname, step_sources)

    return L


def _build_single_test(L: list[str], t: dict, qname, step_sources) -> None:
    L.append(f"### `{t['qualified_name']}`")
    L.append("")
    L.append(f"- file: `{t.get('file_path', '')}` : {t.get('line_number', 0)}")
    L.append(f"- method: `{t.get('method', '')}` · module: `{t.get('test_module', '')}`")
    if t.get("description"):
        L.append(f"- description: {t['description']}")

    verifies = [
        qname.get(e["target_uid"], e["target_uid"])
        for e in t.get("edges", [])
        if e["relation_type"] == "VERIFIES"
    ]
    if verifies:
        L.append(f"- VERIFIES ({len(verifies)}): "
                 + ", ".join(f"`{v}`" for v in sorted(verifies)))
    L.append("")

    children = sorted(
        t.get("composes", []),
        key=lambda c: (c.get("kind") != "test_step", c.get("order", 0)),
    )
    for c in children:
        kind = c.get("kind")
        if kind == "assertion":
            desc = c.get("description") or ""
            line_abs = c.get("line_number") or 0
            loc = f", line {line_abs}" if line_abs else ""
            header = (f"- `{c['name']}` (assertion, order {c.get('order')}"
                      f"{loc}) — operator `{c.get('operator')}`")
            if "\n" in desc:
                # Multi-line assert text — a fenced block on its own
                # lines (never inline after the bullet, which would
                # break the markdown).
                L.append(header + ":")
                L.append("")
                L.append(_code_block(desc))
                L.append("")
            else:
                L.append(header + ": " + _inline_code(desc))
        elif kind == "test_step":
            L.append(f"- `{c['name']}` ({c.get('description')}, lines "
                     f"{c.get('body_start')}–{c.get('body_end')})")
            callees = [
                qname.get(e["target_uid"], e["target_uid"])
                for e in c.get("edges", [])
                if e["relation_type"] == "CALLEE"
            ]
            if callees:
                L.append(f"  - CALLEE: {', '.join(f'`{x}`' for x in sorted(callees))}")
            src = step_sources.get(c.get("uid", ""))
            if src:
                L.append("")
                L.append(_code_block(src))
                L.append("")
    L.append("")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", required=True, type=Path,
                    help="Serialized LayerGraph JSON (tests/unit_test_data/*_one_hop.json)")
    ap.add_argument("--db", type=Path, default=None,
                    help="Archived sqlite db (enriches steps with source)")
    ap.add_argument("--test-cpp", type=Path, default=None,
                    help="Fixture test source file for completeness cross-check")
    ap.add_argument("--out", required=True, type=Path,
                    help="Output markdown path")
    ap.add_argument("--title", default="as-built fixture report")
    args = ap.parse_args(argv)

    report = build_report(
        args.json, args.db, args.test_cpp, args.title,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
