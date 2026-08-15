from pathlib import Path

from doxygen_index.parser.cpp_parser import (
    extract_include_directives,
    extract_include_guard,
    extract_namespace_padding,
)


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
