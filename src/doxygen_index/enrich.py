"""LLM-based description enrichment for parsed test metadata.

When LLM credentials are available (``LLM_API_KEY`` env var), this module
enriches the ``description`` field on test-related nodes produced by the
doxygen-index parser.  The enrichment runs **in-memory** on a
:class:`ParseResult` before it is written to JSON or Neo4j, producing an
"llm-enriched" code graph.

The enrichment works by:
1. Grouping test nodes with their composed children (fixtures, steps, assertions)
   using the composition edge lists in ParseResult.
2. Building structured prompts for each node type — fixture, step, assertion —
   that include the code under test and sibling (peer) context.
3. Calling an OpenAI-compatible LLM API for each node.
4. Updating the node's ``description`` field in place.

Public API
----------

Three convenience functions are provided for the most common call
patterns used by the ticketing system:

.. function:: enrich_single_test(result, test_qualified_name, *, model, dry_run, overwrite)

   Enrich descriptions for a single test.  Returns an
   :class:`EnrichmentSummary`.

   >>> from doxygen_index.enrich import enrich_single_test
   >>> summary = enrich_single_test(result, "tests::test_engine::test_set_target")
   >>> print(summary.total_enriched)

.. function:: enrich_test_descriptions(result, test_qualified_names, *, model, dry_run, overwrite)

   Enrich descriptions for one or more named tests.  Returns a dict
   mapping ``test_qualified_name`` to :class:`EnrichmentSummary`.

   >>> from doxygen_index.enrich import enrich_test_descriptions
   >>> results = enrich_test_descriptions(result, [
   ...     "tests::test_engine::test_set_target",
   ...     "tests::test_engine::test_compute",
   ... ])

.. function:: enrich_all_tests(result, *, model, dry_run, overwrite)

   Enrich every test in the ParseResult.  Returns a dict mapping
   ``test_qualified_name`` to :class:`EnrichmentSummary`.

   >>> from doxygen_index.enrich import enrich_all_tests
   >>> results = enrich_all_tests(result)
   >>> for qn, s in results.items():
   ...     print(f"{qn}: {s.total_enriched} nodes enriched")


Usage (from the CLI)::

    doxygen-index project . --enrich

Usage (programmatic)::

    from doxygen_index.enrich import enrich_result

    result = parse_python_dir(...)
    summary = enrich_result(result)
    print(f"Enriched {summary.total_enriched} nodes")

Environment:
    Requires ``LLM_API_KEY`` (required) and optionally ``LLM_BASE_URL``,
    ``LLM_MODEL`` (default: ``"claude-sonnet-4-20250514"``).

    Requires the ``openai`` package: ``pip install openai``
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from doxygen_index.parser.model import ParseResult

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# LLM call helper (provider-agnostic, OpenAI-compatible API)
# ══════════════════════════════════════════════════════════════════════════


def _write_llm_log(
    log_dir: str | Path,
    *,
    qualified_name: str,
    node_type: str,
    system_prompt: str,
    user_prompt: str,
    response: str = "",
    old_description: str = "",
    new_description: str = "",
    model: str = "",
    error: str = "",
) -> None:
    """Write a structured JSON log entry for an LLM enrichment call.

    Creates *log_dir* if it doesn't exist.  Writes one JSON object per
    line (JSONL) to ``enrich_llm_calls.jsonl`` inside *log_dir*.

    Args:
        log_dir: Path to the logs output directory.
        qualified_name: The enriched node's qualified name.
        node_type: One of ``"fixture"``, ``"step"``, ``"assertion"``.
        system_prompt: The system prompt sent to the LLM.
        user_prompt: The user prompt (node context) sent to the LLM.
        response: The LLM's text response (empty on error).
        old_description: The node's description before enrichment.
        new_description: The node's description after enrichment.
        model: The LLM model identifier.
        error: Error message if the call failed (empty on success).
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "qualified_name": qualified_name,
        "node_type": node_type,
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response": response,
        "old_description": old_description,
        "new_description": new_description,
        "error": error,
    }

    log_file = log_path / "enrich_llm_calls.jsonl"
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _llm_complete(
    system: str,
    user: str,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 256,
    temperature: float = 0.3,
    timeout: int = 30,
) -> str:
    """Call an LLM for text completion using an OpenAI-compatible API.

    Reads credentials from environment variables:

    - ``LLM_API_KEY`` — API key (required).
    - ``LLM_BASE_URL`` — Optional base URL override (defaults to
      ``https://api.anthropic.com/v1`` for Anthropic models,
      ``https://api.openai.com/v1`` for others).

    Args:
        system: System prompt.
        user: User message / prompt.
        model: Model identifier (e.g. ``"claude-sonnet-4-20250514"``,
            ``"gpt-4o"``, ``"llama3.1"``).
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature (0.0 = deterministic).
        timeout: Request timeout in seconds.

    Returns:
        The LLM's text response (stripped).

    Raises:
        RuntimeError: If ``LLM_API_KEY`` is not set or the request fails.
    """
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY environment variable is not set. "
            "Set it to your Anthropic or OpenAI API key."
        )

    base_url = os.getenv("LLM_BASE_URL")
    if not base_url:
        if model.startswith("claude"):
            base_url = "https://api.anthropic.com/v1"
        else:
            base_url = "https://api.openai.com/v1"

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError(
            "The 'openai' package is required for LLM calls. "
            "Install it with: pip install openai"
        )

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc


# ══════════════════════════════════════════════════════════════════════════
# Context builders — group children by test, build prompts
# ══════════════════════════════════════════════════════════════════════════


_ENRICH_SYSTEM_PROMPT = """\
You are a test metadata enrichment assistant. Your task is to generate
concise, human-readable descriptions for test elements extracted from a
Python test suite. These descriptions will serve as the bridge between
deterministically extracted test metadata and formal Low-Level
Requirements (LLRs).

Guidelines:
- Write one or two sentences maximum.
- Focus on the *purpose* and *why* — not implementation syntax.
- Use clear, plain English suitable for non-developer stakeholders.
- Connect the element to the code under test when possible.
- For fixtures: explain what the variable represents and why it is needed.
- For steps: explain what action is performed and how it advances the test.
- For assertions: explain what condition is being verified and why it
  matters to the correctness of the code under test.
- If the description cannot be confidently inferred from the context,
  still produce a best-effort description; never return empty.
- Return ONLY the description text, with no prefixes, labels, or markup."""


def _safe_attr(node, attr: str, default: Any = "") -> Any:
    """Read an attribute from a node, returning *default* if missing."""
    return getattr(node, attr, default)


# --- Placeholder detection -------------------------------------------------
#
# The Python parser auto-generates placeholder descriptions for steps and
# assertions so the graph isn't empty before enrichment.  These are NOT
# real human-readable descriptions — they're structural labels like
# "Setup block", "Action block 2", or the raw assert text like
# "assert len(parents) == 1".  Enrichment should overwrite them.

import re as _re

_PLACEHOLDER_PATTERNS = [
    _re.compile(r"^Setup block$"),
    _re.compile(r"^Action block \d+$"),
    _re.compile(r"^assert .+$"),  # raw assert expressions
]


def _is_placeholder_description(desc: str) -> bool:
    """Return True if *desc* is a parser-generated placeholder.

    These are auto-generated labels from the Python parser (e.g.
    ``"Setup block"``, ``"Action block 3"``, ``"assert x == 1"``)
    that should be treated as empty for enrichment purposes.
    """
    if not desc or not desc.strip():
        return True
    stripped = desc.strip()
    return any(p.match(stripped) for p in _PLACEHOLDER_PATTERNS)


def _node_str(node, *extra_fields: str) -> str:
    """Human-readable summary of a node for use in prompts."""
    parts = [
        _safe_attr(node, "qualified_name")
        or _safe_attr(node, "name", "?")
    ]
    for f in extra_fields:
        val = _safe_attr(node, f)
        if val:
            parts.append(f"({f}={val})")
    desc = _safe_attr(node, "description")
    if desc:
        parts.append(f"— {desc}")
    return " ".join(parts)


def _group_test_children(result: ParseResult) -> dict[str, dict[str, list]]:
    """Group test children by parent test qualified_name.

    Uses ``test_compositions`` (COMPOSES edges: parent → child) and the
    flat lists of fixtures, steps, assertions to build a per-test
    grouping.

    Returns:
        A dict mapping ``test_qualified_name`` → ``{"fixtures": [...],
        "steps": [...], "assertions": [...]}``.
    """
    # Build lookups keyed by qualified_name (which is also the refid
    # used in TestCompositionEntry).
    fixtures_by_qn: dict[str, Any] = {}
    for f in result.test_fixtures:
        qn = _safe_attr(f, "qualified_name")
        if qn:
            fixtures_by_qn[qn] = f

    steps_by_qn: dict[str, Any] = {}
    for s in result.test_steps:
        qn = _safe_attr(s, "qualified_name")
        if qn:
            steps_by_qn[qn] = s

    assertions_by_qn: dict[str, Any] = {}
    for a in result.assertions:
        qn = _safe_attr(a, "qualified_name")
        if qn:
            assertions_by_qn[qn] = a

    # Group by parent (COMPOSES edges)
    groups: dict[str, dict[str, list]] = {}
    for comp in result.test_compositions:
        parent_qn = _safe_attr(comp, "parent_refid")
        child_refid = _safe_attr(comp, "child_refid")

        if parent_qn not in groups:
            groups[parent_qn] = {"fixtures": [], "steps": [], "assertions": []}

        child = (
            fixtures_by_qn.get(child_refid)
            or steps_by_qn.get(child_refid)
            or assertions_by_qn.get(child_refid)
        )
        if child is None:
            continue

        kind = _safe_attr(comp, "child_type", "")
        if kind == "TestFixtureNode":
            groups[parent_qn]["fixtures"].append(child)
        elif kind == "TestStepNode":
            groups[parent_qn]["steps"].append(child)
        elif kind == "AssertionNode":
            groups[parent_qn]["assertions"].append(child)

    return groups


def _build_verifies_for_test(
    test: Any,
    result: ParseResult,
    tests_by_qn: dict[str, Any],
) -> list[dict]:
    """Return the code nodes that a test VERIFIES.

    Looks up ``result.verifies`` entries whose ``from_refid`` matches the
    test's qualified_name, then resolves the target refid to a node.
    """
    test_qn = _safe_attr(test, "qualified_name")
    targets: list[dict] = []

    if not test_qn:
        return targets

    for v in result.verifies:
        if _safe_attr(v, "from_refid") == test_qn:
            to_refid = _safe_attr(v, "to_refid")
            to_type = _safe_attr(v, "to_type", "unknown")

            # Resolve the target to a node so we can show its description
            target_node = _resolve_node_by_refid(result, to_refid)
            targets.append({
                "qualified_name": to_refid,
                "kind": to_type,
                "description": _safe_attr(target_node, "description", ""),
            })

    return targets


def _resolve_node_by_refid(result: ParseResult, refid: str) -> Any:
    """Resolve a refid (qualified_name) to a node from any ParseResult list."""
    all_lists = [
        result.classes, result.enums, result.unions, result.interfaces,
        result.concepts, result.methods, result.attributes, result.functions,
        result.namespaces, result.files,
    ]
    for node_list in all_lists:
        for node in node_list:
            if _safe_attr(node, "qualified_name") == refid:
                return node
    return None


def _build_fixture_prompt(
    fixture: Any,
    test: Any,
    siblings: dict[str, list],
    verifies_targets: list[dict],
) -> str:
    """Build a prompt for enriching a TestFixtureNode description."""
    lines = [
        "## Test Context",
        f"Test: {_safe_attr(test, 'qualified_name') or _safe_attr(test, 'name', '?')}",
        f"Description: {_safe_attr(test, 'description') or '(none)'}",
        "",
        "## Fixture to Describe",
        f"Variable: {_safe_attr(fixture, 'name', '?')}",
        f"Type: {_safe_attr(fixture, 'type_signature') or '(unspecified)'}",
        f"Current description: {_safe_attr(fixture, 'description') or '(empty)'}",
        "",
    ]

    if verifies_targets:
        lines.append("Code under test (what this test exercises):")
        for v in verifies_targets:
            lines.append(
                f"  - {v['qualified_name']} ({v['kind']}): "
                f"{v['description'] or '(no description)'}"
            )
        lines.append("")

    # Peer fixtures for cross-context
    fixture_name = _safe_attr(fixture, "name")
    peers = [
        f for f in siblings.get("fixtures", [])
        if _safe_attr(f, "name") != fixture_name
    ]
    if peers:
        lines.append("Other fixtures in this test:")
        for p in peers:
            lines.append(
                f"  - {_safe_attr(p, 'name')} ({_safe_attr(p, 'type_signature', '?')}): "
                f"{_safe_attr(p, 'description') or '(not set)'}"
            )
        lines.append("")

    lines.append(
        "Generate a concise description of this fixture variable: "
        "what does it represent, why was it created in the test, and "
        "how does it relate to the code under test?"
    )
    return "\n".join(lines)


def _build_step_prompt(
    step: Any,
    test: Any,
    siblings: dict[str, list],
    verifies_targets: list[dict],
) -> str:
    """Build a prompt for enriching a TestStepNode description."""
    lines = [
        "## Test Context",
        f"Test: {_safe_attr(test, 'qualified_name') or _safe_attr(test, 'name', '?')}",
        f"Description: {_safe_attr(test, 'description') or '(none)'}",
        "",
        "## Step to Describe",
        f"Step: {_safe_attr(step, 'name', '?')}",
        f"Order: {_safe_attr(step, 'order', '?')}",
        f"Current description: {_safe_attr(step, 'description') or '(empty)'}",
        "",
    ]

    if verifies_targets:
        lines.append("Code under test:")
        for v in verifies_targets:
            lines.append(f"  - {v['qualified_name']} ({v['kind']})")
        lines.append("")

    # Peer steps for ordering context
    step_name = _safe_attr(step, "name")
    peers = [
        s for s in siblings.get("steps", [])
        if _safe_attr(s, "name") != step_name
    ]
    if peers:
        lines.append("Other steps in this test:")
        for p in sorted(peers, key=lambda x: _safe_attr(x, "order", 0)):
            lines.append(
                f"  - step {_safe_attr(p, 'order', '?')}: "
                f"{_safe_attr(p, 'name')} — "
                f"{_safe_attr(p, 'description') or '(not set)'}"
            )
        lines.append("")

    lines.append(
        "Generate a concise description of this test step: "
        "what action does it perform, and how does it advance the "
        "test toward verification?"
    )
    return "\n".join(lines)


def _build_assertion_prompt(
    assertion: Any,
    test: Any,
    siblings: dict[str, list],
    verifies_targets: list[dict],
) -> str:
    """Build a prompt for enriching an AssertionNode description."""
    lines = [
        "## Test Context",
        f"Test: {_safe_attr(test, 'qualified_name') or _safe_attr(test, 'name', '?')}",
        f"Description: {_safe_attr(test, 'description') or '(none)'}",
        "",
        "## Assertion to Describe",
        f"Name: {_safe_attr(assertion, 'name', '?')}",
        f"Phase: {_safe_attr(assertion, 'phase', 'post')}",
        f"Operator: {_safe_attr(assertion, 'operator', '==')}",
        f"Order: {_safe_attr(assertion, 'order', 0)}",
        f"Current description: {_safe_attr(assertion, 'description') or '(empty)'}",
        "",
    ]

    if verifies_targets:
        lines.append("Code under test:")
        for v in verifies_targets:
            lines.append(f"  - {v['qualified_name']} ({v['kind']})")
        lines.append("")

    # Peer assertions for broader context
    assertion_name = _safe_attr(assertion, "name")
    peers = [
        a for a in siblings.get("assertions", [])
        if _safe_attr(a, "name") != assertion_name
    ]
    if peers:
        lines.append("Other assertions in this test:")
        for p in peers:
            lines.append(
                f"  - {_safe_attr(p, 'name')} "
                f"({_safe_attr(p, 'phase', '?')} "
                f"{_safe_attr(p, 'operator', '?')}): "
                f"{_safe_attr(p, 'description') or '(not set)'}"
            )
        lines.append("")

    lines.append(
        "Generate a concise description of this assertion: "
        "what condition does it verify, and why does this condition "
        "matter to the correctness of the code under test?"
    )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# Enrichment result types
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class EnrichmentResult:
    """Result of enriching a single node's description."""

    qualified_name: str
    node_type: str  # "fixture", "step", "assertion"
    old_description: str = ""
    new_description: str = ""
    error: str | None = None
    skipped: bool = False
    skip_reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.new_description) and self.new_description != self.old_description

    @property
    def success(self) -> bool:
        return self.changed and self.error is None


@dataclass
class EnrichmentSummary:
    """Summary of enrichment for one test or a batch.

    For a single-test summary, ``test_name`` is set and the ``fixtures``,
    ``steps``, ``assertions`` buckets are populated.

    For a batch (multi-test) summary aggregated via ``enrich_result`` or
    ``enrich_all_tests``, ``results`` is the flat list of all individual
    :class:`EnrichmentResult` objects, and ``total_*`` are derived from it.
    Use ``to_dict()`` for serialization.
    """

    test_name: str = ""
    fixtures: list[EnrichmentResult] = field(default_factory=list)
    steps: list[EnrichmentResult] = field(default_factory=list)
    assertions: list[EnrichmentResult] = field(default_factory=list)
    results: list[EnrichmentResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Per-category tallies (set explicitly during enrichment)
    total_enriched: int = 0
    total_skipped: int = 0
    total_errors: int = 0

    @property
    def all_results(self) -> list[EnrichmentResult]:
        """All child results (fixtures + steps + assertions) for one test.

        When the summary holds a single test (``test_name`` is set),
        returns the concatenation of ``fixtures``, ``steps``, and
        ``assertions``.  Otherwise falls back to the flat ``results``
        list.
        """
        if self.test_name:
            return self.fixtures + self.steps + self.assertions
        return self.results

    def to_dict(self) -> dict:
        if self.test_name:
            # Single-test summary
            return {
                "test_name": self.test_name,
                "total_enriched": self.total_enriched,
                "total_skipped": self.total_skipped,
                "total_errors": self.total_errors,
                "fixtures": [
                    {
                        "qualified_name": r.qualified_name,
                        "description": r.new_description,
                        "changed": r.changed,
                        "error": r.error,
                    }
                    for r in self.fixtures
                ],
                "steps": [
                    {
                        "qualified_name": r.qualified_name,
                        "description": r.new_description,
                        "changed": r.changed,
                        "error": r.error,
                    }
                    for r in self.steps
                ],
                "assertions": [
                    {
                        "qualified_name": r.qualified_name,
                        "description": r.new_description,
                        "changed": r.changed,
                        "error": r.error,
                    }
                    for r in self.assertions
                ],
                "errors": self.errors,
            }
        else:
            # Batch summary
            return {
                "total_enriched": self.total_enriched,
                "total_skipped": self.total_skipped,
                "total_errors": self.total_errors,
                "results": [
                    {
                        "qualified_name": r.qualified_name,
                        "node_type": r.node_type,
                        "description": r.new_description,
                        "changed": r.changed,
                        "error": r.error,
                        "skipped": r.skipped,
                    }
                    for r in self.results
                ],
                "errors": self.errors,
            }


# ══════════════════════════════════════════════════════════════════════════
# Core enrichment logic
# ══════════════════════════════════════════════════════════════════════════


def enrich_result(
    result: ParseResult,
    *,
    model: str | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
    max_tokens: int = 256,
    log_dir: str | Path | None = None,
    batch_size: int = 10,
) -> EnrichmentSummary:
    """Enrich test descriptions in a :class:`ParseResult`.

    Groups test children, builds LLM prompts, and updates each node's
    ``description`` field in place.  Only nodes without an existing
    description are enriched (unless *overwrite* is True).

    Args:
        result: The parsed project result (modified in place).
        model: LLM model identifier.  Defaults to ``LLM_MODEL`` env var
            or ``"claude-sonnet-4-20250514"``.
        dry_run: If True, build prompts but do not call the LLM.
        overwrite: If True, overwrite existing descriptions.
        max_tokens: Maximum response tokens per LLM call.

    Returns:
        An :class:`EnrichmentSummary` with aggregated per-node results.

    Raises:
        RuntimeError: If ``LLM_API_KEY`` is not set and *dry_run* is
            False.
    """
    model = model or os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    summary = EnrichmentSummary()

    if not result.tests:
        return summary

    # Build test lookup
    tests_by_qn: dict[str, Any] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            tests_by_qn[qn] = t

    # Group children by test
    groups = _group_test_children(result)

    # Pre-compute verifies targets per test
    verifies_cache: dict[str, list[dict]] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            verifies_cache[qn] = _build_verifies_for_test(t, result, tests_by_qn)

    # Count total nodes for progress display
    total_nodes = sum(
        len(children["fixtures"]) + len(children["steps"]) + len(children["assertions"])
        for children in groups.values()
    )
    if not dry_run:
        print(f"  Nodes to enrich: {total_nodes}")

    # Flatten all nodes of each type across all tests for true batching
    all_fixtures: list[Any] = []
    fixture_contexts: list[tuple[Any, dict[str, list]]] = []
    all_steps: list[Any] = []
    step_contexts: list[tuple[Any, dict[str, list]]] = []
    all_assertions: list[Any] = []
    assertion_contexts: list[tuple[Any, dict[str, list]]] = []

    for test_qn, children in groups.items():
        test = tests_by_qn.get(test_qn)
        if test is None:
            continue
        for node in children["fixtures"]:
            all_fixtures.append(node)
            fixture_contexts.append((test, children))
        for node in children["steps"]:
            all_steps.append(node)
            step_contexts.append((test, children))
        for node in children["assertions"]:
            all_assertions.append(node)
            assertion_contexts.append((test, children))

    node_count = 0

    # Combine ALL types into a single batch stream so that mixed
    # fixture/step/assertion nodes go in the same API call.  This
    # minimises the number of calls and maximises KV-cache reuse.
    combined_nodes: list[Any] = []
    combined_contexts: list[tuple[Any, dict[str, list]]] = []
    combined_types: list[str] = []
    combined_builders: list = []

    for node, ctx in zip(all_fixtures, fixture_contexts):
        combined_nodes.append(node)
        combined_contexts.append(ctx)
        combined_types.append("fixture")
        combined_builders.append(_build_fixture_prompt)
    for node, ctx in zip(all_steps, step_contexts):
        combined_nodes.append(node)
        combined_contexts.append(ctx)
        combined_types.append("step")
        combined_builders.append(_build_step_prompt)
    for node, ctx in zip(all_assertions, assertion_contexts):
        combined_nodes.append(node)
        combined_contexts.append(ctx)
        combined_types.append("assertion")
        combined_builders.append(_build_assertion_prompt)

    if combined_nodes:
        import math
        total_batches_global = math.ceil(len(combined_nodes) / batch_size)
        _enrich_node_list(
            combined_nodes,
            None,  # prompt_builder is per-node now
            "mixed",  # node_type is per-node now
            model, max_tokens, dry_run, overwrite, summary,
            verifies_targets=verifies_cache.get(
                _safe_attr(tests_by_qn.get(list(groups.keys())[0], {}), "qualified_name"),
                [],
            ),
            log_dir=log_dir,
            node_offset=0,
            node_total=total_nodes,
            batch_size=batch_size,
            node_contexts=combined_contexts,
            batch_offset=0,
            total_batches_global=total_batches_global,
            node_types=combined_types,
            node_builders=combined_builders,
        )

    return summary


# ══════════════════════════════════════════════════════════════════════════
# Batch enrichment — shared prefix for KV-cache reuse
# ══════════════════════════════════════════════════════════════════════════

# The system prompt carries ALL shared text (guidelines, format, instructions)
# so the LLM can reuse its KV-cache across every batch call.  Only the
# per-batch node details go into the user message.

_BATCH_SYSTEM_PROMPT = """\
You are a test metadata enrichment assistant. Your task is to generate
concise, human-readable descriptions for multiple test elements extracted
from a Python test suite. These descriptions bridge deterministic test
metadata and formal Low-Level Requirements (LLRs).

Guidelines:
- Write one or two sentences per element maximum.
- Focus on the *purpose* and *why* — not implementation syntax.
- Use clear, plain English suitable for non-developer stakeholders.
- Connect each element to the code under test when possible.
- For fixtures: what the variable represents and why it is needed.
- For steps: what action is performed and how it advances the test.
- For assertions: what condition is being verified and why it matters.

Response format — return ONLY a JSON object (no markdown, no explanation):

{
  "qualified_name_1": "one or two sentence description",
  "qualified_name_2": "one or two sentence description"
}"""


def _build_batch_user_prompt(
    elements: list[tuple],
    node_type: str,
    prompt_builder,
    verifies_targets: list[dict],
) -> str:
    """Build the per-batch user prompt — only the elements that vary.

    The system prompt carries everything shared.  This function builds
    the user message, which starts with an identical prefix (for
    KV-cache reuse) followed by batch-specific element details.

    Args:
        elements: List of ``(node, test, siblings)`` tuples (single-type)
            or ``(node, test, siblings, node_type, prompt_builder)``
            tuples (mixed-type).  Extra trailing items are ignored.
        node_type: Fallback type label (``"mixed"`` for combined batches).
        prompt_builder: Fallback builder function.
        verifies_targets: Code nodes under test (shared for compatibility).
    """
    # Detect mixed-type elements (5-tuple vs 3-tuple)
    mixed = len(elements) > 0 and len(elements[0]) >= 5

    # KV-cache optimization: the first line is IDENTICAL across all
    # batches.  Variable info (count, type breakdown) goes at the END
    # so the prefix tokens can be reused from cache.
    if mixed:
        type_counts: dict[str, int] = {}
        for _n, _t, _s, nt, _pb in elements:
            type_counts[nt] = type_counts.get(nt, 0) + 1
        type_str = ", ".join(
            f"{count} {nt}{'s' if count != 1 else ''}"
            for nt, count in sorted(type_counts.items())
        )
    else:
        type_str = f"{len(elements)} test {node_type}s"

    # Constant prefix — same tokens for every batch
    lines = [
        "Describe the test elements below. "
        "Respond with a JSON object mapping each qualified_name "
        "to its description:",
        "",
    ]

    for i, elem in enumerate(elements):
        if mixed:
            node, test, siblings, nt, pb = elem[:5]
        else:
            node, test, siblings = elem[:3]
            nt, pb = node_type, prompt_builder
        qn = _safe_attr(node, "qualified_name") or _safe_attr(node, "name", "?")
        single = pb(node, test, siblings, verifies_targets=verifies_targets)
        single = re.sub(
            r"\nGenerate a concise description:.*$",
            "",
            single,
            flags=re.DOTALL,
        ).strip()
        lines.append(f"## Element {i + 1}: {qn}")
        lines.append(single)
        lines.append("")

    # Variable suffix — count and type breakdown at the END for cache friendliness
    lines.append(f"({len(elements)} elements: {type_str})")
    lines.append("Return the JSON object now:")
    return "\n".join(lines)


def _parse_batch_response(
    response: str,
    nodes: list,
) -> dict[str, str]:
    """Parse an LLM batch response into a dict of qualified_name → description.

    Handles responses wrapped in markdown code fences and extra/missing keys.
    """
    text = response.strip()
    fence_match = re.match(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    brace_start = text.find("{")
    if brace_start == -1:
        raise ValueError(f"No JSON object found in response: {text[:200]}")

    brace_end = text.rfind("}")
    if brace_end == -1 or brace_end <= brace_start:
        raise ValueError(f"Malformed JSON in response: {text[:200]}")

    try:
        result = json.loads(text[brace_start:brace_end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse batch response JSON: {exc}"
            f"\nResponse: {text[:300]}"
        ) from exc

    if not isinstance(result, dict):
        raise ValueError(
            f"Batch response is not a JSON object: {type(result).__name__}"
        )

    return result


def _enrich_node_list(
    nodes: list,
    prompt_builder,
    node_type: str,
    model: str,
    max_tokens: int,
    dry_run: bool,
    overwrite: bool,
    summary: EnrichmentSummary,
    *,
    verifies_targets: list[dict] | None = None,
    log_dir: str | Path | None = None,
    node_offset: int = 0,
    node_total: int = 0,
    batch_size: int = 10,
    node_contexts: list[tuple[Any, dict[str, list]]] | None = None,
    batch_offset: int = 0,
    total_batches_global: int = 0,
    node_types: list[str] | None = None,
    node_builders: list | None = None,
) -> None:
    """Enrich descriptions for a list of child nodes, in batches.

    Groups nodes into batches of *batch_size* and makes one LLM call per
    batch, dramatically reducing API calls for large test suites.

    Args:
        nodes: Flat list of node objects to enrich.
        prompt_builder: Per-node prompt builder function.
        node_type: One of ``"fixture"``, ``"step"``, ``"assertion"``.
        model: LLM model identifier.
        max_tokens: Max response tokens per LLM call (scaled by batch size).
        dry_run: If True, skip LLM calls.
        overwrite: If True, overwrite existing descriptions.
        summary: Accumulator for results.
        verifies_targets: Code nodes under test (per-node via contexts).
        log_dir: Optional directory for LLM call logs.
        node_offset: Starting index for progress display.
        node_total: Total nodes for progress display.
        batch_size: Maximum nodes per LLM batch call.
        node_contexts: Optional parallel list of ``(test, siblings)``
            tuples, one per node.  When provided, each node's prompt
            uses its own test context, allowing batching across tests.
            When None, all nodes share the first element's context
            (backward-compatible single-test path).
    """
    if verifies_targets is None:
        verifies_targets = []

    # Filter to nodes that need enrichment, keeping their contexts
    # Each entry: (node, test_ctx, sib_ctx, node_type, prompt_builder)
    to_enrich: list[tuple[Any, Any, dict[str, list], str, Any]] = []
    for i, node in enumerate(nodes):
        desc = _safe_attr(node, "description")
        # Per-node type and builder (for mixed-type batching)
        nt = node_types[i] if node_types and i < len(node_types) else node_type
        pb = node_builders[i] if node_builders and i < len(node_builders) else prompt_builder
        # Skip nodes that already have a real (non-placeholder) description
        # unless overwrite is requested.
        if desc.strip() and not overwrite and not _is_placeholder_description(desc):
            er = EnrichmentResult(
                qualified_name=_safe_attr(node, "qualified_name")
                or _safe_attr(node, "name", "?"),
                node_type=nt,
                old_description=desc,
                skipped=True,
                skip_reason="Already has a description",
            )
            summary.results.append(er)
            summary.total_skipped += 1
        else:
            if node_contexts and i < len(node_contexts):
                test_ctx, sib_ctx = node_contexts[i]
            else:
                # Backward compat: use first context or empty dicts
                test_ctx = node_contexts[0][0] if node_contexts else {}
                sib_ctx = node_contexts[0][1] if node_contexts else {}
            to_enrich.append((node, test_ctx, sib_ctx, nt, pb))

    if not to_enrich:
        return

    if dry_run:
        for node, test_ctx, sib_ctx, nt, pb in to_enrich:
            er = EnrichmentResult(
                qualified_name=_safe_attr(node, "qualified_name")
                or _safe_attr(node, "name", "?"),
                node_type=nt,
                old_description=_safe_attr(node, "description"),
                skipped=True,
                skip_reason="dry_run mode",
            )
            summary.results.append(er)
            summary.total_skipped += 1
        return

    # Batch the remaining nodes
    batch_count = 0
    for batch_start in range(0, len(to_enrich), batch_size):
        batch = to_enrich[batch_start:batch_start + batch_size]
        batch_count += 1
        batch_num = node_offset + batch_start + 1

        # Build combined prompt — each element uses its own prompt builder
        user_prompt = _build_batch_user_prompt(
            batch, node_type, prompt_builder, verifies_targets,
        )

        # Progress display
        batch_nodes = [n for n, _t, _s, _nt, _pb in batch]
        qnames = [
            _safe_attr(n, "qualified_name") or _safe_attr(n, "name", "?")
            for n in batch_nodes
        ]
        # Show type mix in the label
        types_in_batch = set(nt for _n, _t, _s, nt, _pb in batch)
        if len(types_in_batch) > 1:
            label = f"Mixed ({','.join(sorted(types_in_batch))})"
        else:
            nt_single = types_in_batch.pop() if types_in_batch else node_type
            label = {"fixture": "Fixture", "step": "Step", "assertion": "Assertion"}.get(
                nt_single, nt_single.capitalize()
            )
        total_batches = (len(to_enrich) + batch_size - 1) // batch_size
        global_batch = batch_offset + batch_count
        if total_batches_global:
            print(
                f"  [batch {global_batch}/{total_batches_global}] "
                f"{label}s {batch_num}-{batch_num + len(batch) - 1}/{node_total} "
                f"({len(batch)} nodes) ...",
                end=" ", flush=True,
            )
        else:
            print(
                f"  [batch {batch_count}/{total_batches}] "
                f"{label}s {batch_num}-{batch_num + len(batch) - 1}/{node_total} "
                f"({len(batch)} nodes) ...",
                end=" ", flush=True,
            )
        call_start = time.monotonic()

        response = ""
        descriptions: dict[str, str] = {}
        batch_error: str | None = None

        try:
            response = _llm_complete(
                system=_BATCH_SYSTEM_PROMPT,
                user=user_prompt,
                model=model,
                max_tokens=max_tokens * len(batch),
            )
            descriptions = _parse_batch_response(response, batch_nodes)
        except Exception as exc:
            batch_error = str(exc)
            log.warning(
                "Batch enrichment failed for %s %s-%s: %s",
                node_type, batch_num, batch_num + len(batch) - 1, exc,
            )

        elapsed = time.monotonic() - call_start

        # Warn if response keys don't match expected qualified names
        expected_qns = {
            _safe_attr(n, "qualified_name") or _safe_attr(n, "name", "?")
            for n in batch_nodes
        }
        if not batch_error and descriptions:
            missing = expected_qns - set(descriptions.keys())
            extra = set(descriptions.keys()) - expected_qns
            if missing:
                msg = (
                    f"  ⚠ Batch response missing {len(missing)} keys. "
                    f"Expected like: {sorted(expected_qns)[0][:80]}, "
                    f"Got: {sorted(descriptions.keys())[:2]}"
                )
                print(msg, file=sys.stderr, flush=True)
            if extra:
                msg = (
                    f"  ⚠ Batch response has {len(extra)} unexpected keys: "
                    f"{sorted(extra)[:3]}"
                )
                print(msg, file=sys.stderr, flush=True)

        # Process each node in the batch
        batch_enriched = 0
        batch_errors = 0
        for elem in batch:
            node, test_ctx, sib_ctx = elem[0], elem[1], elem[2]
            nt = elem[3] if len(elem) > 3 else node_type
            qn = _safe_attr(node, "qualified_name") or _safe_attr(node, "name", "?")
            old_desc = _safe_attr(node, "description")
            new_desc = descriptions.get(qn, "")

            er = EnrichmentResult(
                qualified_name=qn,
                node_type=nt,
                old_description=old_desc,
                new_description=new_desc,
                error=batch_error,
            )

            if batch_error:
                summary.total_errors += 1
                summary.errors.append(f"{qn}: {batch_error}")
                batch_errors += 1
            elif new_desc and new_desc != old_desc:
                node.description = new_desc
                summary.total_enriched += 1
                batch_enriched += 1
            else:
                summary.total_skipped += 1
                er.skipped = True
                er.skip_reason = "LLM returned empty or missing key"

            summary.results.append(er)

            # Write per-node log entry
            if log_dir:
                _write_llm_log(
                    log_dir,
                    qualified_name=qn,
                    node_type=nt,
                    system_prompt=_BATCH_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    response=new_desc,
                    old_description=old_desc,
                    new_description=new_desc,
                    model=model,
                    error=batch_error if batch_error else "",
                )

        if batch_error:
            print(f"FAILED ({elapsed:.1f}s): {batch_error}", flush=True)
        else:
            unchanged = len(batch) - batch_enriched - batch_errors
            parts = [f"{batch_enriched} enriched"]
            if unchanged:
                parts.append(f"{unchanged} unchanged")
            print(f"OK ({elapsed:.1f}s) — {', '.join(parts)}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# Single-test enrichment
# ══════════════════════════════════════════════════════════════════════════


def _enrich_one_test(
    result: ParseResult,
    test: Any,
    *,
    tests_by_qn: dict[str, Any],
    groups: dict[str, dict[str, list]],
    verifies_cache: dict[str, list[dict]],
    model: str,
    max_tokens: int,
    dry_run: bool,
    overwrite: bool,
    log_dir: str | Path | None = None,
    node_offset: int = 0,
    node_total: int = 0,
    batch_size: int = 10,
) -> EnrichmentSummary:
    """Enrich all child nodes for a single test.

    Returns an :class:`EnrichmentSummary` with per-category breakdown.
    Uses batched LLM calls for efficiency.
    """
    test_qn = _safe_attr(test, "qualified_name")
    children = groups.get(test_qn, {"fixtures": [], "steps": [], "assertions": []})
    verifies_targets = verifies_cache.get(test_qn, [])

    # Use a flat summary; we'll populate the buckets from results
    flat_summary = EnrichmentSummary()

    offset = node_offset

    # Build per-node contexts for this single test
    fix_contexts = [(test, children)] * len(children["fixtures"])
    step_contexts = [(test, children)] * len(children["steps"])
    assert_contexts = [(test, children)] * len(children["assertions"])

    # Enrich fixtures via batched _enrich_node_list
    _enrich_node_list(
        children["fixtures"],
        _build_fixture_prompt, "fixture",
        model, max_tokens, dry_run, overwrite, flat_summary,
        verifies_targets=verifies_targets,
        log_dir=log_dir,
        node_offset=offset,
        node_total=node_total,
        batch_size=batch_size,
        node_contexts=fix_contexts,
    )
    offset += len(children["fixtures"])

    # Enrich steps
    _enrich_node_list(
        children["steps"],
        _build_step_prompt, "step",
        model, max_tokens, dry_run, overwrite, flat_summary,
        verifies_targets=verifies_targets,
        log_dir=log_dir,
        node_offset=offset,
        node_total=node_total,
        batch_size=batch_size,
        node_contexts=step_contexts,
    )
    offset += len(children["steps"])

    # Enrich assertions
    _enrich_node_list(
        children["assertions"],
        _build_assertion_prompt, "assertion",
        model, max_tokens, dry_run, overwrite, flat_summary,
        verifies_targets=verifies_targets,
        log_dir=log_dir,
        node_offset=offset,
        node_total=node_total,
        batch_size=batch_size,
        node_contexts=assert_contexts,
    )

    # Build a per-category summary from the flat results
    summary = EnrichmentSummary(test_name=test_qn)
    for r in flat_summary.results:
        if r.node_type == "fixture":
            summary.fixtures.append(r)
        elif r.node_type == "step":
            summary.steps.append(r)
        elif r.node_type == "assertion":
            summary.assertions.append(r)

    summary.total_enriched = flat_summary.total_enriched
    summary.total_skipped = flat_summary.total_skipped
    summary.total_errors = flat_summary.total_errors
    return summary

    return summary


def _enrich_single_node(
    node: Any,
    test: Any,
    siblings: dict[str, list],
    prompt_builder,
    node_type: str,
    model: str,
    max_tokens: int,
    dry_run: bool,
    overwrite: bool,
    *,
    verifies_targets: list[dict] | None = None,
    log_dir: str | Path | None = None,
    progress: tuple[int, int] | None = None,
) -> EnrichmentResult:
    """Enrich a single child node, returning an EnrichmentResult."""
    if verifies_targets is None:
        verifies_targets = []

    er = EnrichmentResult(
        qualified_name=_safe_attr(node, "qualified_name")
        or _safe_attr(node, "name", "?"),
        node_type=node_type,
        old_description=_safe_attr(node, "description"),
    )

    # Skip if already enriched
    if er.old_description.strip() and not overwrite:
        er.skipped = True
        er.skip_reason = "Already has a description"
        return er

    if dry_run:
        er.skipped = True
        er.skip_reason = "dry_run mode"
        return er

    # Print progress
    if progress:
        current, total = progress
        label = {"fixture": "Fixture", "step": "Step", "assertion": "Assertion"}.get(
            er.node_type, er.node_type.capitalize()
        )
        print(f"  [{current}/{total}] {label}: {er.qualified_name} ...",
              end=" ", flush=True)
        call_start = time.monotonic()

    user_prompt = ""
    try:
        user_prompt = prompt_builder(
            node, test, siblings,
            verifies_targets=verifies_targets,
        )
        description = _llm_complete(
            system=_ENRICH_SYSTEM_PROMPT,
            user=user_prompt,
            model=model,
            max_tokens=max_tokens,
        )
        er.new_description = description

        if description and description != er.old_description:
            node.description = description
    except Exception as exc:
        er.error = str(exc)
        log.warning("Enrichment failed for %s: %s", er.qualified_name, exc)
    finally:
        if log_dir:
            _write_llm_log(
                log_dir,
                qualified_name=er.qualified_name,
                node_type=er.node_type,
                system_prompt=_ENRICH_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response=er.new_description,
                old_description=er.old_description,
                new_description=er.new_description,
                model=model,
                error=er.error if er.error else "",
            )

    if progress:
        elapsed = time.monotonic() - call_start
        if er.error:
            print(f"FAILED ({elapsed:.1f}s): {er.error}", flush=True)
        else:
            status = "enriched" if er.changed else "unchanged"
            print(f"OK ({elapsed:.1f}s) — {status}", flush=True)

    return er


# ══════════════════════════════════════════════════════════════════════════
# Public API — convenience functions for the ticketing system
# ══════════════════════════════════════════════════════════════════════════


def enrich_single_test(
    result: ParseResult,
    test_qualified_name: str,
    *,
    model: str = "claude-sonnet-4-20250514",
    dry_run: bool = False,
    overwrite: bool = False,
    max_tokens: int = 256,
    log_dir: str | Path | None = None,
    batch_size: int = 10,
) -> EnrichmentSummary:
    """Enrich descriptions for a single test.

    Args:
        result: The parsed project result (modified in place).
        test_qualified_name: The ``qualified_name`` of the TestNode.
        model: LLM model identifier.
        dry_run: If True, simulate without calling the LLM.
        overwrite: If True, overwrite existing descriptions.
        max_tokens: Maximum response tokens per LLM call.

    Returns:
        An :class:`EnrichmentSummary` for the test.

    Raises:
        ValueError: If the TestNode is not found.
    """
    tests_by_qn: dict[str, Any] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            tests_by_qn[qn] = t

    test = tests_by_qn.get(test_qualified_name)
    if test is None:
        raise ValueError(
            f"TestNode not found: {test_qualified_name}"
        )

    groups = _group_test_children(result)
    verifies_cache: dict[str, list[dict]] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            verifies_cache[qn] = _build_verifies_for_test(t, result, tests_by_qn)

    return _enrich_one_test(
        result, test,
        tests_by_qn=tests_by_qn,
        groups=groups,
        verifies_cache=verifies_cache,
        model=model,
        max_tokens=max_tokens,
        dry_run=dry_run,
        overwrite=overwrite,
        log_dir=log_dir,
        batch_size=batch_size,
    )


def enrich_test_descriptions(
    result: ParseResult,
    test_qualified_names: list[str],
    *,
    model: str = "claude-sonnet-4-20250514",
    dry_run: bool = False,
    overwrite: bool = False,
    max_tokens: int = 256,
    log_dir: str | Path | None = None,
    batch_size: int = 10,
) -> dict[str, EnrichmentSummary]:
    """Enrich descriptions for one or more named tests.

    For each ``test_qualified_name``, calls :func:`enrich_single_test`
    and collects the results.

    Args:
        result: The parsed project result (modified in place).
        test_qualified_names: List of TestNode ``qualified_name`` values.
        model: LLM model identifier.
        dry_run: If True, simulate without calling the LLM.
        overwrite: If True, overwrite existing descriptions.
        max_tokens: Maximum response tokens per LLM call.

    Returns:
        A dict mapping ``test_qualified_name`` → ``EnrichmentSummary``.
        Tests that fail enrichment appear with an ``errors`` entry in
        their summary.
    """
    results: dict[str, EnrichmentSummary] = {}

    for qn in test_qualified_names:
        try:
            results[qn] = enrich_single_test(
                result, qn,
                model=model, dry_run=dry_run, overwrite=overwrite,
                max_tokens=max_tokens,
                log_dir=log_dir,
                batch_size=batch_size,
            )
        except Exception as exc:
            results[qn] = EnrichmentSummary(
                test_name=qn,
                errors=[str(exc)],
            )

    return results


def enrich_all_tests(
    result: ParseResult,
    *,
    model: str = "claude-sonnet-4-20250514",
    dry_run: bool = False,
    overwrite: bool = False,
    max_tokens: int = 256,
    log_dir: str | Path | None = None,
    batch_size: int = 10,
) -> dict[str, EnrichmentSummary]:
    """Enrich every test in the ParseResult.

    Args:
        result: The parsed project result (modified in place).
        model: LLM model identifier.
        dry_run: If True, simulate without calling the LLM.
        overwrite: If True, overwrite existing descriptions.
        max_tokens: Maximum response tokens per LLM call.

    Returns:
        A dict mapping ``test_qualified_name`` → ``EnrichmentSummary``.
    """
    tests_by_qn: dict[str, Any] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            tests_by_qn[qn] = t

    groups = _group_test_children(result)

    verifies_cache: dict[str, list[dict]] = {}
    for t in result.tests:
        qn = _safe_attr(t, "qualified_name")
        if qn:
            verifies_cache[qn] = _build_verifies_for_test(t, result, tests_by_qn)

    # Count total enrichable nodes for progress display
    total_nodes = sum(
        len(children["fixtures"]) + len(children["steps"]) + len(children["assertions"])
        for children in groups.values()
    )
    if not dry_run:
        print(f"  Nodes to enrich: {total_nodes}")

    results: dict[str, EnrichmentSummary] = {}
    node_offset = 0
    for qn, test in tests_by_qn.items():
        children = groups.get(qn, {"fixtures": [], "steps": [], "assertions": []})
        try:
            results[qn] = _enrich_one_test(
                result, test,
                tests_by_qn=tests_by_qn,
                groups=groups,
                verifies_cache=verifies_cache,
                model=model,
                max_tokens=max_tokens,
                dry_run=dry_run,
                overwrite=overwrite,
                log_dir=log_dir,
                node_offset=node_offset,
                node_total=total_nodes,
                batch_size=batch_size,
            )
            node_offset += (
                len(children["fixtures"]) + len(children["steps"]) + len(children["assertions"])
            )
        except Exception as exc:
            results[qn] = EnrichmentSummary(
                test_name=qn,
                errors=[str(exc)],
            )

    return results


# ══════════════════════════════════════════════════════════════════════════
# Availability check
# ══════════════════════════════════════════════════════════════════════════


def enrichment_available() -> bool:
    """Return True if the environment is configured for LLM enrichment."""
    return bool(os.getenv("LLM_API_KEY"))


def check_enrichment_config() -> None:
    """Validate that LLM enrichment is configured, raising if not.

    Checks for ``LLM_API_KEY`` environment variable and the ``openai``
    package.  Call this before enabling enrichment to fail fast with a
    clear message.

    Raises:
        RuntimeError: If the environment is not configured.
    """
    if not os.getenv("LLM_API_KEY"):
        raise RuntimeError(
            "LLM enrichment requires LLM_API_KEY environment variable. "
            "Set it to your Anthropic/OpenAI API key, or use --dry-run."
        )
    try:
        import openai  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "LLM enrichment requires the 'openai' package. "
            "Install it with: pip install openai"
        )


def preflight_check(
    model: str | None = None,
    timeout: int = 15,
) -> None:
    """Verify LLM connectivity with a quick test call.

    Sends a trivial prompt to the configured LLM and prints progress.
    Call this before starting enrichment to surface connection issues
    immediately rather than after the first real call.

    Args:
        model: LLM model identifier.  Defaults from ``LLM_MODEL`` env.
        timeout: Seconds to wait for the test call.

    Raises:
        RuntimeError: If the LLM is unreachable or the call fails.
    """
    model = model or os.getenv("LLM_MODEL", "claude-sonnet-4-20250514")
    print(f"  Checking LLM connectivity ({model})...", end=" ", flush=True)
    start = time.monotonic()
    try:
        response = _llm_complete(
            system="Reply with exactly one word: ok.",
            user="ping",
            model=model,
            max_tokens=8,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        print(f"OK ({elapsed:.1f}s)")
        log.info("LLM preflight check passed in %.1fs", elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"FAILED ({elapsed:.1f}s)")
        raise RuntimeError(
            f"LLM connectivity check failed. "
            f"Verify LLM_API_KEY, LLM_BASE_URL, and network access. "
            f"Error: {exc}"
        ) from exc


# ══════════════════════════════════════════════════════════════════════════
# CLI entry point (optional)
# ══════════════════════════════════════════════════════════════════════════


def main():
    """CLI entry point for running enrichment on a ParseResult JSON file.

    Usage::

        python -m doxygen_index.enrich \\
            --input parsed.json --test "tests::test_engine::test_set_target"

        python -m doxygen_index.enrich \\
            --input parsed.json --all

        python -m doxygen_index.enrich \\
            --input parsed.json --all --dry-run
    """
    import argparse
    import importlib

    parser = argparse.ArgumentParser(
        description="Enrich test node descriptions using an LLM."
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to a JSON file produced by doxygen-index (ParseResult JSON).",
    )
    parser.add_argument(
        "--test",
        help="Qualified name of a single TestNode to enrich.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Enrich all test nodes in the input.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "claude-sonnet-4-20250514"),
        help="LLM model to use.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build prompts and simulate without calling the LLM.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing descriptions (default: skip).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=256,
        help="Maximum tokens per LLM response.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for LLM call logs (JSONL).",
    )

    args = parser.parse_args()

    # Load ParseResult from JSON
    # We import here to avoid circular dependency at module load time
    from doxygen_index.json_backend import read_result

    input_path = Path(args.input) if not isinstance(args.input, Path) else args.input
    if hasattr(args.input, 'resolve'):
        pass  # already a Path
    else:
        from pathlib import Path as _Path
        input_path = _Path(args.input)

    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    result = read_result(input_path)

    log_dir = Path(args.log_dir) if args.log_dir else None

    if args.test:
        summary = enrich_single_test(
            result, args.test,
            model=args.model, dry_run=args.dry_run,
            overwrite=args.overwrite, max_tokens=args.max_tokens,
            log_dir=log_dir,
        )
        print(json.dumps(summary.to_dict(), indent=2))
    elif args.all:
        results = enrich_all_tests(
            result,
            model=args.model, dry_run=args.dry_run,
            overwrite=args.overwrite, max_tokens=args.max_tokens,
            log_dir=log_dir,
        )
        combined = {
            "total_tests": len(results),
            "total_enriched": sum(s.total_enriched for s in results.values()),
            "total_skipped": sum(s.total_skipped for s in results.values()),
            "total_errors": sum(s.total_errors for s in results.values()),
            "tests": {
                qn: s.to_dict() for qn, s in results.items()
            },
        }
        print(json.dumps(combined, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    import sys
    from pathlib import Path
    main()
