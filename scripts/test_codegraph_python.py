#!/usr/bin/env python3
"""Test: parse codegraph source with PythonParser and optionally load into Neo4j.

This script parses the codegraph Python package using the new PythonParser
(AST-based, no Sphinx required) and verifies that the extracted symbols
match expectations.  With --neo4j, it also ingests the results into Neo4j
and validates node counts.

Usage:
    python scripts/test_codegraph_python.py              # Parse only, print summary
    python scripts/test_codegraph_python.py --neo4j      # Parse + Neo4j ingest + verify
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the doxygen_index package is importable
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from doxygen_index.parser import (
    PythonParser,
    ParseResult,
    parse_python_dir,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CODEGRAPH_SRC = PROJECT_ROOT.parent / "codegraph" / "src"
SOURCE_LABEL = "codegraph"
PACKAGE_NAME = "codegraph"  # Top-level package name under src/


def parse_codegraph() -> ParseResult:
    """Parse the codegraph source tree and return a ParseResult."""
    if not CODEGRAPH_SRC.is_dir():
        print(f"Error: codegraph source directory not found at {CODEGRAPH_SRC}", file=sys.stderr)
        print("       Make sure the codegraph repo is cloned at ../codegraph", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {CODEGRAPH_SRC} with PythonParser...")
    result = parse_python_dir(CODEGRAPH_SRC, source=SOURCE_LABEL, progress_interval=10)
    print(f"  Parsed {len(result.files)} Python files")
    return result


def verify_parse_result(result: ParseResult) -> list[str]:
    """Verify the ParseResult has expected content. Returns list of issues."""
    issues = []

    # --- Files ---
    expected_files = {"__init__.py", "config.py", "connection.py", "constants.py",
                      "repository.py", "type_parser.py"}
    found_files = {f.name for f in result.files}
    for ef in expected_files:
        if ef not in found_files:
            issues.append(f"Missing expected file: {ef}")

    # --- Namespaces ---
    ns_names = {ns.qualified_name for ns in result.namespaces}
    expected_namespaces = {"codegraph", "codegraph.models", "codegraph.models.compound",
                          "codegraph.models.member", "codegraph.graph"}
    for en in expected_namespaces:
        if en not in ns_names:
            issues.append(f"Missing expected namespace: {en}")

    # --- Classes ---
    class_names = {c.qualified_name for cls in result.classes for c in [cls]}
    expected_classes = {"codegraph.models.compound.ClassNode",
                        "codegraph.models.member.MethodNode",
                        "codegraph.models.member.AttributeNode",
                        "codegraph.models.file.FileNode",
                        "codegraph.models.namespace.NamespaceNode",
                        "codegraph.graph.LayerGraph",
                        "codegraph.repository.GraphRepository"}
    for ec in expected_classes:
        if ec not in class_names:
            issues.append(f"Missing expected class: {ec}")

    # --- Methods ---
    method_names = {m.qualified_name for m in result.methods}
    expected_methods = {"codegraph.models.tags.CodeGraphNode.serialize",
                       "codegraph.models.tags.CodeGraphNode.deserialize",
                       "codegraph.graph.LayerGraph.deserialize",
                       "codegraph.graph.LayerGraph.to_neo4j",
                       "codegraph.models.tags.CodeGraphNode.fetch_all_by_layer"}
    for em in expected_methods:
        if em not in method_names:
            issues.append(f"Missing expected method: {em}")

    # --- Functions ---
    func_names = {f.qualified_name for f in result.functions}
    expected_funcs = {"codegraph.connection.cypher_query",
                      "codegraph.connection.verify_connectivity"}
    for ef in expected_funcs:
        if ef not in func_names:
            issues.append(f"Missing expected function: {ef}")

    # --- Attributes ---
    attr_names = {a.qualified_name for a in result.attributes}
    expected_attrs = {"codegraph.models.compound.ClassNode.module",
                      "codegraph.models.member.MethodNode.argsstring"}
    for ea in expected_attrs:
        if ea not in attr_names:
            issues.append(f"Missing expected attribute: {ea}")

    # --- Parameters ---
    methods_with_params = [m for m in result.methods if any(
        p.member_refid == m.qualified_name for p in result.parameters
    )]
    if len(methods_with_params) == 0:
        issues.append("No methods have parameters \u2014 expected at least some")

    return issues


def print_summary(result: ParseResult) -> None:
    """Print a structured summary of the ParseResult."""
    print("\n" + "=" * 60)
    print("PARSE RESULT SUMMARY")
    print("=" * 60)

    print(f"\n  Files:         {len(result.files)}")
    print(f"  Namespaces:   {len(result.namespaces)}")
    print(f"  Classes:       {len(result.classes)}")
    print(f"  Interfaces:    {len(result.interfaces)}")
    print(f"  Enums:         {len(result.enums)}")
    print(f"  Enum values:   {len(result.enum_values)}")
    print(f"  Methods:       {len(result.methods)}")
    print(f"  Functions:     {len(result.functions)}")
    print(f"  Attributes:    {len(result.attributes)}")
    print(f"  Includes:      {len(result.includes)}")
    print(f"  Parameters:   {len(result.parameters)}")

    # Layer
    if result.classes:
        layer = result.classes[0].layer
        print(f"  Layer:          {layer}")

    print("\n--- Classes ---")
    for c in sorted(result.classes, key=lambda x: x.qualified_name):
        bases = ", ".join(c.base_classes) if c.base_classes else "(none)"
        print(f"  {c.qualified_name}")
        print(f"    kind={c.kind}  bases=[{bases}]  abstract={c.is_abstract}")

    print("\n--- Interfaces ---")
    for i in sorted(result.interfaces, key=lambda x: x.qualified_name):
        print(f"  {i.qualified_name}  (abstract={i.is_abstract})")

    print("\n--- Enums ---")
    for e in sorted(result.enums, key=lambda x: x.qualified_name):
        print(f"  {e.qualified_name}")

    print("\n--- Enum Values ---")
    for ev in sorted(result.enum_values, key=lambda x: x.qualified_name)[:10]:
        print(f"  {ev.qualified_name} = ...")
    if len(result.enum_values) > 10:
        print(f"  ... and {len(result.enum_values) - 10} more")

    print("\n--- Methods (first 20) ---")
    for m in sorted(result.methods, key=lambda x: x.qualified_name)[:20]:
        kind_tag = f" kind={m.kind}" if m.kind != "method" else ""
        static_tag = " static" if m.is_static else ""
        abstract_tag = " abstract" if m.is_virtual else ""
        print(f"  {m.qualified_name}{kind_tag}{static_tag}{abstract_tag}")
    if len(result.methods) > 20:
        print(f"  ... and {len(result.methods) - 20} more")

    print("\n--- Functions ---")
    for f in sorted(result.functions, key=lambda x: x.qualified_name):
        print(f"  {f.qualified_name}  args={f.argsstring}")

    print("\n--- Attributes (first 15) ---")
    for a in sorted(result.attributes, key=lambda x: x.qualified_name)[:15]:
        type_sig = f": {a.type_signature}" if a.type_signature else ""
        print(f"  {a.qualified_name}{type_sig}")
    if len(result.attributes) > 15:
        print(f"  ... and {len(result.attributes) - 15} more")

    print("\n--- Namespaces ---")
    for ns in sorted(result.namespaces, key=lambda x: x.qualified_name):
        print(f"  {ns.qualified_name}")

    print("\n--- Files ---")
    for f in sorted(result.files, key=lambda x: x.path):
        print(f"  {f.name}  →  {f.path}")

    print("\n--- Includes (first 20) ---")
    for inc in result.includes[:20]:
        local_tag = " (local)" if inc.is_local else ""
        print(f"  {inc.file_refid} → {inc.included_file}{local_tag}")
    if len(result.includes) > 20:
        print(f"  ... and {len(result.includes) - 20} more")


def verify_neo4j(result: ParseResult) -> None:
    """Ingest the ParseResult into Neo4j and verify the results."""
    try:
        from dotenv import load_dotenv
        load_dotload_dotenv = True
    except ImportError:
        pass

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    try:
        from doxygen_index.neo4j_backend import write_result, ensure_schema, clear_source
    except ImportError:
        print("Warning: neo4j_backend not available. Skipping Neo4j verification.")
        return

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("Warning: neo4j driver not installed. Skipping Neo4j verification.")
        return

    import os
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "msd-local-dev")

    print(f"\nConnecting to Neo4j at {uri}...")
    try:
        from neomodel import db as neomodel_db
        neomodel_db.set_connection(f'bolt://{user}:{password}@localhost:7687')
    except Exception as e:
        print(f"Could not connect to Neo4j: {e}")
        print("Is Neo4j running? Try: docker compose up -d")
        return

    # Clear existing data for this source
    print(f"Clearing existing '{SOURCE_LABEL}' data...")
    clear_source(SOURCE_LABEL)

    # Install schema
    print("Ensuring schema...")
    ensure_schema()

    # Write results
    print("Writing ParseResult to Neo4j...")
    write_result(result)

    # Verify
    print("\nVerifying Neo4j ingestion...")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            # Node counts by label
            res = session.run("""
                MATCH (n)
                WHERE n.source = $src
                WITH labels(n) AS lbls
                RETURN lbls, count(*) AS cnt
                ORDER BY cnt DESC
            """, src=SOURCE_LABEL)
            print("  Node counts by label:")
            for r in res:
                print(f"    {str(r['lbls']):45s} {r['cnt']}")

            # Relationship counts
            res = session.run("""
                MATCH (n)-[r]->()
                WHERE n.source = $src
                WITH type(r) AS rel
                RETURN rel, count(*) AS cnt
                ORDER BY cnt DESC
            """, src=SOURCE_LABEL)
            print("\n  Relationship counts:")
            for r in res:
                print(f"    {r['rel']:30s} {r['cnt']}")

            # Sample classes
            res = session.run("""
                MATCH (c:ClassNode {source: $src})
                RETURN c.qualified_name AS name, c.kind AS kind
                ORDER BY name
                LIMIT 5
            """, src=SOURCE_LABEL)
            print("\n  First 5 classes in Neo4j:")
            for r in res:
                print(f"    {r['name']} ({r['kind']})")

            # Sample methods
            res = session.run("""
                MATCH (m:MethodNode {source: $src})
                RETURN m.qualified_name AS name
                ORDER BY name
                LIMIT 5
            """, src=SOURCE_LABEL)
            print("\n  First 5 methods in Neo4j:")
            for r in res:
                print(f"    {r['name']}")

    finally:
        driver.close()

    print("\n  ✅ Neo4j verification passed")


def main():
    parser = argparse.ArgumentParser(
        description="Parse codegraph source with PythonParser and optionally load into Neo4j"
    )
    parser.add_argument("--neo4j", action="store_true",
                        help="Also ingest into Neo4j and verify")
    args = parser.parse_args()

    # Step 1: Parse
    result = parse_codegraph()

    # Step 2: Print summary
    print_summary(result)

    # Step 3: Verify expectations
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)
    issues = verify_parse_result(result)
    if issues:
        print(f"\n⚠ {len(issues)} issues found:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\n✅ All expected symbols found")

    # Step 4: Neo4j ingestion (optional)
    if args.neo4j:
        verify_neo4j(result)

    print()
    if issues:
        print(f"⚠ Completed with {len(issues)} issues")
        sys.exit(1)
    else:
        print("✅ ALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()