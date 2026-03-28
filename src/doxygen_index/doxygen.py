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
    dep_name: str,
    include_path: Path,
    xml_output_dir: Path,
    config: Optional[DepConfig] = None,
) -> str:
    """Generate a minimal Doxyfile for XML-only output.

    Args:
        dep_name: Dependency name (used as PROJECT_NAME).
        include_path: Root include directory for the dependency.
        xml_output_dir: Where to write Doxygen XML output.
        config: Dependency configuration. Uses builtin if not provided.

    Returns:
        Doxyfile content as a string.
    """
    if config is None:
        config = get_config(dep_name)
    if config is None:
        config = DepConfig()

    input_path = include_path
    if config.subdir:
        subdir_path = include_path / config.subdir
        if subdir_path.exists():
            input_path = subdir_path
        else:
            print(f"  Warning: subdir '{config.subdir}' not found, using {include_path}")

    return f"""\
# Auto-generated Doxyfile for {dep_name}
PROJECT_NAME           = "{dep_name}"
INPUT                  = {input_path}
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
    dep_name: str,
    include_path: Path,
    output_base: Path,
    config: Optional[DepConfig] = None,
) -> Path | None:
    """Run Doxygen on a single dependency.

    Args:
        dep_name: Dependency name.
        include_path: Root include directory.
        output_base: Base directory for output (XML goes to ``{output_base}/{dep_name}/xml/``).
        config: Dependency configuration.

    Returns:
        Path to the XML output directory, or None on failure.
    """
    xml_dir = output_base / dep_name / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)

    doxyfile_content = generate_doxyfile(dep_name, include_path, xml_dir, config)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".doxyfile", delete=False, prefix=f"doxy_{dep_name}_"
    ) as f:
        f.write(doxyfile_content)
        doxyfile_path = f.name

    try:
        print(f"  Running Doxygen for {dep_name}...")
        subprocess.run(
            ["doxygen", doxyfile_path],
            check=True, capture_output=True, text=True,
        )

        index_xml = xml_dir / "index.xml"
        if not index_xml.exists():
            print(f"  Warning: Doxygen produced no index.xml for {dep_name}")
            return None

        xml_count = len(list(xml_dir.glob("*.xml")))
        print(f"  {dep_name}: {xml_count} XML files generated")
        return xml_dir

    except subprocess.CalledProcessError as e:
        print(f"  Error running Doxygen for {dep_name}: {e.stderr[:200]}")
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
