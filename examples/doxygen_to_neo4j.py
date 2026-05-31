#!/usr/bin/env python3
"""
Doxygen XML to Neo4j Graph Database Ingester

Parses Doxygen XML output and creates a property graph in Neo4j for
codebase navigation, call graph traversal, and design traceability.

Replaces doxygen_to_sqlite.py — writes to Neo4j instead of SQLite.

Usage:
    python doxygen_to_neo4j.py <xml_dir> [options]

Example:
    python doxygen_to_neo4j.py build/Debug/docs/xml
    python doxygen_to_neo4j.py build/Debug/docs/xml --uri bolt://localhost:7687
"""

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase


# ---------------------------------------------------------------------------
# Neo4j schema: constraints and indexes
# ---------------------------------------------------------------------------

CONSTRAINTS_AND_INDEXES = [
    # Uniqueness constraints (also create indexes)
    "CREATE CONSTRAINT file_refid IF NOT EXISTS FOR (f:File) REQUIRE f.refid IS UNIQUE",
    "CREATE CONSTRAINT namespace_refid IF NOT EXISTS FOR (n:Namespace) REQUIRE n.refid IS UNIQUE",
    "CREATE CONSTRAINT compound_refid IF NOT EXISTS FOR (c:Compound) REQUIRE c.refid IS UNIQUE",
    "CREATE CONSTRAINT member_refid IF NOT EXISTS FOR (m:Member) REQUIRE m.refid IS UNIQUE",
    # Lookup indexes for common query patterns
    "CREATE INDEX file_name IF NOT EXISTS FOR (f:File) ON (f.name)",
    "CREATE INDEX file_path IF NOT EXISTS FOR (f:File) ON (f.path)",
    "CREATE INDEX namespace_name IF NOT EXISTS FOR (n:Namespace) ON (n.name)",
    "CREATE INDEX compound_name IF NOT EXISTS FOR (c:Compound) ON (c.name)",
    "CREATE INDEX compound_qualified IF NOT EXISTS FOR (c:Compound) ON (c.qualified_name)",
    "CREATE INDEX compound_kind IF NOT EXISTS FOR (c:Compound) ON (c.kind)",
    "CREATE INDEX member_name IF NOT EXISTS FOR (m:Member) ON (m.name)",
    "CREATE INDEX member_qualified IF NOT EXISTS FOR (m:Member) ON (m.qualified_name)",
    "CREATE INDEX member_kind IF NOT EXISTS FOR (m:Member) ON (m.kind)",
    # Source provenance index (msd, eigen, boost, sdl, etc.)
    "CREATE INDEX file_source IF NOT EXISTS FOR (f:File) ON (f.source)",
    "CREATE INDEX compound_source IF NOT EXISTS FOR (c:Compound) ON (c.source)",
    "CREATE INDEX member_source IF NOT EXISTS FOR (m:Member) ON (m.source)",
    "CREATE INDEX namespace_source IF NOT EXISTS FOR (n:Namespace) ON (n.source)",
    # Full-text index for documentation search
    "CREATE FULLTEXT INDEX doc_search IF NOT EXISTS FOR (n:Compound|Member) ON EACH [n.name, n.qualified_name, n.brief_description, n.detailed_description]",
]


# ---------------------------------------------------------------------------
# XML parsing helpers (same logic as doxygen_to_sqlite.py)
# ---------------------------------------------------------------------------

def get_text(element: Optional[ET.Element], default: str = "") -> str:
    """Extract text content from an element, handling nested elements."""
    if element is None:
        return default
    text_parts = []
    if element.text:
        text_parts.append(element.text)
    for child in element:
        text_parts.append(get_text(child))
        if child.tail:
            text_parts.append(child.tail)
    result = " ".join(text_parts)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


def parse_description(desc_elem: Optional[ET.Element]) -> str:
    """Parse a brief or detailed description element."""
    if desc_elem is None:
        return ""
    return get_text(desc_elem)


def parse_location(loc_elem: Optional[ET.Element]) -> tuple[Optional[str], Optional[int]]:
    """Extract file path and line number from location element."""
    if loc_elem is None:
        return None, None
    file_path = loc_elem.get("file")
    line = loc_elem.get("line")
    return file_path, int(line) if line else None


# ---------------------------------------------------------------------------
# Neo4j batch writer — collects data and writes in batches
# ---------------------------------------------------------------------------

class Neo4jBatchWriter:
    """Collects parsed Doxygen data and writes to Neo4j in batches."""

    def __init__(self, driver, database: str = "neo4j", source: str = "msd"):
        self.driver = driver
        self.database = database
        self.source = source

        # Collected data for batch writing
        self.files: list[dict] = []
        self.namespaces: list[dict] = []
        self.compounds: list[dict] = []
        self.members: list[dict] = []
        self.parameters: list[dict] = []
        self.includes: list[dict] = []
        self.calls: list[dict] = []       # from_refid -> to_refid (calls)
        self.called_by: list[dict] = []   # from_refid -> to_refid (called_by)

        # Caches for resolving file paths to refids
        self.file_path_to_refid: dict[str, str] = {}
        self.file_refid_set: set[str] = set()

    def ensure_schema(self):
        """Create constraints and indexes."""
        with self.driver.session(database=self.database) as session:
            for stmt in CONSTRAINTS_AND_INDEXES:
                session.run(stmt)

    def clear_codebase_data(self, source_filter: Optional[str] = None):
        """Remove existing codebase nodes and relationships.

        Args:
            source_filter: If provided, only delete nodes with this source label.
                           If None, delete all codebase nodes.
        """
        with self.driver.session(database=self.database) as session:
            if source_filter:
                # Delete only nodes from a specific source
                session.run("MATCH (p:Parameter)<-[:HAS_PARAMETER]-(m:Member {source: $src}) DETACH DELETE p", src=source_filter)
                session.run("MATCH (m:Member {source: $src}) DETACH DELETE m", src=source_filter)
                session.run("MATCH (c:Compound {source: $src}) DETACH DELETE c", src=source_filter)
                session.run("MATCH (n:Namespace {source: $src}) DETACH DELETE n", src=source_filter)
                session.run("MATCH (f:File {source: $src}) DETACH DELETE f", src=source_filter)
                print(f"Cleared existing '{source_filter}' data from Neo4j.")
            else:
                # Delete in dependency order to avoid constraint violations
                session.run("MATCH (p:Parameter) DETACH DELETE p")
                session.run("MATCH (m:Member) DETACH DELETE m")
                session.run("MATCH (c:Compound) DETACH DELETE c")
                session.run("MATCH (n:Namespace) DETACH DELETE n")
                session.run("MATCH (f:File) DETACH DELETE f")
                session.run("MATCH (md:Metadata) DETACH DELETE md")
                print("Cleared existing codebase data from Neo4j.")

    # ---- Collection methods (called during XML parsing) ----

    def add_file(self, refid: str, name: str, path: Optional[str], language: str):
        self.files.append({
            "refid": refid,
            "name": name,
            "path": path or "",
            "language": language,
            "source": self.source,
        })
        self.file_refid_set.add(refid)
        if path:
            self.file_path_to_refid[path] = refid

    def add_namespace(self, refid: str, name: str, qualified_name: str):
        self.namespaces.append({
            "refid": refid,
            "name": name,
            "qualified_name": qualified_name,
            "source": self.source,
        })

    def add_compound(self, refid: str, kind: str, name: str, qualified_name: str,
                     file_path: Optional[str], line_number: Optional[int],
                     brief: str, detailed: str, base_classes: list[str],
                     is_final: bool, is_abstract: bool):
        self.compounds.append({
            "refid": refid,
            "kind": kind,
            "name": name,
            "qualified_name": qualified_name,
            "file_path": file_path or "",
            "line_number": line_number,
            "brief_description": brief,
            "detailed_description": detailed,
            "base_classes": base_classes,
            "is_final": is_final,
            "is_abstract": is_abstract,
            "source": self.source,
        })

    def add_member(self, refid: str, compound_refid: Optional[str], kind: str,
                   name: str, qualified_name: str, type_str: str, definition: str,
                   argsstring: str, file_path: Optional[str], line_number: Optional[int],
                   brief: str, detailed: str, protection: str,
                   is_static: bool, is_const: bool, is_constexpr: bool,
                   is_virtual: bool, is_inline: bool, is_explicit: bool):
        self.members.append({
            "refid": refid,
            "compound_refid": compound_refid or "",
            "kind": kind,
            "name": name,
            "qualified_name": qualified_name,
            "type": type_str,
            "definition": definition,
            "argsstring": argsstring,
            "file_path": file_path or "",
            "line_number": line_number,
            "brief_description": brief,
            "detailed_description": detailed,
            "protection": protection,
            "is_static": is_static,
            "is_const": is_const,
            "is_constexpr": is_constexpr,
            "is_virtual": is_virtual,
            "is_inline": is_inline,
            "is_explicit": is_explicit,
            "source": self.source,
        })

    def add_parameter(self, member_refid: str, position: int, name: str,
                      type_str: str, default_value: Optional[str]):
        self.parameters.append({
            "member_refid": member_refid,
            "position": position,
            "name": name or "",
            "type": type_str,
            "default_value": default_value or "",
        })

    def add_include(self, file_refid: str, included_file: str,
                    included_refid: Optional[str], is_local: bool):
        self.includes.append({
            "file_refid": file_refid,
            "included_file": included_file,
            "included_refid": included_refid or "",
            "is_local": is_local,
        })

    def add_call(self, from_member_refid: str, to_member_refid: str, to_member_name: str):
        self.calls.append({
            "from_refid": from_member_refid,
            "to_refid": to_member_refid,
            "to_name": to_member_name,
        })

    def add_called_by(self, member_refid: str, caller_refid: str, caller_name: str):
        self.called_by.append({
            "member_refid": member_refid,
            "caller_refid": caller_refid,
            "caller_name": caller_name,
        })

    # ---- Batch write methods ----

    def flush(self, metadata: Optional[dict] = None):
        """Write all collected data to Neo4j in batches."""
        with self.driver.session(database=self.database) as session:
            self._write_files(session)
            self._write_namespaces(session)
            self._write_compounds(session)
            self._write_members(session)
            self._write_parameters(session)
            self._write_file_relationships(session)
            self._write_include_relationships(session)
            self._write_inheritance_relationships(session)
            self._write_call_relationships(session)
            if metadata:
                self._write_metadata(session, metadata)

    def _write_files(self, session):
        if not self.files:
            return
        session.run(
            """
            UNWIND $batch AS row
            MERGE (f:File {refid: row.refid})
            ON CREATE SET f.name = row.name, f.path = row.path,
                          f.language = row.language, f.source = row.source
            ON MATCH SET f.source = CASE WHEN f.source = row.source THEN f.source
                                         ELSE f.source + ',' + row.source END
            """,
            batch=self.files,
        )
        print(f"  Files: {len(self.files)}")

    def _write_namespaces(self, session):
        if not self.namespaces:
            return
        session.run(
            """
            UNWIND $batch AS row
            MERGE (n:Namespace {refid: row.refid})
            ON CREATE SET n.name = row.name, n.qualified_name = row.qualified_name,
                          n.source = row.source,
                          n.layer = "codebase"
            ON MATCH SET n.source = CASE WHEN n.source CONTAINS row.source THEN n.source
                                         ELSE n.source + ',' + row.source END
            """,
            batch=self.namespaces,
        )
        print(f"  Namespaces: {len(self.namespaces)}")

    def _write_compounds(self, session):
        if not self.compounds:
            return
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
                          c.layer = "codebase"
            ON MATCH SET c.source = CASE WHEN c.source CONTAINS row.source THEN c.source
                                         ELSE c.source + ',' + row.source END
            """,
            batch=self.compounds,
        )
        print(f"  Compounds: {len(self.compounds)}")

    def _write_members(self, session):
        if not self.members:
            return
        # Write in batches of 1000 to avoid transaction size limits
        batch_size = 1000
        for i in range(0, len(self.members), batch_size):
            batch = self.members[i:i + batch_size]
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
                              m.layer = "codebase"
                ON MATCH SET m.source = CASE WHEN m.source CONTAINS row.source THEN m.source
                                              ELSE m.source + ',' + row.source END
                """,
                batch=batch,
            )
        print(f"  Members: {len(self.members)}")

    def _write_parameters(self, session):
        if not self.parameters:
            return
        batch_size = 1000
        for i in range(0, len(self.parameters), batch_size):
            batch = self.parameters[i:i + batch_size]
            session.run(
                """
                UNWIND $batch AS row
                MATCH (m:Member {refid: row.member_refid})
                CREATE (p:Parameter {
                    position: row.position,
                    name: row.name,
                    type: row.type,
                    default_value: row.default_value
                })
                CREATE (m)-[:HAS_PARAMETER]->(p)
                """,
                batch=batch,
            )
        print(f"  Parameters: {len(self.parameters)}")

    def _write_file_relationships(self, session):
        """Create DEFINED_IN relationships from Compounds/Members to Files."""
        # Compound -> File (via file_path)
        session.run(
            """
            MATCH (c:Compound) WHERE c.file_path <> ''
            MATCH (f:File {path: c.file_path})
            MERGE (c)-[:DEFINED_IN]->(f)
            """
        )
        # Member -> Compound (via compound_refid)
        session.run(
            """
            MATCH (m:Member) WHERE m.compound_refid <> ''
            MATCH (c:Compound {refid: m.compound_refid})
            MERGE (c)-[:CONTAINS]->(m)
            """
        )
        # Member -> File (via file_path)
        session.run(
            """
            MATCH (m:Member) WHERE m.file_path <> ''
            MATCH (f:File {path: m.file_path})
            MERGE (m)-[:DEFINED_IN]->(f)
            """
        )
        print("  Relationships: DEFINED_IN, CONTAINS")

    def _write_include_relationships(self, session):
        """Create INCLUDES relationships between Files."""
        if not self.includes:
            return
        # For includes with a known refid, link directly
        resolved = [i for i in self.includes if i["included_refid"]]
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
        # For includes without a refid, store the include name on a
        # relationship to a placeholder or skip (external headers).
        # We skip these — they're system/external headers.
        unresolved = [i for i in self.includes if not i["included_refid"]]
        print(f"  Includes: {len(resolved)} resolved, {len(unresolved)} external (skipped)")

    def _write_inheritance_relationships(self, session):
        """Create INHERITS_FROM relationships between Compounds."""
        # base_classes is stored as a list of names on the Compound node.
        # We match by name to resolve to Compound nodes where possible.
        session.run(
            """
            MATCH (derived:Compound)
            WHERE size(derived.base_classes) > 0
            UNWIND derived.base_classes AS base_name
            MATCH (base:Compound)
            WHERE base.name = base_name OR base.qualified_name = base_name
            MERGE (derived)-[:INHERITS_FROM]->(base)
            """
        )
        print("  Relationships: INHERITS_FROM")

    def _write_call_relationships(self, session):
        """Create CALLS relationships between Members."""
        if not self.calls:
            print("  Calls: 0")
            return
        batch_size = 1000
        created = 0
        for i in range(0, len(self.calls), batch_size):
            batch = self.calls[i:i + batch_size]
            result = session.run(
                """
                UNWIND $batch AS row
                MATCH (caller:Member {refid: row.from_refid})
                MATCH (callee:Member {refid: row.to_refid})
                MERGE (caller)-[:CALLS]->(callee)
                RETURN count(*) AS cnt
                """,
                batch=batch,
            )
            created += result.single()["cnt"]
        print(f"  Calls: {created} (of {len(self.calls)} references)")

    def _write_metadata(self, session, metadata: dict):
        """Write metadata as a single Metadata node."""
        session.run(
            """
            CREATE (md:Metadata $props)
            """,
            props=metadata,
        )


# ---------------------------------------------------------------------------
# XML parsing — walks Doxygen XML and feeds the batch writer
# ---------------------------------------------------------------------------

def parse_compound_file(writer: Neo4jBatchWriter, xml_path: Path) -> None:
    """Parse a compound (class/struct/file) XML file."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"Warning: Could not parse {xml_path}: {e}", file=sys.stderr)
        return

    for compounddef in root.findall(".//compounddef"):
        refid = compounddef.get("id", "")
        kind = compounddef.get("kind", "")
        language = compounddef.get("language", "")
        compoundname = compounddef.findtext("compoundname", "")

        # --- Files ---
        if kind == "file":
            loc = compounddef.find("location")
            file_path = loc.get("file") if loc is not None else None
            writer.add_file(refid, compoundname, file_path, language)

            # Parse includes
            for inc in compounddef.findall("includes"):
                included_file = inc.text or ""
                included_refid = inc.get("refid")
                is_local = inc.get("local") == "yes"
                writer.add_include(refid, included_file, included_refid, is_local)
            continue

        # --- Namespaces ---
        if kind == "namespace":
            name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
            writer.add_namespace(refid, name, compoundname)
            continue

        # --- Classes, structs, unions ---
        if kind in ("class", "struct", "union"):
            name = compoundname.split("::")[-1] if "::" in compoundname else compoundname

            loc = compounddef.find("location")
            file_path, line_number = parse_location(loc)

            brief = parse_description(compounddef.find("briefdescription"))
            detailed = parse_description(compounddef.find("detaileddescription"))

            base_classes = []
            for baseref in compounddef.findall("basecompoundref"):
                base_classes.append(baseref.text or "")

            is_final = compounddef.get("final") == "yes"
            is_abstract = compounddef.get("abstract") == "yes"

            writer.add_compound(
                refid, kind, name, compoundname, file_path, line_number,
                brief, detailed, base_classes, is_final, is_abstract,
            )

            # Parse members
            for sectiondef in compounddef.findall("sectiondef"):
                for memberdef in sectiondef.findall("memberdef"):
                    parse_member(writer, memberdef, refid)


def parse_member(writer: Neo4jBatchWriter, memberdef: ET.Element,
                 compound_refid: Optional[str]) -> None:
    """Parse a member definition element."""
    refid = memberdef.get("id", "")
    kind = memberdef.get("kind", "")
    prot = memberdef.get("prot", "public")

    name = memberdef.findtext("name", "")
    qualified_name = memberdef.findtext("qualifiedname", name)
    type_str = get_text(memberdef.find("type"))
    definition = memberdef.findtext("definition", "")
    argsstring = memberdef.findtext("argsstring", "")

    loc = memberdef.find("location")
    file_path, line_number = parse_location(loc)

    brief = parse_description(memberdef.find("briefdescription"))
    detailed = parse_description(memberdef.find("detaileddescription"))

    is_static = memberdef.get("static") == "yes"
    is_const = memberdef.get("const") == "yes"
    is_constexpr = memberdef.get("constexpr") == "yes"
    is_virtual = memberdef.get("virt") in ("virtual", "pure-virtual")
    is_inline = memberdef.get("inline") == "yes"
    is_explicit = memberdef.get("explicit") == "yes"

    writer.add_member(
        refid, compound_refid, kind, name, qualified_name,
        type_str, definition, argsstring, file_path, line_number,
        brief, detailed, prot,
        is_static, is_const, is_constexpr, is_virtual, is_inline, is_explicit,
    )

    # Parse parameters
    for i, param in enumerate(memberdef.findall("param")):
        param_name = param.findtext("declname", "")
        param_type = get_text(param.find("type"))
        default_value = param.findtext("defval")
        writer.add_parameter(refid, i, param_name, param_type, default_value)

    # Parse call references
    for ref in memberdef.findall("references"):
        to_refid = ref.get("refid", "")
        to_name = ref.text or ""
        writer.add_call(refid, to_refid, to_name)

    # Parse called-by references
    for ref in memberdef.findall("referencedby"):
        caller_refid = ref.get("refid", "")
        caller_name = ref.text or ""
        writer.add_called_by(refid, caller_refid, caller_name)


def parse_index(index_path: Path) -> list[tuple[str, str]]:
    """Parse the index.xml file to get list of all compounds."""
    compounds = []
    try:
        tree = ET.parse(index_path)
        root = tree.getroot()
        for compound in root.findall("compound"):
            refid = compound.get("refid", "")
            kind = compound.get("kind", "")
            compounds.append((refid, kind))
    except ET.ParseError as e:
        print(f"Warning: Could not parse index.xml: {e}", file=sys.stderr)
    return compounds


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest Doxygen XML into Neo4j graph database"
    )
    parser.add_argument("xml_dir", help="Directory containing Doxygen XML output")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                        help="Neo4j Bolt URI (default: bolt://localhost:7687)")
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"),
                        help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "msd-local-dev"),
                        help="Neo4j password (default: msd-local-dev)")
    parser.add_argument("--database", default="neo4j",
                        help="Neo4j database name (default: neo4j)")
    parser.add_argument("--project-root", default=None,
                        help="Project root path for metadata")
    parser.add_argument("--no-clear", action="store_true",
                        help="Don't clear existing codebase data before ingesting")
    parser.add_argument("--source", default="msd",
                        help="Source label for provenance tracking (default: msd)")
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.is_dir():
        print(f"Error: XML directory not found: {xml_dir}", file=sys.stderr)
        sys.exit(1)

    index_path = xml_dir / "index.xml"
    if not index_path.exists():
        print(f"Error: index.xml not found in {xml_dir}", file=sys.stderr)
        sys.exit(1)

    # Connect to Neo4j
    print(f"Connecting to Neo4j at {args.uri}...")
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        driver.verify_connectivity()
    except Exception as e:
        print(f"Error: Could not connect to Neo4j: {e}", file=sys.stderr)
        print("Is Neo4j running? Try: docker compose up -d", file=sys.stderr)
        sys.exit(1)

    writer = Neo4jBatchWriter(driver, database=args.database, source=args.source)

    # Set up schema
    print("Ensuring schema (constraints + indexes)...")
    writer.ensure_schema()

    # Clear existing data
    if not args.no_clear:
        writer.clear_codebase_data()

    # Parse index
    print("Parsing index.xml...")
    compounds = parse_index(index_path)
    print(f"Found {len(compounds)} compounds")

    # Parse all compound XML files
    print("Parsing compound files...")
    for i, (refid, kind) in enumerate(compounds):
        xml_file = xml_dir / f"{refid}.xml"
        if xml_file.exists():
            parse_compound_file(writer, xml_file)

        if (i + 1) % 50 == 0:
            print(f"  Parsed {i + 1}/{len(compounds)} XML files...")

    # Flush all data to Neo4j
    print("\nWriting to Neo4j...")
    metadata = {
        "xml_dir": str(xml_dir.absolute()),
        "source": "doxygen_to_neo4j",
    }
    if args.project_root:
        metadata["project_root"] = args.project_root

    writer.flush(metadata=metadata)

    # Print summary
    print("\nIngestion complete.")
    with driver.session(database=args.database) as session:
        result = session.run(
            """
            MATCH (n)
            WITH labels(n)[0] AS label
            RETURN label, count(*) AS cnt
            ORDER BY cnt DESC
            """
        )
        print("Node counts:")
        for record in result:
            print(f"  {record['label']}: {record['cnt']}")

        result = session.run(
            """
            MATCH ()-[r]->()
            WITH type(r) AS rel_type
            RETURN rel_type, count(*) AS cnt
            ORDER BY cnt DESC
            """
        )
        print("Relationship counts:")
        for record in result:
            print(f"  {record['rel_type']}: {record['cnt']}")

    driver.close()


if __name__ == "__main__":
    main()
