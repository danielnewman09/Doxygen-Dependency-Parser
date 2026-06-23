#!/usr/bin/env python3
"""Parse ticketing_system Python sources into codegraph LayerGraph JSON.

Pipeline:
    1. Parse Python source with PythonParser → ParseResult
    2. Persist to Neo4j via neo4j_backend.write_result()
    3. Query back via LayerGraph.from_neo4j() (edges now populated)
    4. Serialize to JSON via LayerGraph.serialize()

Requires a running Neo4j instance.

Usage:
    python scripts/parse_ticketing.py
    python scripts/parse_ticketing.py --output-dir /tmp
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from doxygen_index.parser import parse_python_dir
from doxygen_index.neo4j_backend import write_result, ensure_schema, clear_source
from codegraph.graph import LayerGraph


# ══════════════════════════════════════════════════════════════════════════
# Neo4j connection
# ══════════════════════════════════════════════════════════════════════════

def connect_neo4j() -> None:
    """Connect to Neo4j using environment variables or defaults."""
    from neomodel import db

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "msd-local-dev")

    host = uri.replace("bolt://", "")
    db.set_connection(f"bolt://{user}:{password}@{host}")
    print(f"Connected to Neo4j at {uri}")


# ══════════════════════════════════════════════════════════════════════════
# Core: parse → persist → query → serialize
# ══════════════════════════════════════════════════════════════════════════

def parse_to_layergraph(
    src_dir: Path,
    source: str,
    tag: str = "as-built",
    clear: bool = True,
) -> LayerGraph:
    """Parse a Python source directory and return a LayerGraph.

    1. Parse with PythonParser → ParseResult
    2. Persist to Neo4j via write_result()
    3. Query back via LayerGraph.from_neo4j()
    """
    # 1. Parse
    print(f"\n  Parsing {src_dir}...")
    result = parse_python_dir(
        src_dir, source=source, progress_interval=20, layer="codebase",
    )

    print(f"  Files:        {len(result.files)}")
    print(f"  Namespaces:   {len(result.namespaces)}")
    print(f"  Classes:      {len(result.classes)}")
    print(f"  Interfaces:   {len(result.interfaces)}")
    print(f"  Enums:        {len(result.enums)}")
    print(f"  Methods:      {len(result.methods)}")
    print(f"  Functions:    {len(result.functions)}")
    print(f"  Attributes:   {len(result.attributes)}")
    print(f"  Includes:     {len(result.includes)}")
    print(f"  Parameters:   {len(result.parameters)}")

    # 2. Set tags on all nodes so from_neo4j can query them back
    for node_list in [result.files, result.namespaces, result.classes,
                       result.enums, result.unions, result.interfaces,
                       result.concepts, result.methods, result.attributes,
                       result.enum_values, result.defines, result.functions,
                       result.implementations]:
        for node in node_list:
            if hasattr(node, "tags"):
                node.tags = [tag]
    for p in result.parameters:
        pass  # ParameterNode has no tags property

    # 3. Ensure schema
    print(f"\n  Ensuring schema...")
    ensure_schema()

    # 4. Clear existing data for this source
    if clear:
        print(f"  Clearing existing '{source}' data...")
        clear_source(source)

    # 5. Persist
    print(f"  Persisting to Neo4j...")
    write_result(result)

    # 6. Query back as LayerGraph
    print(f"  Querying LayerGraph from Neo4j (tag='{tag}')...")
    graph = LayerGraph.from_neo4j(tag)

    # Count
    node_count = sum(1 for _ in graph._all_entries())
    composes_count = sum(
        sum(len(tc) for tc in entry.children.values())
        for entry in graph._all_entries()
    )
    ref_count = sum(len(entry.references) for entry in graph._all_entries())
    print(f"  LayerGraph: {node_count} nodes, {composes_count} COMPOSES, {ref_count} refs")

    return graph


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse ticketing_system Python sources to LayerGraph JSON"
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "build" / "ticketing_parse"),
        help="Output directory for JSON files",
    )
    parser.add_argument(
        "--ticketing-dir",
        default=str(PROJECT_ROOT.parent / "ticketing_system"),
        help="Path to the ticketing_system project root",
    )
    parser.add_argument(
        "--tag", default="as-built",
        help="Tag for LayerGraph query (default: as-built)",
    )
    args = parser.parse_args()

    ticketing_dir = Path(args.ticketing_dir)
    output_dir = Path(args.output_dir)

    targets = {
        "backend_migrated": ticketing_dir / "backend_migrated",
        "frontend_migrated": ticketing_dir / "frontend_migrated",
    }

    for name, path in targets.items():
        if not path.is_dir():
            print(f"Error: {name} not found at {path}", file=sys.stderr)
            sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    connect_neo4j()

    for name, src_dir in targets.items():
        source_label = f"ticketing_system/{name}"

        print(f"\n{'=' * 60}")
        print(f"Parsing {name}/  →  {src_dir}")
        print(f"{'=' * 60}")

        graph = parse_to_layergraph(
            src_dir, source=source_label, tag=args.tag, clear=True,
        )

        # Serialize
        serialized = graph.serialize(fields="all")
        json_path = output_dir / f"{name}_layergraph.json"
        json_path.write_text(json.dumps(serialized, indent=2, default=str))
        print(f"\n  → Wrote {json_path}")

    print(f"\n{'=' * 60}")
    print("Done. Output in:", output_dir)


if __name__ == "__main__":
    main()