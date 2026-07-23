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


def _boost_input_paths(boost_dir: Path, project_source_dirs: list[Path]) -> list[Path]:
    """Return only the boost module dirs/files the project transitively includes.

    Runs ``gcc -E -M`` on the project source files to discover which
    boost headers are transitively included, then returns those specific
    directories and files for use in Doxygen's INPUT.

    Args:
        boost_dir: The boost include directory (e.g. ``.../include``).
        project_source_dirs: Project source dirs to scan for includes.

    Returns:
        List of Paths to include in Doxygen INPUT, or empty list on failure.
    """
    import subprocess as sp

    boost_root = boost_dir / "boost"
    if not boost_root.is_dir():
        return []

    # Find which boost headers the project transitively includes
    needed: set[str] = set()
    try:
        cpp_input = ""
        for src_dir in project_source_dirs:
            for pattern in ("*.hpp", "*.h"):
                for h in src_dir.rglob(pattern):
                    with open(h) as f:
                        for line in f:
                            if "#include" in line and "boost/" in line:
                                cpp_input += line
        if not cpp_input:
            return []

        cpp_input = "#include <cstddef>\n" + cpp_input
        dep_output = sp.run(
            ["gcc", "-E", "-M", "-I", str(boost_dir),
             "-x", "c++", "-"],
            input=cpp_input, capture_output=True, text=True, timeout=30,
        )
        if dep_output.returncode == 0:
            import re
            for token in re.split(r"[\s\\]+", dep_output.stdout):
                if "/boost/" in token:
                    path = token.split("/boost/", 1)[-1]
                    module = path.split("/")[0].rsplit(".", 1)[0]
                    needed.add(module)
    except Exception:
        pass

    if not needed:
        return []

    # Return only the needed module dirs and standalone headers
    paths: list[Path] = []
    for module in sorted(needed):
        mod_dir = boost_root / module
        if mod_dir.is_dir():
            paths.append(mod_dir)
        else:
            for ext in (".hpp", ".h"):
                f = boost_root / f"{module}{ext}"
                if f.exists():
                    paths.append(f)
                    break
    return paths


def generate_doxyfile(
    name: str,
    input_paths: Path | list[Path],
    xml_output_dir: Path,
    *,
    predefined: str = "",
    file_patterns: str = _DEFAULT_FILE_PATTERNS,
    exclude_patterns: str = _DEFAULT_EXCLUDE_PATTERNS,
    recursive: bool = True,
    include_paths: list[Path] | None = None,
) -> str:
    """Generate a minimal Doxyfile for XML-only output.

    Args:
        name: Project/dependency name (used as PROJECT_NAME).
        input_paths: Source directories to parse (single Path for deps, list
            for projects).
        xml_output_dir: Where to write Doxygen XML output.
        predefined: Preprocessor defines (e.g. ``EIGEN_PARSED_BY_DOXYGEN``).
        file_patterns: Doxygen FILE_PATTERNS value.
        exclude_patterns: Doxygen EXCLUDE_PATTERNS value.
        recursive: Whether to recurse into subdirectories.
        include_paths: Directories added to Doxygen's INCLUDE_PATH for
            ``#include`` resolution.  Files here are *not* parsed — only
            used to satisfy preprocessor includes.  Defaults to None.

    Returns:
        Doxyfile content as a string.
    """
    if isinstance(input_paths, Path):
        paths = [input_paths]
    else:
        paths = list(input_paths)

    input_str = " ".join(str(p) for p in paths)
    include_str = " ".join(str(p) for p in include_paths) if include_paths else ""

    return f"""\
# Auto-generated Doxyfile for {name}
PROJECT_NAME           = "{name}"
INPUT                  = {input_str}
RECURSIVE              = {"YES" if recursive else "NO"}
FILE_PATTERNS          = {file_patterns}
EXCLUDE_PATTERNS       = {exclude_patterns}

INCLUDE_PATH           = {include_str}

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
    include_paths: list[Path] | None = None,
) -> Path | None:
    """Run Doxygen on source directories.

    Args:
        name: Project/dependency name.
        input_paths: Source directories to parse (single Path for deps, list
            for projects).
        output_base: Base directory for output.
        predefined: Preprocessor defines.
        file_patterns: Doxygen FILE_PATTERNS value.
        exclude_patterns: Doxygen EXCLUDE_PATTERNS value.
        xml_subdir: Subdirectory under output_base for XML output.
                   Defaults to ``{name}/xml/``.
        include_paths: Directories added to Doxygen's INCLUDE_PATH for
            ``#include`` resolution without parsing their contents.

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
        include_paths=include_paths,
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


def tag_nodes_by_source(
    result: "ParseResult",
    project_dir: Path,
    dep_include_dirs: dict[str, Path],
    project_source: str,
) -> None:
    """Tag every node in *result* with the correct source label and tags.

    Nodes whose ``file_path`` falls under *project_dir* get
    ``source=project_source``; those under a dep include dir get
    ``source=<dep_name>``.  Nodes without a file_path are classified
    by qualified_name when possible.

    Tags are set alongside source: project nodes get ``["as-built"]``,
    dependency nodes get ``["dependency"]``.
    """
    project_dir = project_dir.resolve()
    dir_to_dep: dict[Path, str] = {}
    for dep_name, inc_dir in dep_include_dirs.items():
        dir_to_dep[inc_dir.resolve()] = dep_name

    def _classify(file_path: str, qualified_name: str = "", refid: str = "") -> str:
        # 1. Try explicit file_path resolution
        if file_path:
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

        # 2. Fall back to qualified_name-based classification.
        #    NamespaceNodes (and other nodes without file_paths) are
        #    identified by their namespace prefix matching a known dep.
        if qualified_name:
            for dep_dir, dep_name in dir_to_dep.items():
                if qualified_name == dep_name or qualified_name.startswith(dep_name + "::"):
                    return dep_name
            # Special case: gtest uses the ``testing::`` namespace
            if qualified_name == "testing" or qualified_name.startswith("testing::"):
                return "gtest"

        # 3. Last resort: check if the Doxygen refid contains a dep name
        #    (handles flattened namespaces like ``boost_swap_impl``).
        if refid:
            for dep_name in dir_to_dep.values():
                if dep_name in refid:
                    return dep_name

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
        fp = getattr(node, "file_path", "") or getattr(node, "path", "") or ""
        qn = getattr(node, "qualified_name", "") or ""
        rid = getattr(node, "refid", "") or ""
        source = _classify(fp, qn, rid)
        if hasattr(node, "source"):
            node.source = source
        # Set tags to match the classified source.
        if hasattr(node, "tags"):
            node.tags = ["as-built" if source == project_source else "dependency"]


def resolve_namespace_type_deps(result: "ParseResult") -> None:
    """Scan all nodes for qualified-name references and add DEPENDS_ON edges.

    Examines every node's text fields (type_signature, definition,
    brief_description, detailed_description) for ``namespace::type``
    patterns.  When a match resolves to a known node in *result*,
    a :class:`DependsOnEntry` is added.  Catches stdlib types
    (``std::unique_ptr``, ``std::string``, ...) from cppreference
    parses as well as dependency types that Doxygen ``<ref>`` elements
    missed.
    """
    import re
    from doxygen_index.parser.model import DependsOnEntry

    # Build known qualified-name → refid mapping and a set of known
    # qnames for fast O(1) lookup.  Exclude namespace nodes — we only
    # want DEPENDS_ON to specific types/functions, not bare namespaces.
    qname_to_refid: dict[str, str] = {}
    for node_list in [
        result.classes, result.enums, result.unions,
        result.interfaces, result.concepts, result.functions,
        result.methods, result.attributes,
    ]:
        for node in node_list:
            qn = getattr(node, "qualified_name", "") or ""
            if qn:
                qname_to_refid[qn] = getattr(node, "refid", "")
    known_qnames = set(qname_to_refid)

    # Text fields to scan on each node.
    _text_fields = [
        "type_signature", "definition",
        "brief_description", "detailed_description",
    ]

    # Regex: qualified-name patterns (namespace::identifier).
    _qname_re = re.compile(r"\b\w+(?:::\w+)+")

    # Track (from_refid, to_refid) pairs to avoid duplicate entries.
    seen: set[tuple[str, str]] = {
        (d.from_refid, d.to_refid) for d in result.depends_on
    }

    all_nodes = (
        result.classes + result.enums + result.unions +
        result.interfaces + result.concepts +
        result.methods + result.attributes +
        result.functions + result.defines
    )
    for node in all_nodes:
        node_refid = getattr(node, "refid", "") or ""
        if not node_refid:
            continue
        for field_name in _text_fields:
            text = getattr(node, field_name, "") or ""
            if not text:
                continue
            for match in _qname_re.finditer(text):
                candidate = match.group(0)
                # Try exact match, then strip trailing segments
                # (handles ``std::vector::push_back`` → ``std::vector``).
                check = candidate
                while check:
                    if check in known_qnames:
                        to_refid = qname_to_refid[check]
                        # Skip self-references (node referencing itself).
                        if to_refid == node_refid:
                            break
                        pair = (node_refid, to_refid)
                        if pair not in seen:
                            seen.add(pair)
                            result.depends_on.append(DependsOnEntry(
                                from_refid=node_refid,
                                to_refid=to_refid,
                                to_type="compound",
                            ))
                        break
                    last_sep = check.rfind("::")
                    if last_sep == -1:
                        break
                    check = check[:last_sep]

    # Text fields to scan on each node.
    _text_fields = [
        "type_signature", "definition",
        "brief_description", "detailed_description",
    ]

    # Regex: word boundary + identifier + (::identifier)+ + word boundary.
    _qname_re = re.compile(r"\b\w+(?:::\w+)+")

    # Track (from_refid, to_refid) pairs to avoid duplicate DEPENDS_ON.
    seen: set[tuple[str, str]] = {
        (dep.from_refid, dep.to_refid) for dep in result.depends_on
    }

    all_nodes = (
        result.classes + result.enums + result.unions +
        result.interfaces + result.concepts +
        result.methods + result.attributes +
        result.functions + result.defines
    )
    for node in all_nodes:
        node_refid = getattr(node, "refid", "") or ""
        if not node_refid:
            continue
        for field_name in _text_fields:
            text = getattr(node, field_name, "") or ""
            if not text:
                continue
            for match in _qname_re.finditer(text):
                candidate = match.group(0)
                # Check exact match first, then try stripping trailing
                # segments (handles fully-qualified member refs like
                # ``std::vector::push_back`` → ``std::vector``).
                check = candidate
                while check:
                    if check in known_qnames:
                        to_refid = qname_to_refid[check]
                        pair = (node_refid, to_refid)
                        if pair not in seen:
                            seen.add(pair)
                            result.depends_on.append(DependsOnEntry(
                                from_refid=node_refid,
                                to_refid=to_refid,
                                to_type="compound",
                            ))
                        break
                    # Strip last ::segment
                    last_sep = check.rfind("::")
                    if last_sep == -1:
                        break
                    check = check[:last_sep]


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
    elements — with consistent refids across all symbols.

    This is slower for large deps (boost has 15K headers) but produces a
    single, self-consistent ParseResult where all edge targets resolve.
    Cache the result (e.g. via pickle) for subsequent fast loads.

    Args:
        project_name: Name for the Doxygen PROJECT_NAME.
        project_source_dirs: Source directories of the project itself.
        dep_include_dirs: ``{dep_name: include_dir}`` for each dependency.
        output_base: Base directory for Doxygen XML output.
        predefined: Optional preprocessor defines.

    Returns:
        Path to the XML output directory, or None on failure.
    """
    # INPUT = project sources + dep include dirs.
    # boost: only include the modules the project actually uses
    # (14 of 120+, ~410 files vs 15K — 97% reduction).
    all_inputs = list(project_source_dirs)
    for dep_name, inc_dir in dep_include_dirs.items():
        if dep_name == "boost":
            boost_paths = _boost_input_paths(inc_dir, project_source_dirs)
            if boost_paths:
                all_inputs.extend(boost_paths)
                continue
        all_inputs.append(inc_dir)

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
