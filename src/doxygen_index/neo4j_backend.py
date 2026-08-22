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
  deterministic uid + source), and stale nodes (removed or renamed in
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

import re
import sys
from pathlib import Path
from time import perf_counter

from codegraph import get_backend
from codegraph.graph import LayerGraph

from doxygen_index.graph_json import result_to_graph_json

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
    *result* by :func:`result_to_graph_json` (deterministic uids, full
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
        result, src, text_scan=False, portable=False
    )
    if timings is not None:
        timings["serialization"] = perf_counter() - stage_started

    stage_started = perf_counter()
    graph = LayerGraph.deserialize(
        data, create_missing=False, portable=False
    )
    graph.to_backend(get_backend())
    if timings is not None:
        timings["persistence"] = perf_counter() - stage_started
    print(f"  Wrote {len(data)} nodes to {type(get_backend()).__name__}")


# ---------------------------------------------------------------------------
# Incremental update — write + delete stale nodes
# ---------------------------------------------------------------------------

def _collect_live_refids(result: ParseResult) -> set[str]:
    """Collect all qualified_name values from a ParseResult.

    Used to identify stale compound/member nodes that should be deleted
    during an incremental update.
    """
    live: set[str] = set()
    for lst in (result.classes, result.enums, result.unions, result.interfaces,
                result.concepts, result.methods, result.attributes,
                result.enum_values, result.defines, result.functions,
                result.source_fragments,
                result.namespaces, result.tests, result.assertions,
                result.test_steps, result.test_fixtures, result.literals,
                result.implementations):
        for node in lst:
            qn = getattr(node, "qualified_name", None)
            if qn:
                live.add(qn)
    return live


def _collect_live_file_refids(result: ParseResult) -> set[str]:
    """Collect all FileNode refids from a ParseResult.

    Uses refid (module name) instead of path, which is stable across
    absolute/relative path changes.
    """
    return {f.refid for f in result.files if getattr(f, 'refid', '')}

def _collect_live_member_refids(result: ParseResult) -> set[str]:
    """Collect all member refids from a ParseResult.

    Used to identify stale ParameterNodes (whose ``member_refid`` references
    a member that may have been deleted).
    """
    live: set[str] = set()
    for member_list in (result.methods, result.attributes, result.functions,
                        result.defines):
        for node in member_list:
            refid = getattr(node, "refid", None)
            if refid:
                live.add(refid)
    return live


def delete_stale_nodes(
    source: str,
    live_qualified_names: set[str],
    live_file_refids: set[str],
    live_member_refids: set[str],
) -> dict[str, int]:
    """Delete nodes for *source* whose identity is NOT in the live set.

    Backend-agnostic: fetches every node for *source* via
    ``find_all_by_source`` and deletes those whose identity (qualified_name,
    refid, or member_refid depending on type) is missing from the latest
    parse.  Mirrors the old label-scoped Cypher: compounds/members/
    namespaces/test nodes by qualified_name, files by refid, parameters by
    member_refid.

    Args:
        source: Source label to scope deletion.
        live_qualified_names: Qualified_name values present in the latest
            parse.
        live_file_refids: File refids present in the latest parse.
        live_member_refids: Member refids present in the latest parse
            (used to identify stale ParameterNodes).

    Returns:
        Dict mapping node type → count of deleted nodes.
    """
    deleted_counts: dict[str, int] = {}
    graph = get_backend().graph

    stale_uids: list[str] = []
    for node in graph.find_all_by_source(source):
        ntype = type(node).__name__
        if ntype == "FileNode":
            ident = getattr(node, "refid", "") or ""
            stale = ident not in live_file_refids
        elif ntype == "ParameterNode":
            ident = getattr(node, "member_refid", "") or ""
            stale = ident not in live_member_refids
        else:
            ident = getattr(node, "qualified_name", "") or ""
            stale = ident not in live_qualified_names
        if not stale:
            continue
        key = node.canonical_key or ""
        if key:
            stale_uids.append(key)
            deleted_counts[ntype] = deleted_counts.get(ntype, 0) + 1

    if stale_uids:
        # One aggregate delete (``delete_by_uids``) — per-node deletes
        # were ~26ms/node at cpp-sqlite scale.
        graph.delete_by_uids(stale_uids)

    if deleted_counts:
        parts = [f"{label}: {cnt}" for label, cnt in deleted_counts.items()]
        print(f"  Deleted stale nodes ({', '.join(parts)})")

    return deleted_counts


def update_result(result: ParseResult, source: str) -> dict[str, int]:
    """Incrementally update the graph for *source*.

    1. Collects live node identities from *result*.
    2. Calls :func:`write_result` to create/update nodes (MERGE on
       deterministic uid + source).
    3. Calls :func:`delete_stale_nodes` to remove nodes that are no longer
       present in the source.

    Other sources are left untouched.

    Args:
        result: The latest ParseResult from parsing the source code.
        source: Source label to scope the update.

    Returns:
        Dict mapping node label → count of deleted stale nodes.
    """
    live_qnames = _collect_live_refids(result)
    live_file_refids = _collect_live_file_refids(result)
    live_member_refids = _collect_live_member_refids(result)

    # Pass source explicitly so nodes are tagged against the correct
    # project label (``_infer_source`` would pick the dominant source
    # in mixed parses, mis-tagging project type_params as dependency).
    write_result(result, source=source)

    return delete_stale_nodes(source, live_qnames, live_file_refids, live_member_refids)


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
