#!/usr/bin/env python3
"""Parse codegraph Python sources into codegraph LayerGraph JSON.

Pipeline:
    1. Parse Python source with PythonParser → ParseResult
    2. Persist to Neo4j via neo4j_backend.write_result()
    3. Query back via LayerGraph.from_neo4j() (edges now populated)
    4. Serialize to JSON via LayerGraph.serialize()

Requires a running Neo4j instance.

Usage:
    python scripts/parse_codegraph.py
    python scripts/parse_codegraph.py --output-dir /tmp
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from parse_ticketing import connect_neo4j, parse_to_layergraph


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse codegraph Python sources to LayerGraph JSON"
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "build" / "codegraph_parse"),
        help="Output directory for JSON files",
    )
    parser.add_argument(
        "--codegraph-dir",
        default=str(PROJECT_ROOT.parent / "codegraph" / "src"),
        help="Path to the codegraph src directory",
    )
    parser.add_argument(
        "--tag", default="as-built",
        help="Tag for LayerGraph query (default: as-built)",
    )
    args = parser.parse_args()

    src_dir = Path(args.codegraph_dir)
    output_dir = Path(args.output_dir)

    if not src_dir.is_dir():
        print(f"Error: codegraph source not found at {src_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    connect_neo4j()

    print(f"\n{'=' * 60}")
    print(f"Parsing codegraph/  →  {src_dir}")
    print(f"{'=' * 60}")

    graph = parse_to_layergraph(
        src_dir, source="codegraph", tag=args.tag, clear=True,
    )

    serialized = graph.serialize(fields="all")
    json_path = output_dir / "codegraph_layergraph.json"
    json_path.write_text(json.dumps(serialized, indent=2, default=str))
    print(f"\n  → Wrote {json_path}")


if __name__ == "__main__":
    main()