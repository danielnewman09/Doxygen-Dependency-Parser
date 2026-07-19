"""
Full dependency pipeline — discover, generate Doxygen XML, parse, and export CSV.

Provides the all-in-one workflow for indexing Conan dependencies into
Neo4j-import-compatible CSV files that can be loaded at container startup
via ``neo4j-admin database import full``.

Typical usage::

    from doxygen_index.pipeline import process_dependencies_csv

    csv_dirs = process_dependencies_csv(
        project_dir=".",
        output_dir="build/docs/deps",
        only={"boost", "sqlite3"},
    )
    # csv_dirs["boost"] = Path("build/docs/deps/boost_csv")
    # csv_dirs["sqlite3"] = Path("build/docs/deps/sqlite3_csv")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from doxygen_index.conan import discover_packages
from doxygen_index.doxygen import generate_xml
from doxygen_index.parser import parse_xml_dir, ParseResult
from doxygen_index.csv_export import export_csv


def process_dependencies_csv(
    project_dir: Path | str = ".",
    output_dir: Path | str = "build/docs/deps",
    build_type: str = "Debug",
    only: Optional[set[str]] = None,
    overrides: Optional[dict[str, dict]] = None,
) -> dict[str, Path]:
    """Discover Conan dependencies, generate Doxygen XML, parse, and export CSV.

    This is the main entry point for offline CSV export.  The resulting
    CSV files can be loaded into Neo4j at container startup via
    ``neo4j-admin database import full``.

    Steps:
        1. ``discover_packages`` — find include paths via Conan metadata.
        2. ``generate_xml`` — run Doxygen on each dependency.
        3. ``parse_xml_dir`` — parse Doxygen XML into a :class:`ParseResult`.
        4. ``export_csv`` — write ``nodes.csv`` and ``relationships.csv``.

    Args:
        project_dir: Project root containing conanfile.py.
        output_dir: Base directory.  Each dependency gets a ``<name>_csv/`` subdir.
        build_type: Conan build type (default: ``"Debug"``).
        only: If provided, only process these dependency names.
        overrides: Optional per-dependency overrides (e.g.
            ``{"eigen": {"predefined": "EIGEN_PARSED_BY_DOXYGEN"}}``).

    Returns:
        Mapping of dependency name to its CSV output directory.
    """
    project_dir = Path(project_dir).resolve()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Discover ────────────────────────────────────────────
    packages = discover_packages(
        project_dir=project_dir,
        build_type=build_type,
        only=only,
    )
    if not packages:
        print("\nNo packages found. Run 'conan install . --build=missing' first.",
              file=sys.stderr)
        return {}

    # ── Phase 2: Generate XML ────────────────────────────────────────
    print(f"\n--- Generating Doxygen XML for {len(packages)} dependencies ---")
    xml_dirs = generate_xml(packages, output_dir=output_dir, overrides=overrides)
    if not xml_dirs:
        print("No XML generated.", file=sys.stderr)
        return {}

    # ── Phase 3: Parse + Export CSV ──────────────────────────────────
    csv_dirs: dict[str, Path] = {}
    print(f"\n--- Parsing XML and exporting CSV for {len(xml_dirs)} dependencies ---")
    for dep_name in sorted(xml_dirs.keys()):
        xml_dir = xml_dirs[dep_name]
        csv_dir = output_dir / f"{dep_name}_csv"

        print(f"\n  {dep_name}: parsing {xml_dir} ...")
        result = parse_xml_dir(xml_dir, source=dep_name, layer="dependency")

        export_csv(result, source=dep_name, output_dir=csv_dir)
        csv_dirs[dep_name] = csv_dir

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"CSV export complete: {len(csv_dirs)} dependencies")
    for name, d in sorted(csv_dirs.items()):
        nodes_file = d / "nodes.csv"
        rels_file = d / "relationships.csv"
        nodes_rows = _count_csv_rows(nodes_file)
        rels_rows = _count_csv_rows(rels_file)
        print(f"  {name}/")
        print(f"    nodes.csv: {nodes_rows} rows")
        print(f"    relationships.csv: {rels_rows} rows")

    return csv_dirs


def process_cppreference_csv(
    cache_dir: Path | str = "~/.cache/doxygen-index/cppreference",
    output_dir: Path | str = "cppreference_csv",
    archive_url: str | None = None,
    force: bool = False,
) -> Path:
    """Download cppreference, parse HTML, and export as CSV.

    This is the same logic as ``cmd_cppreference --csv`` but available
    as a standalone function for scripting.

    Args:
        cache_dir: Directory to cache the downloaded archive.
        output_dir: Output directory for CSV files.
        archive_url: Override the cppreference archive URL.
        force: Re-download even if cached.

    Returns:
        Path to the CSV output directory.
    """
    from doxygen_index.cppreference import download, parse

    cache_dir = Path(cache_dir).expanduser()
    archive_root = download(cache_dir, url=archive_url, force=force)
    result = parse(archive_root)

    output_dir = Path(output_dir)
    export_csv(result, source="cppreference", output_dir=output_dir)
    return output_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_csv_rows(path: Path) -> int:
    """Count rows in a CSV file (excluding header)."""
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f) - 1  # subtract header
