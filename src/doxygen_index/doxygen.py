"""
Doxygen runner — generates XML-only Doxyfiles and executes Doxygen.

Uses sensible defaults for all dependencies:
- INPUT is the Conan include directory (auto-discovered)
- FILE_PATTERNS: all common C++ header extensions
- RECURSIVE: YES
- EXCLUDE_PATTERNS: implementation-detail directories (detail, impl, aux_, internal)

No per-library configuration is required.  For the rare edge case
where a library needs a special preprocessor define (e.g. Eigen's
``EIGEN_PARSED_BY_DOXYGEN``), pass ``predefined`` in the optional
``overrides`` dict.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Sensible defaults — work for 99% of C++ libraries.
_DEFAULT_FILE_PATTERNS = "*.h *.hpp *.hxx *.h++ *.hh"
_DEFAULT_EXCLUDE_PATTERNS = "*/detail/* */impl/* */aux_/* */internal/* */experimental/*"


def generate_doxyfile(
    name: str,
    input_paths: Path | list[Path],
    xml_output_dir: Path,
    *,
    predefined: str = "",
    file_patterns: str = _DEFAULT_FILE_PATTERNS,
    exclude_patterns: str = _DEFAULT_EXCLUDE_PATTERNS,
    recursive: bool = True,
) -> str:
    """Generate a minimal Doxyfile for XML-only output.

    Args:
        name: Project/dependency name (used as PROJECT_NAME).
        input_paths: Source directories (single Path for deps, list for projects).
        xml_output_dir: Where to write Doxygen XML output.
        predefined: Preprocessor defines (e.g. ``EIGEN_PARSED_BY_DOXYGEN``).
        file_patterns: Doxygen FILE_PATTERNS value.
        exclude_patterns: Doxygen EXCLUDE_PATTERNS value.
        recursive: Whether to recurse into subdirectories.

    Returns:
        Doxyfile content as a string.
    """
    if isinstance(input_paths, Path):
        paths = [input_paths]
    else:
        paths = list(input_paths)

    input_str = " ".join(str(p) for p in paths)

    return f"""\
# Auto-generated Doxyfile for {name}
PROJECT_NAME           = "{name}"
INPUT                  = {input_str}
RECURSIVE              = {"YES" if recursive else "NO"}
FILE_PATTERNS          = {file_patterns}
EXCLUDE_PATTERNS       = {exclude_patterns}

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
PREDEFINED             = {predefined}

REFERENCED_BY_RELATION = YES
REFERENCES_RELATION    = YES

HAVE_DOT               = NO
"""


def run_doxygen(
    name: str,
    input_paths: Path | list[Path],
    output_base: Path,
    *,
    predefined: str = "",
    file_patterns: str = _DEFAULT_FILE_PATTERNS,
    exclude_patterns: str = _DEFAULT_EXCLUDE_PATTERNS,
    xml_subdir: Optional[str] = None,
) -> Path | None:
    """Run Doxygen on source directories.

    Args:
        name: Project/dependency name.
        input_paths: Source directories (single Path for deps, list for projects).
        output_base: Base directory for output.
        predefined: Preprocessor defines.
        file_patterns: Doxygen FILE_PATTERNS value.
        exclude_patterns: Doxygen EXCLUDE_PATTERNS value.
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

    doxyfile_content = generate_doxyfile(
        name, input_paths, xml_dir,
        predefined=predefined,
        file_patterns=file_patterns,
        exclude_patterns=exclude_patterns,
    )

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
    overrides: Optional[dict[str, dict]] = None,
) -> dict[str, Path]:
    """Generate Doxygen XML for multiple dependencies.

    Args:
        packages: Mapping of dep name to include directory (from ``discover_packages``).
        output_dir: Base directory for all output.
        overrides: Optional per-dependency overrides.  Keys are dep names,
            values are dicts with any of: ``predefined``, ``file_patterns``,
            ``exclude_patterns``.  Only needed for rare edge cases (e.g.
            ``{"eigen": {"predefined": "EIGEN_PARSED_BY_DOXYGEN"}}``).

    Returns:
        Mapping of dep name to XML output directory (only successful ones).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_dirs: dict[str, Path] = {}
    overrides = overrides or {}

    for dep_name, include_path in sorted(packages.items()):
        opts = overrides.get(dep_name, {})
        xml_dir = run_doxygen(
            dep_name, include_path, output_dir,
            predefined=opts.get("predefined", ""),
            file_patterns=opts.get("file_patterns", _DEFAULT_FILE_PATTERNS),
            exclude_patterns=opts.get("exclude_patterns", _DEFAULT_EXCLUDE_PATTERNS),
        )
        if xml_dir:
            xml_dirs[dep_name] = xml_dir

    return xml_dirs


def run_unified_doxygen(
    project_name: str,
    project_source_dirs: list[Path],
    dep_include_dirs: dict[str, Path],
    output_base: Path,
    *,
    predefined: str = "",
) -> Path | None:
    """Run a single Doxygen parse covering both project source AND all
    dependency include directories.

    Because everything is parsed in one Doxygen run, cross-references
    (INVOKES, INHERITS_FROM, type references) between project code and
    dependency symbols are captured natively in the XML ``<references>``
    elements.

    After parsing, call :func:`split_result_by_source` to tag nodes
    with the correct ``source`` label based on their file location.

    Args:
        project_name: Name for the Doxygen PROJECT_NAME.
        project_source_dirs: Source directories of the project itself.
        dep_include_dirs: ``{dep_name: include_dir}`` for each dependency.
        output_base: Base directory for Doxygen XML output.
        predefined: Optional preprocessor defines.

    Returns:
        Path to the XML output directory, or None on failure.
    """
    # INPUT = project sources + all dep include dirs
    all_inputs = list(project_source_dirs) + list(dep_include_dirs.values())

    return run_doxygen(
        project_name,
        all_inputs,
        output_base,
        predefined=predefined,
        file_patterns=_DEFAULT_FILE_PATTERNS,
        exclude_patterns=_DEFAULT_EXCLUDE_PATTERNS,
        xml_subdir=f"{project_name}_unified/xml",
    )


def split_result_by_source(
    result: "ParseResult",
    project_dir: Path,
    dep_include_dirs: dict[str, Path],
    project_source: str = "project",
) -> dict[str, "ParseResult"]:
    """Split a unified ParseResult into per-source ParseResults.

    Nodes are assigned to a source based on their ``file_path``:
    - Files under *project_dir* → *project_source*
    - Files under a dep include dir → that dep's name
    - Everything else → *project_source* (fallback)

    INVOKES edges crossing source boundaries are preserved as-is
    (they become DEPENDS_ON-like edges between the source-specific
    graphs at CSV export time).

    Args:
        result: ParseResult from a unified Doxygen run.
        project_dir: Root of the project source tree.
        dep_include_dirs: ``{dep_name: include_dir}`` for each dependency.
        project_source: Source label for project-owned nodes.

    Returns:
        ``{source_label: ParseResult}`` — one ParseResult per source.
    """
    from doxygen_index.parser import ParseResult

    project_dir = project_dir.resolve()
    # Build a reverse lookup: resolved include dir → dep name
    dir_to_dep: dict[Path, str] = {}
    for dep_name, inc_dir in dep_include_dirs.items():
        dir_to_dep[inc_dir.resolve()] = dep_name

    def _classify(file_path: str) -> str:
        """Return the source label for a file path."""
        if not file_path:
            return project_source
        fp = Path(file_path)
        if not fp.is_absolute():
            # Doxygen reports paths relative to INPUT dirs — try resolving
            # against project_dir first, then dep dirs
            candidate = (project_dir / fp).resolve()
            try:
                candidate.relative_to(project_dir)
                return project_source
            except ValueError:
                pass
            for dep_dir, dep_name in dir_to_dep.items():
                try:
                    (dep_dir / fp).relative_to(dep_dir)
                    return dep_name
                except ValueError:
                    pass
            return project_source
        # Absolute path
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

    # Collect all node lists
    node_sets: list[tuple[str, list]] = [
        ("files", result.files),
        ("namespaces", result.namespaces),
        ("classes", result.classes),
        ("enums", result.enums),
        ("unions", result.unions),
        ("interfaces", result.interfaces),
        ("concepts", result.concepts),
        ("methods", result.methods),
        ("attributes", result.attributes),
        ("enum_values", result.enum_values),
        ("defines", result.defines),
        ("functions", result.functions),
        ("parameters", result.parameters),
        ("implementations", result.implementations),
    ]

    # Split nodes by source
    per_source: dict[str, dict[str, list]] = {}
    for attr_name, nodes in node_sets:
        for node in nodes:
            fp = getattr(node, "file_path", "") or ""
            src = _classify(fp)
            # Override the node's source attribute
            if hasattr(node, "source"):
                node.source = src
            per_source.setdefault(src, {}).setdefault(attr_name, []).append(node)

    # Build ParseResult per source
    results: dict[str, ParseResult] = {}
    all_sources = set(per_source.keys()) | {project_source}
    for src in all_sources:
        pr = ParseResult()
        buckets = per_source.get(src, {})
        for attr_name, _ in node_sets:
            setattr(pr, attr_name, buckets.get(attr_name, []))
        # Carry over cross-source invocations
        for inv in result.invokes:
            from_src = _classify(_file_path_for_refid(result, inv.from_refid))
            to_src = _classify(_file_path_for_refid(result, inv.to_refid))
            if from_src == src or to_src == src:
                pr.invokes.append(inv)
        results[src] = pr

    return results


def _file_path_for_refid(result: "ParseResult", refid: str) -> str:
    """Look up the file_path for a node by refid."""
    for node_list in (
        result.files, result.namespaces, result.classes, result.enums,
        result.unions, result.interfaces, result.concepts,
        result.methods, result.attributes, result.enum_values,
        result.defines, result.functions, result.parameters,
    ):
        for node in node_list:
            if getattr(node, "refid", "") == refid:
                return getattr(node, "file_path", "") or ""
    return ""
