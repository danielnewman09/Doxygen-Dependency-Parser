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

import hashlib
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
    """Set a source-aware deterministic ``uid`` on *node* in place.

    By default neomodel assigns a random UUID to ``uid``.  When re-indexing
    the same source, this causes ``create_or_update`` to CREATE duplicates
    instead of MERGE-updating existing nodes.

    The deterministic uid is ``SHA1(SHA1(identity_fields), source)`` which:
    - is stable across re-indexes (same source + same identity → same uid)
    - is unique per source (same qualified_name in two sources → different uid)
    """
    identity_fields = list(getattr(node, "_identity_fields", ()) or ())
    identity_values = []
    for field in identity_fields:
        val = getattr(node, field, "")
        identity_values.append(str(val) if val is not None else "")
    identity_hash = hashlib.sha1("|".join(identity_values).encode()).hexdigest()
    source = getattr(node, "source", "")
    final_hash = hashlib.sha1((identity_hash + str(source)).encode()).hexdigest()
    node.uid = final_hash


def _merge_by_keys(node) -> dict:
    """Return ``merge_by`` dict for ``create_or_update``.

    Tells neomodel to MERGE on the node's identity fields **plus** source,
    so updates only match within the same source label.
    """
    identity_fields = list(getattr(node, "_identity_fields", ()) or ())
    keys = identity_fields + ["source"]
    return {"keys": keys}


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
    """Write a ParseResult to Neo4j.

    Nodes are saved via neomodel .save() (MERGE on unique identifier).
    Relationships use .connect() where models declare them, Cypher otherwise.
    """
    batch_refs: list[list] = [
        result.files, result.namespaces, result.classes,
        result.enums, result.unions, result.interfaces, result.concepts,
        result.methods, result.attributes, result.enum_values,
        result.defines, result.functions, result.implementations,
        result.tests, result.assertions, result.test_steps,
        result.test_fixtures, result.literals,
    ]
    batch_labels = [
        "Files", "Namespaces", "Classes", "Enums", "Unions",
        "Interfaces", "Concepts", "Methods", "Attributes", "EnumValues",
        "Defines", "Functions", "Implementations",
        "Tests", "Assertions", "TestSteps", "TestFixtures", "Literals",
    ]
    # Persist nodes, replacing result lists in-place with saved instances
    # so element_id is set for subsequent .connect() calls.
    # create_or_update() returns a list; we unwrap the first element.

    # ── Preserve LLM-enriched descriptions ─────────────────────────
    # The parser auto-generates placeholder descriptions for test nodes
    # (e.g. "assert ==" for assertions).  If a prior enrichment run
    # wrote richer descriptions, we must not overwrite them with the
    # parser's placeholders during re-index.
    _preserve_descriptions(
        result.tests, result.assertions,
        result.test_steps, result.test_fixtures,
    )

    for i, node_list in enumerate(batch_refs):
        if node_list:
            saved = []
            for node in node_list:
                _ensure_deterministic_uid(node)
                merge_by = _merge_by_keys(node)
                result_nodes = node.__class__.create_or_update(
                    node.__properties__, merge_by=merge_by,
                )
                saved.append(result_nodes[0])
            # Replace in-place: update the actual result attribute
            batch_refs[i][:] = saved
            print(f"  {batch_labels[i]}: {len(node_list)}")

    # Parameters use Cypher MERGE — no UniqueIdProperty on ParameterNode
    _write_parameters(result)

    # Relationships
    _write_compound_member_connect(result)
    _write_namespace_composition(result)
    _write_file_relationships()
    _write_include_relationships(result)
    _write_inheritance_relationships()
    _write_specialization_relationships(result)
    _write_template_param_relationships(result)
    _write_invoke_relationships(result)
    _write_implementation_relationships(result)
    _write_test_composition_relationships(result)
    _write_verifies_relationships(result)
    _write_operand_relationships(result)
    _write_callee_relationships(result)
    _write_of_type_relationships(result)
    _write_checked_by_relationships(result)
    _write_defined_in_relationships(result)


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
            # All nodes of this type are stale
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

    # Delete stale files (by path)
    _delete_stale("FileNode", "path", live_file_paths)

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
    live_file_paths = _collect_live_file_paths(result)
    live_member_refids = _collect_live_member_refids(result)

    write_result(result)

    return delete_stale_nodes(source, live_qnames, live_file_paths, live_member_refids)


# ---------------------------------------------------------------------------
# Relationship helpers (Cypher via db.cypher_query)
# ---------------------------------------------------------------------------

def _write_parameters(result: ParseResult) -> None:
    if not result.parameters:
        return
    batch_size = 1000
    batch_dicts = [p.__properties__ for p in result.parameters]
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        db.cypher_query("""
            UNWIND $batch AS row
            MATCH (m:MemberNode {refid: row.member_refid})
            MERGE (m)-[:HAS_PARAMETER]->(p:ParameterNode {
                position: row.position,
                name: row.name,
                type: row.type
            })
            ON CREATE SET p.default_value = row.default_value,
                          p.member_refid = row.member_refid
        """, {"batch": batch})
    print(f"  Parameters: {len(batch_dicts)}")


def _write_compound_member_connect(result: ParseResult) -> None:
    """Create COMPOSES relationships using neomodel .connect().

    Builds lookup dicts by refid, then connects:
      - ClassNode/InterfaceNode → MethodNode via .methods.connect()
      - ClassNode → AttributeNode via .attributes.connect()
      - EnumNode → EnumValueNode via .values.connect()

    Failures are counted and reported.
    """
    compound_by_refid: dict[str, object] = {}
    for c in result.classes + result.enums + result.unions + result.interfaces:
        compound_by_refid[c.refid] = c

    success, skipped, failed = 0, 0, 0

    for m in result.methods:
        parent = compound_by_refid.get(m.compound_refid)
        if parent is None or not hasattr(parent, 'methods'):
            skipped += 1
            continue
        try:
            parent.methods.connect(m)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect MethodNode {m.qualified_name} "
                  f"to parent {m.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    for a in result.attributes:
        parent = compound_by_refid.get(a.compound_refid)
        if parent is None or not hasattr(parent, 'attributes'):
            skipped += 1
            continue
        try:
            parent.attributes.connect(a)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect AttributeNode {a.qualified_name} "
                  f"to parent {a.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    for v in result.enum_values:
        parent = compound_by_refid.get(v.compound_refid)
        if parent is None or not hasattr(parent, 'values'):
            skipped += 1
            continue
        try:
            parent.values.connect(v)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect EnumValueNode {v.qualified_name} "
                  f"to parent {v.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Relationships via .connect(): {success} connected, "
          f"{skipped} skipped, {failed} failed")


def _write_file_relationships() -> None:
    db.cypher_query("""
        MATCH (c:CompoundNode) WHERE c.file_path <> ''
        MATCH (f:FileNode {path: c.file_path})
        MERGE (c)-[:DEFINED_IN]->(f)
    """)
    db.cypher_query("""
        MATCH (m:MemberNode) WHERE m.file_path <> ''
        MATCH (f:FileNode {path: m.file_path})
        MERGE (m)-[:DEFINED_IN]->(f)
    """)
    db.cypher_query("""
        MATCH (n:NamespaceNode) WHERE n.refid <> ''
        MATCH (f:FileNode {refid: n.refid})
        MERGE (n)-[:DEFINED_IN]->(f)
    """)
    print("  Relationships: DEFINED_IN")


def _write_include_relationships(result: ParseResult) -> None:
    resolved = [asdict(i) for i in result.includes if i.included_refid]
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
    # Build namespace lookup by qualified_name (which equals refid)
    ns_by_qname: dict[str, object] = {ns.qualified_name: ns for ns in result.namespaces}

    success, skipped, failed = 0, 0, 0

    # --- Namespace → ClassNode ---
    for cls in result.classes:
        ns_qname = _namespace_for(cls.qualified_name, getattr(cls, 'module', ''))
        parent_ns = ns_by_qname.get(ns_qname)
        if parent_ns is None or not hasattr(parent_ns, 'classes'):
            skipped += 1
            continue
        try:
            parent_ns.classes.connect(cls)
            success += 1
        except Exception:
            failed += 1

    # --- Namespace → InterfaceNode ---
    for iface in result.interfaces:
        ns_qname = _namespace_for(iface.qualified_name, getattr(iface, 'module', ''))
        parent_ns = ns_by_qname.get(ns_qname)
        if parent_ns is None or not hasattr(parent_ns, 'interfaces'):
            skipped += 1
            continue
        try:
            parent_ns.interfaces.connect(iface)
            success += 1
        except Exception:
            failed += 1

    # --- Namespace → EnumNode ---
    for enum in result.enums:
        ns_qname = _namespace_for(enum.qualified_name, getattr(enum, 'module', ''))
        parent_ns = ns_by_qname.get(ns_qname)
        if parent_ns is None or not hasattr(parent_ns, 'enums'):
            skipped += 1
            continue
        try:
            parent_ns.enums.connect(enum)
            success += 1
        except Exception:
            failed += 1

    # --- Namespace → FunctionNode ---
    for func in result.functions:
        ns_qname = _namespace_for(func.qualified_name)
        parent_ns = ns_by_qname.get(ns_qname)
        if parent_ns is None or not hasattr(parent_ns, 'functions'):
            skipped += 1
            continue
        try:
            parent_ns.functions.connect(func)
            success += 1
        except Exception:
            failed += 1

    # --- Namespace → child NamespaceNode (COMPOSES) ---
    for ns in result.namespaces:
        if '.' not in ns.qualified_name and '::' not in ns.qualified_name:
            continue  # top-level namespace has no parent
        parent_qname = _namespace_for(ns.qualified_name)
        parent_ns = ns_by_qname.get(parent_qname)
        if parent_ns is None or not hasattr(parent_ns, 'namespaces'):
            skipped += 1
            continue
        try:
            parent_ns.namespaces.connect(ns)
            success += 1
        except Exception:
            failed += 1

    print(f"  Relationships: NS_COMPOSES ({success} connected, {skipped} skipped, {failed} failed)")


def _write_inheritance_relationships() -> None:
    db.cypher_query("""
        MATCH (derived:CompoundNode)
        WHERE size(derived.base_classes) > 0
        UNWIND derived.base_classes AS base_name
        MATCH (base:CompoundNode)
        WHERE base.name = base_name OR base.qualified_name = base_name
        MERGE (derived)-[:INHERITS_FROM]->(base)
    """)
    print("  Relationships: INHERITS_FROM")


def _write_specialization_relationships(result: ParseResult) -> None:
    """Create SPECIALIZES edges using specialization refs from ParseResult."""
    if not result.specializes_refs:
        print("  Relationships: SPECIALIZES (0 edges)")
        return

    # Build lookup: qualified_name → compound node (must have element_id from save)
    compounds_by_qn: dict[str, object] = {}
    for node_list in [result.classes, result.enums, result.unions, result.interfaces, result.concepts]:
        for node in node_list:
            compounds_by_qn[node.qualified_name] = node

    count = 0
    for spec_ref in result.specializes_refs:
        primary = compounds_by_qn.get(spec_ref.primary_template_qualified_name)
        spec = compounds_by_qn.get(spec_ref.from_qualified_name)
        if primary and spec and hasattr(spec, 'specializes'):
            try:
                spec.specializes.connect(primary)
                count += 1
            except Exception as e:
                print(f"Warning: could not connect SPECIALIZES "
                      f"{spec_ref.from_qualified_name} → "
                      f"{spec_ref.primary_template_qualified_name}: {e}",
                      file=sys.stderr)

    print(f"  Relationships: SPECIALIZES ({count} edges)")


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
            MERGE (tp:ClassNode {qualified_name: 'type_param:' + row.from_qn + ':' + toString(row.position)})
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

    # Build refid → saved member lookup
    member_by_refid: dict[str, object] = {}
    for node_list in [result.methods, result.attributes, result.enum_values,
                      result.defines, result.functions, result.test_steps]:
        for node in node_list:
            member_by_refid[node.refid] = node

    # Build qualified_name → saved implementation lookup
    impl_by_qname: dict[str, object] = {}
    for impl in result.implementations:
        impl_by_qname[impl.qualified_name] = impl

    success, failed = 0, 0
    for ref in result.implementation_refs:
        member = member_by_refid.get(ref.member_refid)
        impl = impl_by_qname.get(ref.implementation.qualified_name)
        if member is None or impl is None:
            failed += 1
            continue
        try:
            member.implementation_ref.connect(impl)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect HAS_IMPLEMENTATION for "
                  f"{ref.member_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Relationships: HAS_IMPLEMENTATION ({success} edges, {failed} failed)")


def _write_invoke_relationships(result: ParseResult) -> None:
    if not result.invokes:
        print("  Invokes: 0")
        return
    batch_size = 1000
    batch_dicts = [asdict(c) for c in result.invokes]
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (invoker:MethodNode|FunctionNode {refid: row.from_refid})
            MATCH (invokee:MethodNode|FunctionNode {refid: row.to_refid})
            MERGE (invoker)-[:INVOKES]->(invokee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Invokes: {created} (of {len(batch_dicts)} references)")


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
