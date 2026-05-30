"""
Neo4j backend — ingests ParseResult into a Neo4j graph database.

Requires the ``neo4j`` extra: ``pip install doxygen-index[neo4j]``
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from doxygen_index.parser import ParseResult, parse_xml_dir


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CONSTRAINTS_AND_INDEXES = [
    # Uniqueness constraints
    "CREATE CONSTRAINT file_refid IF NOT EXISTS FOR (f:File) REQUIRE f.refid IS UNIQUE",
    # Use INDEX instead of CONSTRAINT for refid to allow design-layer nodes
    # (which have no refid) to coexist with as-built/dependency nodes.
    "CREATE INDEX namespace_refid IF NOT EXISTS FOR (n:Namespace) ON (n.refid)",
    "CREATE INDEX compound_refid IF NOT EXISTS FOR (c:Compound) ON (c.refid)",
    "CREATE INDEX member_refid IF NOT EXISTS FOR (m:Member) ON (m.refid)",
    # Lookup indexes
    "CREATE INDEX file_name IF NOT EXISTS FOR (f:File) ON (f.name)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (f:File) ON (f.path)",
    "CREATE INDEX namespace_name IF NOT EXISTS FOR (n:Namespace) ON (n.name)",
    "CREATE INDEX compound_name IF NOT EXISTS FOR (c:Compound) ON (c.name)",
    "CREATE INDEX compound_qualified IF NOT EXISTS FOR (c:Compound) ON (c.qualified_name)",
    "CREATE INDEX compound_kind IF NOT EXISTS FOR (c:Compound) ON (c.kind)",
    "CREATE INDEX member_name IF NOT EXISTS FOR (m:Member) ON (m.name)",
    "CREATE INDEX member_qualified IF NOT EXISTS FOR (m:Member) ON (m.qualified_name)",
    "CREATE INDEX member_kind IF NOT EXISTS FOR (m:Member) ON (m.kind)",
    # Layer indexes (aligned with codebase graph primitives)
    "CREATE INDEX compound_layer IF NOT EXISTS FOR (c:Compound) ON (c.layer)",
    "CREATE INDEX member_layer IF NOT EXISTS FOR (m:Member) ON (m.layer)",
    "CREATE INDEX namespace_layer IF NOT EXISTS FOR (n:Namespace) ON (n.layer)",
    # Source provenance
    "CREATE INDEX file_source IF NOT EXISTS FOR (f:File) ON (f.source)",
    "CREATE INDEX compound_source IF NOT EXISTS FOR (c:Compound) ON (c.source)",
    "CREATE INDEX member_source IF NOT EXISTS FOR (m:Member) ON (m.source)",
    "CREATE INDEX namespace_source IF NOT EXISTS FOR (n:Namespace) ON (n.source)",
    # Full-text search
    "CREATE FULLTEXT INDEX doc_search IF NOT EXISTS FOR (n:Compound|Member) ON EACH [n.name, n.qualified_name, n.brief_description, n.detailed_description]",
]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def _get_driver(uri: str, user: str, password: str):
    """Create a Neo4j driver."""
    from neo4j import GraphDatabase
    return GraphDatabase.driver(uri, auth=(user, password))


def ensure_schema(driver, database: str = "neo4j") -> None:
    """Create constraints and indexes if they don't exist.

    Also drops any legacy UNIQUE constraints on refid that were replaced
    with non-unique indexes in the updated schema (v0.2+).
    """
    with driver.session(database=database) as session:
        # Drop legacy refid constraints (replaced by INDEX below)
        for legacy in [
            "DROP CONSTRAINT namespace_refid IF EXISTS",
            "DROP CONSTRAINT compound_refid IF EXISTS",
            "DROP CONSTRAINT member_refid IF EXISTS",
        ]:
            try:
                session.run(legacy)
            except Exception:
                pass  # may not exist
        for stmt in CONSTRAINTS_AND_INDEXES:
            try:
                session.run(stmt)
            except Exception:
                pass  # may already exist


def clear_source(driver, source: str, database: str = "neo4j") -> None:
    """Remove all nodes with a specific source label."""
    with driver.session(database=database) as session:
        session.run("MATCH (p:Parameter)<-[:HAS_PARAMETER]-(m:Member {source: $src}) DETACH DELETE p", src=source)
        session.run("MATCH (m:Member {source: $src}) DETACH DELETE m", src=source)
        session.run("MATCH (c:Compound {source: $src}) DETACH DELETE c", src=source)
        session.run("MATCH (n:Namespace {source: $src}) DETACH DELETE n", src=source)
        session.run("MATCH (f:File {source: $src}) DETACH DELETE f", src=source)
    print(f"  Cleared existing '{source}' data from Neo4j.")


def clear_all(driver, database: str = "neo4j") -> None:
    """Remove all codebase nodes and relationships."""
    with driver.session(database=database) as session:
        session.run("MATCH (p:Parameter) DETACH DELETE p")
        session.run("MATCH (m:Member) DETACH DELETE m")
        session.run("MATCH (c:Compound) DETACH DELETE c")
        session.run("MATCH (n:Namespace) DETACH DELETE n")
        session.run("MATCH (f:File) DETACH DELETE f")
        session.run("MATCH (md:Metadata) DETACH DELETE md")
    print("  Cleared all codebase data from Neo4j.")


def write_result(driver, result: ParseResult, database: str = "neo4j") -> None:
    """Write a ParseResult to Neo4j using MERGE to handle duplicates."""
    with driver.session(database=database) as session:
        _write_files(session, result)
        _write_namespaces(session, result)
        _write_compounds(session, result)
        _write_members(session, result)
        _write_parameters(session, result)
        _write_file_relationships(session)
        _write_include_relationships(session, result)
        _write_inheritance_relationships(session)
        _write_call_relationships(session, result)


def _write_files(session, result: ParseResult):
    if not result.files:
        return
    batch = [asdict(f) for f in result.files]
    session.run(
        """
        UNWIND $batch AS row
        MERGE (f:File {refid: row.refid})
        ON CREATE SET f.name = row.name, f.path = row.path,
                      f.language = row.language, f.source = row.source
        ON MATCH SET f.source = CASE WHEN f.source CONTAINS row.source THEN f.source
                                     ELSE f.source + ',' + row.source END
        """,
        batch=batch,
    )
    print(f"  Files: {len(batch)}")


def _write_namespaces(session, result: ParseResult):
    if not result.namespaces:
        return
    batch = [asdict(n) for n in result.namespaces]
    session.run(
        """
        UNWIND $batch AS row
        MERGE (n:Namespace {refid: row.refid})
        ON CREATE SET n.name = row.name, n.qualified_name = row.qualified_name,
                      n.source = row.source,
                      n.layer = "dependency"
        ON MATCH SET n.source = CASE WHEN n.source CONTAINS row.source THEN n.source
                                     ELSE n.source + ',' + row.source END
        """,
        batch=batch,
    )
    print(f"  Namespaces: {len(batch)}")


def _write_compounds(session, result: ParseResult):
    if not result.compounds:
        return
    batch = [asdict(c) for c in result.compounds]
    session.run(
        """
        UNWIND $batch AS row
        MERGE (c:Compound {refid: row.refid})
        ON CREATE SET c.kind = row.kind, c.name = row.name,
                      c.qualified_name = row.qualified_name,
                      c.file_path = row.file_path, c.line_number = row.line_number,
                      c.brief_description = row.brief_description,
                      c.detailed_description = row.detailed_description,
                      c.base_classes = row.base_classes,
                      c.is_final = row.is_final, c.is_abstract = row.is_abstract,
                      c.source = row.source,
                      c.layer = "dependency"
        ON MATCH SET c.source = CASE WHEN c.source CONTAINS row.source THEN c.source
                                     ELSE c.source + ',' + row.source END
        """,
        batch=batch,
    )
    print(f"  Compounds: {len(batch)}")


def _write_members(session, result: ParseResult):
    if not result.members:
        return
    batch_size = 1000
    batch_dicts = [asdict(m) for m in result.members]
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        session.run(
            """
            UNWIND $batch AS row
            MERGE (m:Member {refid: row.refid})
            ON CREATE SET m.compound_refid = row.compound_refid,
                          m.kind = row.kind, m.name = row.name,
                          m.qualified_name = row.qualified_name,
                          m.type = row.type, m.definition = row.definition,
                          m.argsstring = row.argsstring,
                          m.file_path = row.file_path, m.line_number = row.line_number,
                          m.brief_description = row.brief_description,
                          m.detailed_description = row.detailed_description,
                          m.protection = row.protection,
                          m.is_static = row.is_static, m.is_const = row.is_const,
                          m.is_constexpr = row.is_constexpr,
                          m.is_virtual = row.is_virtual, m.is_inline = row.is_inline,
                          m.is_explicit = row.is_explicit, m.source = row.source,
                          m.layer = "dependency"
            ON MATCH SET m.source = CASE WHEN m.source CONTAINS row.source THEN m.source
                                          ELSE m.source + ',' + row.source END
            """,
            batch=batch,
        )
    print(f"  Members: {len(batch_dicts)}")


def _write_parameters(session, result: ParseResult):
    if not result.parameters:
        return
    batch_size = 1000
    batch_dicts = [asdict(p) for p in result.parameters]
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        session.run(
            """
            UNWIND $batch AS row
            MATCH (m:Member {refid: row.member_refid})
            MERGE (m)-[:HAS_PARAMETER]->(p:Parameter {
                position: row.position,
                name: row.name,
                type: row.type
            })
            ON CREATE SET p.default_value = row.default_value
            """,
            batch=batch,
        )
    print(f"  Parameters: {len(batch_dicts)}")


def _write_file_relationships(session):
    session.run("""
        MATCH (c:Compound) WHERE c.file_path <> ''
        MATCH (f:File {path: c.file_path})
        MERGE (c)-[:DEFINED_IN]->(f)
    """)
    session.run("""
        MATCH (m:Member) WHERE m.compound_refid <> ''
        MATCH (c:Compound {refid: m.compound_refid})
        MERGE (c)-[:CONTAINS]->(m)
    """)
    session.run("""
        MATCH (m:Member) WHERE m.file_path <> ''
        MATCH (f:File {path: m.file_path})
        MERGE (m)-[:DEFINED_IN]->(f)
    """)
    print("  Relationships: DEFINED_IN, CONTAINS")


def _write_include_relationships(session, result: ParseResult):
    resolved = [asdict(i) for i in result.includes if i.included_refid]
    if resolved:
        batch_size = 1000
        for i in range(0, len(resolved), batch_size):
            batch = resolved[i:i + batch_size]
            session.run(
                """
                UNWIND $batch AS row
                MATCH (src:File {refid: row.file_refid})
                MATCH (dst:File {refid: row.included_refid})
                MERGE (src)-[:INCLUDES {
                    included_file: row.included_file,
                    is_local: row.is_local
                }]->(dst)
                """,
                batch=batch,
            )
    unresolved = [i for i in result.includes if not i.included_refid]
    print(f"  Includes: {len(resolved)} resolved, {len(unresolved)} external (skipped)")


def _write_inheritance_relationships(session):
    session.run("""
        MATCH (derived:Compound)
        WHERE size(derived.base_classes) > 0
        UNWIND derived.base_classes AS base_name
        MATCH (base:Compound)
        WHERE base.name = base_name OR base.qualified_name = base_name
        MERGE (derived)-[:INHERITS_FROM]->(base)
    """)
    print("  Relationships: INHERITS_FROM")


def _write_call_relationships(session, result: ParseResult):
    if not result.calls:
        print("  Calls: 0")
        return
    batch_size = 1000
    batch_dicts = [asdict(c) for c in result.calls]
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        r = session.run(
            """
            UNWIND $batch AS row
            MATCH (caller:Member {refid: row.from_refid})
            MATCH (callee:Member {refid: row.to_refid})
            MERGE (caller)-[:CALLS]->(callee)
            RETURN count(*) AS cnt
            """,
            batch=batch,
        )
        created += r.single()["cnt"]
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
    xml_dir = Path(xml_dir)
    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD", "msd-local-dev")

    driver = _get_driver(uri, user, password)
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Error: Could not connect to Neo4j at {uri}: {e}", file=sys.stderr)
        return

    ensure_schema(driver, database)

    if clear:
        clear_source(driver, source, database)

    print(f"Parsing {xml_dir}...")
    result = parse_xml_dir(xml_dir, source=source)

    print("Writing to Neo4j...")
    write_result(driver, result, database)

    # Summary
    with driver.session(database=database) as session:
        r = session.run("""
            MATCH (n) WHERE n.source IS NOT NULL
            WITH n.source AS src, labels(n)[0] AS label
            RETURN src, label, count(*) AS cnt
            ORDER BY src, label
        """)
        print("\nNode counts by source:")
        for record in r:
            print(f"  [{record['src']}] {record['label']}: {record['cnt']}")

    driver.close()
