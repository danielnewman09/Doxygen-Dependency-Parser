"""
Doxygen XML parser — extracts symbols, documentation, and relationships.

Parses Doxygen-generated XML files into a neutral data model (dataclasses)
that can be consumed by any backend (e.g. Neo4j).

This package provides:

* :class:`LanguageParser` — abstract base class for language-specific parsers.
* :class:`CppParser` — concrete parser for C/C++ Doxygen output (default).
* :func:`parse_xml_dir` — public entry point that parses a whole directory.
* Data model classes (:class:`ParseResult`, :class:`IncludeEntry`, …).
* Helper functions (:func:`get_text`, :func:`parse_description`, …).
* C++ utilities (:func:`normalize_argsstring`, :func:`derive_module`, …).
"""

from __future__ import annotations

from pathlib import Path

# Re-export data model
from doxygen_index.parser.model import (
    IncludeEntry,
    TemplateParamEntry,
    TemplateParamRef,
    SpecializesRef,
    InvokeEntry,
    ImplementationRef,
    ParseResult,
)

# Re-export helpers
from doxygen_index.parser.helpers import (
    get_text,
    parse_description,
    parse_location,
    parse_template_params,
    parse_index,
)

# Re-export base class
from doxygen_index.parser.base import LanguageParser

# Re-export C++ parser and C++-specific utilities
from doxygen_index.parser.cpp_parser import (
    CppParser,
    normalize_argsstring,
    derive_module,
    derive_source_type,
    detect_template_specialization,
    extract_implementations,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_xml_dir(
    xml_dir: Path,
    source: str = "msd",
    progress_interval: int = 50,
    layer: str = "dependency",
    language_parser: LanguageParser | None = None,
) -> ParseResult:
    """Parse all Doxygen XML in a directory and return a ParseResult.

    Args:
        xml_dir: Directory containing Doxygen XML output (must have index.xml).
        source: Source label for provenance tracking.
        progress_interval: Print progress every N compounds (0 to disable).
        layer: Layer label ("codebase" for project code, "dependency" for deps).
        language_parser: Language-specific parser to use. Defaults to
            :class:`CppParser` for C/C++ Doxygen output. Pass a custom
            :class:`LanguageParser` subclass to handle other languages.

    Returns:
        ParseResult with all parsed data.
    """
    if language_parser is None:
        language_parser = CppParser()

    index_path = xml_dir / "index.xml"
    if not index_path.exists():
        raise FileNotFoundError(f"index.xml not found in {xml_dir}")

    compounds = parse_index(index_path)
    result = ParseResult()

    for i, (refid, kind) in enumerate(compounds):
        xml_file = xml_dir / f"{refid}.xml"
        if xml_file.exists():
            language_parser.parse_compound_file(xml_file, source, result, layer)

        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Parsed {i + 1}/{len(compounds)} XML files...")

    # Post-processing: language-specific cross-referencing
    language_parser.post_process(result)

    return result