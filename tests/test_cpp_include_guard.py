from pathlib import Path

from doxygen_index.parser.cpp_parser import (
    CppParser,
    extract_include_directives,
    extract_include_guard,
    extract_namespace_padding,
    extract_preceding_doc_comment,
    extract_residual_source_fragments,
)
from doxygen_index.parser.model import ParseResult
from doxygen_index.graph_json import result_to_graph_json
from codegraph import SourceFragmentNode


def test_extract_include_guard_preserves_exact_macro(tmp_path: Path):
    header = tmp_path / "Widget.hpp"
    header.write_text(
        "// copyright\n#ifndef PROJECT_WIDGET_HPP\n"
        "#define PROJECT_WIDGET_HPP\nclass Widget {};\n#endif\n",
        encoding="utf-8",
    )
    assert extract_include_guard(header) == "PROJECT_WIDGET_HPP"


def test_extract_include_guard_rejects_mismatched_define(tmp_path: Path):
    header = tmp_path / "Widget.hpp"
    header.write_text(
        "#ifndef PROJECT_WIDGET_HPP\n#define OTHER_WIDGET_HPP\n",
        encoding="utf-8",
    )
    assert extract_include_guard(header) == ""


def test_extract_include_guard_returns_empty_for_source(tmp_path: Path):
    source = tmp_path / "Widget.cpp"
    source.write_text('#include "Widget.hpp"\n', encoding="utf-8")
    assert extract_include_guard(source) == ""


def test_extract_include_directives_preserves_order_and_spelling(tmp_path: Path):
    source = tmp_path / "Widget.cpp"
    source.write_text(
        "#include <vector>\n\n#include \"Widget.hpp\"  // local\n",
        encoding="utf-8",
    )
    assert extract_include_directives(source) == ["<vector>", "", '"Widget.hpp"']


def test_extract_namespace_padding_preserves_inner_blank_lines(tmp_path: Path):
    source = tmp_path / "Widget.cpp"
    source.write_text(
        "namespace sample\n{\n\nvoid f() {}\n\n}  // namespace sample\n",
        encoding="utf-8",
    )
    assert extract_namespace_padding(source) == (1, 1)


def test_extract_namespace_padding_handles_no_inner_blank_lines(tmp_path: Path):
    source = tmp_path / "Widget.cpp"
    source.write_text(
        "namespace sample\n{\nvoid f() {}\n}  // namespace sample\n",
        encoding="utf-8",
    )
    assert extract_namespace_padding(source) == (0, 0)


def test_extract_preceding_doc_comment_preserves_block_syntax(tmp_path: Path):
    header = tmp_path / "Widget.hpp"
    header.write_text(
        "/*!\\brief Exact source documentation */\nstruct Widget {};\n",
        encoding="utf-8",
    )
    assert extract_preceding_doc_comment(header, 2) == (
        "/*!\\brief Exact source documentation */\n"
    )


def test_residual_fragments_capture_unowned_multiline_macro_with_metadata(tmp_path: Path):
    header = tmp_path / "Widget.hpp"
    header.write_text(
        "namespace sample\n{\nstruct Widget {};\n\n"
        "// registration\nREGISTER_WIDGET(Widget,\n                option);\n}\n",
        encoding="utf-8",
    )
    fragments = extract_residual_source_fragments(header, [(3, 3)], source="demo")
    assert len(fragments) == 1
    fragment = fragments[0]
    assert fragment.file_path == str(header)
    assert (fragment.start_line, fragment.end_line) == (5, 7)
    assert fragment.placement == "sample"
    assert fragment.text == "// registration\nREGISTER_WIDGET(Widget,\n                option);\n"


def test_residual_fragments_capture_non_macro_pragma(tmp_path: Path):
    source = tmp_path / "Widget.cpp"
    source.write_text("#include <vector>\n#pragma clang diagnostic push\nvoid f() {}\n", encoding="utf-8")
    fragments = extract_residual_source_fragments(source, [(3, 3)])
    assert [(f.start_line, f.end_line, f.text) for f in fragments] == [
        (2, 2, "#pragma clang diagnostic push\n"),
    ]


def test_parse_source_dir_populates_residual_fragment_metadata(tmp_path: Path, monkeypatch):
    """The parser attaches residuals to real file ingestion, not only helpers."""
    source = tmp_path / "src" / "Widget.hpp"
    source.parent.mkdir()
    source.write_text(
        "namespace sample\n{\n#pragma clang diagnostic push\n}\n",
        encoding="utf-8",
    )
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()
    (xml_dir / "index.xml").write_text(
        '<doxygenindex><compound refid="_widget_8hpp" kind="file">'
        '<name>Widget.hpp</name></compound></doxygenindex>',
        encoding="utf-8",
    )
    (xml_dir / "_widget_8hpp.xml").write_text(
        '<doxygen><compounddef id="_widget_8hpp" kind="file" language="C++">'
        '<compoundname>Widget.hpp</compoundname>'
        '<location file="src/Widget.hpp"/>'
        '</compounddef></doxygen>',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = ParseResult()
    CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
    assert len(result.source_fragments) == 1
    fragment = result.source_fragments[0]
    assert fragment.text == "#pragma clang diagnostic push\n"
    assert fragment.placement == "sample"
    assert fragment.source == "demo"


def test_residual_fragment_survives_graph_json_serialization():
    fragment = SourceFragmentNode(
        qualified_name="src/Widget.hpp#4-4",
        file_path="src/Widget.hpp",
        start_line=4,
        end_line=4,
        placement="sample",
        text="#pragma once\n",
        source="demo",
    )
    graph = result_to_graph_json(ParseResult(source_fragments=[fragment]), source="demo")
    entry = next(item for item in graph if item["type"] == "SourceFragmentNode")
    assert entry["text"] == "#pragma once\n"
    assert entry["placement"] == "sample"
