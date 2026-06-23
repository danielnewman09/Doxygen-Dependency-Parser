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
import os
import sys
from pathlib import Path


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
    """Add database target arguments."""
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
    # Output
    # ------------------------------------------------------------------

    source = args.source or config.name
    json_path = output_dir / f"{config.name}.json"

    if args.format == "neo4j":
        if config.language == "python":
            from doxygen_index.neo4j_backend import (
                ensure_schema, clear_source, write_result as neo4j_write,
            )
            from neomodel import get_config, db
            print(f"\n--- {config.name} → Neo4j ---")
            _bolt_host = args.neo4j_uri.replace("bolt://", "")
            neo_config = get_config()
            neo_config.database_url = f"bolt://{args.neo4j_user}:{args.neo4j_password}@{_bolt_host}"
            neo_config.database_name = getattr(args, 'neo4j_database', 'neo4j')
            db.set_connection(neo_config.database_url)
            ensure_schema()
            if args.clear:
                clear_source(source)
            neo4j_write(result)
        else:
            from doxygen_index.neo4j_backend import ingest as neo4j_ingest
            print(f"\n--- {config.name} → Neo4j ---")
            neo4j_ingest(
                xml_dir, source=source,
                uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password,
                layer="codebase",
                clear=args.clear,
            )
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
    if args.format != "neo4j":
        print(f"\nOutput: {json_path}")


def _parse_python_project(
    args: argparse.Namespace,
    config: ProjectConfig,
) -> ParseResult:
    """Parse a Python project using the AST-based parser.

    No external tools required — uses Python's ``ast`` module.
    """
    from doxygen_index.parser import parse_python_dir

    # Parse user exclude_patterns (space-separated globs → dir names)
    user_excludes = []
    if config.exclude_patterns:
        user_excludes = config.exclude_patterns.split()

    print(f"\nParsing Python source...")
    result = parse_python_dir(
        config.input_paths,
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
    from codegraph.viz import export_html_from_json

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
    from codegraph.viz import export_html_from_json

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
    """List all known dependency configurations."""
    from doxygen_index.deps_config import list_known_deps
    configs = list_known_deps()
    print("Known dependency configurations:")
    for name, config in sorted(configs.items()):
        subdir = config.subdir or "(root)"
        print(f"  {name:20s}  patterns={config.file_patterns:15s}  subdir={subdir}")


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
    """Ingest existing Doxygen XML into databases."""
    output_dir = Path(args.output_dir)

    if not args.neo4j:
        print("Error: specify --neo4j", file=sys.stderr)
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

    print(f"Ingesting {len(xml_dirs)} dependencies: {', '.join(sorted(xml_dirs))}\n")

    for dep_name, xml_dir in sorted(xml_dirs.items()):
        if args.neo4j:
            from doxygen_index.neo4j_backend import ingest as neo4j_ingest
            print(f"--- {dep_name} → Neo4j ---")
            neo4j_ingest(
                xml_dir, source=dep_name,
                uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password,
            )
            print()


def cmd_full(args: argparse.Namespace) -> None:
    """Discover, generate, and ingest — all in one."""
    from doxygen_index.conan import discover_packages
    from doxygen_index.doxygen import generate_xml

    if not args.neo4j:
        print("Error: specify --neo4j", file=sys.stderr)
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

    # Phase 3: Ingest
    print(f"\n--- Ingesting {len(xml_dirs)} dependencies ---\n")
    for dep_name, xml_dir in sorted(xml_dirs.items()):
        if args.neo4j:
            from doxygen_index.neo4j_backend import ingest as neo4j_ingest
            print(f"--- {dep_name} → Neo4j ---")
            neo4j_ingest(
                xml_dir, source=dep_name,
                uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password,
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


def cmd_cppreference(args: argparse.Namespace) -> None:
    """Download, parse, and ingest cppreference into databases."""
    from doxygen_index.cppreference import download, parse

    if not args.neo4j:
        print("Error: specify --neo4j", file=sys.stderr)
        sys.exit(1)

    cache_dir = Path(args.cache_dir).expanduser()
    archive_root = download(cache_dir, url=args.archive_url, force=args.force)

    print("\nParsing cppreference HTML ...")
    result = parse(archive_root)

    source = "cppreference"

    if args.neo4j:
        from neomodel import get_config, db
        from doxygen_index.neo4j_backend import (
            ensure_schema,
            clear_source,
            write_result as neo4j_write,
        )
        print(f"\n--- cppreference → Neo4j ({args.neo4j_uri}) ---")

        # Configure neomodel connection
        _bolt_host = args.neo4j_uri.replace("bolt://", "")
        config = get_config()
        config.database_url = f"bolt://{args.neo4j_user}:{args.neo4j_password}@{_bolt_host}"
        config.database_name = getattr(args, 'neo4j_database', 'neo4j')
        db.set_connection(config.database_url)

        ensure_schema()
        if args.clear:
            clear_source(source)
        neo4j_write(result)

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
    _add_db_args(sp)
    sp.add_argument("--clear", action="store_true",
                    help="Clear existing data for this source before ingesting")
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
    sp = subparsers.add_parser("list-deps", help="List known dependency configurations")
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
    sp.set_defaults(func=cmd_ingest)

    # full
    sp = subparsers.add_parser("full", help="Discover, generate, and ingest (all-in-one)")
    _add_common_args(sp)
    _add_output_args(sp)
    _add_db_args(sp)
    sp.set_defaults(func=cmd_full)

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
                    help="Clear existing cppreference data before ingesting")
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
