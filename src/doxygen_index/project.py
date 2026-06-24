"""
Project configuration — reads .doxygen-index.toml to configure source
code indexing of arbitrary C++ or Python repositories.

No auto-detection. Everything is explicitly specified in the config file.
Supported languages: ``cpp`` (via Doxygen XML) and ``python`` (via AST).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback


@dataclass
class HtmlConfig:
    """Configuration for HTML graph visualization via codegraph.

    When present in the TOML, the ``doxygen-index`` command generates
    an interactive HTML graph alongside the JSON output.
    """
    output_dir: Path               # where to write JSON + HTML
    size: str = "large"            # "large" or "small"


@dataclass
class ProjectConfig:
    """Configuration for indexing a project's source code.

    All paths in input_paths are absolute (resolved from config dir at load time).
    """
    name: str
    input_paths: list[Path]             # absolute paths to source dirs
    language: str = "cpp"               # "cpp" or "python"
    output_dir: Path | None = None     # where to write JSON/XML output (None = default)
    html_config: HtmlConfig | None = None  # [codegraph-html] section, if present
    file_patterns: str = "*.h *.hpp *.hxx *.cpp *.cxx *.cc"
    recursive: bool = True
    exclude_patterns: str = ""          # Doxygen EXCLUDE_PATTERNS / Python glob excludes
    predefined: str = ""                # Doxygen PREDEFINED macros
    test_paths: list[Path] = None        # directories containing test files (Python only)


def load_config(project_dir: Path | str) -> tuple[ProjectConfig, Path]:
    """Load .doxygen-index.toml from a project directory.

    Args:
        project_dir: Path to the project directory containing the config file.

    Returns:
        Tuple of (ProjectConfig, config_dir_path).

    Raises:
        SystemExit: If no config file found or required fields missing.
    """
    project_dir = Path(project_dir).resolve()
    config_path = project_dir / ".doxygen-index.toml"

    if not config_path.exists():
        _print_config_help(project_dir, config_path)
        sys.exit(1)

    data = tomllib.loads(config_path.read_text())
    proj = data.get("project", {})

    if "name" not in proj:
        print(f"Error: [project] section must specify 'name' in {config_path}",
              file=sys.stderr)
        sys.exit(1)
    if "input_paths" not in proj:
        print(f"Error: [project] section must specify 'input_paths' in {config_path}",
              file=sys.stderr)
        sys.exit(1)

    # Resolve relative paths to absolute (relative to config file directory)
    base = config_path.parent
    resolved_paths = [
        (base / p).resolve() for p in proj["input_paths"]
    ]

    # Verify all input paths exist
    for p in resolved_paths:
        if not p.exists():
            print(f"Warning: input path does not exist: {p}", file=sys.stderr)

    # Resolve output_dir (relative to config file directory)
    output_dir_raw = proj.get("output_dir")
    resolved_output_dir = (base / output_dir_raw).resolve() if output_dir_raw else None

    # Resolve test_paths (relative to config file directory)
    test_paths_raw = proj.get("test_paths", [])
    resolved_test_paths = [
        (base / p).resolve() for p in test_paths_raw
    ] if test_paths_raw else None
    for p in (resolved_test_paths or []):
        if not p.exists():
            print(f"Warning: test path does not exist: {p}", file=sys.stderr)

    # Parse optional [codegraph-html] section
    html_config = None
    if "codegraph-html" in data:
        html_data = data.get("codegraph-html", {})
        html_output_raw = html_data.get("output_dir", "codegraph")
        html_config = HtmlConfig(
            output_dir=(base / html_output_raw).resolve(),
            size=html_data.get("size", "large"),
        )

    return ProjectConfig(
        name=proj["name"],
        input_paths=resolved_paths,
        language=proj.get("language", "cpp"),
        output_dir=resolved_output_dir,
        html_config=html_config,
        file_patterns=proj.get("file_patterns", "*.h *.hpp *.hxx *.cpp *.cxx *.cc"),
        recursive=proj.get("recursive", True),
        exclude_patterns=proj.get("exclude_patterns", ""),
        predefined=proj.get("predefined", ""),
        test_paths=resolved_test_paths,
    ), project_dir


def _print_config_help(project_dir: Path, config_path: Path) -> None:
    """Print a helpful error message with a minimal config template."""
    template = f"""\
[project]
name = "{project_dir.name}"
language = "cpp"          # or "python"
input_paths = ["include", "src"]
# output_dir = "."             # where to write output (default: build/docs/doxygen-<name>/)
# file_patterns = "*.h *.hpp *.cpp"
# recursive = true
# exclude_patterns = "*/test/* */build/*"
# predefined = "SOME_MACRO=1"
# test_paths = ["tests"]       # Python: also parse test dirs for TestNode extraction

# [codegraph-html]        # uncomment to enable HTML graph visualization
# output_dir = "codegraph"  # where to write JSON + HTML (default: codegraph)
# size = "large"            # "large" or "small"
"""
    print(f"Error: no .doxygen-index.toml found in {project_dir}", file=sys.stderr)
    print(f"Create {config_path} with:", file=sys.stderr)
    print(template, file=sys.stderr)
