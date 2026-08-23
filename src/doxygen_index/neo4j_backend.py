"""Graph backend — ingests ParseResult into the active codegraph backend.

Backend-agnostic: works identically against Neo4j or SQLite (selected via
``CODEGRAPH_BACKEND``).  Zero raw Cypher — the write path goes through
the LayerGraph bridge (``result_to_graph_json`` →
``LayerGraph.deserialize`` → ``graph.to_backend``), and deletion/update
go through repository calls (``find_all_by_source`` + ``delete_by_uid``).

Provides two modes for writing parsed source code into the graph:

* **Incremental update** (:func:`update_result`, the default):
  Re-indexes a source without destroying the existing graph.  New nodes
  are created, changed nodes are updated in place (via MERGE on
  deterministic canonical key + source), and stale nodes (removed or renamed in
  the source) are deleted.  Other sources are left untouched.

* **Full rewrite** (:func:`write_result` + :func:`clear_source`):
  Wipes all nodes for a source label, then re-creates everything from
  scratch.  Use ``--clear`` on the CLI or ``incremental=False`` in the
  Python API when a full reset is desired.

The :func:`ingest` function defaults to incremental mode
(``incremental=True``); pass ``clear=True`` for a full re-write.
The CLI uses incremental by default; ``--clear`` opts into full re-write.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from codegraph import get_backend
from codegraph.graph import LayerGraph

from doxygen_index.graph_json import PARSER_LOCATOR_FIELDS, result_to_graph_json

# Import all node models so CodeGraphNode._registry is populated before
# backend.apply_schema() enumerates labels for uid indexes.
from codegraph import (  # noqa: F401 — needed for apply_schema
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
    SourceFragmentNode,
)
from codegraph.models.test import (  # noqa: F401 — needed for apply_schema
    TestNode, AssertionNode, TestStepNode, TestFixtureNode,
)
from codegraph.models.literal import LiteralNode  # noqa: F401

from doxygen_index.parser import ParseResult, parse_xml_dir


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect_neo4j() -> None:
    """Verify the codegraph backend can reach Neo4j.

    ``get_backend()`` auto-configures from the ``.env`` file (or
    environment variables).  This function just triggers lazy
    initialisation and confirms connectivity.

    Exits with a helpful message on auth or connection failure.
    """
    from neo4j.exceptions import AuthError, ServiceUnavailable

    try:
        backend = get_backend()
        if not backend.health_check():
            raise ServiceUnavailable("health check returned False")
    except AuthError:
        print(
            "\nError: Neo4j authentication failed.\n"
            "  Check the credentials in your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ServiceUnavailable:
        print(
            "\nError: Could not reach Neo4j.\n"
            "  Is the database running?",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"\nError: Could not connect to Neo4j: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema(stdout=None) -> None:
    """Create the codegraph-managed schema (per-label uid indexes).

    The backend owns schema creation — ``install_all_labels()`` is a
    no-op for the pure-Python model layer.  ``MERGE``/``MATCH`` on
    ``uid`` needs these indexes; without them batched writes degrade
    to O(N²) label scans (measured ~80x slower at 20k nodes).
    """
    get_backend().apply_schema()


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def clear_source(source: str) -> None:
    """Remove all nodes with a specific source label (backend-agnostic).

    One aggregate ``delete_by_source`` call (DETACH DELETE / indexed SQL
    DELETE with FK cascade) — never per-node round trips.
    """
    graph = get_backend().graph
    deleted = graph.delete_by_source(source)
    print(f"  Cleared {deleted} existing '{source}' nodes.")


def clear_all() -> None:
    """Remove all codebase nodes and relationships."""
    get_backend().wipe()
    print("  Cleared all codebase data from the graph.")


# ---------------------------------------------------------------------------
# Canonical identity: keys are computed by result_to_graph_json
# Enriched description preservation
# ---------------------------------------------------------------------------

# Regex patterns that match auto-generated / placeholder descriptions
# produced by the Python parser.  Descriptions matching these are
# considered "placeholder" and should not overwrite LLM-enriched values.
_AUTO_DESC_PATTERNS = [
    re.compile(r"^assert\s", re.IGNORECASE),     # "assert ==", "assert is", etc.
    re.compile(r"^Setup block$", re.IGNORECASE),  # TestStepNode default
    re.compile(r"^Action block\s", re.IGNORECASE), # TestStepNode default
    re.compile(r"^$"),                            # empty string
]


def _is_placeholder_description(desc: str | None) -> bool:
    """Return True if *desc* is an auto-generated placeholder."""
    if not desc or not desc.strip():
        return True
    for pat in _AUTO_DESC_PATTERNS:
        if pat.match(desc):
            return True
    return False


def _preserve_descriptions(*node_lists: list) -> None:
    """Pre-fetch existing non-placeholder descriptions and merge them
    into incoming nodes so that ``save()`` doesn't overwrite enriched
    data with parser-generated placeholders.

    Called before serialization in :func:`write_result`.
    """
    # Collect all qualified names from incoming test-related nodes
    # whose descriptions look like placeholders.
    candidates: dict[str, list] = {}  # qname → [node, ...]
    for node_list in node_lists:
        for node in node_list:
            desc = getattr(node, "description", None)
            if _is_placeholder_description(desc):
                qname = getattr(node, "qualified_name", "")
                if qname:
                    candidates.setdefault(qname, []).append(node)

    if not candidates:
        return

    existing = fetch_node_descriptions(list(candidates.keys()))

    # Merge existing non-placeholder descriptions into incoming nodes
    preserved = 0
    for qname, nodes in candidates.items():
        rich_desc = existing.get(qname)
        if rich_desc and not _is_placeholder_description(rich_desc):
            for node in nodes:
                node.description = rich_desc
                preserved += 1

    if preserved:
        print(f"  Preserved {preserved} enriched descriptions")


def fetch_node_descriptions(
    qualified_names: list[str],
    *,
    include_placeholder: bool = False,
) -> dict[str, str]:
    """Fetch ``description`` values held in the graph by qualified name.

    Reads through the active backend's repository (``find_by_qualified_name``
    per qname).  By default only **non-placeholder** descriptions are
    returned (parser placeholders like ``"Setup block"`` or ``"assert …"``
    are filtered out), so the result is suitable for feeding straight into
    :func:`~doxygen_index.parser.python.test_comments.write_test_comments`
    as the ``descriptions`` override — i.e. materialising already-enriched
    graph values into source-file comment blocks without re-running the LLM.

    The backend must already be configured.  Lookup errors are reported on
    stderr and yield an empty dict rather than raising, so callers can fall
    back to scaffold/placeholder behaviour when the graph is unreachable.

    Args:
        qualified_names: Qualified names to look up.
        include_placeholder: If True, return placeholder descriptions too
            (still skipping empty/missing values).

    Returns:
        ``{qualified_name: description}`` for the found nodes.
    """
    if not qualified_names:
        return {}

    graph = get_backend().graph
    out: dict[str, str] = {}
    for qn in qualified_names:
        try:
            node = graph.find_by_qualified_name(qn)
        except Exception as exc:  # connection / query failure
            print(f"Warning: could not fetch descriptions from graph: {exc}",
                  file=sys.stderr)
            return out
        if node is None:
            continue
        desc = getattr(node, "description", "") or ""
        if not desc:
            continue
        if not include_placeholder and _is_placeholder_description(desc):
            continue
        out.setdefault(qn, desc)
    return out


def _collect_test_qualified_names(result: ParseResult) -> list[str]:
    """Return the qualified names of all test-related nodes in *result*."""
    qns: list[str] = []
    for lst in (result.tests, result.test_steps,
                result.test_fixtures, result.assertions):
        for node in lst:
            qn = getattr(node, "qualified_name", "") or ""
            if qn:
                qns.append(qn)
    return qns


def _infer_source(result: ParseResult) -> str:
    """Return the most common ``source`` value among *result*'s nodes."""
    from collections import Counter

    counts: Counter = Counter()
    for node_list in (
        result.files, result.namespaces, result.classes,
        result.enums, result.unions, result.interfaces, result.concepts,
        result.methods, result.attributes, result.enum_values,
        result.defines, result.functions, result.parameters,
        result.tests, result.assertions, result.test_steps,
        result.test_fixtures, result.literals, result.implementations,
    ):
        for node in node_list:
            src = getattr(node, "source", "") or ""
            if src:
                counts[src] += 1
    return counts.most_common(1)[0][0] if counts else ""


def write_result(
    result: ParseResult,
    source: str | None = None,
    *,
    timings: dict[str, float] | None = None,
) -> None:
    """Write a ParseResult to the active backend via the LayerGraph bridge.

    Backend-agnostic — zero raw Cypher.  Nodes + edges are built from
    *result* by :func:`result_to_graph_json` (deterministic canonical keys, full
    edge inventory), deserialized into a :class:`LayerGraph`
    (unresolvable edge targets are dropped, mirroring the old Cypher
    ``MATCH``-both-endpoints semantics), and persisted via
    ``graph.to_backend``.

    Args:
        result: The parsed output.
        source: Project source label.  When omitted, inferred from the
            nodes (most common ``source`` value).
        timings: Optional mapping populated with serialization and persistence
            durations for stage-level CLI diagnostics.
    """
    # The backend owns schema creation (per-label uid indexes); idempotent.
    ensure_schema()

    # Preserve LLM-enriched descriptions: read existing non-placeholder
    # descriptions for test-related nodes and merge them into the
    # incoming nodes before serialization.  ``save()`` MERGEs properties
    # so values not present in the new row survive on re-index.
    _preserve_descriptions(
        result.tests, result.assertions,
        result.test_steps, result.test_fixtures,
    )

    src = source or _infer_source(result)
    stage_started = perf_counter()
    data = result_to_graph_json(
        result, src, text_scan=False
    )
    if timings is not None:
        timings["serialization"] = perf_counter() - stage_started

    stage_started = perf_counter()
    graph = LayerGraph.deserialize(data, create_missing=False)
    graph.to_backend(get_backend())
    if timings is not None:
        timings["persistence"] = perf_counter() - stage_started
    print(f"  Wrote {len(data)} nodes to {type(get_backend()).__name__}")


# ---------------------------------------------------------------------------
# Incremental update — write + delete stale nodes
# ---------------------------------------------------------------------------

class CanonicalReconciliationError(ValueError):
    """Raised when an incremental update cannot be reconciled safely."""


@dataclass(frozen=True)
class _InventoryEntry:
    """One canonical node observed by the reconciliation pass."""

    key: str
    node_type: str
    source: str
    fingerprint: str = ""


@dataclass
class CanonicalInventory:
    """Canonical node inventory grouped for reconciliation diagnostics."""

    entries: dict[str, _InventoryEntry] = field(default_factory=dict)
    by_source: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    by_type: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )

    def add(
        self,
        key: str,
        node_type: str,
        source: str,
        *,
        fingerprint: str = "",
        allow_identical_duplicate: bool = False,
    ) -> None:
        """Register one key, rejecting conflicts instead of last-write-wins."""
        existing = self.entries.get(key)
        if existing is not None:
            if (
                not allow_identical_duplicate
                or existing.node_type != node_type
                or existing.source != source
                or existing.fingerprint != fingerprint
            ):
                raise CanonicalReconciliationError(
                    "ambiguous canonical identity: "
                    f"{key!r} is claimed by "
                    f"{existing.node_type}/{existing.source} and "
                    f"{node_type}/{source}"
                )
            return

        entry = _InventoryEntry(key, node_type, source, fingerprint)
        self.entries[key] = entry
        self.by_source[source].add(key)
        self.by_type[node_type].add(key)

    def keys_for_source(self, source: str) -> set[str]:
        """Return a copy of the canonical keys owned by *source*."""
        return set(self.by_source.get(source, set()))

    def counts_for_keys(self, keys: set[str]) -> dict[str, int]:
        """Return deletion/reporting counts grouped by persisted node type."""
        counts = Counter(self.entries[key].node_type for key in keys)
        return dict(counts)


def _parse_result_node_lists(result: ParseResult) -> tuple[list, ...]:
    """Return every parser-owned node list that can be persisted.

    Keep this inventory in the same order as the LayerGraph bridge.  The
    bridge additionally synthesizes template-parameter ClassNodes; those are
    accounted for from its normalized output below.
    """
    return (
        result.files,
        result.namespaces,
        result.classes,
        result.enums,
        result.unions,
        result.interfaces,
        result.concepts,
        result.methods,
        result.attributes,
        result.enum_values,
        result.defines,
        result.source_fragments,
        result.functions,
        result.parameters,
        result.tests,
        result.assertions,
        result.test_steps,
        result.test_fixtures,
        result.literals,
        result.implementations,
    )


def _registry_type(node_type: str):
    """Resolve a serialized node type through Codegraph's model registry."""
    from codegraph.models.tags import CodeGraphNode

    model_type = CodeGraphNode._registry.get(node_type)
    if model_type is None:
        raise CanonicalReconciliationError(
            f"canonical inventory contains unregistered node type {node_type!r}"
        )
    return model_type


def _validate_canonical_key(
    key: Any,
    *,
    node_type: str,
    source: str,
    node: object | None = None,
) -> dict[str, str]:
    """Strictly validate a canonical key and its registry/type contract."""
    if not isinstance(key, str) or not key.strip():
        raise CanonicalReconciliationError(
            f"{node_type} in source {source!r} has an empty canonical key"
        )

    from codegraph.identity import CanonicalIdentity
    from codegraph.identity.registry import spec_for

    try:
        identity = CanonicalIdentity.from_key(key)
    except Exception as exc:
        raise CanonicalReconciliationError(
            f"{node_type} in source {source!r} has invalid canonical key "
            f"{key!r}"
        ) from exc

    model_type = _registry_type(node_type)
    spec = spec_for(model_type)
    if spec is None or identity.category != spec.category:
        expected = spec.category if spec is not None else "<missing spec>"
        raise CanonicalReconciliationError(
            f"canonical key {key!r} has category {identity.category!r}, "
            f"expected {expected!r} for {node_type}"
        )

    if identity.scope.repository_id != source:
        raise CanonicalReconciliationError(
            f"canonical key {key!r} is scoped to repository "
            f"{identity.scope.repository_id!r}, not source {source!r}"
        )

    values = dict(identity.values)
    for field_name, value in values.items():
        if value:
            continue
        # The current registry serializes integer position 0 through an
        # ``or \"\"`` conversion.  The parser still supplied a complete,
        # unambiguous position, so retain compatibility with that v1 wire
        # representation while rejecting every other missing identity input.
        if (
            field_name == "position"
            and node is not None
            and type(node).__name__ == "ParameterNode"
            and getattr(node, "position", None) == 0
        ):
            continue
        raise CanonicalReconciliationError(
            f"{node_type} {key!r} has incomplete identity field "
            f"{field_name!r}"
        )
    return values


def _portable_node_fingerprint(node: object) -> str:
    """Fingerprint normalized node data while excluding parser locators."""
    try:
        payload = node.serialize(fields="all")
    except Exception as exc:
        raise CanonicalReconciliationError(
            f"could not fingerprint {type(node).__name__} for canonical "
            "duplicate detection"
        ) from exc

    payload = dict(payload)
    payload.pop("canonical_key", None)
    payload.pop("edges", None)
    for field_name in PARSER_LOCATOR_FIELDS:
        payload.pop(field_name, None)
    return json.dumps(payload, sort_keys=True, default=str)


def _validate_parent_key_references(
    entries: list[dict],
    keys: set[str],
) -> None:
    """Reject missing/ambiguous parent-relative identity inputs.

    ``TestNode`` is the one intentional root-relative type: its parent key is
    the stable ``cg:v1:root`` sentinel used by the identity matrix.  All
    other parent-relative keys must point at another incoming canonical key.
    """
    parent_fields = {
        "parent_callable_key",
        "file_key",
        "parent_key",
    }
    for entry in entries:
        node_type = entry.get("node_type") or entry.get("type") or ""
        key = entry.get("canonical_key")
        if not key:
            continue
        from codegraph.identity import CanonicalIdentity

        values = dict(CanonicalIdentity.from_key(key).values)
        for field_name in parent_fields & values.keys():
            parent_key = values[field_name]
            if field_name == "parent_key" and (
                node_type == "TestNode" and parent_key == "cg:v1:root"
            ):
                continue
            if parent_key not in keys:
                raise CanonicalReconciliationError(
                    f"{node_type} {key!r} has unresolved "
                    f"{field_name} {parent_key!r}"
                )


def _build_incoming_inventory(
    result: ParseResult,
    source: str,
) -> tuple[CanonicalInventory, list[dict]]:
    """Normalize *result* once and build its canonical live inventory."""
    data = result_to_graph_json(result, source, text_scan=False)
    actual_nodes_by_key: dict[str, object] = {}
    actual_fingerprints: dict[str, str] = {}

    # Validate every real ParseResult node before accepting the bridge's
    # deduplicated output.  This catches two distinct payloads that happen to
    # produce one canonical key instead of allowing graph_json's first-entry
    # dedupe to hide the ambiguity.
    for node_list in _parse_result_node_lists(result):
        for node in node_list:
            node_type = type(node).__name__
            node_source = getattr(node, "source", "") or source
            key = getattr(node, "canonical_key", "") or ""
            _validate_canonical_key(
                key, node_type=node_type, source=node_source, node=node
            )
            fingerprint = _portable_node_fingerprint(node)
            previous = actual_fingerprints.get(key)
            if previous is not None and previous != fingerprint:
                raise CanonicalReconciliationError(
                    f"distinct {node_type} payloads share canonical key {key!r}"
                )
            actual_fingerprints[key] = fingerprint
            actual_nodes_by_key[key] = node

    inventory = CanonicalInventory()
    normalized_entries: list[dict] = []
    for entry in data:
        node_type = entry.get("node_type") or entry.get("type") or ""
        node_source = entry.get("source") or source
        key = entry.get("canonical_key")
        node = actual_nodes_by_key.get(key)
        _validate_canonical_key(
            key, node_type=node_type, source=node_source, node=node
        )
        inventory.add(
            key,
            node_type,
            node_source,
            fingerprint=actual_fingerprints.get(key, ""),
            allow_identical_duplicate=True,
        )
        normalized_entries.append(entry)

    normalized_keys = set(inventory.entries)
    missing = set(actual_nodes_by_key) - normalized_keys
    if missing:
        raise CanonicalReconciliationError(
            "normalized graph omitted parser-owned canonical keys: "
            + ", ".join(sorted(missing))
        )
    _validate_parent_key_references(normalized_entries, normalized_keys)
    return inventory, data


def _build_persisted_inventory(source: str) -> CanonicalInventory:
    """Read and strictly validate all persisted keys for one source."""
    inventory = CanonicalInventory()
    graph = get_backend().graph
    for node in graph.find_all_by_source(source):
        node_type = type(node).__name__
        key = getattr(node, "canonical_key", "") or ""
        _validate_canonical_key(key, node_type=node_type, source=source, node=node)
        if key in inventory.entries:
            raise CanonicalReconciliationError(
                f"persisted source {source!r} contains duplicate canonical key "
                f"{key!r}"
            )
        inventory.add(
            key,
            node_type,
            source,
            fingerprint=_portable_node_fingerprint(node),
        )
    return inventory


def delete_stale_nodes(
    source: str,
    stale_keys: set[str],
    persisted: CanonicalInventory,
) -> dict[str, int]:
    """Batch-delete a precomputed source-scoped canonical stale set.

    Args:
        source: Source label to scope deletion.
        stale_keys: Canonical keys selected before graph mutation.
        persisted: Validated persisted inventory for *source*.

    Returns:
        Dict mapping node type → count of deleted nodes.
    """
    source_keys = persisted.keys_for_source(source)
    stale_keys = set(stale_keys)
    if not stale_keys <= source_keys:
        raise CanonicalReconciliationError(
            "stale canonical deletion escaped its source scope: "
            + ", ".join(sorted(stale_keys - source_keys))
        )

    deleted_counts = persisted.counts_for_keys(stale_keys)
    if stale_keys:
        # The compatibility method is named ``delete_by_uids`` in the
        # repository API, but canonical_key is the storage key it receives.
        # Keep the whole candidate set batched and deterministic.
        get_backend().graph.delete_by_uids(sorted(stale_keys))

    if deleted_counts:
        parts = [
            f"{label}: {cnt}"
            for label, cnt in sorted(deleted_counts.items())
        ]
        print(f"  Deleted stale nodes ({', '.join(parts)})")

    return deleted_counts


def update_result(result: ParseResult, source: str) -> dict[str, int]:
    """Incrementally update the graph for *source*.

    1. Normalizes and validates all incoming canonical identities.
    2. Validates persisted identities and computes the complete stale set.
    3. Writes/updates nodes (MERGE on canonical key + source).
    4. Batch-deletes the precomputed stale set.

    Other sources are left untouched.

    Args:
        result: The latest ParseResult from parsing the source code.
        source: Source label to scope the update.

    Returns:
        Dict mapping node label → count of deleted stale nodes.
    """
    # Match write_result's enriched-description behavior before the single
    # normalization pass captures the incoming payload.
    _preserve_descriptions(
        result.tests, result.assertions,
        result.test_steps, result.test_fixtures,
    )
    incoming, data = _build_incoming_inventory(result, source)
    persisted = _build_persisted_inventory(source)
    stale_keys = persisted.keys_for_source(source) - incoming.keys_for_source(source)

    # Using the already validated data makes the liveness proof and the graph
    # write observe exactly one canonical view.
    ensure_schema()
    graph = LayerGraph.deserialize(data, create_missing=False)
    graph.to_backend(get_backend())
    print(f"  Wrote {len(data)} nodes to {type(get_backend()).__name__}")

    return delete_stale_nodes(source, stale_keys, persisted)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest(
    xml_dir: Path | str,
    source: str = "msd",
    database: str = "neo4j",
    clear: bool = False,
    layer: str = "dependency",
    incremental: bool = True,
) -> None:
    """Parse Doxygen XML and ingest into the active backend.

    ``get_backend()`` auto-configures from the ``.env`` file (or
    environment variables).  No explicit credentials are needed.

    By default, performs an **incremental update**: new nodes are created,
    changed nodes are updated in place, and stale nodes (no longer in the
    source) are deleted — without wiping the existing source first.
    Pass ``clear=True`` (or ``incremental=False``) for a full re-write.

    Args:
        xml_dir: Directory containing Doxygen XML output.
        source: Source label for provenance tracking.
        database: Ignored (kept for API compatibility); the backend is
            selected via ``CODEGRAPH_BACKEND``.
        clear: If True, clear existing data for this source before a
            full re-write.  Ignored when ``incremental`` is True (the default).
        layer: Layer label ("codebase" for project code, "dependency" for deps).
        incremental: If True (the default), incrementally update instead of
            full re-write.  Set to False to force a full re-write.
    """
    xml_dir = Path(xml_dir)

    ensure_schema()

    if incremental:
        print(f"Parsing {xml_dir}... (layer={layer}, incremental update)")
        result = parse_xml_dir(xml_dir, source=source, layer=layer)
        update_result(result, source=source)
    else:
        if clear:
            clear_source(source)

        print(f"Parsing {xml_dir}... (layer={layer})")
        result = parse_xml_dir(xml_dir, source=source, layer=layer)

        print(f"Writing to {type(get_backend()).__name__}...")
        write_result(result, source=source)

    # Summary
    from collections import Counter

    counts: Counter = Counter()
    for node in get_backend().graph.find_all_by_source(source):
        counts[source] += 1
    print("\nNode counts by source:")
    for src, cnt in sorted(counts.items()):
        print(f"  [{src}]: {cnt}")
