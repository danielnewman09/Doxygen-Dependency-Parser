"""
Neo4j backend — ingests ParseResult into a Neo4j graph database.

Uses neomodel for node persistence (replaces raw Cypher MERGE with .save()).
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from neomodel import db

# Import all node models so neomodel registry discovers them before
# install_all_labels is called.
from codegraph import (  # noqa: F401 — needed for install_all_labels
    ClassNode, InterfaceNode, EnumNode, UnionNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
)

from doxygen_index.parser import ParseResult, parse_xml_dir


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
        ("MATCH (m:Member {source: $src}) "
         "WITH collect(m.refid) AS refids "
         "MATCH (p:ParameterNode) WHERE p.member_refid IN refids "
         "DETACH DELETE p",
         {"src": source}),
        ("MATCH (m:Member {source: $src}) DETACH DELETE m",
         {"src": source}),
        ("MATCH (c:Compound {source: $src}) DETACH DELETE c",
         {"src": source}),
        ("MATCH (n:NamespaceNode {source: $src}) DETACH DELETE n",
         {"src": source}),
        ("MATCH (f:FileNode {source: $src}) DETACH DELETE f",
         {"src": source}),
    ]
    for query, params in queries:
        db.cypher_query(query, params)
    print(f"  Cleared existing '{source}' data from Neo4j.")


def clear_all() -> None:
    """Remove all codebase nodes and relationships."""
    queries = [
        "MATCH (p:ParameterNode) DETACH DELETE p",
        "MATCH (m:Member) DETACH DELETE m",
        "MATCH (c:Compound) DETACH DELETE c",
        "MATCH (n:NamespaceNode) DETACH DELETE n",
        "MATCH (f:FileNode) DETACH DELETE f",
        "MATCH (md:Metadata) DETACH DELETE md",
    ]
    for query in queries:
        db.cypher_query(query)
    print("  Cleared all codebase data from Neo4j.")


# ---------------------------------------------------------------------------
# Node writer — uses neomodel .save()
# ---------------------------------------------------------------------------

def write_result(result: ParseResult) -> None:
    """Write a ParseResult to Neo4j.

    Nodes are saved via neomodel .save() (MERGE on unique identifier).
    Relationships use .connect() where models declare them, Cypher otherwise.
    """
    typed_batches = [
        (result.files, "Files"),
        (result.namespaces, "Namespaces"),
        (result.classes, "Classes"),
        (result.enums, "Enums"),
        (result.unions, "Unions"),
        (result.interfaces, "Interfaces"),
        (result.methods, "Methods"),
        (result.attributes, "Attributes"),
        (result.enum_values, "EnumValues"),
        (result.defines, "Defines"),
        (result.functions, "Functions"),
    ]
    for node_list, label in typed_batches:
        if node_list:
            for node in node_list:
                node.__class__.create_or_update(node.__properties__)
            print(f"  {label}: {len(node_list)}")

    # Parameters use Cypher MERGE — no UniqueIdProperty on ParameterNode
    _write_parameters(result)

    # Relationships
    _write_compound_member_connect(result)
    _write_file_relationships()
    _write_include_relationships(result)
    _write_inheritance_relationships()
    _write_specialization_relationships(result)
    _write_invoke_relationships(result)


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
            MATCH (m:Member {refid: row.member_refid})
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
        MATCH (c:Compound) WHERE c.file_path <> ''
        MATCH (f:FileNode {path: c.file_path})
        MERGE (c)-[:DEFINED_IN]->(f)
    """)
    db.cypher_query("""
        MATCH (m:Member) WHERE m.file_path <> ''
        MATCH (f:FileNode {path: m.file_path})
        MERGE (m)-[:DEFINED_IN]->(f)
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


def _write_inheritance_relationships() -> None:
    db.cypher_query("""
        MATCH (derived:Compound)
        WHERE size(derived.base_classes) > 0
        UNWIND derived.base_classes AS base_name
        MATCH (base:Compound)
        WHERE base.name = base_name OR base.qualified_name = base_name
        MERGE (derived)-[:INHERITS_FROM]->(base)
    """)
    print("  Relationships: INHERITS_FROM")


def _write_specialization_relationships(result: ParseResult) -> None:
    """Create SPECIALIZES edges: specialization → primary template."""
    # Build lookup: qualified_name → compound node (must have element_id from save)
    compounds_by_qn: dict[str, object] = {}
    for node_list in [result.classes, result.enums, result.unions, result.interfaces]:
        for node in node_list:
            compounds_by_qn[node.qualified_name] = node

    count = 0
    for qn, spec in compounds_by_qn.items():
        if "<" not in qn or not qn.endswith(">"):
            continue
        # Only treat as specialization if <...> is in the leaf segment
        segments = qn.split("::")
        if "<" not in segments[-1]:
            continue  # nested type carrying outer template params (e.g. chunk_view<V>::iterator)
        leaf_name = segments[-1].split("<")[0]
        primary_qn = "::".join(segments[:-1] + [leaf_name])
        primary = compounds_by_qn.get(primary_qn)
        if primary:
            try:
                spec.specializes.connect(primary)
                count += 1
            except Exception as e:
                print(f"Warning: could not connect SPECIALIZES {qn} → {primary_qn}: {e}",
                      file=sys.stderr)

    print(f"  Relationships: SPECIALIZES ({count} edges)")


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
            MATCH (invoker:Method|Function {refid: row.from_refid})
            MATCH (invokee:Method|Function {refid: row.to_refid})
            MERGE (invoker)-[:INVOKES]->(invokee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Invokes: {created} (of {len(batch_dicts)} references)")


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
) -> None:
    """Parse Doxygen XML and ingest into Neo4j.

    Args:
        xml_dir: Directory containing Doxygen XML output.
        source: Source label for provenance tracking.
        uri: Neo4j Bolt URI (default: ``$NEO4J_URI`` or ``bolt://localhost:7687``).
        user: Neo4j username (default: ``$NEO4J_USER`` or ``neo4j``).
        password: Neo4j password (default: ``$NEO4J_PASSWORD`` or ``msd-local-dev``).
        database: Neo4j database name.
        clear: If True, clear existing data for this source before ingesting.
    """
    from neomodel import get_config

    xml_dir = Path(xml_dir)
    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD", "msd-local-dev")
    database = database or os.environ.get("NEO4J_DATABASE", "neo4j")

    # Configure neomodel connection
    config = get_config()
    _bolt_host = uri.replace("bolt://", "")
    config.database_url = f"bolt://{user}:{password}@{_bolt_host}"
    config.database_name = database
    db.set_connection(config.database_url)

    # Verify connectivity
    try:
        db.cypher_query("RETURN 1")
    except Exception as e:
        print(f"Error: Could not connect to Neo4j at {uri}: {e}", file=sys.stderr)
        return

    ensure_schema()

    if clear:
        clear_source(source)

    print(f"Parsing {xml_dir}...")
    result = parse_xml_dir(xml_dir, source=source)

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
