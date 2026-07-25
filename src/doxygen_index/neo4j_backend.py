"""
Neo4j backend — ingests ParseResult into a Neo4j graph database.

Provides two modes for writing parsed source code into Neo4j:

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

Uses neomodel for node persistence and Cypher for relationship creation.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from neomodel import db

# Import all node models so neomodel registry discovers them before
# install_all_labels is called.
from codegraph import (  # noqa: F401 — needed for install_all_labels
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
)
from codegraph.models.test import (  # noqa: F401 — needed for install_all_labels
    TestNode, AssertionNode, TestStepNode, TestFixtureNode,
)
from codegraph.models.literal import LiteralNode  # noqa: F401

from doxygen_index.parser import ParseResult, parse_xml_dir, TemplateParamRef, SpecializesRef, ImplementationRef
from doxygen_index.parser.model import VerifiesEntry, OperandEntry, CalleeEntry, TestCompositionEntry, FixtureOfTypeEntry, FixtureCheckedByEntry, FixtureDefinedInEntry


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect_neo4j(
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> None:
    """Configure the neomodel connection and verify it works.

    Loads ``.env`` first (without overriding real env vars), then resolves
    credentials from arguments → environment → hardcoded defaults.

    Exits with a helpful message on auth or connection failure.
    """
    from neomodel import get_config
    from neo4j.exceptions import AuthError, ServiceUnavailable

    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD", "msd-local-dev")
    database = database or os.environ.get("NEO4J_DATABASE", "neo4j")

    _bolt_host = uri.replace("bolt://", "")
    config = get_config()
    config.database_url = f"bolt://{user}:{password}@{_bolt_host}"
    config.database_name = database

    # Verify connectivity — db.set_connection() runs an internal version
    # check that can raise AuthError / ServiceUnavailable.
    try:
        db.set_connection(config.database_url)
        db.cypher_query("RETURN 1")
    except AuthError:
        print(
            f"\nError: Neo4j authentication failed for user '{user}' at {uri}.\n"
            f"  Check the credentials in your .env file or pass them via "
            f"--neo4j-user / --neo4j-password.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ServiceUnavailable:
        print(
            f"\nError: Could not reach Neo4j at {uri}.\n"
            f"  Is the database running?",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as e:
        print(f"\nError: Could not connect to Neo4j at {uri}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema(stdout=None) -> None:
    """Install neomodel labels, constraints, and indexes."""
    db.install_all_labels(stdout=stdout)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def clear_source(source: str) -> None:
    """Remove all nodes with a specific source label."""
    queries = [
        # Delete ImplementationNodes first (members have HAS_IMPLEMENTATION edges to them)
        ("MATCH (impl:ImplementationNode {source: $src}) DETACH DELETE impl",
         {"src": source}),
        # Delete ParameterNodes first (they reference member refids)
        ("MATCH (m:MemberNode {source: $src}) "
         "WITH collect(m.refid) AS refids "
         "MATCH (p:ParameterNode) WHERE p.member_refid IN refids "
         "DETACH DELETE p",
         {"src": source}),
        # Delete type_parameter ClassNodes (created by TEMPLATE_PARAM ingestion)
        ("MATCH (tp:ClassNode {kind: 'type_parameter', source: $src}) "
         "DETACH DELETE tp",
         {"src": source}),
        # Delete members, compounds, namespaces, files
        ("MATCH (m:MemberNode {source: $src}) DETACH DELETE m",
         {"src": source}),
        ("MATCH (c:CompoundNode {source: $src}) DETACH DELETE c",
         {"src": source}),
        ("MATCH (n:NamespaceNode {source: $src}) DETACH DELETE n",
         {"src": source}),
        ("MATCH (f:FileNode {source: $src}) DETACH DELETE f",
         {"src": source}),
        # Delete test-related nodes (TestNode, AssertionNode, TestStepNode,
        # LiteralNode, TestFixtureNode)
        ("MATCH (a:AssertionNode {source: $src}) DETACH DELETE a",
         {"src": source}),
        ("MATCH (s:TestStepNode {source: $src}) DETACH DELETE s",
         {"src": source}),
        # Delete TestFixtureNode BEFORE TestNode — TestFixtureNode has its
        # own 'source' property so we can delete directly without relying
        # on the COMPOSES relationship to parent TestNodes.
        ("MATCH (f:TestFixtureNode {source: $src}) DETACH DELETE f",
         {"src": source}),
        ("MATCH (t:TestNode {source: $src}) DETACH DELETE t",
         {"src": source}),
        ("MATCH (l:LiteralNode {source: $src}) DETACH DELETE l",
         {"src": source}),
    ]
    for query, params in queries:
        db.cypher_query(query, params)
    print(f"  Cleared existing '{source}' data from Neo4j.")


def clear_all() -> None:
    """Remove all codebase nodes and relationships."""
    queries = [
        "MATCH (impl:ImplementationNode) DETACH DELETE impl",
        "MATCH (p:ParameterNode) DETACH DELETE p",
        "MATCH (m:MemberNode) DETACH DELETE m",
        "MATCH (c:CompoundNode) DETACH DELETE c",
        "MATCH (n:NamespaceNode) DETACH DELETE n",
        "MATCH (f:FileNode) DETACH DELETE f",
        "MATCH (md:Metadata) DETACH DELETE md",
    ]
    for query in queries:
        db.cypher_query(query)
    print("  Cleared all codebase data from Neo4j.")


# ---------------------------------------------------------------------------
# Deterministic uid & merge helpers
# ---------------------------------------------------------------------------

def _ensure_deterministic_uid(node) -> None:
    """Set a deterministic ``uid`` on *node* in place via the codegraph
    canonical ``compute_uid`` function.

    Computes ``compute_uid(source, *identity_values)`` where ``source``
    is the project label and ``identity_values`` are the node's
    ``_identity_fields`` values (with ``argsstring`` normalised via
    :func:`codegraph.uid.normalize_argsstring`).

    The uid is:
    - deterministic: same source + same identity → same uid every time
    - source-scoped: same qualified_name in two sources → different uid
    - consistent with ``codegraph.uid.compute_uid(source, *fields)``
    """
    from codegraph.uid import compute_uid, normalize_argsstring

    identity_fields = list(getattr(node, "_identity_fields", ()) or ())
    identity_values = []
    for field in identity_fields:
        val = getattr(node, field, "")
        val_str = str(val) if val is not None else ""
        if field == "argsstring":
            val_str = normalize_argsstring(val_str)
        identity_values.append(val_str)
    source = getattr(node, "source", "")
    node.uid = compute_uid(str(source) if source else "", *identity_values)


def _merge_by_keys(node) -> dict:
    """Return ``merge_by`` dict for ``create_or_update``.

    MERGE on ``uid`` — the deterministic hash of source + identity fields
    computed by ``_ensure_deterministic_uid``.  Same uid as
    ``CodeGraphNode._compute_uid`` produces.
    """
    return {"keys": ["uid"]}


# ---------------------------------------------------------------------------
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
    into incoming nodes so that ``create_or_update`` doesn't overwrite
    enriched data with parser-generated placeholders.

    Called before ``create_or_update`` in :func:`write_result`.
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

    # Batch-fetch existing descriptions from Neo4j
    from neomodel import db
    qnames = list(candidates.keys())
    query = """
        UNWIND $qnames AS qname
        MATCH (n)
        WHERE n.qualified_name = qname
          AND n.description IS NOT NULL
          AND n.description <> ''
        RETURN n.qualified_name AS qname, n.description AS description
    """
    results, _ = db.cypher_query(query, {"qnames": qnames})

    existing: dict[str, str] = {}
    for row in results:
        existing[row[0]] = row[1] or ""

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

    Queries Neo4j for every node whose ``qualified_name`` is in
    *qualified_names* and returns a ``{qualified_name: description}`` map.
    By default only **non-placeholder** descriptions are returned (parser
    placeholders like ``"Setup block"`` or ``"assert …"`` are filtered out),
    so the result is suitable for feeding straight into
    :func:`~doxygen_index.parser.python.test_comments.write_test_comments`
    as the ``descriptions`` override — i.e. materialising already-enriched
    graph values into source-file comment blocks without re-running the LLM.

    A Neo4j connection must already be configured (call
    :func:`connect_neo4j` first).  Query errors are reported on stderr and
    yield an empty dict rather than raising, so callers can fall back to
    scaffold/placeholder behaviour when the graph is unreachable.

    Args:
        qualified_names: Qualified names to look up.
        include_placeholder: If True, return placeholder descriptions too
            (still skipping empty/missing values).

    Returns:
        ``{qualified_name: description}`` for the found nodes.
    """
    if not qualified_names:
        return {}
    from neomodel import db

    query = """
        UNWIND $qnames AS qname
        MATCH (n)
        WHERE n.qualified_name = qname
          AND n.description IS NOT NULL
          AND n.description <> ''
        RETURN n.qualified_name AS qname, n.description AS description
    """
    try:
        results, _ = db.cypher_query(query, {"qnames": list(qualified_names)})
    except Exception as exc:  # connection / query failure
        print(f"Warning: could not fetch descriptions from Neo4j: {exc}",
              file=sys.stderr)
        return {}

    out: dict[str, str] = {}
    for row in results:
        qn = row[0]
        desc = row[1] or ""
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


def write_result(result: ParseResult) -> None:
    """Write a ParseResult to Neo4j using batched Cypher queries.

    Nodes are inserted via UNWIND + MERGE in batches of 1000.
    Relationships use UNWIND + MATCH + MERGE.  This is orders of
    magnitude faster than per-node neomodel ``.save()`` calls.
    """
    batch_refs: list[list] = [
        result.files, result.namespaces, result.classes,
        result.enums, result.unions, result.interfaces, result.concepts,
        result.methods, result.attributes, result.enum_values,
        result.defines, result.functions, result.implementations,
        result.tests, result.assertions, result.test_steps,
        result.test_fixtures, result.literals,
    ]
    batch_labels: list[str] = [
        "FileNode", "NamespaceNode", "ClassNode",
        "EnumNode", "UnionNode", "InterfaceNode", "ConceptNode",
        "MethodNode", "AttributeNode", "EnumValueNode",
        "DefineNode", "FunctionNode", "ImplementationNode",
        "TestNode", "AssertionNode", "TestStepNode",
        "TestFixtureNode", "LiteralNode",
    ]

    _preserve_descriptions(
        result.tests, result.assertions,
        result.test_steps, result.test_fixtures,
    )

    # ── Batch-insert nodes ──────────────────────────────────────
    total_nodes = sum(len(nl) for nl in batch_refs)
    print(f"  Computing UIDs for {total_nodes} nodes ...", end=" ", flush=True)
    uid_count = 0
    for i, node_list in enumerate(batch_refs):
        for node in node_list:
            _ensure_deterministic_uid(node)
            uid_count += 1
            if uid_count % 5000 == 0:
                print(f"\n    ... {uid_count}/{total_nodes}", end=" ", flush=True)
    print(f"\n  Writing {total_nodes} nodes to Neo4j ...", flush=True)
    _BATCH = 1000
    for i, node_list in enumerate(batch_refs):
        if not node_list:
            continue
        label = batch_labels[i]
        # Build full neomodel label hierarchy (e.g. MethodNode:MemberNode)
        full_labels = ":".join(node_list[0].inherited_labels())
        props_list = [
            {k: v for k, v in n.__properties__.items() if v is not None}
            for n in node_list
        ]
        for j in range(0, len(props_list), _BATCH):
            batch = props_list[j:j + _BATCH]
            db.cypher_query(f"""
                UNWIND $batch AS props
                MERGE (n:{full_labels} {{uid: props.uid}})
                SET n = props
            """, {"batch": batch})
            if len(props_list) > _BATCH:
                print(f"  {label}: {min(j + _BATCH, len(props_list))}/{len(props_list)}", flush=True)
        print(f"  {label}: {len(node_list)}", flush=True)

    # ── Ensure per-label uid indexes ───────────────────────────
    for lbl in ("AttributeNode", "FunctionNode", "ImplementationNode"):
        try:
            db.cypher_query(
                f"CREATE INDEX IF NOT EXISTS FOR (n:{lbl}) ON (n.uid)"
            )
        except Exception:
            pass
    print("  Index: per-label uid indexes ready", flush=True)

    # Parameters
    print(f"  Parameters phase: {len(result.parameters)} params", flush=True)
    _write_parameters(result)

    # ── Batch-insert relationships ─────────────────────────────
    _write_compound_member_connect(result)
    _write_namespace_composition(result)
    _write_file_relationships(result)
    _write_include_relationships(result)
    _write_inheritance_relationships(result)
    _write_invoke_relationships(result)
    _write_specialization_relationships(result)
    _write_template_param_relationships(result)
    _write_implementation_relationships(result)
    _write_test_composition_relationships(result)
    _write_verifies_relationships(result)
    _write_operand_relationships(result)
    _write_callee_relationships(result)
    _write_of_type_relationships(result)
    _write_checked_by_relationships(result)
    _write_defined_in_relationships(result)
    _write_depends_on_relationships(result)


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
                result.namespaces, result.tests, result.assertions,
                result.test_steps, result.test_fixtures, result.literals,
                result.implementations):
        for node in lst:
            qn = getattr(node, "qualified_name", None)
            if qn:
                live.add(qn)
    return live


def _collect_live_file_paths(result: ParseResult) -> set[str]:
    """Collect all FileNode paths from a ParseResult."""
    return {f.path for f in result.files if f.path}


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
    live_file_paths: set[str],
    live_member_refids: set[str],
) -> dict[str, int]:
    """Delete nodes for *source* whose identity is NOT in the live set.

    Called after :func:`write_result` to remove nodes that existed in the
    previous index but were removed or renamed in the source code.

    Args:
        source: Source label to scope deletion.
        live_qualified_names: Set of qualified_name values present in the
            latest parse (for compounds, members, namespaces).
        live_file_paths: Set of file paths present in the latest parse.
        live_member_refids: Set of member refids present in the latest parse
            (used to identify stale ParameterNodes).

    Returns:
        Dict mapping node label → count of deleted nodes.
    """
    deleted_counts: dict[str, int] = {}

    def _delete_stale(label: str, identity_prop: str, live_set: set[str]) -> int:
        """Delete nodes of *label* for *source* where identity_prop NOT IN live_set."""
        if not live_set:
            query = (
                f"MATCH (n:{label} {{source: $src}}) "
                f"DETACH DELETE n "
                f"RETURN count(n) AS cnt"
            )
            result, _ = db.cypher_query(query, {"src": source})
        else:
            query = (
                f"MATCH (n:{label} {{source: $src}}) "
                f"WHERE NOT n.{identity_prop} IN $live "
                f"DETACH DELETE n "
                f"RETURN count(n) AS cnt"
            )
            result, _ = db.cypher_query(query, {"src": source, "live": list(live_set)})
        cnt = result[0][0] if result else 0
        if cnt:
            deleted_counts[label] = cnt
        return cnt

    def _delete_stale_parameter_nodes() -> int:
        """Delete ParameterNodes whose member_refid is NOT in the live set."""
        if not live_member_refids:
            query = (
                "MATCH (p:ParameterNode) "
                "MATCH (m:MemberNode {source: $src}) WHERE p.member_refid = m.refid "
                "DETACH DELETE p "
                "RETURN count(p) AS cnt"
            )
            result, _ = db.cypher_query(query, {"src": source})
        else:
            query = (
                "MATCH (p:ParameterNode) "
                "MATCH (m:MemberNode {source: $src}) "
                "WHERE p.member_refid = m.refid AND NOT p.member_refid IN $live "
                "DETACH DELETE p "
                "RETURN count(p) AS cnt"
            )
            result, _ = db.cypher_query(
                query, {"src": source, "live": list(live_member_refids)}
            )
        cnt = result[0][0] if result else 0
        if cnt:
            deleted_counts["ParameterNode"] = cnt
        return cnt

    # Delete stale compound nodes (by qualified_name)
    # Includes type_parameter ClassNodes — they have qualified_name set
    _delete_stale("CompoundNode", "qualified_name", live_qualified_names)

    # Delete stale member nodes (by qualified_name)
    _delete_stale("MemberNode", "qualified_name", live_qualified_names)

    # Delete stale namespaces (by qualified_name)
    _delete_stale("NamespaceNode", "qualified_name", live_qualified_names)

    # Delete stale files (by refid — stable across absolute/relative path changes)
    _delete_stale("FileNode", "refid", live_file_paths)

    # Delete stale test-related nodes
    _delete_stale("TestNode", "qualified_name", live_qualified_names)
    _delete_stale("AssertionNode", "qualified_name", live_qualified_names)
    _delete_stale("TestStepNode", "qualified_name", live_qualified_names)
    _delete_stale("TestFixtureNode", "qualified_name", live_qualified_names)
    _delete_stale("LiteralNode", "qualified_name", live_qualified_names)

    # Delete stale ImplementationNodes (by qualified_name)
    _delete_stale("ImplementationNode", "qualified_name", live_qualified_names)

    # Delete stale ParameterNodes (by member_refid)
    _delete_stale_parameter_nodes()

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

    write_result(result)

    return delete_stale_nodes(source, live_qnames, live_file_refids, live_member_refids)


# ---------------------------------------------------------------------------
# Relationship helpers (Cypher via db.cypher_query)
# ---------------------------------------------------------------------------

def _write_parameters(result: ParseResult) -> None:
    if not result.parameters:
        return
    # Build member_refid → uid map for indexed lookup
    refid_to_uid: dict[str, str] = {}
    for m in result.methods:
        if hasattr(m, 'uid') and m.uid and hasattr(m, 'refid') and m.refid:
            refid_to_uid[m.refid] = m.uid

    batch_size = 1000
    batch_dicts = [p.__properties__ for p in result.parameters]
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        # Convert member_refid → member_uid for indexed MATCH
        for row in batch:
            row["_member_uid"] = refid_to_uid.get(row.get("member_refid", ""), "")
        db.cypher_query("""
            UNWIND $batch AS row
            WITH row WHERE row._member_uid <> ''
            MATCH (m:MemberNode {uid: row._member_uid})
            MERGE (m)-[:HAS_PARAMETER]->(p:ParameterNode {
                position: row.position,
                name: row.name,
                type: row.type
            })
            ON CREATE SET p.default_value = row.default_value,
                          p.member_refid = row.member_refid
        """, {"batch": batch})
        print(f"  Parameters: {min(i + batch_size, len(batch_dicts))}/{len(batch_dicts)}", flush=True)
    print(f"  Parameters: {len(batch_dicts)}")


def _rel_batch(batch_data: list[dict], rel_type: str,
               from_key: str, to_key: str,
               from_label: str = "", to_label: str = "",
               label: str = "") -> int:
    """Batch-insert relationships via UNWIND + CREATE.

    Uses label-qualified MATCH so uid constraints are leveraged.
    Pass ``from_label`` / ``to_label`` as the base neomodel label
    (e.g. 'CompoundNode', 'MemberNode', 'NamespaceNode', 'FileNode')
    that has a uid constraint.

    Prints progress every 1000 edges.
    """
    if not batch_data:
        return 0
    from_clause = f"a:{from_label}" if from_label else "a"
    to_clause = f"b:{to_label}" if to_label else "b"
    total = 0
    for i in range(0, len(batch_data), 1000):
        batch = batch_data[i:i + 1000]
        results, _ = db.cypher_query(f"""
            UNWIND $batch AS row
            MATCH ({from_clause} {{{from_key}: row.from}})
            MATCH ({to_clause} {{{to_key}: row.to}})
            MERGE (a)-[:{rel_type}]->(b)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            total += results[0][0]
        if label:
            print(f"  {label}: {min(i + 1000, len(batch_data))}/{len(batch_data)}", flush=True)
    return total


def _write_compound_member_connect(result: ParseResult) -> None:
    """Create COMPOSES edges (compound → member) via batched Cypher."""
    # MethodNode.compound_refid → ClassNode/InterfaceNode.refid
    # Build refid→uid map so we can MATCH on indexed uid
    refid_to_uid: dict[str, str] = {}
    for lst in (result.classes, result.enums, result.unions, result.interfaces):
        for c in lst:
            if hasattr(c, 'uid') and c.uid and hasattr(c, 'refid') and c.refid:
                refid_to_uid[c.refid] = c.uid

    edges = [{"from": refid_to_uid[m.compound_refid], "to": m.uid}
             for m in result.methods
             if hasattr(m, 'uid') and m.uid
             and m.compound_refid in refid_to_uid]
    print(f"  COMPOSES methods: {len(edges)} edges → batching...")
    _rel_batch(edges, "COMPOSES", "uid", "uid",
              from_label="CompoundNode", to_label="MemberNode",
              label="COMPOSES methods")

    attr_edges = [{"from": refid_to_uid[a.compound_refid], "to": a.uid}
                  for a in result.attributes
                  if hasattr(a, 'uid') and a.uid
                  and a.compound_refid in refid_to_uid]
    _rel_batch(attr_edges, "COMPOSES", "uid", "uid",
              from_label="CompoundNode", to_label="MemberNode",
              label="COMPOSES attributes")

    enum_edges = [{"from": refid_to_uid[v.compound_refid], "to": v.uid}
                  for v in result.enum_values
                  if hasattr(v, 'uid') and v.uid
                  and v.compound_refid in refid_to_uid]
    _rel_batch(enum_edges, "COMPOSES", "uid", "uid",
              from_label="CompoundNode", to_label="MemberNode",
              label="COMPOSES enum_values")

    total = len(edges) + len(attr_edges) + len(enum_edges)
    print(f"  Relationships: COMPOSES (compound→member) ({total} edges)")


def _write_file_relationships(result: ParseResult) -> None:
    """Create DEFINED_IN edges via batched Cypher.

    Uses the in-memory ParseResult to build (uid, file_path) pairs
    instead of unindexed cross-product MATCH in Neo4j.
    """
    total = 0
    # Compounds → FileNode (by path)
    print("  DEFINED_IN (compounds) ...", end=" ", flush=True)
    compounds = (result.classes + result.enums + result.unions +
                 result.interfaces + result.concepts)
    edges = _filepath_edges(compounds, result.files)
    if edges:
        total += _rel_batch(edges, "DEFINED_IN", "uid", "uid",
                            from_label="CompoundNode", to_label="FileNode",
                            label="DEFINED_IN compounds")
    # Members → FileNode (by path)
    print("  DEFINED_IN (members) ...", end=" ", flush=True)
    members = (result.methods + result.attributes + result.functions +
               result.tests + result.test_steps + result.defines)
    edges = _filepath_edges(members, result.files)
    if edges:
        total += _rel_batch(edges, "DEFINED_IN", "uid", "uid",
                            from_label="MemberNode", to_label="FileNode",
                            label="DEFINED_IN members")
    # Namespaces → FileNode (by refid)
    ns_edges = []
    ns_by_refid = {f.refid: f for f in result.files if getattr(f, 'refid', '')}
    for ns in result.namespaces:
        if hasattr(ns, 'uid') and ns.uid and ns.refid in ns_by_refid:
            ns_edges.append({"from": ns.uid, "to": ns_by_refid[ns.refid].uid})
    if ns_edges:
        total += _rel_batch(ns_edges, "DEFINED_IN", "uid", "uid",
                            from_label="NamespaceNode", to_label="FileNode",
                            label="DEFINED_IN namespaces")
    print(f"  DEFINED_IN: {total} edges", flush=True)


def _filepath_edges(source_nodes: list, files: list) -> list[dict]:
    """Build (from_uid, to_uid) edges for DEFINED_IN by matching file_path → FileNode.path."""
    file_by_path: dict[str, str] = {}
    for f in files:
        fp = getattr(f, 'path', '') or getattr(f, 'file_path', '')
        if fp and hasattr(f, 'uid') and f.uid:
            file_by_path[fp] = f.uid
    edges = []
    for node in source_nodes:
        fp = getattr(node, 'file_path', '')
        if fp and hasattr(node, 'uid') and node.uid and fp in file_by_path:
            edges.append({"from": node.uid, "to": file_by_path[fp]})
    return edges


def _write_include_relationships(result: ParseResult) -> None:
    resolved = [asdict(i) for i in result.includes if i.included_refid]
    print(f"  INCLUDES: {len(resolved)} edges ...", end=" ", flush=True)
    if resolved:
        batch_size = 1000
        for i in range(0, len(resolved), batch_size):
            batch = resolved[i:i + batch_size]
            db.cypher_query("""
                UNWIND $batch AS row
                MATCH (src:FileNode {refid: row.file_refid})
                MATCH (dst:FileNode {refid: row.included_refid})
                MERGE (src)-[:INCLUDES {
                    included_file: row.included_file,
                    is_local: row.is_local
                }]->(dst)
            """, {"batch": batch})
    unresolved = [i for i in result.includes if not i.included_refid]
    print(f"  Includes: {len(resolved)} resolved, {len(unresolved)} external (skipped)")


def _namespace_for(qualified_name: str, module: str = "") -> str:
    """Determine the containing namespace qualified name.

    For Python: module is like 'codegraph.graph' and qualified_name uses '.'.
    For C++: module is like 'cpp_sqlite' and qualified_name uses '::'.
    Falls back to splitting the qualified_name on the last separator.
    """
    if module:
        return module
    # Try Python-style '.' first, then C++-style '::'
    if '.' in qualified_name:
        return qualified_name.rsplit('.', 1)[0]
    if '::' in qualified_name:
        return qualified_name.rsplit('::', 1)[0]
    return ""


def _write_namespace_composition(result: ParseResult) -> None:
    """Create COMPOSES relationships from namespaces to their contained entities.

    For C++ (Doxygen): compounds have compound_refid pointing to their
    parent namespace refid.

    For Python: the ``module`` field on compounds and the module portion
    of ``qualified_name`` on functions identify their containing namespace.
    """
    # Build namespace lookup by qualified_name → uid
    ns_by_qname: dict[str, str] = {
        ns.qualified_name: ns.uid for ns in result.namespaces
        if hasattr(ns, 'uid') and ns.uid
    }

    edges: list[dict] = []
    def _add_nodes(nodes, ns_attr: str = 'qualified_name'):
        for node in nodes:
            if not hasattr(node, 'uid') or not node.uid:
                continue
            qn = getattr(node, ns_attr, '')
            ns_qname = _namespace_for(qn, getattr(node, 'module', ''))
            parent_uid = ns_by_qname.get(ns_qname)
            if parent_uid:
                edges.append({"from": parent_uid, "to": node.uid})
    _add_nodes(result.classes)
    _add_nodes(result.interfaces)
    _add_nodes(result.enums)
    _add_nodes(result.functions)
    for ns in result.namespaces:
        if hasattr(ns, 'uid') and ns.uid and ('::' in ns.qualified_name or '.' in ns.qualified_name):
            parent_qname = _namespace_for(ns.qualified_name)
            parent_uid = ns_by_qname.get(parent_qname)
            if parent_uid:
                edges.append({"from": parent_uid, "to": ns.uid})

    count = _rel_batch(edges, "COMPOSES", "uid", "uid",
                       from_label="NamespaceNode",
                       to_label="CompoundNode|MemberNode|NamespaceNode|FileNode",
                       label="NS_COMPOSES")
    print(f"  Relationships: NS_COMPOSES ({count} edges)")


def _write_inheritance_relationships(result: ParseResult) -> None:
    """Create INHERITS_FROM edges using InheritsEntry.

    Resolves base classes via refid first (for same-source matches),
    then falls back to qualified_name (for cross-source matches, e.g.
    cpp-sqlite inheriting from cppreference's std::runtime_error).
    """
    if not result.inherits:
        print("  Relationships: INHERITS_FROM (0 edges)")
        return

    # Build uid lookups
    refid_to_uid: dict[str, str] = {}
    qname_to_uid: dict[str, str] = {}
    for cls in result.classes:
        if hasattr(cls, 'uid') and cls.uid:
            refid_to_uid[cls.refid] = cls.uid
            qname_to_uid[cls.qualified_name] = cls.uid
    for iface in result.interfaces:
        if hasattr(iface, 'uid') and iface.uid:
            refid_to_uid[iface.refid] = iface.uid
            qname_to_uid[iface.qualified_name] = iface.uid

    edges, skipped = [], 0
    for inh in result.inherits:
        derived_uid = refid_to_uid.get(inh.from_refid)
        if derived_uid is None:
            skipped += 1; continue
        base_uid = refid_to_uid.get(inh.to_refid)
        if base_uid is None and inh.to_name:
            base_uid = qname_to_uid.get(inh.to_name)
        if base_uid is None:
            skipped += 1; continue
        edges.append({"from": derived_uid, "to": base_uid})

    if not edges:
        print(f"  Relationships: INHERITS_FROM (0 edges, {skipped} unresolved)")
        return

    count = _rel_batch(edges, "INHERITS_FROM", "uid", "uid",
                       from_label="CompoundNode", to_label="CompoundNode",
                       label="INHERITS_FROM")
    print(f"  Relationships: INHERITS_FROM ({count} edges, {skipped} unresolved)")


def _write_specialization_relationships(result: ParseResult) -> None:
    """Create SPECIALIZES edges via batched Cypher."""
    if not result.specializes_refs:
        print("  Relationships: SPECIALIZES (0 edges)")
        return

    qname_to_uid: dict[str, str] = {}
    for lst in [result.classes, result.enums, result.unions, result.interfaces, result.concepts]:
        for node in lst:
            if hasattr(node, 'uid') and node.uid:
                qname_to_uid[node.qualified_name] = node.uid

    edges = []
    for sr in result.specializes_refs:
        primary_uid = qname_to_uid.get(sr.primary_template_qualified_name)
        spec_uid = qname_to_uid.get(sr.from_qualified_name)
        if primary_uid and spec_uid:
            edges.append({"from": spec_uid, "to": primary_uid})

    count = _rel_batch(edges, "SPECIALIZES", "uid", "uid",
                       from_label="CompoundNode", to_label="CompoundNode",
                       label="SPECIALIZES")
    print(f"  Relationships: SPECIALIZES ({count} edges)", flush=True)


def _write_template_param_relationships(result: ParseResult) -> None:
    """Create TEMPLATE_PARAM edges from compounds/members to type-parameter nodes,
    and ENFORCES_CONCEPT edges from type-parameter nodes to concepts.

    For each template param ref, we:
    1. Find the source node (compound or member) by qualified_name.
    2. Create a lightweight ClassNode(kind='type_parameter') for the param slot
       carrying metadata (name, position, defval) on the node itself.
    3. Connect source → type_parameter via TEMPLATE_PARAM (no edge properties).
    4. If the type_constraint matches a known concept, connect
       type_parameter → ConceptNode via ENFORCES_CONCEPT.
    """
    if not result.template_param_refs:
        print("  Relationships: TEMPLATE_PARAM (0 edges)")
        print("  Relationships: ENFORCES_CONCEPT (0 edges)")
        return

    # Build lookup: refid → node
    refid_map: dict[str, object] = {}
    for node_list in [result.classes, result.enums, result.unions, result.interfaces, result.concepts]:
        for node in node_list:
            refid_map[node.refid] = node
    for node_list in [result.methods, result.attributes, result.functions]:
        for node in node_list:
            refid_map[node.refid] = node

    # Build lookup: concept qualified_name → ConceptNode (for ENFORCES_CONCEPT)
    concept_by_qn: dict[str, object] = {
        c.qualified_name: c for c in result.concepts
    }

    # Use Cypher for bulk creation of type-parameter nodes and TEMPLATE_PARAM edges
    batch_dicts = []
    for tp in result.template_param_refs:
        source_node = refid_map.get(tp.from_refid)
        if source_node is None:
            continue
        batch_dicts.append({
            "from_qn": source_node.qualified_name,
            "position": tp.position,
            "declname": tp.declname,
            "defname": tp.defname,
            "defval": tp.defval,
            "source": source_node.source if hasattr(source_node, 'source') else "",
        })

    if not batch_dicts:
        print("  Relationships: TEMPLATE_PARAM (0 edges)")
        print("  Relationships: ENFORCES_CONCEPT (0 edges)")
        return

    batch_size = 500
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (source:CompoundNode|MemberNode {qualified_name: row.from_qn})
            MERGE (tp:ClassNode:CompoundNode {qualified_name: 'type_param:' + row.from_qn + ':' + toString(row.position)})
            ON CREATE SET tp.kind = 'type_parameter',
                          tp.name = CASE
                              WHEN row.declname <> '' THEN row.declname
                              WHEN row.defname <> '' THEN row.defname
                              ELSE 'T'
                          END,
                          tp.source = row.source,
                          tp.definition = 'position=' + toString(row.position) +
                                        CASE WHEN row.defval <> '' THEN ' defval=' + row.defval ELSE '' END
            ON MATCH SET tp.kind = 'type_parameter',
                         tp.name = CASE
                             WHEN row.declname <> '' THEN row.declname
                             WHEN row.defname <> '' THEN row.defname
                             ELSE 'T'
                         END,
                         tp.source = row.source,
                         tp.definition = 'position=' + toString(row.position) +
                                       CASE WHEN row.defval <> '' THEN ' defval=' + row.defval ELSE '' END
            MERGE (source)-[r:TEMPLATE_PARAM]->(tp)
            RETURN count(r) AS cnt
        """, {"batch": batch})
        if results:
            created += sum(r[0] for r in results)

    print(f"  Relationships: TEMPLATE_PARAM ({created} edges)")

    # ENFORCES_CONCEPT edges: type_parameter → ConceptNode
    enforces_batch = []
    for tp in result.template_param_refs:
        if tp.concept_qualified_name:
            source_node = refid_map.get(tp.from_refid)
            if source_node is None:
                continue
            source_qn = source_node.qualified_name
            enforces_batch.append({
                "tp_qn": f"type_param:{source_qn}:{tp.position}",
                "concept_qn": tp.concept_qualified_name,
            })

    if enforces_batch:
        ec_count = 0
        for i in range(0, len(enforces_batch), batch_size):
            batch = enforces_batch[i:i + batch_size]
            results, _meta = db.cypher_query("""
                UNWIND $batch AS row
                MATCH (tp:ClassNode {qualified_name: row.tp_qn})
                MATCH (c:ConceptNode {qualified_name: row.concept_qn})
                MERGE (tp)-[r:ENFORCES_CONCEPT]->(c)
                RETURN count(r) AS cnt
            """, {"batch": batch})
            if results:
                ec_count += sum(r[0] for r in results)
        print(f"  Relationships: ENFORCES_CONCEPT ({ec_count} edges)")
    else:
        print("  Relationships: ENFORCES_CONCEPT (0 edges)")


def _write_implementation_relationships(result: ParseResult) -> None:
    """Create HAS_IMPLEMENTATION relationships from members to ImplementationNodes."""
    if not result.implementation_refs:
        print("  Relationships: HAS_IMPLEMENTATION (0 edges)")
        return

    # Build uid lookups (not saved instances)
    member_uid_by_refid: dict[str, str] = {}
    for node_list in [result.methods, result.attributes, result.enum_values,
                      result.defines, result.functions, result.test_steps]:
        for node in node_list:
            if hasattr(node, 'uid') and node.uid:
                member_uid_by_refid[node.refid] = node.uid

    impl_qname_to_uid: dict[str, str] = {}
    for impl in result.implementations:
        if hasattr(impl, 'uid') and impl.uid:
            impl_qname_to_uid[impl.qualified_name] = impl.uid

    edges = []
    for ref in result.implementation_refs:
        member_uid = member_uid_by_refid.get(ref.member_refid)
        impl_uid = impl_qname_to_uid.get(ref.implementation.qualified_name)
        if member_uid and impl_uid:
            edges.append({"from": member_uid, "to": impl_uid})

    count = _rel_batch(edges, "HAS_IMPLEMENTATION", "uid", "uid",
                       from_label="MemberNode", to_label="ImplementationNode",
                       label="HAS_IMPLEMENTATION")
    print(f"  Relationships: HAS_IMPLEMENTATION ({count} edges)", flush=True)


def _write_invoke_relationships(result: ParseResult) -> None:
    if not result.invokes:
        print("  Invokes: 0")
        return
    # Build refid→uid map for indexed uid-based matching
    refid_to_uid: dict[str, str] = {}
    for lst in (result.methods, result.functions, result.defines):
        for n in lst:
            if hasattr(n, 'uid') and n.uid and hasattr(n, 'refid') and n.refid:
                refid_to_uid[n.refid] = n.uid
    edges = [{"from": refid_to_uid[c.from_refid], "to": refid_to_uid[c.to_refid]}
             for c in result.invokes
             if c.from_refid in refid_to_uid and c.to_refid in refid_to_uid]
    count = _rel_batch(edges, "INVOKES", "uid", "uid",
                       from_label="MemberNode", to_label="MemberNode",
                       label="INVOKES")
    print(f"  Invokes: {count} (of {len(result.invokes)} references)")


# ---------------------------------------------------------------------------
# Test-related relationship writers
# ---------------------------------------------------------------------------

def _write_test_composition_relationships(result: ParseResult) -> None:
    """Create COMPOSES edges for test relationships.

    Two kinds:
    - Namespace → TestNode (from result.compositions where child_type is TestNode)
    - TestNode → AssertionNode / TestStepNode (from result.test_compositions)
    """
    # Namespace → TestNode
    ns_test_count = 0
    for comp in result.compositions:
        if comp.child_type == "TestNode":
            db.cypher_query("""
                MATCH (ns:NamespaceNode {refid: $parent})
                MATCH (t:TestNode {refid: $child})
                MERGE (ns)-[:COMPOSES]->(t)
            """, {"parent": comp.parent_refid, "child": comp.child_refid})
            ns_test_count += 1

    # TestNode → AssertionNode / TestStepNode
    child_count = 0
    if result.test_compositions:
        batch_dicts = [asdict(tc) for tc in result.test_compositions]
        batch_size = 1000
        for i in range(0, len(batch_dicts), batch_size):
            batch = batch_dicts[i:i + batch_size]
            results, _meta = db.cypher_query("""
                UNWIND $batch AS row
                MATCH (parent:TestNode {refid: row.parent_refid})
                MATCH (child {refid: row.child_refid})
                MERGE (parent)-[:COMPOSES]->(child)
                RETURN count(*) AS cnt
            """, {"batch": batch})
            if results:
                child_count += results[0][0]

    print(f"  Relationships: TEST_COMPOSES ({ns_test_count} ns→test, {child_count} test→children)")


def _write_verifies_relationships(result: ParseResult) -> None:
    """Create VERIFIES edges from TestNode to tested code nodes."""
    if not result.verifies:
        print("  Relationships: VERIFIES (0 edges)")
        return
    batch_dicts = [asdict(v) for v in result.verifies]
    batch_size = 1000
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (test:TestNode {refid: row.from_refid})
            MATCH (target {refid: row.to_refid})
            MERGE (test)-[:VERIFIES]->(target)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Relationships: VERIFIES ({created} edges)")


def _write_operand_relationships(result: ParseResult) -> None:
    """Create LEFT_OPERAND and RIGHT_OPERAND edges from AssertionNode to operands."""
    if not result.operands:
        print("  Relationships: OPERANDS (0 edges)")
        return
    batch_dicts = [asdict(o) for o in result.operands]
    batch_size = 1000
    left_count = 0
    right_count = 0
    for side, rel_type, counter in [("left", "LEFT_OPERAND", "left"), ("right", "RIGHT_OPERAND", "right")]:
        side_batch = [b for b in batch_dicts if b["side"] == side]
        if not side_batch:
            continue
        for i in range(0, len(side_batch), batch_size):
            batch = side_batch[i:i + batch_size]
            results, _meta = db.cypher_query(f"""
                UNWIND $batch AS row
                MATCH (assertion:AssertionNode {{refid: row.from_refid}})
                MATCH (operand {{refid: row.to_refid}})
                MERGE (assertion)-[:{rel_type}]->(operand)
                RETURN count(*) AS cnt
            """, {"batch": batch})
            if results:
                if side == "left":
                    left_count += results[0][0]
                else:
                    right_count += results[0][0]
    print(f"  Relationships: LEFT_OPERAND ({left_count} edges), RIGHT_OPERAND ({right_count} edges)")


def _write_callee_relationships(result: ParseResult) -> None:
    """Create CALLEE edges from TestStepNode to called methods/functions/classes."""
    if not result.callees:
        print("  Relationships: CALLEE (0 edges)")
        return
    batch_dicts = [asdict(c) for c in result.callees]
    batch_size = 1000
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (step:TestStepNode {refid: row.from_refid})
            MATCH (callee {refid: row.to_refid})
            MERGE (step)-[:CALLEE]->(callee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Relationships: CALLEE ({created} edges)")


def _write_of_type_relationships(result: ParseResult) -> None:
    """Create OF_TYPE edges from TestFixtureNode to type definitions."""
    if not result.fixture_of_types:
        print("  Relationships: OF_TYPE (0 edges)")
        return
    batch_dicts = [asdict(fo) for fo in result.fixture_of_types]
    batch_size = 1000
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (fixture:TestFixtureNode {refid: row.from_refid})
            MATCH (target {refid: row.to_refid})
            MERGE (fixture)-[:OF_TYPE]->(target)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Relationships: OF_TYPE ({created} edges)")


def _write_checked_by_relationships(result: ParseResult) -> None:
    """Create CHECKED_BY edges from TestFixtureNode to AssertionNode."""
    if not result.fixture_checked_by:
        print("  Relationships: CHECKED_BY (0 edges)")
        return
    batch_dicts = [asdict(cb) for cb in result.fixture_checked_by]
    batch_size = 1000
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (fixture:TestFixtureNode {refid: row.from_refid})
            MATCH (assertion:AssertionNode {refid: row.to_refid})
            MERGE (fixture)-[:CHECKED_BY]->(assertion)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Relationships: CHECKED_BY ({created} edges)")


def _write_defined_in_relationships(result: ParseResult) -> None:
    """Create DEFINED_IN edges from TestFixtureNode to TestStepNode."""
    if not result.fixture_defined_in:
        print("  Relationships: DEFINED_IN (0 edges)")
        return
    batch_dicts = [asdict(di) for di in result.fixture_defined_in]
    batch_size = 1000
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (fixture:TestFixtureNode {refid: row.from_refid})
            MATCH (step:TestStepNode {refid: row.to_refid})
            MERGE (fixture)-[:DEFINED_IN]->(step)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Relationships: DEFINED_IN ({created} edges)")


def _write_depends_on_relationships(result: ParseResult) -> None:
    """Create DEPENDS_ON edges from function/method/attribute nodes to the
    types they depend on.

    Uses the in-memory refid-to-uid mapping for both source and target
    nodes, which handles same-source and cross-source (cppreference,
    dependency) references when they are included in the merged
    ParseResult.
    """
    if not result.depends_on:
        print("  Relationships: DEPENDS_ON (0 edges)")
        return

    # Build refid → uid mapping for all node types that can appear
    # as source or target of DEPENDS_ON edges.
    refid_to_uid: dict[str, str] = {}
    # Track which refids belong to compounds vs members for labeled MATCH.
    compound_refids: set[str] = set()
    member_refids: set[str] = set()
    for lst in (
        result.classes, result.enums, result.unions,
        result.interfaces, result.concepts,
    ):
        for node in lst:
            if hasattr(node, 'uid') and node.uid and hasattr(node, 'refid') and node.refid:
                refid_to_uid[node.refid] = node.uid
                compound_refids.add(node.refid)
    for lst in (result.methods, result.functions, result.attributes):
        for node in lst:
            if hasattr(node, 'uid') and node.uid and hasattr(node, 'refid') and node.refid:
                refid_to_uid[node.refid] = node.uid
                member_refids.add(node.refid)

    compound_edges: list[dict] = []
    member_edges: list[dict] = []
    skipped = 0
    for dep in result.depends_on:
        from_uid = refid_to_uid.get(dep.from_refid)
        to_uid = refid_to_uid.get(dep.to_refid)
        if from_uid and to_uid:
            edge = {"from": from_uid, "to": to_uid}
            if dep.to_refid in compound_refids:
                compound_edges.append(edge)
            else:
                member_edges.append(edge)
        else:
            skipped += 1

    total = 0
    if compound_edges:
        total += _rel_batch(
            compound_edges, "DEPENDS_ON", "uid", "uid",
            from_label="MemberNode", to_label="CompoundNode",
            label="DEPENDS_ON (compound)",
        )
    if member_edges:
        total += _rel_batch(
            member_edges, "DEPENDS_ON", "uid", "uid",
            from_label="MemberNode", to_label="MemberNode",
            label="DEPENDS_ON (member)",
        )
    print(f"  Relationships: DEPENDS_ON ({total} edges, {skipped} unresolved)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest(
    xml_dir: Path | str,
    source: str = "msd",
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str = "neo4j",
    clear: bool = False,
    layer: str = "dependency",
    incremental: bool = True,
) -> None:
    """Parse Doxygen XML and ingest into Neo4j.

    By default, performs an **incremental update**: new nodes are created,
    changed nodes are updated in place, and stale nodes (no longer in the
    source) are deleted — without wiping the existing source first.
    Pass ``clear=True`` (or ``incremental=False``) for a full re-write.

    Args:
        xml_dir: Directory containing Doxygen XML output.
        source: Source label for provenance tracking.
        uri: Neo4j Bolt URI (default: ``$NEO4J_URI`` or ``bolt://localhost:7687``).
        user: Neo4j username (default: ``$NEO4J_USER`` or ``neo4j``).
        password: Neo4j password (default: ``$NEO4J_PASSWORD`` or ``msd-local-dev``).
        database: Neo4j database name.
        clear: If True, clear existing data for this source before a
            full re-write.  Ignored when ``incremental`` is True (the default).
        layer: Layer label ("codebase" for project code, "dependency" for deps).
        incremental: If True (the default), incrementally update instead of
            full re-write.  Set to False to force a full re-write.
    """
    connect_neo4j(uri=uri, user=user, password=password, database=database)

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

        print("Writing to Neo4j...")
        write_result(result)

    # Summary
    results, _meta = db.cypher_query("""
        MATCH (n) WHERE n.source IS NOT NULL
        RETURN n.source AS src, count(*) AS cnt
        ORDER BY src
    """)
    print("\nNode counts by source:")
    for src, cnt in results:
        print(f"  [{src}]: {cnt}")
