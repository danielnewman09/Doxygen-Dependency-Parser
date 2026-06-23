"""
Neo4j backend — ingests ParseResult into a Neo4j graph database.

Uses neomodel for node persistence (replaces raw Cypher MERGE with .save()).
"""

from __future__ import annotations

import os
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

from doxygen_index.parser import ParseResult, parse_xml_dir, TemplateParamRef, SpecializesRef, ImplementationRef


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
# Node writer — uses neomodel .save()
# ---------------------------------------------------------------------------

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
    ]
    batch_labels = [
        "Files", "Namespaces", "Classes", "Enums", "Unions",
        "Interfaces", "Concepts", "Methods", "Attributes", "EnumValues",
        "Defines", "Functions", "Implementations",
    ]
    # Persist nodes, replacing result lists in-place with saved instances
    # so element_id is set for subsequent .connect() calls.
    # create_or_update() returns a list; we unwrap the first element.
    for i, node_list in enumerate(batch_refs):
        if node_list:
            saved = []
            for node in node_list:
                result_nodes = node.__class__.create_or_update(node.__properties__)
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
                      result.defines, result.functions]:
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
        layer: Layer label ("codebase" for project code, "dependency" for deps).
    """
    connect_neo4j(uri=uri, user=user, password=password, database=database)

    xml_dir = Path(xml_dir)

    ensure_schema()

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
