"""
Doxygen runner — generates XML-only Doxyfiles and executes Doxygen.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from doxygen_index.deps_config import DepConfig, get_config, BUILTIN_CONFIGS


def generate_doxyfile(
    name: str,
    input_paths: Path | list[Path],
    xml_output_dir: Path,
    config: Optional[DepConfig] = None,
) -> str:
    """Generate a minimal Doxyfile for XML-only output.

    Args:
        name: Project/dependency name (used as PROJECT_NAME).
        input_paths: Source directories (single Path for deps, list for projects).
        xml_output_dir: Where to write Doxygen XML output.
        config: Configuration (DepConfig for deps, ProjectConfig for projects).
                Uses builtin if not provided.

    Returns:
        Doxyfile content as a string.
    """
    if config is None:
        config = get_config(name)
    if config is None:
        config = DepConfig()

    # Normalise to list
    if isinstance(input_paths, Path):
        paths = [input_paths]
    else:
        paths = list(input_paths)

    # Apply subdir if present (dependency mode)
    subdir = getattr(config, 'subdir', None)
    if subdir and len(paths) == 1:
        subdir_path = paths[0] / subdir
        if subdir_path.exists():
            paths = [subdir_path]
        else:
            print(f"  Warning: subdir '{subdir}' not found, using {paths[0]}")

    input_str = " ".join(str(p) for p in paths)

    return f"""\
# Auto-generated Doxyfile for {name}
PROJECT_NAME           = "{name}"
INPUT                  = {input_str}
RECURSIVE              = {"YES" if config.recursive else "NO"}
FILE_PATTERNS          = {config.file_patterns}
EXCLUDE_PATTERNS       = {config.exclude_patterns}

GENERATE_HTML          = NO
GENERATE_LATEX         = NO
GENERATE_XML           = YES
XML_OUTPUT             = {xml_output_dir}

EXTRACT_ALL            = YES
EXTRACT_PRIVATE        = NO
EXTRACT_STATIC         = YES
EXTRACT_LOCAL_CLASSES  = YES

QUIET                  = YES
WARNINGS               = NO
WARN_IF_UNDOCUMENTED   = NO

JAVADOC_AUTOBRIEF      = YES
BUILTIN_STL_SUPPORT    = YES
ENABLE_PREPROCESSING   = YES
MACRO_EXPANSION        = YES
EXPAND_ONLY_PREDEF     = NO
PREDEFINED             = {config.predefined}

REFERENCED_BY_RELATION = YES
REFERENCES_RELATION    = YES

HAVE_DOT               = NO
"""


def run_doxygen(
    name: str,
    input_paths: Path | list[Path],
    output_base: Path,
    config: Optional[DepConfig] = None,
    xml_subdir: Optional[str] = None,
) -> Path | None:
    """Run Doxygen on source directories.

    Args:
        name: Project/dependency name.
        input_paths: Source directories (single Path for deps, list for projects).
        output_base: Base directory for output.
        config: Configuration (DepConfig or ProjectConfig).
        xml_subdir: Subdirectory under output_base for XML output.
                   Defaults to ``{name}/xml/``.

    Returns:
        Path to the XML output directory, or None on failure.
    """
    if xml_subdir is None:
        xml_dir = output_base / name / "xml"
    else:
        xml_dir = output_base / xml_subdir
    xml_dir.mkdir(parents=True, exist_ok=True)

    doxyfile_content = generate_doxyfile(name, input_paths, xml_dir, config)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".doxyfile", delete=False, prefix=f"doxy_{name}_"
    ) as f:
        f.write(doxyfile_content)
        doxyfile_path = f.name

    try:
        print(f"  Running Doxygen for {name}...")
        subprocess.run(
            ["doxygen", doxyfile_path],
            check=True, capture_output=True, text=True,
        )

        index_xml = xml_dir / "index.xml"
        if not index_xml.exists():
            print(f"  Warning: Doxygen produced no index.xml for {name}")
            return None

        xml_count = len(list(xml_dir.glob("*.xml")))
        print(f"  {name}: {xml_count} XML files generated")
        return xml_dir

    except subprocess.CalledProcessError as e:
        print(f"  Error running Doxygen for {name}: {e.stderr[:200]}")
        return None
    except FileNotFoundError:
        print("Error: 'doxygen' not found on PATH.", file=sys.stderr)
        return None
    finally:
        os.unlink(doxyfile_path)


def generate_xml(
    packages: dict[str, Path],
    output_dir: Path | str,
    dep_configs: Optional[dict[str, DepConfig]] = None,
) -> dict[str, Path]:
    """Generate Doxygen XML for multiple dependencies.

    Args:
        packages: Mapping of dep name to include directory (from ``discover_packages``).
        output_dir: Base directory for all output.
        dep_configs: Optional config overrides per dependency.

    Returns:
        Mapping of dep name to XML output directory (only successful ones).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_dirs: dict[str, Path] = {}

    for dep_name, include_path in sorted(packages.items()):
        config = get_config(dep_name, dep_configs)
        xml_dir = run_doxygen(dep_name, include_path, output_dir, config)
        if xml_dir:
            xml_dirs[dep_name] = xml_dir

    return xml_dirs
