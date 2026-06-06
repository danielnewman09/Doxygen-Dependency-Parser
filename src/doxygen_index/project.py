"""
Project configuration — reads .doxygen-index.toml to configure Doxygen
indexing of arbitrary C++ repositories.

No auto-detection. Everything is explicitly specified in the config file.
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
class ProjectConfig:
    """Configuration for indexing a project's source code.

    All paths in input_paths are absolute (resolved from config dir at load time).
    """
    name: str
    input_paths: list[Path]             # absolute paths to source dirs
    file_patterns: str = "*.h *.hpp *.hxx *.cpp *.cxx *.cc"
    recursive: bool = True
    exclude_patterns: str = ""          # Doxygen EXCLUDE_PATTERNS
    predefined: str = ""                # Doxygen PREDEFINED macros


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

    return ProjectConfig(
        name=proj["name"],
        input_paths=resolved_paths,
        file_patterns=proj.get("file_patterns", "*.h *.hpp *.hxx *.cpp *.cxx *.cc"),
        recursive=proj.get("recursive", True),
        exclude_patterns=proj.get("exclude_patterns", ""),
        predefined=proj.get("predefined", ""),
    ), project_dir


def _print_config_help(project_dir: Path, config_path: Path) -> None:
    """Print a helpful error message with a minimal config template."""
    template = f"""\
[project]
name = "{project_dir.name}"
input_paths = ["include", "src"]
# file_patterns = "*.h *.hpp *.cpp"
# recursive = true
# exclude_patterns = "*/test/* */build/*"
# predefined = "SOME_MACRO=1"
"""
    print(f"Error: no .doxygen-index.toml found in {project_dir}", file=sys.stderr)
    print(f"Create {config_path} with:", file=sys.stderr)
    print(template, file=sys.stderr)
