"""
Abstract base class for language-specific Doxygen XML parsing.

Each programming language that Doxygen can document has its own set of
compound and member kinds, naming conventions, and post-processing needs.
Subclass :class:`LanguageParser` to handle a new language; implementers
need only fill in the methods for the kinds they support.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path

from codegraph import FileNode, NamespaceNode

from doxygen_index.parser.model import IncludeEntry, ParseResult


class LanguageParser(ABC):
    """Abstract base class for language-specific Doxygen XML parsing.

    A ``LanguageParser`` knows how to interpret the XML elements that
    Doxygen produces for a particular programming language.  The core
    extension points are:

    * :meth:`parse_compound` — handle a ``<compounddef>`` element for a
      type compound (class, struct, concept, enum, …).
    * :meth:`parse_member` — handle a ``<memberdef>`` element (method,
      variable, …).
    * :meth:`post_process` — language-specific cross-referencing pass
      after all files are parsed.

    The base class also handles ``file`` and ``namespace`` compound kinds
    directly in :meth:`parse_compound_file`, because those are not
    language-specific.

    Usage::

        from doxygen_index.parser import CppParser, parse_xml_dir

        result = parse_xml_dir(xml_dir, language_parser=CppParser())
    """

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def parse_compound(
        self,
        compounddef: ET.Element,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> str | None:
        """Parse a type ``<compounddef>`` element and add entries to *result*.

        This is called for every compound that is *not* a ``file`` or
        ``namespace`` (those are handled by the base class).

        Args:
            compounddef: The XML element for the compound.
            source: Source label for provenance tracking.
            result: Accumulator for parsed entries.
            layer: Layer label (``"codebase"`` or ``"dependency"``).

        Returns:
            The ``qualified_name`` of the parsed compound, or ``None``
            if the compound kind is not handled by this parser.
        """
        ...

    @abstractmethod
    def parse_member(
        self,
        memberdef: ET.Element,
        compound_refid: str,
        parent_qualified_name: str,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> None:
        """Parse a ``<memberdef>`` element and add entries to *result*.

        Args:
            memberdef: The XML element for the member.
            compound_refid: The refid of the containing compound.
            parent_qualified_name: Qualified name of the parent compound.
            source: Source label for provenance tracking.
            result: Accumulator for parsed entries.
            layer: Layer label.
        """
        ...

    @abstractmethod
    def post_process(self, result: ParseResult) -> None:
        """Run language-specific post-processing on *result*.

        Called after all XML files in a directory have been parsed.
        Use this pass to resolve cross-references (e.g. matching
        constraint text to known concept names).
        """
        ...

    # ------------------------------------------------------------------
    # Concrete: full compound file parsing
    # ------------------------------------------------------------------

    def parse_compound_file(
        self,
        xml_path: Path,
        source: str,
        result: ParseResult,
        layer: str = "dependency",
    ) -> None:
        """Parse a compound XML file, iterating over ``<compounddef>`` elements.

        For each ``<compounddef>``:

        * ``file`` and ``namespace`` kinds are handled directly
          (language-agnostic).
        * All other kinds are delegated to :meth:`parse_compound`.
          If it returns a qualified name, members within
          ``<sectiondef>`` elements are then processed via
          :meth:`parse_member`.
        """
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"Warning: Could not parse {xml_path}: {e}", file=sys.stderr)
            return

        for compounddef in root.findall(".//compounddef"):
            refid = compounddef.get("id", "")
            kind = compounddef.get("kind", "")
            compoundname = compounddef.findtext("compoundname", "")

            # --- Files (language-agnostic) ---
            if kind == "file":
                self._parse_file_compound(compounddef, refid, compoundname, source, result)
                continue

            # --- Namespaces (language-agnostic) ---
            if kind == "namespace":
                self._parse_namespace_compound(compounddef, refid, compoundname, source, result, layer)
                continue

            # --- Language-specific type compound ---
            qualified_name = self.parse_compound(compounddef, source, result, layer)
            if qualified_name is None:
                print(
                    f"Warning: Unknown compound kind '{kind}' for refid={refid}, skipping",
                    file=sys.stderr,
                )
                continue

            # --- Parse members (shared across compound types) ---
            for sectiondef in compounddef.findall("sectiondef"):
                for memberdef in sectiondef.findall("memberdef"):
                    self.parse_member(
                        memberdef, refid, qualified_name, source, result, layer,
                    )

    # ------------------------------------------------------------------
    # Language-agnostic compound handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_file_compound(
        compounddef: ET.Element,
        refid: str,
        compoundname: str,
        source: str,
        result: ParseResult,
    ) -> None:
        """Parse a ``kind="file"`` compounddef."""
        loc = compounddef.find("location")
        file_path = loc.get("file") if loc is not None else None
        language = compounddef.get("language", "")

        result.files.append(FileNode(
            refid=refid,
            name=compoundname,
            path=file_path or "",
            language=language,
            source=source,
        ))

        for inc in compounddef.findall("includes"):
            result.includes.append(IncludeEntry(
                file_refid=refid,
                included_file=inc.text or "",
                included_refid=inc.get("refid") or "",
                is_local=inc.get("local") == "yes",
            ))

    @staticmethod
    def _parse_namespace_compound(
        compounddef: ET.Element,
        refid: str,
        compoundname: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Parse a ``kind="namespace"`` compounddef."""
        name = compoundname.split("::")[-1] if "::" in compoundname else compoundname

        result.namespaces.append(NamespaceNode(
            refid=refid,
            name=name,
            qualified_name=compoundname,
            source=source,
            layer=layer,
        ))