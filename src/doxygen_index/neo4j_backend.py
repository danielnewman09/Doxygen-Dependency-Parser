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
    CompoundNode,
    FileNode,
    MemberNode,
    NamespaceNode,
    ParameterNode,
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
    """Remove all nodes with a specific source label.

    Uses db.cypher_query for fast bulk deletion.
    Label names match the neomodel class names (FileNode, CompoundNode, etc.).
    """
    queries = [
        ("MATCH (p:ParameterNode)<-[:HAS_PARAMETER]-(m:MemberNode {source: $src}) DETACH DELETE p",
         {"src": source}),
        ("MATCH (m:MemberNode {source: $src}) DETACH DELETE m",
         {"src": source}),
        ("MATCH (c:CompoundNode {source: $src}) DETACH DELETE c",
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
    """Remove all codebase nodes and relationships.

    Uses db.cypher_query for fast bulk deletion.
    Label names match the neomodel class names.
    """
    queries = [
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
# Node writer — uses neomodel .save()
# ---------------------------------------------------------------------------

def write_result(result: ParseResult) -> None:
    """Write a ParseResult to Neo4j.

    Nodes are saved via neomodel .save() (MERGE on unique identifier).
    Relationships are created via db.cypher_query for speed and to
    handle relationship types not modelled as neomodel relationships.
    """
    # --- Nodes ---
    if result.files:
        for f in result.files:
            f.save()
        print(f"  Files: {len(result.files)}")

    if result.namespaces:
        for ns in result.namespaces:
            ns.save()
        print(f"  Namespaces: {len(result.namespaces)}")

    if result.compounds:
        for c in result.compounds:
            c.save()
        print(f"  Compounds: {len(result.compounds)}")

    if result.members:
        for m in result.members:
            m.save()
        print(f"  Members: {len(result.members)}")

    # Parameters use Cypher MERGE — no UniqueIdProperty on ParameterNode
    _write_parameters(result)

    # --- Relationships ---
    _write_file_relationships()
    _write_include_relationships(result)
    _write_inheritance_relationships()
    _write_call_relationships(result)


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


def _write_file_relationships() -> None:
    db.cypher_query("""
        MATCH (c:CompoundNode) WHERE c.file_path <> ''
        MATCH (f:FileNode {path: c.file_path})
        MERGE (c)-[:DEFINED_IN]->(f)
    """)
    db.cypher_query("""
        MATCH (m:MemberNode) WHERE m.compound_refid <> ''
        MATCH (c:CompoundNode {refid: m.compound_refid})
        MERGE (c)-[:COMPOSES]->(m)
    """)
    db.cypher_query("""
        MATCH (m:MemberNode) WHERE m.file_path <> ''
        MATCH (f:FileNode {path: m.file_path})
        MERGE (m)-[:DEFINED_IN]->(f)
    """)
    print("  Relationships: DEFINED_IN, COMPOSES")


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
        MATCH (derived:CompoundNode)
        WHERE size(derived.base_classes) > 0
        UNWIND derived.base_classes AS base_name
        MATCH (base:CompoundNode)
        WHERE base.name = base_name OR base.qualified_name = base_name
        MERGE (derived)-[:INHERITS_FROM]->(base)
    """)
    print("  Relationships: INHERITS_FROM")


def _write_call_relationships(result: ParseResult) -> None:
    if not result.calls:
        print("  Calls: 0")
        return
    batch_size = 1000
    batch_dicts = [asdict(c) for c in result.calls]
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (caller:MemberNode {refid: row.from_refid})
            MATCH (callee:MemberNode {refid: row.to_refid})
            MERGE (caller)-[:CALLS]->(callee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Calls: {created} (of {len(batch_dicts)} references)")


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
        WITH n.source AS src, labels(n)[0] AS label
        RETURN src, label, count(*) AS cnt
        ORDER BY src, label
    """)
    print("\nNode counts by source:")
    for src, label, cnt in results:
        print(f"  [{src}] {label}: {cnt}")
