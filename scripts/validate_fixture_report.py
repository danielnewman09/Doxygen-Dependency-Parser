#!/usr/bin/env python3
"""Structural validation of the fixture markdown report.

Checks the report has no broken code fences (a fence that shares a line
with other content) and that every code fence is balanced.
"""
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/unit_test_data/cpp_sqlite_fixture_report.md")
lines = path.read_text(encoding="utf-8").splitlines()

bad_fence_lines = []
fence_stack = []
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    if not stripped.startswith("```"):
        continue
    # fence must be the only content on its line
    if stripped not in ("```", "```cpp"):
        bad_fence_lines.append((i, line))
    if stripped == "```":
        # bare fence closes the innermost open fence
        if fence_stack:
            fence_stack.pop()
        else:
            fence_stack.append("unopened-bare")
    else:
        # fenced block with an info string opens a new fence
        fence_stack.append(stripped[3:])

problems = []
if bad_fence_lines:
    problems.append(f"{len(bad_fence_lines)} fence line(s) with extra content: {bad_fence_lines[:5]}")
if fence_stack:
    problems.append(f"unbalanced fences at EOF: {fence_stack}")

if problems:
    print("PROBLEMS:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print(f"OK: {path} — {len(lines)} lines, fences balanced, no inline fences")
