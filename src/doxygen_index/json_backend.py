"""
JSON output backend — serializes ParseResult to a JSON file.

No external dependencies. Immediately useful for piping to tools,
feeding to LLMs, or checkpointing parse results for later reuse.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from doxygen_index.parser import ParseResult


def write_result(
    result: ParseResult,
    output_path: Path,
    source: str = "",
) -> None:
    """Write ParseResult to a JSON file.

    Args:
        result: The parsed Doxygen output.
        output_path: Where to write the JSON file.
        source: Source label for provenance (project name).
    """
    data = _build_payload(result, source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, default=str))


def _build_payload(result: ParseResult, source: str) -> dict:
    """Build the serializable payload from a ParseResult."""
    return {
        "metadata": {
            "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "format_version": 1,
        },
        "files": [n.__properties__ for n in result.files],
        "namespaces": [n.__properties__ for n in result.namespaces],
        "classes": [n.__properties__ for n in result.classes],
        "enums": [n.__properties__ for n in result.enums],
        "unions": [n.__properties__ for n in result.unions],
        "interfaces": [n.__properties__ for n in result.interfaces],
        "concepts": [n.__properties__ for n in result.concepts],
        "methods": [n.__properties__ for n in result.methods],
        "attributes": [n.__properties__ for n in result.attributes],
        "enum_values": [n.__properties__ for n in result.enum_values],
        "defines": [n.__properties__ for n in result.defines],
        "functions": [n.__properties__ for n in result.functions],
        "parameters": [n.__properties__ for n in result.parameters],
        "includes": [asdict(i) for i in result.includes],
        "invokes": [asdict(i) for i in result.invokes],
        "invoked_by": [asdict(i) for i in result.invoked_by],
        "template_param_refs": [asdict(t) for t in result.template_param_refs],
        "specializes_refs": [asdict(s) for s in result.specializes_refs],
        # Test-related nodes and relationships
        "tests": [n.__properties__ for n in result.tests],
        "assertions": [n.__properties__ for n in result.assertions],
        "test_steps": [n.__properties__ for n in result.test_steps],
        "test_fixtures": [n.__properties__ for n in result.test_fixtures],
        "literals": [n.__properties__ for n in result.literals],
        "verifies": [asdict(v) for v in result.verifies],
        "operands": [asdict(o) for o in result.operands],
        "callees": [asdict(c) for c in result.callees],
        "test_compositions": [asdict(tc) for tc in result.test_compositions],
        "fixture_of_types": [asdict(fo) for fo in result.fixture_of_types],
        "fixture_checked_by": [asdict(cb) for cb in result.fixture_checked_by],
        "fixture_defined_in": [asdict(di) for di in result.fixture_defined_in],
        "compositions": [asdict(c) for c in result.compositions],
        "inherits": [asdict(h) for h in result.inherits],
        "depends_on": [asdict(d) for d in result.depends_on],
        "namespace_includes": [asdict(ni) for ni in result.namespace_includes],
    }
