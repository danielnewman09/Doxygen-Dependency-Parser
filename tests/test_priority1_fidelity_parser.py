"""Focused parser tests for Priority-1 as-built fidelity behaviors.

Covers the source-fidelity slices added for the code-only round-trip
completion plan (docs/plans/2026-08-16-priority-1-roundtrip-completion.md,
work package 2):

- nested enums composed by their owning class (with underlying type);
- source-spelled base lists (template specializations must not inherit the
  primary template's bases from Doxygen's XML);
- plain ``//`` doc-comment attachment rules (adjacent attaches; separated
  by a blank line stays residual);
- contiguous in-class bodies extend the owned span; distant out-of-line
  bodies do not (no crossing overlap);
- empty namespace shells + re-opened namespace regions + leading blanks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from doxygen_index.parser import ParseResult
from doxygen_index.parser.cpp_parser import (
    CppParser,
    build_owned_spans,
    extract_residual_source_fragments,
    _source_span,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_source(tmp_path: Path, source: str) -> ParseResult:
    """Parse a source file's Doxygen XML with the real parser."""
    header = tmp_path / "snippet.hpp"
    header.write_text(source, encoding="utf-8")
    lines = source.splitlines()

    def line_of(substr: str, fallback: int) -> int:
        for idx, line in enumerate(lines):
            if substr in line:
                return idx + 1
        return fallback

    color_line = line_of("enum class Color", 0)
    ctor_line = line_of("= delete", 0)
    run_line = line_of("void run()", 0)
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir(exist_ok=True)
    (xml_dir / "index.xml").write_text(
        '<doxygenindex><compound refid="_snippet_8hpp" kind="file">'
        "<name>snippet.hpp</name></compound>"
        '<compound refid="namespacens" kind="namespace">'
        "<name>ns</name></compound>"
        '<compound refid="structns_1_1_widget" kind="class">'
        "<name>ns::Widget</name></compound></doxygenindex>",
        encoding="utf-8",
    )
    (xml_dir / "_snippet_8hpp.xml").write_text(
        '<doxygen><compounddef id="_snippet_8hpp" kind="file" language="C++">'
        f"<compoundname>snippet.hpp</compoundname>"
        f'<location file="{header}"/>'
        "</compounddef></doxygen>",
        encoding="utf-8",
    )
    (xml_dir / "namespacens.xml").write_text(
        '<doxygen><compounddef id="namespacens" kind="namespace">'
        "<compoundname>ns</compoundname>"
        f'<location file="{header}" line="1"/>'
        "</compounddef></doxygen>",
        encoding="utf-8",
    )
    (xml_dir / "structns_1_1_widget.xml").write_text(
        '<doxygen><compounddef id="structns_1_1_widget" kind="class">'
        "<compoundname>ns::Widget</compoundname>"
        f'<location file="{header}" line="{line_of("class Widget", 2)}"/>'
        "<sectiondef kind=\"public-type\">"
        '{% if color_line %}<memberdef kind="enum" id="widget_1a_color" strong="yes">'
        "<type>uint8_t</type><name>Color</name>"
        "<qualifiedname>ns::Widget::Color</qualifiedname>"
        '<enumvalue id="widget_1a_color_1"><name>RED</name></enumvalue>'
        '<enumvalue id="widget_1a_color_2"><name>GREEN</name></enumvalue>'
        f'<location file="{header}" line="{color_line}" bodyfile="{header}" '
        f'bodystart="{color_line + 1}" bodyend="{color_line + 3}"/>'
        "</memberdef>{% endif %}</sectiondef>"
        "<sectiondef kind=\"public-func\">"
        '<memberdef kind="function" id="widget_1a_ctor" static="no">'
        "<type></type><name>Widget</name><argsstring>(const Widget &amp;)</argsstring>"
        f'<location file="{header}" line="{ctor_line}"/>'
        "</memberdef>"
        '{% if run_line %}<memberdef kind="function" id="widget_1a_run" static="no">'
        "<type>void</type><name>run</name><argsstring>()</argsstring>"
        f'<location file="{header}" line="{run_line}"/>'
        "</memberdef>{% endif %}</sectiondef>"
        "</compounddef></doxygen>",
        encoding="utf-8",
    )
    result = ParseResult()
    CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
    return result


# ---------------------------------------------------------------------------
# WP2.3 — nested enums
# ---------------------------------------------------------------------------

class TestNestedEnum:
    def test_nested_enum_is_composed_by_owning_class(self, tmp_path):
        result = _parse_source(tmp_path, (
            "namespace ns {\n"
            "class Widget\n"
            "{\n"
            "public:\n"
            "  /*!\n"
            "   * Palette of widget colors\n"
            "   */\n"
            "  enum class Color : uint8_t\n"
            "  {\n"
            "    RED,\n"
            "    GREEN\n"
            "  };\n"
            "\n"
            "  void run()\n"
            "  {\n"
            "  }\n"
            "};\n"
            "}\n"
        ))
        enum = next(e for e in result.enums if e.qualified_name == "ns::Widget::Color")
        widget = next(c for c in result.classes if c.qualified_name == "ns::Widget")
        # composed by the class refid, not a synthetic namespace
        assert enum.compound_refid == widget.refid
        assert enum.underlying_type == "uint8_t"
        assert enum.enum_class is True
        assert enum.visibility == "public"
        # the enum owns its doc comment through its closing brace
        assert enum.start_line == 5
        assert enum.end_line == 11
        # enumerators are composed by the enum itself
        colors = [v for v in result.enum_values if v.qualified_name.startswith("ns::Widget::Color")]
        assert [v.name for v in colors] == ["RED", "GREEN"]
        assert all(v.compound_refid == enum.refid for v in colors)
        # no synthetic namespace named Widget
        assert not any(ns.qualified_name == "Widget" for ns in result.namespaces)

    def test_nested_enum_span_does_not_leak_into_fragments(self, tmp_path):
        result = _parse_source(tmp_path, (
            "namespace ns {\n"
            "class Widget\n"
            "{\n"
            "public:\n"
            "  enum class Color : uint8_t\n"
            "  {\n"
            "    RED\n"
            "  };\n"
            "};\n"
            "}\n"
        ))
        path = next(c.file_path for c in result.classes if c.qualified_name == "ns::Widget")
        spans, problems = build_owned_spans(path, result)
        assert problems == []
        fragments = extract_residual_source_fragments(path, spans)
        assert all("Color" not in f.text for f in fragments)


# ---------------------------------------------------------------------------
# WP2.2 — source-spelled base lists
# ---------------------------------------------------------------------------

class TestBaseSpecifiers:
    def test_specialization_keeps_only_source_bases(self, tmp_path):
        """Doxygen lists the primary template's bases on a specialization
        (``IsVector<std::vector<T>>`` reports both ``std::false_type`` and
        ``std::true_type``).  The source declaration is authoritative."""
        header = tmp_path / "traits.hpp"
        source = (
            "namespace ns {\n"
            "template <typename T>\n"
            "struct IsVector : std::false_type\n"
            "{\n"
            "};\n"
            "\n"
            "template <typename T, typename A>\n"
            "struct IsVector<std::vector<T, A>> : std::true_type\n"
            "{\n"
            "};\n"
            "}\n"
        )
        header.write_text(source, encoding="utf-8")
        xml_dir = tmp_path / "xml"
        xml_dir.mkdir(exist_ok=True)
        (xml_dir / "index.xml").write_text(
            '<doxygenindex><compound refid="namespacens" kind="namespace">'
            "<name>ns</name></compound>"
            '<compound refid="structns_1_1_is_vector" kind="struct">'
            "<name>ns::IsVector</name></compound>"
            '<compound refid="structns_1_1_is_vector_3_01std_1_1vector_3_01_t_00_01_a_01_4_01_4" kind="struct">'
            "<name>ns::IsVector&lt; std::vector&lt; T, A &gt; &gt;</name></compound>"
            "</doxygenindex>", encoding="utf-8",
        )
        (xml_dir / "namespacens.xml").write_text(
            '<doxygen><compounddef id="namespacens" kind="namespace">'
            "<compoundname>ns</compoundname>"
            f'<location file="{header}" line="1"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        # Doxygen's quirk: the specialization's XML carries BOTH bases.
        (xml_dir / "structns_1_1_is_vector.xml").write_text(
            '<doxygen><compounddef id="structns_1_1_is_vector" kind="struct">'
            "<compoundname>ns::IsVector</compoundname>"
            '<basecompoundref prot="public" virt="non-virtual">std::false_type</basecompoundref>'
            f'<location file="{header}" line="3"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        (xml_dir / "structns_1_1_is_vector_3_01std_1_1vector_3_01_t_00_01_a_01_4_01_4.xml").write_text(
            '<doxygen><compounddef id="structns_1_1_is_vector_3_01std_1_1vector_3_01_t_00_01_a_01_4_01_4" kind="struct">'
            "<compoundname>ns::IsVector&lt; std::vector&lt; T, A &gt; &gt;</compoundname>"
            '<basecompoundref prot="public" virt="non-virtual">std::false_type</basecompoundref>'
            '<basecompoundref prot="public" virt="non-virtual">std::true_type</basecompoundref>'
            f'<location file="{header}" line="8"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        result = ParseResult()
        CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
        spec = next(
            c for c in result.classes
            if c.qualified_name == "ns::IsVector< std::vector< T, A > >"
        )
        assert spec.base_specifiers == ["std::true_type"]
        primary = next(c for c in result.classes if c.qualified_name == "ns::IsVector")
        assert primary.base_specifiers == ["std::false_type"]

    def test_class_base_spelling_keeps_public(self, tmp_path):
        header = tmp_path / "err.hpp"
        header.write_text(
            "namespace ns {\nclass TransactionError : public std::runtime_error\n{\n};\n}\n",
            encoding="utf-8",
        )
        xml_dir = tmp_path / "xml"
        xml_dir.mkdir(exist_ok=True)
        (xml_dir / "index.xml").write_text(
            '<doxygenindex><compound refid="namespacens" kind="namespace">'
            "<name>ns</name></compound>"
            '<compound refid="classns_1_1_transaction_error" kind="class">'
            "<name>ns::TransactionError</name></compound>"
            "</doxygenindex>", encoding="utf-8",
        )
        (xml_dir / "namespacens.xml").write_text(
            '<doxygen><compounddef id="namespacens" kind="namespace">'
            "<compoundname>ns</compoundname>"
            f'<location file="{header}" line="1"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        (xml_dir / "classns_1_1_transaction_error.xml").write_text(
            '<doxygen><compounddef id="classns_1_1_transaction_error" kind="class">'
            "<compoundname>ns::TransactionError</compoundname>"
            '<basecompoundref prot="public" virt="non-virtual">std::runtime_error</basecompoundref>'
            f'<location file="{header}" line="2"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        result = ParseResult()
        CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
        cls = next(c for c in result.classes if c.qualified_name == "ns::TransactionError")
        assert cls.base_specifiers == ["public std::runtime_error"]


# ---------------------------------------------------------------------------
# WP2.5 / ownership — contiguous vs distant bodies
# ---------------------------------------------------------------------------

class TestBodySpanContiguity:
    def test_distant_out_of_line_body_does_not_extend_declaration_span(self, tmp_path):
        """A same-file out-of-line definition must not glue onto the
        declaration span (it would swallow unrelated code and produce a
        crossing ownership overlap with the enclosing class)."""
        header = tmp_path / "db.hpp"
        source = (
            "namespace ns {\n"
            "class Database\n"
            "{\n"
            "public:\n"
            "  void run(Func&& f);\n"
            "};\n"
            "\n"
            "template <typename Func>\n"
            "void Database::run(Func&& f)\n"
            "{\n"
            "  f();\n"
            "}\n"
            "}\n"
        )
        header.write_text(source, encoding="utf-8")
        result = ParseResult()
        result.classes.append(_FakeClass(
            refid="class_a", qualified_name="ns::Database",
            file_path=str(header), line_number=3,
            start_line=3, end_line=6,
        ))
        result.methods.append(_FakeMethod(
            refid="method_a", qualified_name="ns::Database::run",
            file_path=str(header), line_number=5,
            start_line=5, end_line=5,
            body_start=9, body_end=12, body_file=str(header), body="void Database::run(Func&& f)\n{\n  f();\n}",
        ))
        spans, problems = build_owned_spans(header, result)
        # The method span is its declaration only (5), never 5-12.
        method_spans = [
            s for s in spans if s.owner == "ns::Database::run" and s.end == 5
        ]
        assert (method_spans[0].start, method_spans[0].end) == (5, 5)
        assert any(
            s.owner == "ns::Database::run" and s.start == 9 and s.end == 12
            for s in spans
        ), "the out-of-line body span must be owned separately"
        assert problems == []

    def test_contiguous_in_class_body_extends_span(self, tmp_path):
        header = tmp_path / "logger.hpp"
        header.write_text(
            "class Logger\n{\npublic:\n  static Logger& getInstance()\n  {\n    static Logger i;\n    return i;\n  }\n};\n",
            encoding="utf-8",
        )
        start, end = _source_span(
            str(header), 4,
            body_file=str(header), body_start=4, body_end=8,
        )
        assert (start, end) == (4, 8)


# ---------------------------------------------------------------------------
# WP2.1 / WP3.4 — namespace shells, regions, leading blanks
# ---------------------------------------------------------------------------

class TestNamespaceLayout:
    def test_empty_namespace_shell_name(self, tmp_path):
        from doxygen_index.parser.cpp_parser import _top_level_namespace_name
        cpp = tmp_path / "empty.cpp"
        cpp.write_text(
            '#include "x.hpp"\n\nnamespace cpp_sqlite\n{\n\n\n}\n', encoding="utf-8"
        )
        assert _top_level_namespace_name(cpp) == "cpp_sqlite"

    def test_reopened_namespace_regions_are_separate(self, tmp_path):
        from doxygen_index.parser.cpp_parser import extract_namespace_regions
        header = tmp_path / "db.hpp"
        header.write_text(
            "namespace cpp_sqlite\n{\nclass A {};\n}\n\n"
            "// file-level comment\n#include \"dep.hpp\"\n\n"
            "namespace cpp_sqlite\n{\n\ntemplate <typename T>\nvoid f();\n}\n",
            encoding="utf-8",
        )
        regions = extract_namespace_regions(header)
        assert len(regions) == 2
        assert regions[0]["name"] == "cpp_sqlite"
        assert regions[0]["open_line"] == 1 and regions[0]["close_line"] == 4
        assert regions[1]["open_line"] == 9 and regions[1]["close_line"] == 14
        assert regions[1]["leading_blank_lines"] == 1

    def test_leading_blank_lines_before_guard(self, tmp_path):
        from doxygen_index.parser.cpp_parser import extract_leading_blank_lines
        header = tmp_path / "db.hpp"
        header.write_text("\n#ifndef DB_HPP\n#define DB_HPP\n#endif\n", encoding="utf-8")
        assert extract_leading_blank_lines(header) == 1


# ---------------------------------------------------------------------------
# WP3.2 — plain // comment attachment
# ---------------------------------------------------------------------------

class TestPlainCommentAttachment:
    def test_adjacent_plain_comment_attaches(self, tmp_path):
        result = _parse_source(tmp_path, (
            "namespace ns {\n"
            "class Widget\n"
            "{\n"
            "public:\n"
            "  // Delete copy constructor\n"
            "  Widget(const Widget&) = delete;\n"
            "};\n"
            "}\n"
        ))
        method = next(
            m for m in result.methods if m.qualified_name.startswith("ns::Widget::Widget")
        )
        assert "// Delete copy constructor" in method.source_documentation

    def test_plain_comment_separated_by_blank_stays_residual(self, tmp_path):
        """A ``// --- section header ---`` followed by a blank line is an
        ordinary comment, not a doc comment: it stays a residual at
        namespace level instead of attaching to the next declaration."""
        header = tmp_path / "traits.hpp"
        source = (
            "namespace ns {\n"
            "\n"
            "// --- Basic type concepts ---\n"
            "\n"
            "template <typename T>\n"
            "concept isIntegral = std::integral<T>;\n"
            "}\n"
        )
        header.write_text(source, encoding="utf-8")
        xml_dir = tmp_path / "xml"
        xml_dir.mkdir(exist_ok=True)
        (xml_dir / "index.xml").write_text(
            '<doxygenindex><compound refid="namespacens" kind="namespace">'
            "<name>ns</name></compound>"
            '<compound refid="conceptns_1_1is_integral" kind="concept">'
            "<name>ns::isIntegral</name></compound>"
            "</doxygenindex>", encoding="utf-8",
        )
        (xml_dir / "namespacens.xml").write_text(
            '<doxygen><compounddef id="namespacens" kind="namespace">'
            "<compoundname>ns</compoundname>"
            f'<location file="{header}" line="1"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        (xml_dir / "conceptns_1_1is_integral.xml").write_text(
            '<doxygen><compounddef id="conceptns_1_1is_integral" kind="concept">'
            "<compoundname>ns::isIntegral</compoundname>"
            '<initializer>template&lt;typename T&gt; concept ns::isIntegral = std::integral&lt;T&gt;</initializer>'
            f'<location file="{header}" line="6"/></compounddef></doxygen>',
            encoding="utf-8",
        )
        result = ParseResult()
        CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
        concept = next(c for c in result.concepts if c.qualified_name == "ns::isIntegral")
        assert concept.source_documentation == ""
        spans, problems = build_owned_spans(header, result)
        assert problems == []
        fragments = extract_residual_source_fragments(str(header), spans)
        assert any("Basic type concepts" in f.text for f in fragments)


class _FakeClass:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMethod(_FakeClass):
    pass
