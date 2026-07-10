"""Tests for bidirectional test-description comment sync.

Verifies the two halves of the source ↔ codegraph description loop:

* :func:`read_test_comments` — parses ``# codegraph:test-desc <qn>``
  blocks out of a ``.py`` file into a ``{qualified_name: description}``
  map, including indented blocks inside function bodies.
* :func:`write_test_comments` — writes enriched descriptions back as
  tagged comment blocks anchored above each test element, idempotently.
* Round-trip — after write-back, re-parsing restores the descriptions
  onto the correct nodes' ``description`` fields (the bidirectional
  mapping to the correct field).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from doxygen_index.parser import parse_python_dir
from doxygen_index.parser.python.test_comments import (
    TAG,
    read_test_comments,
    write_test_comments,
    _is_placeholder_description,
)


# ══════════════════════════════════════════════════════════════════════════
# read_test_comments
# ══════════════════════════════════════════════════════════════════════════


class TestReadTestComments:
    """Parsing of tagged comment blocks from source text."""

    def test_reads_top_level_block(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text(
            "# codegraph:test-desc mod.test_x\n"
            "# Verifies the thing works.\n"
            "# Second line of the description.\n"
            "def test_x():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        m = read_test_comments(f)
        # Consecutive non-blank "# " lines reflow into one paragraph.
        assert m == {
            "mod.test_x": "Verifies the thing works. Second line of the description."
        }

    def test_reads_indented_block_inside_function(self, tmp_path):
        """Blocks inside a function body are indented — they must still parse."""
        f = tmp_path / "t.py"
        f.write_text(
            "def test_x():\n"
            "    # codegraph:test-desc mod.test_x::step_0\n"
            "    # Sets up the object under test.\n"
            "    obj = Thing()\n"
            "    # codegraph:test-desc mod.test_x::post_0\n"
            "    # Checks the result.\n"
            "    assert obj.result == 1\n",
            encoding="utf-8",
        )
        m = read_test_comments(f)
        assert m == {
            "mod.test_x::step_0": "Sets up the object under test.",
            "mod.test_x::post_0": "Checks the result.",
        }

    def test_block_ends_at_non_comment_line(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text(
            "# codegraph:test-desc mod.test_x\n"
            "# Desc line.\n"
            "\n"
            "# An unrelated comment, not part of the block.\n"
            "def test_x(): pass\n",
            encoding="utf-8",
        )
        m = read_test_comments(f)
        assert m == {"mod.test_x": "Desc line."}

    def test_adjacent_blocks_do_not_merge(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text(
            "# codegraph:test-desc mod.test_x::a\n"
            "# Desc a.\n"
            "# codegraph:test-desc mod.test_x::b\n"
            "# Desc b.\n"
            "x = 1\n",
            encoding="utf-8",
        )
        m = read_test_comments(f)
        assert m == {"mod.test_x::a": "Desc a.", "mod.test_x::b": "Desc b."}

    def test_missing_file_returns_empty(self, tmp_path):
        assert read_test_comments(tmp_path / "nope.py") == {}

    def test_multiline_description_preserved(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text(
            "# codegraph:test-desc qn\n"
            "# First.\n"
            "# Second.\n"
            "#\n"
            "# Fourth after a blank-comment line.\n"
            "pass\n",
            encoding="utf-8",
        )
        m = read_test_comments(f)
        assert m["qn"] == "First. Second.\n\nFourth after a blank-comment line."

    def test_ignores_unrelated_comments(self, tmp_path):
        f = tmp_path / "t.py"
        f.write_text(
            "# Just a regular comment.\n"
            "# Another note.\n"
            "def test_x(): pass\n",
            encoding="utf-8",
        )
        assert read_test_comments(f) == {}


# ══════════════════════════════════════════════════════════════════════════
# _is_placeholder_description
# ══════════════════════════════════════════════════════════════════════════


class TestPlaceholderDetection:
    def test_empty_is_placeholder(self):
        assert _is_placeholder_description("")
        assert _is_placeholder_description("   ")

    def test_setup_block_is_placeholder(self):
        assert _is_placeholder_description("Setup block")

    def test_action_block_is_placeholder(self):
        assert _is_placeholder_description("Action block 3")

    def test_assert_text_is_placeholder(self):
        assert _is_placeholder_description("assert x == 1")
        assert _is_placeholder_description("assert ==")

    def test_real_description_is_not_placeholder(self):
        assert not _is_placeholder_description(
            "Verifies that updating a single field persists the change."
        )


# ══════════════════════════════════════════════════════════════════════════
# write_test_comments
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fixture_dir(tmp_path):
    """Copy the samplepkg fixture into a temp dir for round-trip tests."""
    src = Path(__file__).parent / "languages" / "python"
    dst = tmp_path / "python"
    shutil.copytree(src, dst)
    return dst


def _parse(dst):
    return parse_python_dir(
        [dst], source="t", layer="codebase", exclude_dirs=None, progress_interval=0,
    )


def _enrich(result, qn, **descs):
    """Set enriched-style descriptions on the test named *qn* and its children."""
    for t in result.tests:
        if t.qualified_name == qn:
            t.description = descs.get("test", "LLM: verifies behaviour.")
    for s in result.test_steps:
        if s.qualified_name == qn + "::step_0":
            s.description = descs.get("step", "LLM: sets up and drives the test.")
    for a in result.assertions:
        if a.qualified_name == qn + "::post_0":
            a.description = descs.get("assertion", "LLM: checks the expected value.")
    for f in result.test_fixtures:
        if f.qualified_name == qn + "::evaluator":
            f.description = descs.get("fixture", "LLM: the object under test.")


class TestWriteTestComments:
    """Writing enriched descriptions back as comment blocks."""

    QN = "samplepkg.test_calculator.test_evaluator_step"
    FILE = "samplepkg/test_calculator.py"

    def test_writes_blocks_for_all_node_types(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        report = write_test_comments(r)
        assert report.nodes_written >= 4
        assert report.files_changed == [
            str(fixture_dir / self.FILE)
        ]
        text = (fixture_dir / self.FILE).read_text()
        assert f"# {TAG} {self.QN}\n" in text
        assert f"# {TAG} {self.QN}::step_0\n" in text
        assert f"# {TAG} {self.QN}::post_0\n" in text
        assert f"# {TAG} {self.QN}::evaluator\n" in text

    def test_assertion_block_placed_above_assert(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        write_test_comments(r)
        lines = (fixture_dir / self.FILE).read_text().splitlines()
        # Find the assert line for this test
        idx = next(
            i for i, ln in enumerate(lines)
            if "assert evaluator.current == 15.0" in ln
        )
        # The tagged block must be the lines immediately above it
        assert lines[idx - 1].strip().startswith("# LLM:")
        assert TAG in lines[idx - 2]

    def test_test_block_placed_above_def(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        write_test_comments(r)
        lines = (fixture_dir / self.FILE).read_text().splitlines()
        idx = next(
            i for i, ln in enumerate(lines)
            if ln.startswith("def test_evaluator_step")
        )
        assert lines[idx - 1].strip().startswith("# LLM:")
        assert TAG in lines[idx - 2]

    def test_idempotent_rewrite(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        write_test_comments(r)
        text_after = (fixture_dir / self.FILE).read_text()

        # Re-parse the now-commented file and write again
        r2 = _parse(fixture_dir)
        _enrich(r2, self.QN)  # same descriptions
        report2 = write_test_comments(r2)
        assert report2.files_changed == []
        text_again = (fixture_dir / self.FILE).read_text()
        assert text_again == text_after

    def test_skips_test_node_when_description_equals_docstring(self, fixture_dir):
        """A TestNode description matching its docstring brief is not duplicated."""
        r = _parse(fixture_dir)
        # Set the test description to exactly the docstring brief
        test = next(t for t in r.tests if t.qualified_name == self.QN)
        # The fixture's docstring is "Evaluator accumulates results across multiple steps."
        test.description = "Evaluator accumulates results across multiple steps."
        report = write_test_comments(r)
        text = (fixture_dir / self.FILE).read_text()
        # No test-level tag block should be written for this node
        assert f"# {TAG} {self.QN}\n" not in text
        assert report.nodes_skipped_docstring >= 1

    def test_skips_placeholder_descriptions(self, fixture_dir):
        """Nodes still carrying parser placeholders are not written."""
        r = _parse(fixture_dir)  # all placeholders / docstrings
        report = write_test_comments(r)
        assert report.nodes_written == 0
        assert report.nodes_skipped_placeholder >= 1
        assert report.files_changed == []

    def test_descriptions_override_takes_precedence(self, fixture_dir):
        r = _parse(fixture_dir)  # placeholder descriptions
        override = {
            self.QN: "Override: verifies accumulation.",
            self.QN + "::step_0": "Override: drives two operations.",
        }
        report = write_test_comments(r, descriptions=override)
        assert report.nodes_written == 2
        text = (fixture_dir / self.FILE).read_text()
        assert "Override: verifies accumulation." in text
        assert "Override: drives two operations." in text

    def test_dry_run_writes_nothing(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        before = (fixture_dir / self.FILE).read_text()
        report = write_test_comments(r, dry_run=True)
        assert report.files_changed
        assert (fixture_dir / self.FILE).read_text() == before


# ══════════════════════════════════════════════════════════════════════════
# Round-trip: write back, then re-parse → descriptions restored
# ══════════════════════════════════════════════════════════════════════════


class TestRoundTrip:
    """The bidirectional mapping: written comments map back to the correct field."""

    QN = "samplepkg.test_calculator.test_evaluator_step"

    def test_descriptions_survive_reparse(self, fixture_dir):
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        write_test_comments(r)

        # Re-parse the file that now carries the comment blocks
        r2 = _parse(fixture_dir)
        test = next(t for t in r2.tests if t.qualified_name == self.QN)
        assert test.description == "LLM: verifies behaviour."
        step = next(
            s for s in r2.test_steps
            if s.qualified_name == self.QN + "::step_0"
        )
        assert step.description == "LLM: sets up and drives the test."
        assertion = next(
            a for a in r2.assertions
            if a.qualified_name == self.QN + "::post_0"
        )
        assert assertion.description == "LLM: checks the expected value."
        fixture = next(
            f for f in r2.test_fixtures
            if f.qualified_name == self.QN + "::evaluator"
        )
        assert fixture.description == "LLM: the object under test."

    def test_comment_takes_precedence_over_docstring(self, fixture_dir):
        """A test-level comment overrides the function's docstring brief."""
        r = _parse(fixture_dir)
        test = next(t for t in r.tests if t.qualified_name == self.QN)
        test.description = "Enriched: distinct from the docstring."
        write_test_comments(r)

        r2 = _parse(fixture_dir)
        test2 = next(t for t in r2.tests if t.qualified_name == self.QN)
        assert test2.description == "Enriched: distinct from the docstring."
        assert test2.description != "Evaluator accumulates results across multiple steps."

    def test_nodes_without_comment_keep_placeholder(self, fixture_dir):
        """Nodes that were not enriched keep their parser placeholder description."""
        r = _parse(fixture_dir)
        # Enrich only the step of one test; leave a different test untouched
        target_step = next(
            s for s in r.test_steps
            if s.qualified_name
            == "samplepkg.test_calculator.test_evaluator_from_zero::step_0"
        )
        target_step.description = "LLM: from_zero setup."
        write_test_comments(r)

        r2 = _parse(fixture_dir)
        # The enriched step keeps its description
        s2 = next(
            s for s in r2.test_steps
            if s.qualified_name
            == "samplepkg.test_calculator.test_evaluator_from_zero::step_0"
        )
        assert s2.description == "LLM: from_zero setup."
        # An untouched test's step still has the placeholder
        other = next(
            s for s in r2.test_steps
            if s.qualified_name
            == "samplepkg.test_calculator.test_parser_parse::step_0"
        )
        assert other.description == "Setup block"

    def test_stale_comment_removed_when_description_reverts(self, fixture_dir):
        """If a description reverts to a placeholder, its block is removed."""
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        write_test_comments(r)
        assert f"# {TAG} {self.QN}::step_0" in (
            (fixture_dir / "samplepkg/test_calculator.py").read_text()
        )

        # Re-parse (the comments restore the enriched descriptions), then
        # write back with an override that reverts the step to its parser
        # placeholder.  The step's block must be removed; the test-level
        # block (still enriched) must remain.
        r2 = _parse(fixture_dir)
        report = write_test_comments(
            r2,
            descriptions={
                self.QN: "just the test",
                self.QN + "::step_0": "Setup block",  # placeholder → revert
            },
        )
        text = (fixture_dir / "samplepkg/test_calculator.py").read_text()
        assert f"# {TAG} {self.QN}\n" in text
        assert f"# {TAG} {self.QN}::step_0" not in text
        assert report.nodes_written >= 1  # test (+ other enriched children) remain
        assert report.nodes_skipped_placeholder >= 1  # the reverted step


# ══════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Scaffold mode — add comment slots without enriching first
# ══════════════════════════════════════════════════════════════════════════


class TestScaffold:
    """Scaffold mode inserts empty comment slots for every test element,
    so descriptions can be authored by hand without an LLM enrichment run.
    """

    QN = "samplepkg.test_calculator.test_evaluator_step"
    FILE = "samplepkg/test_calculator.py"

    def test_scaffold_writes_slot_for_every_element(self, fixture_dir):
        r = _parse(fixture_dir)  # all placeholders / docstrings
        report = write_test_comments(r, scaffold=True)
        assert report.nodes_scaffolded >= 1
        assert report.nodes_skipped_placeholder == 0
        assert report.files_changed
        text = (fixture_dir / self.FILE).read_text()
        # Every test element gets a (bare) tag line, including the test whose
        # description is currently just the docstring brief.
        assert f"# {TAG} {self.QN}\n" in text
        assert f"# {TAG} {self.QN}::step_0\n" in text
        assert f"# {TAG} {self.QN}::post_0\n" in text
        assert f"# {TAG} {self.QN}::evaluator\n" in text

    def test_scaffold_slot_is_bare_tag_line(self, fixture_dir):
        """An empty slot is just the tag line — no description text yet."""
        r = _parse(fixture_dir)
        write_test_comments(r, scaffold=True)
        lines = (fixture_dir / self.FILE).read_text().splitlines()
        step_tag_idx = next(
            i for i, ln in enumerate(lines)
            if ln.strip() == f"# {TAG} {self.QN}::step_0"
        )
        # The line directly below the slot tag is the step's first code line
        # (no '# …' description line present yet).
        assert not lines[step_tag_idx + 1].lstrip().startswith("#")

    def test_empty_slot_does_not_clobber_placeholder_on_reparse(self, fixture_dir):
        """A bare scaffold slot reads back as empty and is ignored."""
        r = _parse(fixture_dir)
        write_test_comments(r, scaffold=True)
        r2 = _parse(fixture_dir)
        step = next(
            s for s in r2.test_steps
            if s.qualified_name == self.QN + "::step_0"
        )
        # Still the parser placeholder — the empty slot did not wipe it.
        assert step.description == "Setup block"

    def test_hand_filled_slot_restores_description(self, fixture_dir):
        """Filling a slot by hand, then re-parsing, restores the description."""
        r = _parse(fixture_dir)
        write_test_comments(r, scaffold=True)
        f = fixture_dir / self.FILE
        text = f.read_text()
        tag = f"# {TAG} {self.QN}::step_0"
        text = text.replace(tag, tag + "\n    # Hand-written step description.", 1)
        f.write_text(text)

        r2 = _parse(fixture_dir)
        step = next(
            s for s in r2.test_steps
            if s.qualified_name == self.QN + "::step_0"
        )
        assert step.description == "Hand-written step description."

    def test_scaffold_preserves_already_real_descriptions(self, fixture_dir):
        """An element that already has a real description is written, not slotted."""
        r = _parse(fixture_dir)
        _enrich(r, self.QN)  # gives real descriptions to this test's children
        report = write_test_comments(r, scaffold=True)
        text = (fixture_dir / self.FILE).read_text()
        # The enriched step carries its description, not a bare slot.
        assert "LLM: sets up and drives the test." in text
        # An untouched test's step still gets a bare slot.
        assert f"# {TAG} samplepkg.test_calculator.test_parser_parse::step_0\n" in text

    def test_scaffold_idempotent(self, fixture_dir):
        r = _parse(fixture_dir)
        write_test_comments(r, scaffold=True)
        text_after = (fixture_dir / self.FILE).read_text()
        r2 = _parse(fixture_dir)
        report2 = write_test_comments(r2, scaffold=True)
        assert report2.files_changed == []
        assert (fixture_dir / self.FILE).read_text() == text_after

    def test_scaffold_dry_run_writes_nothing(self, fixture_dir):
        r = _parse(fixture_dir)
        before = (fixture_dir / self.FILE).read_text()
        report = write_test_comments(r, scaffold=True, dry_run=True)
        assert report.nodes_scaffolded >= 1
        assert (fixture_dir / self.FILE).read_text() == before


class TestLineWrapping:
    """Comment blocks wrap to a max line length (textwrap), reflowing on read
    so wrapping is lossless and idempotent.
    """

    QN = "samplepkg.test_calculator.test_evaluator_step"
    FILE = "samplepkg/test_calculator.py"

    def test_long_description_wraps_to_width(self, fixture_dir):
        long = ("Sets up an Evaluator starting at zero and applies two "
                "operations to verify that accumulation across steps produces "
                "the expected total without drift.")
        r = _parse(fixture_dir)
        _enrich(r, self.QN)
        # Inject a long description and write at a narrow width.
        for s in r.test_steps:
            if s.qualified_name == self.QN + "::step_0":
                s.description = long
        write_test_comments(r, width=40)
        text = (fixture_dir / self.FILE).read_text()
        lines = text.splitlines()
        # Every continuation line under the step tag fits within 40 chars.
        tag = f"    # {TAG} {self.QN}::step_0"
        idx = lines.index(tag)
        cont = []
        j = idx + 1
        while j < len(lines) and lines[j].lstrip().startswith("#"):
            cont.append(lines[j])
            j += 1
        assert cont, "expected continuation lines"
        assert all(len(ln) <= 40 for ln in cont)
        # The tag line itself is not wrapped (structural identifier).
        assert len(tag) <= 88

    def test_wrapping_reflow_is_lossless(self, fixture_dir):
        """write (wrapped) → read (reflow) restores the original description."""
        long = ("Sets up an Evaluator and applies two operations to verify "
                "that accumulation across steps produces the expected total.")
        r = _parse(fixture_dir)
        for s in r.test_steps:
            if s.qualified_name == self.QN + "::step_0":
                s.description = long
        write_test_comments(r, width=40)
        r2 = _parse(fixture_dir)
        step = next(s for s in r2.test_steps
                    if s.qualified_name == self.QN + "::step_0")
        assert step.description == long  # reflowed back to one line

    def test_wrapping_idempotent(self, fixture_dir):
        long = ("Sets up an Evaluator and applies two operations to verify "
                "that accumulation across steps produces the expected total.")
        r = _parse(fixture_dir)
        for s in r.test_steps:
            if s.qualified_name == self.QN + "::step_0":
                s.description = long
        write_test_comments(r, width=40)
        text_after = (fixture_dir / self.FILE).read_text()
        r2 = _parse(fixture_dir)
        report2 = write_test_comments(r2, width=40)
        assert report2.files_changed == []
        assert (fixture_dir / self.FILE).read_text() == text_after

    def test_paragraph_break_preserved_as_bare_comment(self, fixture_dir):
        """A blank line in the description becomes a bare '#' paragraph break."""
        desc = "First paragraph of the step.\n\nSecond paragraph here."
        r = _parse(fixture_dir)
        for s in r.test_steps:
            if s.qualified_name == self.QN + "::step_0":
                s.description = desc
        write_test_comments(r, width=80)
        text = (fixture_dir / self.FILE).read_text()
        # A bare '#' line separates the two paragraphs.
        assert "    #\n" in text or "    #\n    #" in text
        r2 = _parse(fixture_dir)
        step = next(s for s in r2.test_steps
                    if s.qualified_name == self.QN + "::step_0")
        assert step.description == desc

    def test_default_width_is_88(self, fixture_dir):
        """Default wrapping width matches Black's line length (88)."""
        long = "word " * 30  # 150 chars
        r = _parse(fixture_dir)
        for s in r.test_steps:
            if s.qualified_name == self.QN + "::step_0":
                s.description = long.strip()
        write_test_comments(r)  # default width
        text = (fixture_dir / self.FILE).read_text()
        lines = text.splitlines()
        tag = f"    # {TAG} {self.QN}::step_0"
        idx = lines.index(tag)
        cont = []
        j = idx + 1
        while j < len(lines) and lines[j].lstrip().startswith("#"):
            cont.append(lines[j])
            j += 1
        assert all(len(ln) <= 88 for ln in cont)


class TestGraphDescriptionsOverride:
    """The `descriptions` override is how graph-side enriched values are
    fed into write_test_comments.  Verify the override → comment flow.
    """

    QN = "samplepkg.test_calculator.test_evaluator_step"
    STEP_QN = QN + "::step_0"
    FILE = "samplepkg/test_calculator.py"

    def test_override_writes_graph_description(self, fixture_dir):
        r = _parse(fixture_dir)
        override = {
            self.QN: "Graph: drives the evaluator across steps.",
            self.STEP_QN: "Graph: sets up the evaluator.",
        }
        report = write_test_comments(r, descriptions=override)
        assert report.files_changed
        text = (fixture_dir / self.FILE).read_text()
        assert "# Graph: drives the evaluator across steps." in text
        assert "# Graph: sets up the evaluator." in text

    def test_override_then_reparse_restores_description(self, fixture_dir):
        r = _parse(fixture_dir)
        override = {self.STEP_QN: "Graph: sets up the evaluator."}
        write_test_comments(r, descriptions=override)
        r2 = _parse(fixture_dir)
        step = next(s for s in r2.test_steps
                    if s.qualified_name == self.STEP_QN)
        assert step.description == "Graph: sets up the evaluator."

    def test_override_plus_scaffold_fills_described_and_slots_rest(self, fixture_dir):
        r = _parse(fixture_dir)
        override = {self.STEP_QN: "Graph: sets up the evaluator."}
        report = write_test_comments(r, descriptions=override, scaffold=True)
        text = (fixture_dir / self.FILE).read_text()
        # The overridden step carries its real description.
        assert "# Graph: sets up the evaluator." in text
        # An undescribed sibling step gets a bare slot.
        assert (f"# {TAG} "
                f"samplepkg.test_calculator.test_evaluator_from_zero::step_0\n"
                in text)
        assert report.nodes_scaffolded >= 1


class TestFetchNodeDescriptions:
    """Unit-test the Neo4j read helper with a mocked cypher_query."""

    def test_returns_non_placeholder_descriptions(self, monkeypatch):
        from doxygen_index import neo4j_backend as nb
        from neomodel import db

        rows = [
            ["a.test_evaluator", "Graph: drives the evaluator."],
            ["a.test_evaluator::step_0", "Setup block"],   # placeholder
            ["a.other::step_0", ""],                       # empty
        ]
        monkeypatch.setattr(db, "cypher_query",
                            lambda q, params=None, **k: (rows, None))
        out = nb.fetch_node_descriptions(
            ["a.test_evaluator", "a.test_evaluator::step_0", "a.other::step_0"]
        )
        assert out == {"a.test_evaluator": "Graph: drives the evaluator."}

    def test_include_placeholder_returns_placeholders_too(self, monkeypatch):
        from doxygen_index import neo4j_backend as nb
        from neomodel import db
        rows = [
            ["a.test_evaluator", "Graph: drives the evaluator."],
            ["a.test_evaluator::step_0", "Setup block"],
        ]
        monkeypatch.setattr(db, "cypher_query",
                            lambda q, params=None, **k: (rows, None))
        out = nb.fetch_node_descriptions(["a.test_evaluator",
                                          "a.test_evaluator::step_0"],
                                         include_placeholder=True)
        assert out == {
            "a.test_evaluator": "Graph: drives the evaluator.",
            "a.test_evaluator::step_0": "Setup block",
        }

    def test_query_error_returns_empty_with_warning(self, monkeypatch, capsys):
        from doxygen_index import neo4j_backend as nb
        from neomodel import db

        def boom(q, params=None, **k):
            raise RuntimeError("no connection")
        monkeypatch.setattr(db, "cypher_query", boom)
        out = nb.fetch_node_descriptions(["a.b"])
        assert out == {}
        assert "could not fetch descriptions from Neo4j" in capsys.readouterr().err

    def test_empty_input_returns_empty(self):
        from doxygen_index.neo4j_backend import fetch_node_descriptions
        assert fetch_node_descriptions([]) == {}


class TestEdgeCases:
    def test_decorated_test_gets_comment_above_decorator(self, tmp_path):
        """A comment must not land between a decorator and the def."""
        f = tmp_path / "t.py"
        f.write_text(
            textwrap.dedent('''\
                import pytest

                @pytest.mark.integration
                def test_decorated():
                    assert True
                '''),
            encoding="utf-8",
        )
        # Build a minimal ParseResult by parsing the single file via the
        # public parser (root = tmp_path so module name is 't').
        result = parse_python_dir(
            [tmp_path], source="t", layer="codebase",
            exclude_dirs=None, progress_interval=0,
        )
        assert result.tests
        result.tests[0].description = "LLM: verifies decoration handling."
        write_test_comments(result)
        lines = f.read_text().splitlines()
        def_idx = next(i for i, ln in enumerate(lines) if ln.startswith("def test_decorated"))
        dec_idx = next(i for i, ln in enumerate(lines) if ln.startswith("@pytest"))
        # The tag block sits at/above the decorator, not between dec and def
        tag_idx = next(i for i, ln in enumerate(lines) if TAG in ln)
        assert tag_idx < dec_idx < def_idx