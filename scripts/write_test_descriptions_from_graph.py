#!/usr/bin/env python3
"""One-time script: write enriched descriptions from Neo4j into test source.

The codegraph stores enriched test-element descriptions on its Neo4j nodes
(``description`` property of Test/TestStep/TestFixture/Assertion nodes).  This
script reads those existing descriptions back out of the graph and writes
them as ``# codegraph:test-desc <qualified_name>`` comment blocks into the
test source files — so the enriched values are reflected in the source
without re-running the LLM.

It is the *graph → source* materialisation path.  The reverse direction
(source comments → parsed node ``description``) happens automatically on
every parse via ``read_test_comments`` / ``_apply_comment``.

Workflow::

    1. Parse the project's Python sources (honours ``.doxygen-index.toml``).
    2. Connect to Neo4j.
    3. Fetch non-placeholder descriptions for every test-related qualified
       name found in the parse.
    4. Write them into the source files as comment blocks.

Requires a running Neo4j instance with the project already indexed.

Usage::

    # write enriched descriptions as comments
    python scripts/write_test_descriptions_from_graph.py

    # preview only (no files written)
    python scripts/write_test_descriptions_from_graph.py --dry-run

    # also insert empty slots for any element not yet described
    python scripts/write_test_descriptions_from_graph.py --scaffold

    # point at a different project
    python scripts/write_test_descriptions_from_graph.py --project-dir /path/to/repo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from doxygen_index.project import load_config
from doxygen_index.parser import parse_python_dir
from doxygen_index.neo4j_backend import (
    connect_neo4j,
    fetch_node_descriptions,
)
from doxygen_index.parser.python.test_comments import write_test_comments


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write Neo4j-enriched test descriptions into test "
                    "source files as comment blocks.",
    )
    parser.add_argument(
        "--project-dir",
        default=str(PROJECT_ROOT),
        help="Project directory containing .doxygen-index.toml "
             "(default: this repo).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the comment edits but do not write to disk.",
    )
    parser.add_argument(
        "--scaffold",
        action="store_true",
        help="Also insert an empty '# codegraph:test-desc <qn>' slot for "
             "every test element that has no description in the graph, so "
             "you can author descriptions by hand.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=88,
        help="Maximum line length (characters) for written comment blocks. "
             "The tag line is never wrapped; only '# …' description lines "
             "wrap to this width. Default 88 (matches Black).",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()

    # ── 1. Load config & parse ──────────────────────────────────────────
    config, _ = load_config(project_dir)
    print(f"Project: {config.name}")
    print(f"Input paths: {', '.join(str(p) for p in config.input_paths)}")

    all_dirs = list(config.input_paths)
    if config.test_paths:
        for tp in config.test_paths:
            if tp not in all_dirs:
                all_dirs.append(tp)
                print(f"  Including test path: {tp}")

    user_excludes = (config.exclude_patterns.split()
                     if config.exclude_patterns else None)

    print("\nParsing Python source...")
    result = parse_python_dir(
        all_dirs,
        source=config.name,
        layer="codebase",
        exclude_dirs=user_excludes or None,
    )
    total = (len(result.tests) + len(result.test_steps)
             + len(result.test_fixtures) + len(result.assertions))
    print(f"  Found {total} test elements "
          f"({len(result.tests)} tests, {len(result.test_steps)} steps, "
          f"{len(result.test_fixtures)} fixtures, {len(result.assertions)} "
          f"assertions)")

    # ── 2. Connect to Neo4j & fetch descriptions ──────────────────────
    print("\nConnecting to Neo4j...")
    connect_neo4j()

    qns: list[str] = []
    for lst in (result.tests, result.test_steps,
                result.test_fixtures, result.assertions):
        for node in lst:
            qn = getattr(node, "qualified_name", "") or ""
            if qn:
                qns.append(qn)
    print(f"  Looking up {len(qns)} qualified names...")
    override = fetch_node_descriptions(qns)
    print(f"  Found {len(override)} non-placeholder descriptions in the graph")

    # ── 3. Write comments ──────────────────────────────────────────────
    print("\nWriting test description comments...")
    report = write_test_comments(
        result,
        dry_run=args.dry_run,
        descriptions=override,
        scaffold=args.scaffold,
        width=args.width,
    )
    print(f"  Files changed:         {len(report.files_changed)}")
    print(f"  Files unchanged:      {len(report.files_unchanged)}")
    print(f"  Nodes written:        {report.nodes_written}")
    if report.nodes_scaffolded:
        print(f"  Slots scaffolded:     {report.nodes_scaffolded}")
    print(f"  Skipped (placeholder): {report.nodes_skipped_placeholder}")
    print(f"  Skipped (docstring):    {report.nodes_skipped_docstring}")
    if report.errors:
        print("  Errors:")
        for err in report.errors:
            print(f"    - {err}")
    if args.dry_run:
        print("  (dry-run — no files written)")


if __name__ == "__main__":
    main()