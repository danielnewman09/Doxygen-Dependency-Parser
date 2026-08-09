"""Unit tests for Doxyfile generation and EXCLUDE_PATTERNS merging.

The parse-hygiene defaults (``*/detail/*``, ``*/impl/*``, ``*/internal/*``,
``*/aux_/*``, ``*/experimental/*``) always apply so Doxygen never documents
internal implementation trees.  A project config's ``exclude_patterns`` is
added on top — it must not silently *replace* the defaults, which would pull
e.g. ``boost/detail/*`` into the graph (that was the root cause of the old
boost ``BOOST_UTF8_*`` PREDEFINED hack: the header was being parsed at all).
"""

from pathlib import Path

from doxygen_index.doxygen import (
    _DEFAULT_EXCLUDE_PATTERNS,
    _merge_exclude_patterns,
    generate_doxyfile,
)


def test_defaults_always_present():
    merged = _merge_exclude_patterns("")
    for pattern in _DEFAULT_EXCLUDE_PATTERNS.split():
        assert pattern in merged.split()


def test_user_patterns_added():
    merged = _merge_exclude_patterns("*/test/* */build/*")
    assert "*/test/*" in merged.split()
    assert "*/build/*" in merged.split()
    # hygiene defaults still there
    assert "*/detail/*" in merged.split()


def test_user_patterns_do_not_replace_defaults():
    # The cpp-sqlite fixture config: without merging this would disable
    # */detail/* entirely and pull boost/detail/* into the graph.
    merged = _merge_exclude_patterns("*/test/* */build/* */.git/*")
    assert merged.split() == [
        "*/detail/*", "*/impl/*", "*/aux_/*", "*/internal/*",
        "*/experimental/*", "*/test/*", "*/build/*", "*/.git/*",
    ]


def test_merge_deduplicates():
    merged = _merge_exclude_patterns(_DEFAULT_EXCLUDE_PATTERNS)
    assert merged == _DEFAULT_EXCLUDE_PATTERNS
    assert len(merged.split()) == len(set(merged.split()))


def test_merge_tolerates_none():
    assert _merge_exclude_patterns(None) == _DEFAULT_EXCLUDE_PATTERNS


def test_generate_doxyfile_writes_merged_patterns_verbatim():
    # run_doxygen merges the hygiene defaults + user patterns, then
    # generate_doxyfile writes the combined string into the Doxyfile.
    merged = _merge_exclude_patterns("*/test/*")
    doxyfile = generate_doxyfile(
        "t", Path("/tmp/in"), Path("/tmp/out/xml"),
        exclude_patterns=merged,
    )
    line = next(
        line for line in doxyfile.splitlines()
        if line.startswith("EXCLUDE_PATTERNS")
    )
    assert "*/detail/*" in line
    assert "*/test/*" in line
