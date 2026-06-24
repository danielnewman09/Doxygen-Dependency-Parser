"""Tests for LLM test-description enrichment — prompt building and dry-run.

These tests verify the deterministic parts of the enrichment pipeline
without making live LLM calls.  They parse the Python fixture under
``tests/languages/python/samplepkg`` and exercise:

- Prompt builders for fixtures, steps, and assertions.
- EnrichmentResult and EnrichmentSummary dataclass behaviors.
- Dry-run mode (builds prompts, no LLM).
- Skip logic for already-enriched nodes.
- Overwrite mode.
- CLI convenience functions.
"""

import json
from pathlib import Path

import pytest

from doxygen_index.parser import parse_python_dir
from doxygen_index.enrich import (
    EnrichmentResult,
    EnrichmentSummary,
    enrich_result,
    enrich_single_test,
    enrich_test_descriptions,
    enrich_all_tests,
    _build_fixture_prompt,
    _build_step_prompt,
    _build_assertion_prompt,
    _ENRICH_SYSTEM_PROMPT,
    _write_llm_log,
    _safe_attr,
    _group_test_children,
    _build_verifies_for_test,
    _safe_attr,
)


# ── Fixture ────────────────────────────────────────────────────────────

LANGUAGES_DIR = Path(__file__).parent / "languages"
PYTHON_FIXTURE_DIR = LANGUAGES_DIR / "python"


@pytest.fixture(scope="module")
def parse_result():
    """Parse the samplepkg Python fixture once for all tests in this module."""
    result = parse_python_dir(
        [PYTHON_FIXTURE_DIR / "samplepkg"],
        source="samplepkg",
        layer="codebase",
        exclude_dirs=None,
    )
    return result


@pytest.fixture
def test_groups(parse_result):
    """Return the grouped test children from the parse result."""
    return _group_test_children(parse_result)


@pytest.fixture
def tests_by_qn(parse_result):
    """Return tests keyed by qualified_name."""
    lookup: dict[str, object] = {}
    for t in parse_result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            lookup[qn] = t
    return lookup


# ════════════════════════════════════════════════════════════════════════
# Test system prompt
# ════════════════════════════════════════════════════════════════════════


class TestSystemPrompt:
    """Verify the system prompt contains required constraints."""

    def test_system_prompt_not_empty(self):
        assert len(_ENRICH_SYSTEM_PROMPT) > 100

    def test_system_prompt_explains_purpose(self):
        assert "bridge" in _ENRICH_SYSTEM_PROMPT.lower()
        assert "requirement" in _ENRICH_SYSTEM_PROMPT.lower()

    def test_system_prompt_constrains_length(self):
        assert "one or two sentences" in _ENRICH_SYSTEM_PROMPT.lower()
        assert "concise" in _ENRICH_SYSTEM_PROMPT.lower()

    def test_system_prompt_covers_all_node_types(self):
        assert "fixture" in _ENRICH_SYSTEM_PROMPT.lower()
        assert "step" in _ENRICH_SYSTEM_PROMPT.lower()
        assert "assertion" in _ENRICH_SYSTEM_PROMPT.lower()

    def test_system_prompt_forbids_empty_responses(self):
        assert "never return empty" in _ENRICH_SYSTEM_PROMPT.lower()


# ════════════════════════════════════════════════════════════════════════
# Test prompt building (no Neo4j required)
# ════════════════════════════════════════════════════════════════════════


class TestBuildFixturePrompt:
    """Verify fixture prompt structure."""

    def test_build_minimal_fixture_prompt(self, parse_result, tests_by_qn):
        """Prompt contains the fixture's name and type even without peers."""
        # Find a test with fixtures
        for group_qn, children in _group_test_children(parse_result).items():
            if children["fixtures"]:
                test = tests_by_qn[group_qn]
                fixture = children["fixtures"][0]
                verifies = _build_verifies_for_test(test, parse_result, tests_by_qn)
                prompt = _build_fixture_prompt(fixture, test, children, verifies)

                assert _safe_attr(fixture, "name") in prompt
                assert "what does it represent" in prompt.lower()
                assert "why was it created" in prompt.lower()
                return

        pytest.skip("No test fixtures found in parse result")

    def test_build_fixture_prompt_includes_peer_fixtures(
        self, parse_result, tests_by_qn
    ):
        """Prompt references other fixtures in the test for cross-context."""
        # Find a test with multiple fixtures
        for group_qn, children in _group_test_children(parse_result).items():
            if len(children["fixtures"]) >= 2:
                test = tests_by_qn[group_qn]
                fixture = children["fixtures"][0]
                verifies = _build_verifies_for_test(test, parse_result, tests_by_qn)
                prompt = _build_fixture_prompt(fixture, test, children, verifies)

                assert "Other fixtures in this test" in prompt
                # Should mention at least one peer fixture
                peer_name = _safe_attr(children["fixtures"][1], "name")
                assert peer_name in prompt
                return

        pytest.skip("No test with multiple fixtures found")


class TestBuildStepPrompt:
    """Verify step prompt structure."""

    def test_build_step_prompt(self, parse_result, tests_by_qn):
        """Step prompt includes step name and ordering."""
        for group_qn, children in _group_test_children(parse_result).items():
            if children["steps"]:
                test = tests_by_qn[group_qn]
                step = children["steps"][0]
                verifies = _build_verifies_for_test(test, parse_result, tests_by_qn)
                prompt = _build_step_prompt(step, test, children, verifies)

                assert _safe_attr(step, "name") in prompt
                assert "what action does it perform" in prompt.lower()
                return

        pytest.skip("No test steps found in parse result")


class TestBuildAssertionPrompt:
    """Verify assertion prompt structure."""

    def test_build_assertion_prompt(self, parse_result, tests_by_qn):
        """Assertion prompt includes phase and operator."""
        for group_qn, children in _group_test_children(parse_result).items():
            if children["assertions"]:
                test = tests_by_qn[group_qn]
                assertion = children["assertions"][0]
                verifies = _build_verifies_for_test(test, parse_result, tests_by_qn)
                prompt = _build_assertion_prompt(
                    assertion, test, children, verifies
                )

                phase = _safe_attr(assertion, "phase", "post")
                assert phase in prompt
                assert "what condition does it verify" in prompt.lower()
                return

        pytest.skip("No assertions found in parse result")


# ════════════════════════════════════════════════════════════════════════
# Test enrichment result types
# ════════════════════════════════════════════════════════════════════════


class TestEnrichmentResult:
    """Test EnrichmentResult dataclass behavior."""

    def test_created_result_is_not_changed(self):
        r = EnrichmentResult(
            qualified_name="test::x",
            node_type="fixture",
        )
        assert not r.changed
        assert not r.success

    def test_different_description_is_changed(self):
        r = EnrichmentResult(
            qualified_name="test::x",
            node_type="fixture",
            old_description="old",
            new_description="new description",
        )
        assert r.changed
        assert r.success

    def test_same_description_is_not_changed(self):
        r = EnrichmentResult(
            qualified_name="test::x",
            node_type="fixture",
            old_description="same",
            new_description="same",
        )
        assert not r.changed

    def test_error_result_is_not_success(self):
        r = EnrichmentResult(
            qualified_name="test::x",
            node_type="fixture",
            old_description="old",
            new_description="new",
            error="LLM timeout",
        )
        assert not r.success  # error overrides changed

    def test_skipped_result(self):
        r = EnrichmentResult(
            qualified_name="test::x",
            node_type="fixture",
            skipped=True,
            skip_reason="Already has description",
        )
        assert r.skipped
        assert not r.success


class TestEnrichmentSummary:
    """Test EnrichmentSummary aggregation."""

    def test_empty_summary(self):
        s = EnrichmentSummary()
        assert s.total_enriched == 0
        assert s.total_skipped == 0
        assert s.total_errors == 0
        assert s.all_results == s.results  # no test_name set

    def test_single_test_summary_with_buckets(self):
        """Fixtures, steps, assertions are separated for single tests."""
        s = EnrichmentSummary(
            test_name="tests::test_x",
            fixtures=[
                EnrichmentResult("a", "fixture", old_description="",
                                 new_description="engine", skipped=False),
                EnrichmentResult("b", "fixture", skipped=True,
                                 skip_reason="already set"),
            ],
            steps=[
                EnrichmentResult("c", "step", old_description="",
                                 new_description="calls init", skipped=False),
            ],
            assertions=[
                EnrichmentResult("d", "assertion", old_description="",
                                 new_description="", error="timeout"),
            ],
        )
        s.total_enriched = 2
        s.total_skipped = 1
        s.total_errors = 1

        assert s.total_enriched == 2
        assert s.total_skipped == 1
        assert s.total_errors == 1
        assert len(s.all_results) == 4  # fixtures + steps + assertions

    def test_single_test_to_dict(self):
        s = EnrichmentSummary(
            test_name="tests::test_x",
            fixtures=[
                EnrichmentResult("a", "fixture", old_description="",
                                 new_description="engine"),
            ],
        )
        d = s.to_dict()
        assert d["test_name"] == "tests::test_x"
        assert len(d["fixtures"]) == 1
        assert d["fixtures"][0]["description"] == "engine"
        assert d["fixtures"][0]["changed"] is True

    def test_batch_summary_to_dict(self):
        s = EnrichmentSummary()
        s.results = [
            EnrichmentResult("a", "fixture", old_description="",
                             new_description="engine"),
        ]
        s.total_enriched = 1
        d = s.to_dict()
        assert "test_name" not in d
        assert d["total_enriched"] == 1
        assert len(d["results"]) == 1


# ════════════════════════════════════════════════════════════════════════
# Test enrichment dry-run mode against real parse output
# ════════════════════════════════════════════════════════════════════════


class TestEnrichDryRun:
    """Integration tests using dry_run mode — no LLM calls."""

    def test_dry_run_enrich_result(self, parse_result):
        """In dry-run mode, all nodes get skip markers."""
        summary = enrich_result(
            parse_result,
            model="test-model",
            dry_run=True,
            overwrite=True,  # avoid pre-skip from existing descriptions
        )

        # All results should be skipped (dry-run)
        assert summary.total_enriched == 0
        for r in summary.results:
            assert r.skipped
            assert "dry_run" in r.skip_reason

    def test_dry_run_enrich_single_test(self, parse_result):
        """Dry-run single test enrichment works."""
        # Find first test
        if not parse_result.tests:
            pytest.skip("No tests in parse result")

        test_qn = _safe_attr(parse_result.tests[0], "qualified_name")
        summary = enrich_single_test(
            parse_result, test_qn,
            model="test-model", dry_run=True, overwrite=True,
        )

        assert summary.test_name == test_qn
        assert summary.total_enriched == 0
        for r in summary.all_results:
            assert r.skipped
            assert f"dry_run" in r.skip_reason

    def test_dry_run_enrich_all_tests(self, parse_result):
        """Dry-run enrich_all_tests covers every test."""
        if not parse_result.tests:
            pytest.skip("No tests in parse result")

        results = enrich_all_tests(
            parse_result,
            model="test-model", dry_run=True, overwrite=True,
        )

        assert len(results) > 0
        for qn, summary in results.items():
            assert summary.total_enriched == 0
            for r in summary.all_results:
                assert r.skipped
                assert "dry_run" in r.skip_reason

    def test_skip_already_enriched_nodes(self, parse_result):
        """Nodes with existing descriptions are skipped when overwrite=False."""
        if not parse_result.test_fixtures:
            pytest.skip("No test fixtures in parse result")

        # Pre-set a description on one fixture
        fixture = parse_result.test_fixtures[0]
        original_desc = getattr(fixture, "description", "")
        fixture.description = "Already described fixture."

        try:
            summary = enrich_result(
                parse_result,
                model="test-model",
                dry_run=True,
                overwrite=False,
            )

            # Find the result for our enriched fixture
            enriched_result = next(
                (r for r in summary.results
                 if r.qualified_name == _safe_attr(fixture, "qualified_name")),
                None,
            )
            if enriched_result:
                assert enriched_result.old_description == "Already described fixture."
                # Should be skipped due to existing description
                assert enriched_result.skipped
                assert enriched_result.skip_reason == "Already has a description"
        finally:
            # Restore original
            if original_desc:
                fixture.description = original_desc

    def test_overwrite_targets_enriched_nodes(self, parse_result):
        """With overwrite=True, already-enriched nodes are not skipped early."""
        if not parse_result.test_fixtures:
            pytest.skip("No test fixtures in parse result")

        fixture = parse_result.test_fixtures[0]
        original_desc = getattr(fixture, "description", "")
        fixture.description = "old description"

        try:
            summary = enrich_result(
                parse_result,
                model="test-model",
                dry_run=True,
                overwrite=True,
            )

            # Find the result for our fixture
            enriched_result = next(
                (r for r in summary.results
                 if r.qualified_name == _safe_attr(fixture, "qualified_name")),
                None,
            )
            if enriched_result:
                # With overwrite=True, should NOT be skipped for "already has desc"
                # It WILL be skipped for dry_run
                assert enriched_result.skipped
                assert "dry_run" in enriched_result.skip_reason
                assert enriched_result.old_description == "old description"
        finally:
            if original_desc:
                fixture.description = original_desc


class TestEnrichmentConvenienceFunctions:
    """Test the public API convenience functions."""

    def test_enrich_test_descriptions(self, parse_result):
        """enrich_test_descriptions handles named tests."""
        if not parse_result.tests:
            pytest.skip("No tests in parse result")

        qns = [
            _safe_attr(t, "qualified_name")
            for t in parse_result.tests[:2]
        ]
        results = enrich_test_descriptions(
            parse_result, qns,
            model="test-model", dry_run=True, overwrite=False,
        )

        assert len(results) == len(qns)
        for qn in qns:
            assert qn in results
            assert isinstance(results[qn], EnrichmentSummary)
            assert results[qn].total_enriched == 0

    def test_enrich_test_descriptions_missing_test(self):
        """Missing test produces error summary."""
        results = enrich_test_descriptions(
            parse_result,
            ["tests::nonexistent_test"],
            model="test-model", dry_run=True,
        )
        assert "tests::nonexistent_test" in results
        assert results["tests::nonexistent_test"].errors

    def test_enrich_single_test_missing_raises(self, parse_result):
        """Missing test raises ValueError."""
        with pytest.raises(ValueError, match="TestNode not found"):
            enrich_single_test(
                parse_result, "tests::nonexistent_test",
                model="test-model", dry_run=True,
            )


# ════════════════════════════════════════════════════════════════════════
# Test LLM call logging
# ════════════════════════════════════════════════════════════════════════


class TestLLMCallLogging:
    """Verify that LLM call logs are written when log_dir is set."""

    def test_write_llm_log_creates_dir_and_file(self, tmp_path):
        """_write_llm_log creates the log directory and writes a JSONL line."""
        log_dir = tmp_path / "logs"
        _write_llm_log(
            log_dir,
            qualified_name="tests::test_x::fixture_a",
            node_type="fixture",
            system_prompt="You are a test assistant.",
            user_prompt="Describe this fixture",
            response="A fixture that sets up test state.",
            old_description="",
            new_description="A fixture that sets up test state.",
            model="claude-sonnet-4-20250514",
        )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        assert log_dir.exists()
        assert log_file.exists()

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1

        entry = json.loads(lines[0])
        assert entry["qualified_name"] == "tests::test_x::fixture_a"
        assert entry["node_type"] == "fixture"
        assert entry["response"] == "A fixture that sets up test state."
        assert entry["model"] == "claude-sonnet-4-20250514"
        assert "timestamp" in entry
        assert entry["error"] == ""

    def test_write_llm_log_appends_multiple_calls(self, tmp_path):
        """Multiple calls write one JSONL line each."""
        log_dir = tmp_path / "logs"

        for i in range(3):
            _write_llm_log(
                log_dir,
                qualified_name=f"tests::test_{i}",
                node_type="step",
                system_prompt="sys",
                user_prompt=f"prompt {i}",
                response=f"response {i}",
                model="test-model",
            )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["qualified_name"] == f"tests::test_{i}"
            assert entry["user_prompt"] == f"prompt {i}"

    def test_write_llm_log_captures_error(self, tmp_path):
        """Error field is populated when an LLM call fails."""
        log_dir = tmp_path / "logs"
        _write_llm_log(
            log_dir,
            qualified_name="tests::test_fail",
            node_type="assertion",
            system_prompt="sys",
            user_prompt="prompt",
            response="",
            model="test-model",
            error="LLM timeout after 30s",
        )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        entry = json.loads(log_file.read_text(encoding="utf-8").strip())
        assert entry["error"] == "LLM timeout after 30s"
        assert entry["response"] == ""

    def test_dry_run_with_log_dir_does_not_log(self, parse_result, tmp_path):
        """When dry_run is True, no LLM calls happen → no log entries."""
        log_dir = tmp_path / "logs"
        enrich_result(
            parse_result,
            dry_run=True,
            overwrite=True,
            log_dir=log_dir,
        )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        # dry_run skips all nodes, so logging is never triggered
        assert not log_file.exists()

    def test_log_dir_accepted_by_all_public_apis(self, parse_result, tmp_path):
        """All public enrichment functions accept log_dir without error."""
        log_dir = tmp_path / "logs"

        # enrich_result
        summary = enrich_result(
            parse_result, dry_run=True, overwrite=True, log_dir=log_dir,
        )
        assert summary.total_skipped > 0

        # enrich_single_test
        qn = _safe_attr(parse_result.tests[0], "qualified_name")
        summary = enrich_single_test(
            parse_result, qn, dry_run=True, overwrite=True, log_dir=log_dir,
        )
        assert summary.test_name == qn

        # enrich_test_descriptions
        qns = [_safe_attr(t, "qualified_name") for t in parse_result.tests[:2]]
        results = enrich_test_descriptions(
            parse_result, qns, dry_run=True, overwrite=True, log_dir=log_dir,
        )
        assert len(results) == 2

        # enrich_all_tests
        results = enrich_all_tests(
            parse_result, dry_run=True, overwrite=True, log_dir=log_dir,
        )
        assert len(results) == len(parse_result.tests)

    def test_log_entry_structure_is_valid_jsonl(self, tmp_path):
        """Every log line is valid, self-contained JSON."""
        log_dir = tmp_path / "logs"

        _write_llm_log(
            log_dir,
            qualified_name="a", node_type="fixture",
            system_prompt="s", user_prompt="u",
            response="r", model="m",
        )
        _write_llm_log(
            log_dir,
            qualified_name="b", node_type="step",
            system_prompt='s2', user_prompt='u2',
            response='r2', model='m2',
        )

        log_file = log_dir / "enrich_llm_calls.jsonl"
        for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
            entry = json.loads(line)  # must not raise
            assert isinstance(entry, dict)
            assert "timestamp" in entry
            assert "qualified_name" in entry
            assert "node_type" in entry
            assert "system_prompt" in entry
            assert "user_prompt" in entry
            assert "response" in entry
            assert "model" in entry
