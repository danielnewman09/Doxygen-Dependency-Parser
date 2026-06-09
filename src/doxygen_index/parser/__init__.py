"""
Doxygen XML parser — extracts symbols, documentation, and relationships.

Parses Doxygen-generated XML files into a neutral data model (dataclasses)
that can be consumed by any backend (e.g. Neo4j).  Also supports direct
Python source parsing via :class:`PythonParser`.

This package provides:

* :class:`LanguageParser` — abstract base class for language-specific parsers.
* :class:`CppParser` — concrete parser for C/C++ Doxygen output (default).
* :class:`PythonParser` — concrete parser for Python source files.
* :func:`parse_xml_dir` — parse Doxygen XML directory (C/C++).
* :func:`parse_python_dir` — parse Python source directory.
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

# Re-export Python parser
from doxygen_index.parser.python_parser import PythonParser


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

    result = ParseResult()
    language_parser.parse_source_dir(xml_dir, source, result, layer, progress_interval)
    language_parser.post_process(result)
    return result


def parse_python_dir(
    source_dir: Path,
    source: str = "python",
    progress_interval: int = 50,
    layer: str = "codebase",
    language_parser: PythonParser | None = None,
) -> ParseResult:
    """Parse all Python source files in a directory and return a ParseResult.

    Args:
        source_dir: Root directory of the Python package to parse.
        source: Source label for provenance tracking.
        progress_interval: Print progress every N files (0 to disable).
        layer: Layer label ("codebase" or "dependency").
        language_parser: Python parser instance. Defaults to a new
            :class:`PythonParser`.

    Returns:
        ParseResult with all parsed data.
    """
    if language_parser is None:
        language_parser = PythonParser()

    result = ParseResult()
    language_parser.parse_source_dir(source_dir, source, result, layer, progress_interval)
    language_parser.post_process(result)
    return result