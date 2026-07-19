"""
CLI entry point for doxygen-index.

Usage::

    doxygen-index project <project-dir>
    doxygen-index project            # uses .doxygen-index.toml in current dir
    doxygen-index                    # same as above (auto-detects config)
    doxygen-index discover [--build-type Debug] [--project-dir .]
    doxygen-index generate --output-dir build/docs/deps [--only eigen,sdl]
    doxygen-index ingest --output-dir build/docs/deps --neo4j
    doxygen-index full --output-dir build/docs/deps --neo4j
    doxygen-index list-deps

The ``project`` command reads a ``.doxygen-index.toml`` config file from
the project directory.  When no subcommand is given and a config file
exists in the current directory, ``project`` is assumed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


CONFIG_FILENAME = ".doxygen-index.toml"


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared across subcommands."""
    parser.add_argument("--project-dir", default=".",
                        help="Project root containing conanfile.py (default: .)")
    parser.add_argument("--build-type", default="Debug",
                        help="Conan build type to match packages (default: Debug)")
    parser.add_argument("--only", default=None,
                        help="Comma-separated list of deps to process")


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    """Add output directory argument."""
    parser.add_argument("--output-dir", required=True,
                        help="Base directory for Doxygen XML output")


def _add_db_args(parser: argparse.ArgumentParser) -> None:
    """Add database and CSV target arguments."""
    parser.add_argument("--neo4j", action="store_true",
                        help="Ingest into Neo4j graph database")
    parser.add_argument("--neo4j-uri",
                        default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                        help="Neo4j Bolt URI")
    parser.add_argument("--neo4j-user",
                        default=os.environ.get("NEO4J_USER", "neo4j"),
                        help="Neo4j username")
    parser.add_argument("--neo4j-password",
                        default=os.environ.get("NEO4J_PASSWORD", "msd-local-dev"),
                        help="Neo4j password")
    parser.add_argument("--csv", action="store_true",
                        help="Export to CSV files for neo4j-admin import")
    parser.add_argument("--csv-dir", default=None,
                        help="Output directory for CSV files (default: <output-dir>/csv)")


def _confirm_destructive(label: str, yes: bool) -> None:
    """Prompt for confirmation before a destructive operation.

    Args:
        label: Human-readable description of what will be destroyed.
        yes: If True, skip the prompt (--yes flag was passed).

    Raises:
        SystemExit: If the user declines.
    """
    if yes:
        return
    print(f"\n  ⚠  This will DELETE ALL existing graph data for '{label}'.")
    response = input("  Proceed? [y/N]: ").strip().lower()
    if response not in ("y", "yes"):
        print("  Aborted.")
        sys.exit(1)


def _parse_only(only_str: str | None) -> set[str] | None:
    if only_str is None:
        return None
    return set(only_str.split(","))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_project(args: argparse.Namespace) -> None:
    """Parse an arbitrary project's source code.

    Loads .doxygen-index.toml from the project directory, generates
    Doxygen XML (C++) or parses Python source directly (via ``ast``),
    and outputs results (JSON by default).
    """
    from doxygen_index.project import load_config, ProjectConfig
    from doxygen_index.parser import parse_xml_dir, parse_python_dir, ParseResult
    from doxygen_index.json_backend import write_result as json_write

    project_dir = Path(args.project_dir).resolve()

    # Load config
    config, config_dir = load_config(project_dir)
    print(f"Project: {config.name}")
    print(f"Language: {config.language}")
    print(f"Input paths: {', '.join(str(p) for p in config.input_paths)}")

    # Determine output directory: CLI flag > config file > default
    if args.output_dir:
        output_dir = Path(args.output_dir)
    elif config.output_dir:
        output_dir = config.output_dir
    else:
        output_dir = config_dir / "build" / "docs" / f"doxygen-{config.name}"
    print(f"Output dir: {output_dir}")

    # ------------------------------------------------------------------
    # Branch on language
    # ------------------------------------------------------------------

    xml_dir: Path | None = None  # only set for C++

    # --neo4j is a shorthand for --format neo4j
    if getattr(args, 'neo4j', False):
        args.format = "neo4j"

    if config.language == "python":
        result = _parse_python_project(args, config)
    elif config.language == "cpp":
        result, xml_dir = _parse_cpp_project(args, config, output_dir)
    else:
        print(f"Error: unsupported language '{config.language}' "
              f"(use 'cpp' or 'python')", file=sys.stderr)
        sys.exit(1)

    # --generate-only exits early (C++ only)
    if args.generate_only:
        return

    # ------------------------------------------------------------------
    # LLM enrichment (optional — requires LLM_API_KEY)
    # ------------------------------------------------------------------
    if args.enrich:
        from doxygen_index.enrich import (
            enrich_result, enrichment_available, check_enrichment_config,
        )
        try:
            check_enrichment_config()
        except RuntimeError as exc:
            print(f"Warning: enrichment skipped — {exc}", file=sys.stderr)
        else:
            print("\n--- LLM enrichment ---")
            if args.dry_run_enrich:
                print("(dry-run mode — prompts built, no LLM calls)")
            else:
                from doxygen_index.enrich import preflight_check
                try:
                    preflight_check()
                except RuntimeError as exc:
                    print(f"Error: enrichment aborted — {exc}", file=sys.stderr)
                    return
            summary = enrich_result(
                result,
                dry_run=args.dry_run_enrich,
                overwrite=args.overwrite_enrich,
                log_dir=output_dir / "logs" if not args.dry_run_enrich else None,
                batch_size=args.enrich_batch_size,
            )
            print(f"  Enriched: {summary.total_enriched}")
            print(f"  Skipped:  {summary.total_skipped}")
            if summary.total_errors:
                print(f"  Errors:   {summary.total_errors}")
            if args.output_enrich_summary:
                summary_path = output_dir / f"{config.name}_enrichment_summary.json"
                summary_path.write_text(
                    json.dumps(summary.to_dict(), indent=2),
                    encoding="utf-8",
                )
                print(f"  Summary:  {summary_path}")

    # ------------------------------------------------------------------
    # Reflect enriched descriptions back into test source files
    # ------------------------------------------------------------------
    # Writes the (now possibly enriched) test-node descriptions as
    # ``# codegraph:test-desc <qualified_name>`` comment blocks into
    # the parsed ``.py`` files.  Idempotent: re-running replaces blocks
    # in place.  Works with or without --enrich (e.g. to materialise
    # descriptions read back from Neo4j via --descriptions-from).
    if args.write_test_comments:
        from doxygen_index.parser.python.test_comments import write_test_comments

        override = None
        if args.descriptions_from:
            from pathlib import Path as _P
            try:
                override = json.loads(_P(args.descriptions_from).read_text())
            except Exception as exc:
                print(f"Warning: could not load descriptions file: {exc}",
                      file=sys.stderr)
        print("\n--- Writing test description comments ---")
        report = write_test_comments(
            result,
            dry_run=args.dry_run_test_comments,
            descriptions=override,
            scaffold=getattr(args, "scaffold_test_comments", False),
        )
        print(f"  Files changed:    {len(report.files_changed)}")
        print(f"  Files unchanged:  {len(report.files_unchanged)}")
        print(f"  Nodes written:    {report.nodes_written}")
        if report.nodes_scaffolded:
            print(f"  Slots scaffolded: {report.nodes_scaffolded}")
        print(f"  Skipped (placeholder): {report.nodes_skipped_placeholder}")
        print(f"  Skipped (docstring):   {report.nodes_skipped_docstring}")
        if report.errors:
            print(f"  Errors: {len(report.errors)}")
            for e in report.errors:
                print(f"    - {e}")
        if args.dry_run_test_comments:
            print("  (dry-run — no files written)")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    source = args.source or config.name
    json_path = output_dir / f"{config.name}.json"

    if args.format == "neo4j":
        from doxygen_index.neo4j_backend import (
            connect_neo4j, ensure_schema, clear_source,
            write_result as neo4j_write, update_result as neo4j_update,
        )
        print(f"\n--- {config.name} → Neo4j ---")
        connect_neo4j(
            uri=args.neo4j_uri, user=args.neo4j_user,
            password=args.neo4j_password,
        )
        ensure_schema()
        if args.clear:
            _confirm_destructive(f"source '{source}'", args.yes)
            clear_source(source)
            neo4j_write(result)
        else:
            # Incremental is the default — add new, update changed, delete stale
            neo4j_update(result, source=source)
    else:
        json_write(result, json_path, source=source)
        print(f"Output: {json_path}")

    # ------------------------------------------------------------------
    # HTML graph visualization (if [codegraph-html] is configured)
    # ------------------------------------------------------------------
    if config.html_config and args.format == "json":
        _generate_html(result, config, source)

    # Summary
    print()
    print(f"  Classes:      {len(result.classes)}")
    print(f"  Methods:      {len(result.methods)}")
    print(f"  Functions:    {len(result.functions)}")
    print(f"  Concepts:     {len(result.concepts)}")
    print(f"  Enums:        {len(result.enums)}")
    print(f"  Namespaces:   {len(result.namespaces)}")
    print(f"  Files:        {len(result.files)}")
    print(f"  Includes:     {len(result.includes)}")
    print(f"  Invokes:      {len(result.invokes)}")
    if result.tests:
        print(f"  Tests:        {len(result.tests)}")
        print(f"  Test fixtures:{len(result.test_fixtures)}")
        print(f"  Assertions:   {len(result.assertions)}")
        print(f"  Test steps:   {len(result.test_steps)}")
    if args.format != "neo4j":
        print(f"\nOutput: {json_path}")


def _parse_python_project(
    args: argparse.Namespace,
    config: ProjectConfig,
) -> ParseResult:
    """Parse a Python project using the AST-based parser.

    No external tools required — uses Python's ``ast`` module.
    Source directories from ``config.input_paths`` are always parsed.
    Test directories (from ``--test-paths`` CLI flag or ``test_paths``
    in the config file) are also parsed so that ``test_*`` functions
    and ``Test*`` classes are extracted as :class:`TestNode` instances.
    """
    from doxygen_index.parser import parse_python_dir

    # Parse user exclude_patterns (space-separated globs → dir names)
    user_excludes = []
    if config.exclude_patterns:
        user_excludes = config.exclude_patterns.split()

    # Collect all directories to parse: source + test paths
    all_dirs = list(config.input_paths)

    # Test paths: CLI flag overrides config
    test_paths = None
    if getattr(args, 'test_paths', None):
        test_paths = [Path(p).resolve() for p in args.test_paths]
    elif config.test_paths:
        test_paths = config.test_paths

    if test_paths:
        for tp in test_paths:
            if tp not in all_dirs:
                all_dirs.append(tp)
                print(f"  Including test path: {tp}")

    print(f"\nParsing Python source...")
    result = parse_python_dir(
        all_dirs,
        source=config.name,
        layer="codebase",
        exclude_dirs=user_excludes or None,
    )
    return result


def _parse_cpp_project(
    args: argparse.Namespace,
    config: ProjectConfig,
    output_dir: Path,
) -> tuple[ParseResult, Path]:
    """Parse a C++ project via Doxygen XML generation and parsing.

    Returns:
        Tuple of (ParseResult, xml_dir).  When --generate-only is set,
        the ParseResult is empty and the function signals the caller
        to exit early via args.generate_only.
    """
    from doxygen_index.doxygen import run_doxygen
    from doxygen_index.parser import parse_xml_dir, ParseResult

    # Phase 1: Generate Doxygen XML (unless --parse-only)
    if not args.parse_only:
        print(f"\nFile patterns: {config.file_patterns}")
        if config.exclude_patterns:
            print(f"Exclude: {config.exclude_patterns}")
        if config.predefined:
            print(f"Predefined: {config.predefined}")
        print()

        xml_dir = run_doxygen(
            name=config.name,
            input_paths=config.input_paths,
            output_base=output_dir,
            config=config,
            xml_subdir="xml",
        )
        if xml_dir is None:
            print("Doxygen generation failed.", file=sys.stderr)
            sys.exit(1)
    else:
        xml_dir = Path(args.xml_dir)
        if not xml_dir.exists():
            print(f"Error: XML directory not found: {xml_dir}", file=sys.stderr)
            sys.exit(1)

    # Phase 2: Parse XML (unless --generate-only)
    if args.generate_only:
        print(f"\nXML generated at: {xml_dir}")
        return ParseResult(), xml_dir

    print(f"\nParsing XML...")
    result = parse_xml_dir(xml_dir, source=config.name, layer="codebase")
    return result, xml_dir


def _generate_html(
    result: ParseResult,
    config: ProjectConfig,
    source: str,
) -> None:
    """Generate an interactive HTML graph from a ParseResult.

    Uses codegraph's ``export_html_from_json`` on the backend.  Writes
    both a LayerGraph-compatible JSON and a self-contained HTML file
    to the ``[codegraph-html]`` output directory.
    """
    from doxygen_index.graph_json import write_graph_json
    from codegraph.export.viz import export_html_from_json

    html_cfg = config.html_config
    html_cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write LayerGraph-compatible JSON
    graph_json_path = html_cfg.output_dir / f"{config.name}.json"
    print(f"\n--- Graph visualization ---")
    write_graph_json(result, graph_json_path, source=source)
    print(f"  Graph JSON: {graph_json_path}")

    # 2. Render HTML
    html_path = html_cfg.output_dir / f"{config.name}.html"
    export_html_from_json(
        graph_json_path, html_path,
        title=config.name,
        size=html_cfg.size,
    )
    print(f"  HTML:       {html_path}")


def cmd_html(args: argparse.Namespace) -> None:
    """Generate an interactive HTML graph from existing parse output.

    Reads a LayerGraph-compatible JSON file (produced by ``doxygen-index``
    when ``[codegraph-html]`` is configured) and renders it as a
    self-contained HTML file using codegraph's visualization engine.
    """
    from doxygen_index.project import load_config
    from codegraph.export.viz import export_html_from_json

    project_dir = Path(args.project_dir).resolve()
    config, _ = load_config(project_dir)

    if not config.html_config:
        print("Error: no [codegraph-html] section in .doxygen-index.toml",
              file=sys.stderr)
        sys.exit(1)

    html_cfg = config.html_config
    json_path = html_cfg.output_dir / f"{config.name}.json"

    if not json_path.exists():
        print(f"Error: graph JSON not found: {json_path}", file=sys.stderr)
        print("  Run 'doxygen-index' first to generate the JSON.",
              file=sys.stderr)
        sys.exit(1)

    html_path = html_cfg.output_dir / f"{config.name}.html"
    size = args.size or html_cfg.size

    print(f"Generating HTML from {json_path}...")
    result_path = export_html_from_json(
        json_path, html_path,
        title=config.name,
        size=size,
    )
    print(f"HTML written to {result_path}")


def cmd_list_deps(args: argparse.Namespace) -> None:
    """List all dependencies discovered by Conan."""
    from doxygen_index.conan import discover_packages
    packages = discover_packages(
        project_dir=args.project_dir,
        build_type=args.build_type,
        only=_parse_only(args.only),
    )
    if not packages:
        print("\nNo packages found. Run 'conan install . --build=missing' first.")
        return
    print(f"\nDiscovered {len(packages)} dependencies:")
    for name, path in sorted(packages.items()):
        print(f"  {name}: {path}")


def cmd_discover(args: argparse.Namespace) -> None:
    """Discover Conan dependency include paths."""
    from doxygen_index.conan import discover_packages
    packages = discover_packages(
        project_dir=args.project_dir,
        build_type=args.build_type,
        only=_parse_only(args.only),
    )
    if not packages:
        print("\nNo packages found. Run 'conan install . --build=missing' first.")
        sys.exit(1)
    print(f"\nDiscovered {len(packages)} dependencies.")


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate Doxygen XML for Conan dependencies."""
    from doxygen_index.conan import discover_packages
    from doxygen_index.doxygen import generate_xml

    packages = discover_packages(
        project_dir=args.project_dir,
        build_type=args.build_type,
        only=_parse_only(args.only),
    )
    if not packages:
        print("\nNo packages found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nGenerating Doxygen XML for {len(packages)} dependencies...")
    xml_dirs = generate_xml(packages, output_dir=args.output_dir)

    print(f"\nGenerated XML for {len(xml_dirs)} dependencies:")
    for name, xml_dir in sorted(xml_dirs.items()):
        xml_count = len(list(xml_dir.glob("*.xml")))
        print(f"  {name}: {xml_count} XML files")


def cmd_ingest(args: argparse.Namespace) -> None:
    """Ingest existing Doxygen XML into databases or export to CSV."""
    output_dir = Path(args.output_dir)

    if not args.neo4j and not args.csv:
        print("Error: specify --neo4j or --csv", file=sys.stderr)
        sys.exit(1)

    # Find existing XML dirs
    xml_dirs: dict[str, Path] = {}
    if not output_dir.exists():
        print(f"Error: output directory not found: {output_dir}", file=sys.stderr)
        sys.exit(1)

    only = _parse_only(args.only)
    for subdir in sorted(output_dir.iterdir()):
        if subdir.is_dir() and (subdir / "xml" / "index.xml").exists():
            dep_name = subdir.name
            if only is None or dep_name in only:
                xml_dirs[dep_name] = subdir / "xml"

    if not xml_dirs:
        print("No XML directories found. Run 'doxygen-index generate' first.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(xml_dirs)} dependencies: {', '.join(sorted(xml_dirs))}\n")

    # ── CSV export ───────────────────────────────────────────
    if args.csv:
        from doxygen_index.csv_export import export_csv
        from doxygen_index.parser import parse_xml_dir
        csv_base = Path(args.csv_dir) if args.csv_dir else output_dir / "csv"
        for dep_name, xml_dir in sorted(xml_dirs.items()):
            print(f"--- {dep_name} → CSV ---")
            result = parse_xml_dir(xml_dir, source=dep_name, layer="dependency")
            export_csv(result, source=dep_name, output_dir=csv_base / dep_name)

    # ── Neo4j ingest ─────────────────────────────────────────
    if args.neo4j:
        if args.clear:
            _confirm_destructive("all dependency sources", args.yes)
        for dep_name, xml_dir in sorted(xml_dirs.items()):
            from doxygen_index.neo4j_backend import ingest as neo4j_ingest
            print(f"--- {dep_name} → Neo4j ---")
            neo4j_ingest(
                xml_dir, source=dep_name,
                uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password,
                incremental=not args.clear,
            )
            print()


def cmd_full(args: argparse.Namespace) -> None:
    """Discover, generate, and ingest/export — all in one."""
    from doxygen_index.conan import discover_packages
    from doxygen_index.doxygen import generate_xml

    if not args.neo4j and not args.csv:
        print("Error: specify --neo4j or --csv", file=sys.stderr)
        sys.exit(1)

    # Phase 1: Discover
    packages = discover_packages(
        project_dir=args.project_dir,
        build_type=args.build_type,
        only=_parse_only(args.only),
    )
    if not packages:
        print("\nNo packages found.", file=sys.stderr)
        sys.exit(1)

    # Phase 2: Generate
    print(f"\n--- Generating Doxygen XML for {len(packages)} dependencies ---")
    xml_dirs = generate_xml(packages, output_dir=args.output_dir)

    if not xml_dirs:
        print("No XML generated.", file=sys.stderr)
        sys.exit(1)

    # Phase 3: CSV export
    if args.csv:
        from doxygen_index.csv_export import export_csv
        from doxygen_index.parser import parse_xml_dir
        csv_base = Path(args.csv_dir) if args.csv_dir else Path(args.output_dir) / "csv"
        print(f"\n--- Exporting CSV for {len(xml_dirs)} dependencies ---\n")
        for dep_name, xml_dir in sorted(xml_dirs.items()):
            print(f"--- {dep_name} → CSV ---")
            result = parse_xml_dir(xml_dir, source=dep_name, layer="dependency")
            export_csv(result, source=dep_name, output_dir=csv_base / dep_name)

    # Phase 4: Neo4j ingest
    if args.neo4j:
        if args.clear:
            _confirm_destructive("all dependency sources", args.yes)
        print(f"\n--- Ingesting {len(xml_dirs)} dependencies ---\n")
        for dep_name, xml_dir in sorted(xml_dirs.items()):
            from doxygen_index.neo4j_backend import ingest as neo4j_ingest
            print(f"--- {dep_name} → Neo4j ---")
            neo4j_ingest(
                xml_dir, source=dep_name,
                uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password,
                incremental=not args.clear,
            )
            print()

    # Summary
    print("--- Summary ---")
    print(f"Dependencies processed: {len(xml_dirs)}")
    for name, xml_dir in sorted(xml_dirs.items()):
        xml_count = len(list(xml_dir.glob("*.xml")))
        print(f"  {name}: {xml_count} XML files")
    if args.neo4j:
        print(f"Neo4j: {args.neo4j_uri}")
    if args.csv:
        csv_base = Path(args.csv_dir) if args.csv_dir else Path(args.output_dir) / "csv"
        print(f"CSV: {csv_base}")


def cmd_codegraph(args: argparse.Namespace) -> None:
    """Full codegraph ingestion: unified Doxygen on project + deps → CSV.

    Runs a single Doxygen parse covering the project source AND all
    dependency include directories.  This naturally produces
    cross-references (INVOKES edges) from project code to dependency
    symbols, which become DEPENDS_ON in the graph.
    """
    from doxygen_index.conan import discover_packages
    from doxygen_index.doxygen import run_unified_doxygen
    from doxygen_index.parser import parse_xml_dir
    from doxygen_index.csv_export import export_csv

    project_dir = Path(args.project_dir).resolve()
    csv_base = Path(args.csv_dir) if args.csv_dir else Path(args.output_dir) / "csv"
    csv_base.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Discover Conan deps ──────────────────────────
    dep_include_dirs = discover_packages(
        project_dir=project_dir,
        build_type=args.build_type,
        only=_parse_only(args.only),
    )
    if dep_include_dirs:
        print(f"Discovered {len(dep_include_dirs)} dependencies.")
    else:
        print("No Conan dependencies found (continuing with project-only parse).")

    # ── Phase 2: Find project source dirs ─────────────────────
    project_sources = _find_project_source_dirs(project_dir)
    print(f"Project source dirs: {[str(d) for d in project_sources]}")

    # ── Phase 3: Unified Doxygen run ──────────────────────────
    project_name = project_dir.name
    print(f"\n--- Unified Doxygen: {project_name} + {len(dep_include_dirs)} deps ---")
    xml_dir = run_unified_doxygen(
        project_name=project_name,
        project_source_dirs=project_sources,
        dep_include_dirs=dep_include_dirs,
        output_base=Path(args.output_dir),
    )
    if not xml_dir:
        print("Doxygen failed.", file=sys.stderr)
        sys.exit(1)

    # ── Phase 4: Parse unified XML ────────────────────────────
    print(f"\n--- Parsing unified XML ---")
    result = parse_xml_dir(xml_dir, source=project_name, layer="dependency")

    # ── Phase 5: Tag nodes by source ──────────────────────────
    # Overwrite each node's source attribute based on file location.
    # Project-owned nodes get source=project_name; dep-owned nodes
    # get source=<dep_name>.  This way the single CSV contains all
    # nodes and cross-source INVOKES edges naturally.
    _tag_nodes_by_source(result, project_dir, dep_include_dirs, project_name)
    by_source = _count_by_source(result)
    for src, count in sorted(by_source.items()):
        print(f"  {src}: {count} nodes")

    # ── Phase 6: Export single CSV with everything ────────────
    print(f"\n--- Exporting CSV ---")
    export_csv(result, source=project_name, output_dir=csv_base / project_name)

    # ── Phase 7: cppreference (optional) ──────────────────────
    if args.cppreference:
        from doxygen_index.cppreference import download, parse
        print("\n=== cppreference ===")
        cache_dir = Path(args.cppreference_cache_dir).expanduser()
        archive_root = download(
            cache_dir,
            url=args.cppreference_archive_url,
            force=args.cppreference_force,
        )
        cppref_result = parse(archive_root)
        export_csv(cppref_result, source="cppreference",
                   output_dir=csv_base / "cppreference")

    # ── Summary ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Codegraph CSV export complete.")
    print(f"Output directory: {csv_base}")
    for subdir in sorted(csv_base.iterdir()):
        if subdir.is_dir():
            nodes = subdir / "nodes.csv"
            rels = subdir / "relationships.csv"
            n_rows = sum(1 for _ in open(nodes)) - 1 if nodes.exists() else 0
            r_rows = sum(1 for _ in open(rels)) - 1 if rels.exists() else 0
            print(f"  {subdir.name}/  nodes={n_rows}  rels={r_rows}")
    print()
    print("To load into Neo4j:")
    for subdir in sorted(csv_base.iterdir()):
        if subdir.is_dir():
            nodes = subdir / "nodes.csv"
            rels = subdir / "relationships.csv"
            if nodes.exists():
                print(f"  --nodes={nodes} \\")
            if rels.exists():
                print(f"  --relationships={rels} \\")


def _tag_nodes_by_source(
    result: "ParseResult",
    project_dir: Path,
    dep_include_dirs: dict[str, Path],
    project_source: str,
) -> None:
    """Tag every node in *result* with the correct source label.

    Nodes whose ``file_path`` falls under *project_dir* get
    ``source=project_source``; those under a dep include dir get
    ``source=<dep_name>``.
    """
    project_dir = project_dir.resolve()
    dir_to_dep: dict[Path, str] = {}
    for dep_name, inc_dir in dep_include_dirs.items():
        dir_to_dep[inc_dir.resolve()] = dep_name

    def _classify(file_path: str) -> str:
        if not file_path:
            return project_source
        fp = Path(file_path)
        if fp.is_absolute():
            rp = fp.resolve()
            try:
                rp.relative_to(project_dir)
                return project_source
            except ValueError:
                pass
            for dep_dir, dep_name in dir_to_dep.items():
                try:
                    rp.relative_to(dep_dir)
                    return dep_name
                except ValueError:
                    pass
        return project_source

    all_nodes = (
        result.files + result.namespaces +
        result.classes + result.enums + result.unions +
        result.interfaces + result.concepts +
        result.methods + result.attributes +
        result.enum_values + result.defines +
        result.functions + result.parameters +
        result.implementations
    )
    for node in all_nodes:
        fp = getattr(node, "file_path", "") or ""
        if hasattr(node, "source"):
            node.source = _classify(fp)


def _count_by_source(result: "ParseResult") -> dict[str, int]:
    counts: dict[str, int] = {}
    all_nodes = (
        result.files + result.namespaces +
        result.classes + result.enums + result.unions +
        result.interfaces + result.concepts +
        result.methods + result.attributes +
        result.enum_values + result.defines +
        result.functions + result.parameters +
        result.implementations
    )
    for node in all_nodes:
        src = getattr(node, "source", "unknown") or "unknown"
        counts[src] = counts.get(src, 0) + 1
    return counts


def _find_project_source_dirs(project_dir: Path) -> list[Path]:
    """Heuristically find source/include directories in a C++ project."""
    dirs = []
    for candidate in ["include", "src", "source", "lib"]:
        d = project_dir / candidate
        if d.is_dir():
            # Check it actually has C++ files
            has_headers = any(d.rglob("*.h")) or any(d.rglob("*.hpp"))
            has_source = any(d.rglob("*.cpp")) or any(d.rglob("*.cc"))
            if has_headers or has_source:
                dirs.append(d)
    if not dirs:
        # Fallback: use the project dir itself
        dirs.append(project_dir)
    return dirs


def cmd_cppreference(args: argparse.Namespace) -> None:
    """Download, parse, and ingest cppreference into databases."""
    from doxygen_index.cppreference import download, parse

    if not args.neo4j and not args.csv:
        print("Error: specify --neo4j or --csv", file=sys.stderr)
        sys.exit(1)

    cache_dir = Path(args.cache_dir).expanduser()
    archive_root = download(cache_dir, url=args.archive_url, force=args.force)

    print("\nParsing cppreference HTML ...")
    result = parse(archive_root)

    source = "cppreference"

    # ── CSV export ───────────────────────────────────────────
    if args.csv:
        from doxygen_index.csv_export import export_csv
        csv_dir = Path(args.csv_dir).expanduser() if args.csv_dir else Path("cppreference_csv")
        export_csv(result, source=source, output_dir=csv_dir)

    # ── Neo4j ingest ─────────────────────────────────────────
    if args.neo4j:
        from doxygen_index.neo4j_backend import (
            connect_neo4j,
            ensure_schema,
            clear_source,
            write_result as neo4j_write,
            update_result as neo4j_update,
        )
        print(f"\n--- cppreference → Neo4j ({args.neo4j_uri}) ---")

        connect_neo4j(
            uri=args.neo4j_uri, user=args.neo4j_user,
            password=args.neo4j_password,
        )

        ensure_schema()
        if args.clear:
            _confirm_destructive(f"source '{source}'", args.yes)
            clear_source(source)
            neo4j_write(result)
        else:
            neo4j_update(result, source=source)

        results, _meta = db.cypher_query("""
            MATCH (n) WHERE n.source CONTAINS 'cppreference'
            WITH labels(n)[0] AS label
            RETURN label, count(*) AS cnt ORDER BY label
        """)
        print("\nNode counts:")
        for label, cnt in results:
            print(f"  {label}: {cnt}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Load .env from the current working directory so that
    # NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD are available as argparse
    # defaults.  We pass the path explicitly because python-dotenv's
    # find_dotenv() defaults to searching from the caller's source file
    # location, not from CWD — which is wrong for a CLI tool.
    # Existing real environment variables always win.
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

    parser = argparse.ArgumentParser(
        prog="doxygen-index",
        description="Index Doxygen XML and Conan C++ dependencies into graph databases",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command")

    # project — parse an arbitrary C++ or Python repository
    sp = subparsers.add_parser("project",
                               help="Parse a project (requires .doxygen-index.toml in project dir)")
    sp.add_argument("project_dir", nargs="?", default=".",
                    help="Path to project directory containing .doxygen-index.toml (default: .)")
    sp.add_argument("--output-dir", default=None,
                    help="Base directory for output (default: <project>/build/docs/doxygen-<name>/)")
    sp.add_argument("--format", choices=["json", "neo4j"], default="json",
                    help="Output format (default: json)")
    sp.add_argument("--source", default=None,
                    help="Source label for provenance (default: project name from config)")
    sp.add_argument("--generate-only", action="store_true",
                    help="Only run Doxygen, skip XML parsing (C++ only)")
    sp.add_argument("--parse-only", action="store_true",
                    help="Only parse existing XML, skip Doxygen generation (C++ only)")
    sp.add_argument("--xml-dir", default=None,
                    help="XML directory to parse (required with --parse-only)")
    sp.add_argument("--test-paths", nargs="*", default=None,
                    help="Directories containing test files to also parse (Python only). "
                         "Overrides test_paths in .doxygen-index.toml.")
    _add_db_args(sp)
    sp.add_argument("--clear", action="store_true",
                    help="Clear existing data for this source before a full re-write. "
                         "By default, incremental update is used (adds new, updates "
                         "changed, deletes stale nodes without wiping).")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (e.g. when using --clear).")
    # LLM enrichment options
    sp.add_argument("--enrich", action="store_true",
                    help="Enrich test node descriptions using an LLM "
                         "(requires LLM_API_KEY env var). "
                         "Runs a preflight check and shows per-node progress.")
    sp.add_argument("--dry-run-enrich", action="store_true",
                    help="With --enrich: build prompts but do not call the LLM")
    sp.add_argument("--overwrite-enrich", action="store_true",
                    help="With --enrich: overwrite existing descriptions "
                         "(default: skip already-described nodes)")
    sp.add_argument("--output-enrich-summary", action="store_true",
                    help="With --enrich: write enrichment summary JSON to output dir")
    sp.add_argument("--enrich-batch-size", type=int, default=10, metavar="N",
                    help="With --enrich: nodes per LLM batch call (default: 10)")
    # Reflect enriched descriptions back into test source files as comments
    sp.add_argument("--write-test-comments", action="store_true",
                    help="Write enriched test-node descriptions back into the "
                         "parsed .py files as '# codegraph:test-desc <qn>' "
                         "comment blocks. Idempotent. Use after --enrich, or "
                         "with --descriptions-from to materialise graph values.")
    sp.add_argument("--descriptions-from", default=None, metavar="JSON",
                    help="With --write-test-comments: path to a JSON file "
                         "mapping qualified_name -> description. Overrides the "
                         "parsed nodes' own descriptions (e.g. values read "
                         "from Neo4j).")
    sp.add_argument("--dry-run-test-comments", action="store_true",
                    help="With --write-test-comments: compute edits but do not "
                         "write to disk.")
    sp.add_argument("--scaffold-test-comments", action="store_true",
                    help="With --write-test-comments: insert an empty comment "
                         "slot (# codegraph:test-desc <qn>) for every test "
                         "element that does not yet have a real description, so "
                         "you can author descriptions by hand without enriching. "
                         "Fill the slot by adding '# ...' lines beneath the tag.")
    sp.set_defaults(func=cmd_project)

    # html — generate HTML graph from existing parse output
    sp = subparsers.add_parser("html",
                               help="Generate HTML graph visualization from existing JSON")
    sp.add_argument("project_dir", nargs="?", default=".",
                    help="Path to project directory containing .doxygen-index.toml (default: .)")
    sp.add_argument("--size", choices=["large", "small"], default=None,
                    help="Layout size (default: from config or 'large')")
    sp.set_defaults(func=cmd_html)

    # list-deps
    sp = subparsers.add_parser("list-deps", help="List discovered Conan dependencies")
    _add_common_args(sp)
    sp.set_defaults(func=cmd_list_deps)

    # discover
    sp = subparsers.add_parser("discover", help="Discover Conan dependency include paths")
    _add_common_args(sp)
    sp.set_defaults(func=cmd_discover)

    # generate
    sp = subparsers.add_parser("generate", help="Generate Doxygen XML for dependencies")
    _add_common_args(sp)
    _add_output_args(sp)
    sp.set_defaults(func=cmd_generate)

    # ingest
    sp = subparsers.add_parser("ingest", help="Ingest existing XML into databases")
    _add_common_args(sp)
    _add_output_args(sp)
    _add_db_args(sp)
    sp.add_argument("--clear", action="store_true",
                    help="Full re-write: clear existing data for each source first. "
                         "By default, incremental update is used.")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (e.g. when using --clear).")
    sp.set_defaults(func=cmd_ingest)

    # full
    sp = subparsers.add_parser("full", help="Discover, generate, and ingest (all-in-one)")
    _add_common_args(sp)
    _add_output_args(sp)
    _add_db_args(sp)
    sp.add_argument("--clear", action="store_true",
                    help="Full re-write: clear existing data for each source first. "
                         "By default, incremental update is used.")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (e.g. when using --clear).")
    sp.set_defaults(func=cmd_full)

    # codegraph — combined deps + cppreference → CSV export
    sp = subparsers.add_parser(
        "codegraph",
        help="Full codegraph ingestion: discover deps, generate XML, parse, "
             "and export CSV (optionally including cppreference)",
    )
    _add_common_args(sp)
    _add_output_args(sp)
    sp.add_argument("--csv-dir", default=None,
                    help="Output directory for CSV files (default: <output-dir>/csv)")
    sp.add_argument("--cppreference", action="store_true",
                    help="Also download, parse, and export cppreference")
    sp.add_argument("--cppreference-cache-dir",
                    default="~/.cache/doxygen-index/cppreference",
                    help="Directory to cache the cppreference archive")
    sp.add_argument("--cppreference-archive-url", default=None,
                    help="Override the cppreference archive URL")
    sp.add_argument("--cppreference-force", action="store_true",
                    help="Re-download cppreference even if cached")
    sp.set_defaults(func=cmd_codegraph)

    # cppreference
    sp = subparsers.add_parser("cppreference",
                               help="Download and ingest cppreference C++ standard library docs")
    _add_db_args(sp)
    sp.add_argument("--cache-dir", default="~/.cache/doxygen-index/cppreference",
                    help="Directory to cache the downloaded archive")
    sp.add_argument("--archive-url", default=None,
                    help="Override the cppreference archive URL")
    sp.add_argument("--force", action="store_true",
                    help="Re-download even if cached")
    sp.add_argument("--clear", action="store_true",
                    help="Full re-write: clear existing cppreference data first. "
                         "By default, incremental update is used.")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (e.g. when using --clear).")
    sp.set_defaults(func=cmd_cppreference)

    args = parser.parse_args()

    # If no subcommand given, default to "project" if a config file exists
    # in the current directory.  This lets users simply `cd` into a project
    # directory and run `doxygen-index`.
    if args.command is None:
        config_path = Path.cwd() / CONFIG_FILENAME
        if config_path.exists():
            args = parser.parse_args(["project"] + sys.argv[1:])
        else:
            parser.error("No subcommand given.  Run 'doxygen-index project .' "
                         "or create a .doxygen-index.toml in the current directory.")

    args.func(args)


if __name__ == "__main__":
    main()
