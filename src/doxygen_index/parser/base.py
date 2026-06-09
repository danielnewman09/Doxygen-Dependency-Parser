"""
Abstract base class for language-specific source parsing.

A :class:`LanguageParser` defines how to turn a directory of source files
into a :class:`~doxygen_index.parser.model.ParseResult`.  Each language
implements its own discovery and extraction logic (Doxygen XML for C++,
AST for Python, etc.) but produces the same neutral data model.

The contract has two methods:

* :meth:`parse_source_dir` — walk a source directory and populate the
  result with all discovered symbols.
* :meth:`post_process` — run cross-referencing passes after all symbols
  have been extracted (e.g. resolving concept constraints in C++).

C++-specific implementation lives in :class:`~doxygen_index.parser.cpp_parser.CppParser`,
which internally delegates to XML-parsing helpers.  Python support lives
in :class:`~doxygen_index.parser.python_parser.PythonParser`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from doxygen_index.parser.model import ParseResult


class LanguageParser(ABC):
    """Abstract base class for language-specific source parsing.

    A ``LanguageParser`` knows how to discover and extract symbols from a
    directory of source files, populating a shared
    :class:`~doxygen_index.parser.model.ParseResult` that any backend
    (Neo4j, JSON, etc.) can consume.

    Subclasses must implement:

    * :meth:`parse_source_dir` — the main extraction pass.
    * :meth:`post_process` — cross-referencing / resolution pass.

    Usage::

        from doxygen_index.parser import CppParser, parse_xml_dir

        # Doxygen XML (C++)
        result = parse_xml_dir(xml_dir, language_parser=CppParser())

        # Python source (AST)
        from doxygen_index.parser import PythonParser
        parser = PythonParser()
        result = ParseResult()
        parser.parse_source_dir(src_dir, "myproject", result)
        parser.post_process(result)
    """

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_source_dir(
        self,
        source_dir: Path,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
        progress_interval: int = 0,
    ) -> None:
        """Parse all source artifacts in *source_dir* and populate *result*.

        Discovers files in *source_dir*, extracts symbols (classes,
        functions, etc.), and appends them to *result*.

        Args:
            source_dir: Root directory of the source or build output.
                For C++ this is the Doxygen XML directory; for Python
                it's the root of the Python package.
            source: Provenance label (e.g. ``"myproject"``, ``"msd"``).
            result: Accumulator — the caller creates it, subclasses
                populate it.
            layer: Layer label (``"codebase"`` or ``"dependency"``).
            progress_interval: Print progress every N files.  0 disables.
        """
        ...

    @abstractmethod
    def post_process(self, result: ParseResult) -> None:
        """Run language-specific post-processing on *result*.

        Called after :meth:`parse_source_dir` has finished.  Use this
        pass to resolve cross-references that require global knowledge
        (e.g. matching constraint text to known concept names in C++).
        """
        ...