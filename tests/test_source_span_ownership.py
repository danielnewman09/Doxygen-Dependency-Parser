"""Source-span ownership: Phase 1–4 of the residual-source-span plan.

Covers:

* Phase 1 — inclusive ``start_line``/``end_line`` metadata on every
  structured node that owns C++ source (file, namespace, compounds,
  members), populated from Doxygen locations and derived end boundaries.
* Phase 2 — ``build_owned_spans``: the complete ordered ownership map for
  a file, with safe nested merging and overlap reporting.
* Phase 3 — pure residual subtraction: ``extract_residual_source_fragments``
  emits one ``SourceFragmentNode`` per remaining non-whitespace run with no
  macro-name or casing rules.
* Phase 4.1 — fragment metadata survives parse → graph JSON → LayerGraph.
"""

from __future__ import annotations

from pathlib import Path

from codegraph import (
    AttributeNode,
    ClassNode,
    SourceFragmentNode,
)

from doxygen_index.parser.cpp_parser import (
    CppParser,
    OwnedSpan,
    build_owned_spans,
    extract_residual_source_fragments,
    _source_span,
    _derive_span_end,
    _normalize_ownership,
)
from doxygen_index.parser.model import ParseResult
from doxygen_index.graph_json import result_to_graph_json


# ---------------------------------------------------------------------------
# Fixture: a namespace, a documented struct, an attribute, an inline method,
# and a free function — every construct the plan's Phase 1.2 fixture lists.
# ---------------------------------------------------------------------------

FIXTURE_SOURCE = """/** A free function. */
int compute(int value)
{
  return value * 2;
}

namespace sample
{

/** A documented struct. */
struct Widget
{
  /** The id. */
  int id = 0;

  /** Inline method. */
  void reset()
  {
    id = 0;
  }
};

}  // namespace sample
"""

# 1-based line map for the fixture above (assertions reference these).
L_COMPUTE_DOC = 1
L_COMPUTE = 2
L_COMPUTE_END = 5
L_NS_OPEN = 7
L_NS_BRACE = 8
L_WIDGET_DOC = 10
L_WIDGET = 11
L_ID_DOC = 13
L_ID = 14
L_RESET_DOC = 16
L_RESET = 17
L_RESET_END = 20
L_NS_CLOSE = 23


def _write_fixture(tmp_path: Path) -> Path:
    header = tmp_path / "fixture.hpp"
    header.write_text(FIXTURE_SOURCE, encoding="utf-8")
    return header


def _write_xml(tmp_path: Path, header: Path) -> Path:
    """Write the Doxygen XML tree for the fixture and return the xml dir."""
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir(exist_ok=True)
    rel = header.name
    abs_rel = str(header)
    (xml_dir / "index.xml").write_text(
        '<doxygenindex><compound refid="_fixture_8hpp" kind="file">'
        f"<name>{rel}</name></compound>"
        '<compound refid="namespacesample" kind="namespace">'
        "<name>sample</name></compound>"
        '<compound refid="structsample_1_1_widget" kind="class">'
        "<name>sample::Widget</name></compound></doxygenindex>",
        encoding="utf-8",
    )
    (xml_dir / "_fixture_8hpp.xml").write_text(
        '<doxygen><compounddef id="_fixture_8hpp" kind="file" language="C++">'
        f"<compoundname>{rel}</compoundname>"
        f'<location file="{abs_rel}"/>'
        "<sectiondef><memberdef kind=\"function\" id=\"compute_1a\">"
        "<name>compute</name><argsstring>(int value)</argsstring>"
        '<type>int</type>'
        f'<location file="{abs_rel}" line="{L_COMPUTE}" bodystart="{L_COMPUTE}" bodyend="{L_COMPUTE_END}"/>'
        "</memberdef></sectiondef></compounddef></doxygen>",
        encoding="utf-8",
    )
    (xml_dir / "namespacesample.xml").write_text(
        '<doxygen><compounddef id="namespacesample" kind="namespace">'
        "<compoundname>sample</compoundname>"
        f'<location file="{abs_rel}" line="{L_NS_OPEN}"/>'
        "</compounddef></doxygen>",
        encoding="utf-8",
    )
    (xml_dir / "structsample_1_1_widget.xml").write_text(
        '<doxygen><compounddef id="structsample_1_1_widget" kind="class">'
        "<compoundname>sample::Widget</compoundname>"
        f'<location file="{abs_rel}" line="{L_WIDGET}"/>'
        "<sectiondef><memberdef kind=\"variable\" id=\"widget_1a_id\">"
        "<name>id</name><type>int</type>"
        f'<location file="{abs_rel}" line="{L_ID}"/>'
        "</memberdef><memberdef kind=\"function\" id=\"widget_1a_reset\">"
        "<name>reset</name><argsstring>()</argsstring><type>void</type>"
        f'<location file="{abs_rel}" line="{L_RESET}" bodystart="{L_RESET}" bodyend="{L_RESET_END}"/>'
        "</memberdef></sectiondef></compounddef></doxygen>",
        encoding="utf-8",
    )
    return xml_dir


def _parse_fixture(tmp_path: Path) -> tuple[ParseResult, Path]:
    """Parse the fixture with the real CppParser; return (result, header)."""
    header = _write_fixture(tmp_path)
    xml_dir = _write_xml(tmp_path, header)
    result = ParseResult()
    CppParser().parse_source_dir(xml_dir, source="demo", result=result, layer="as-built")
    return result, header


# ---------------------------------------------------------------------------
# Phase 1 — source-span metadata
# ---------------------------------------------------------------------------


class TestSourceSpanMetadata:
    def test_structured_nodes_carry_inclusive_spans(self, tmp_path: Path):
        result, header = _parse_fixture(tmp_path)

        compute = next(n for n in result.functions if n.qualified_name == "compute")
        assert (compute.start_line, compute.end_line) == (L_COMPUTE_DOC, L_COMPUTE_END)

        ns = next(n for n in result.namespaces if n.qualified_name == "sample")
        assert (ns.file_path, ns.start_line, ns.end_line) == (
            str(header), L_NS_OPEN, L_NS_CLOSE,
        )

        widget = next(n for n in result.classes if n.qualified_name == "sample::Widget")
        assert (widget.start_line, widget.end_line) == (L_WIDGET_DOC, 21)

        ident = next(n for n in result.attributes if n.name == "id")
        assert (ident.start_line, ident.end_line) == (L_ID_DOC, L_ID)

        reset = next(n for n in result.methods if n.name == "reset")
        assert (reset.start_line, reset.end_line) == (L_RESET_DOC, L_RESET_END)

        file_node = next(n for n in result.files if n.path == str(header))
        assert (file_node.start_line, file_node.end_line) == (1, L_NS_CLOSE)

    def test_child_spans_lie_within_parent_spans(self, tmp_path: Path):
        result, _header = _parse_fixture(tmp_path)
        ns = next(n for n in result.namespaces if n.qualified_name == "sample")
        widget = next(n for n in result.classes if n.qualified_name == "sample::Widget")

        for child in result.methods + result.attributes:
            assert ns.start_line <= child.start_line <= child.end_line <= ns.end_line, (
                f"{child.qualified_name} escapes namespace span"
            )
        for child in result.methods + result.attributes:
            assert widget.start_line <= child.start_line and child.end_line <= widget.end_line, (
                f"{child.qualified_name} escapes Widget span"
            )

    def test_span_end_derivation_handles_multiline_declarations(self, tmp_path: Path):
        source = tmp_path / "multi.hpp"
        source.write_text(
            "std::optional<int> lookup(\n"
            "    const std::string& key,\n"
            "    bool cache) const;\n",
            encoding="utf-8",
        )
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        assert _derive_span_end(lines, 1) == 3

    def test_span_end_derivation_ignores_braces_in_comments_and_strings(self, tmp_path: Path):
        source = tmp_path / "tricky.hpp"
        source.write_text(
            'const char* note = "};";\n'
            "/* a { comment */\n"
            "struct Tricky { };\n",
            encoding="utf-8",
        )
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        assert _derive_span_end(lines, 1) == 1  # string braces ignored
        assert _derive_span_end(lines, 3) == 3

    def test_source_span_includes_template_prefix(self, tmp_path: Path):
        source = tmp_path / "tpl.hpp"
        source.write_text(
            "template <typename T>\n"
            "class Box { };\n",
            encoding="utf-8",
        )
        span = _source_span(str(source), 2)
        assert span == (1, 2)

    def test_source_span_captures_define_continuations(self, tmp_path: Path):
        source = tmp_path / "def.hpp"
        source.write_text(
            "#define GREET(name) \\\n"
            "  do { \\\n"
            "    hi(name); \\\n"
            "  } while (0)\n",
            encoding="utf-8",
        )
        lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
        assert _derive_span_end(lines, 1) == 4


class TestDocCommentAssociation:
    """Review fix 1: identical doc comments must attach to the declaration
    they precede, never to an earlier copy of the same text."""

    def test_identical_doc_comments_attach_to_nearest_declaration(self, tmp_path: Path):
        source = tmp_path / "identical.hpp"
        source.write_text(
            "/** Same. */\n"
            "int first;\n"
            "\n"
            "/** Same. */\n"
            "int second;\n",
            encoding="utf-8",
        )
        doc = "/** Same. */\n"
        assert _source_span(str(source), 2, doc_comment=doc) == (1, 2)
        assert _source_span(str(source), 5, doc_comment=doc) == (4, 5)

    def test_identical_docs_do_not_leak_declarations_into_fragments(self, tmp_path: Path):
        source = tmp_path / "identical.hpp"
        source.write_text(
            "/** Same. */\n"
            "int first;\n"
            "\n"
            "/** Same. */\n"
            "int second;\n",
            encoding="utf-8",
        )
        result = ParseResult()
        doc = "/** Same. */\n"
        for line, name in ((2, "first"), (5, "second")):
            span = _source_span(str(source), line, doc_comment=doc)
            assert span is not None
            result.attributes.append(AttributeNode(
                refid=f"r:{name}", compound_refid="", kind="variable",
                name=name, qualified_name=f"sample::{name}",
                file_path=str(source), line_number=line,
                start_line=span[0], end_line=span[1],
                source="demo",
            ))
        fragments = _extract(source, result)
        assert fragments == []
        assert "int first" not in "".join(f.text or "" for f in fragments)
        assert "int second" not in "".join(f.text or "" for f in fragments)


# ---------------------------------------------------------------------------
# Phase 2 — ownership maps
# ---------------------------------------------------------------------------


class TestOwnershipMap:
    def test_build_owned_spans_returns_exact_ordered_map(self, tmp_path: Path):
        result, header = _parse_fixture(tmp_path)
        spans, problems = build_owned_spans(header, result)
        assert problems == []
        keyed = [
            (s.start, s.end, s.owner_type, s.owner, s.kind)
            for s in spans
        ]
        assert keyed == [
            (L_COMPUTE_DOC, L_COMPUTE_END, "FunctionNode", "compute", "node"),
            (L_NS_OPEN, L_NS_OPEN, "NamespaceNode", "sample", "ns-open"),
            (L_NS_BRACE, L_NS_BRACE, "NamespaceNode", "sample", "boilerplate"),
            (L_WIDGET_DOC, 21, "ClassNode", "sample::Widget", "node"),
            (L_ID_DOC, L_ID, "AttributeNode", "sample::Widget::id", "node"),
            (L_RESET_DOC, L_RESET_END, "MethodNode", "sample::Widget::reset()", "node"),
            (L_NS_CLOSE, L_NS_CLOSE, "NamespaceNode", "sample", "ns-close"),
        ]

    def test_overlapping_non_nested_spans_are_reported(self):
        result = ParseResult()
        result.classes.append(ClassNode(
            refid="a", kind="class", name="A", qualified_name="A",
            file_path="x.hpp", line_number=1, start_line=1, end_line=10,
            source="demo",
        ))
        result.classes.append(ClassNode(
            refid="b", kind="class", name="B", qualified_name="B",
            file_path="x.hpp", line_number=8, start_line=8, end_line=20,
            source="demo",
        ))
        spans, problems = build_owned_spans("x.hpp", result)
        assert any("overlaps" in p for p in problems)

    def test_nested_spans_merge_safely_without_reporting(self):
        result = ParseResult()
        result.classes.append(ClassNode(
            refid="a", kind="class", name="A", qualified_name="A",
            file_path="x.hpp", line_number=1, start_line=1, end_line=20,
            source="demo",
        ))
        result.classes.append(ClassNode(
            refid="b", kind="class", name="B", qualified_name="A::B",
            file_path="x.hpp", line_number=5, start_line=5, end_line=10,
            source="demo",
        ))
        spans, problems = build_owned_spans("x.hpp", result)
        assert problems == []
        assert [(s.start, s.end) for s in spans] == [(1, 20), (5, 10)]

    def test_crossing_overlap_hidden_by_nested_span_is_reported(self):
        """Review fix 3: a nested span must not hide a crossing overlap
        between two non-adjacent spans."""
        spans = [
            OwnedSpan(1, 100, owner="A", owner_type="ClassNode"),
            OwnedSpan(2, 3, owner="B", owner_type="ClassNode"),
            OwnedSpan(50, 110, owner="C", owner_type="ClassNode"),
        ]
        merged, problems = _normalize_ownership(spans)
        assert [(s.start, s.end) for s in merged] == [(1, 100), (2, 3), (50, 110)]
        assert len(problems) == 1
        assert "ClassNode 'C' [50-110]" in problems[0]
        assert "ClassNode 'A' [1-100]" in problems[0]
        assert "overlaps" in problems[0]

    def test_identical_spans_merge_and_stay_silent(self):
        spans = [
            OwnedSpan(2, 5, owner="A", owner_type="ClassNode"),
            OwnedSpan(2, 5, owner="A", owner_type="ClassNode"),
        ]
        merged, problems = _normalize_ownership(spans)
        assert [(s.start, s.end) for s in merged] == [(2, 5)]
        assert problems == []

    def test_simple_crossing_overlap_still_reported(self):
        spans = [
            OwnedSpan(1, 10, owner="A", owner_type="ClassNode"),
            OwnedSpan(8, 20, owner="B", owner_type="ClassNode"),
        ]
        _merged, problems = _normalize_ownership(spans)
        assert len(problems) == 1
        assert "B" in problems[0] and "A" in problems[0]

    def test_body_in_other_file_is_owned_in_its_implementation_file(self, tmp_path: Path):
        impl = tmp_path / "impl.cpp"
        impl.write_text(
            "#include \"x.hpp\"\n\nnamespace sample {\n\nint compute(int v) { return v; }\n\n}\n",
            encoding="utf-8",
        )
        result = ParseResult()
        result.methods.append(_method_node(
            name="compute", qname="sample::compute(int)",
            header="x.hpp", header_line=2,
            body_file=str(impl), body_start=5, body_end=5,
        ))
        spans, problems = build_owned_spans(str(impl), result)
        assert problems == []
        body_span = next(
            s for s in spans
            if s.owner == "sample::compute(int)" and s.kind == "node"
        )
        assert (body_span.start, body_span.end) == (5, 5)


def _method_node(
    *,
    name: str,
    qname: str,
    header: str,
    header_line: int,
    body_file: str = "",
    body_start: int = 0,
    body_end: int = 0,
):
    from codegraph import MethodNode

    return MethodNode(
        refid=f"r:{qname}", compound_refid="c", kind="function", name=name,
        qualified_name=qname, file_path=header, line_number=header_line,
        start_line=header_line, end_line=header_line,
        body_file=body_file, body_start=body_start, body_end=body_end,
        source="demo",
    )


# ---------------------------------------------------------------------------
# Phase 3 — pure residual subtraction
# ---------------------------------------------------------------------------


def _extract(file_path: Path, result: ParseResult) -> list[SourceFragmentNode]:
    spans, problems = build_owned_spans(file_path, result)
    assert problems == []
    return extract_residual_source_fragments(file_path, spans, source="demo")


class TestPureSubtraction:
    def test_lowercase_macro_invocation_is_captured(self, tmp_path: Path):
        """Residuals stop at content: namespace boundary blank lines belong
        to the FileNode layout metadata (namespace_leading/trailing_blank_\n        lines), never to a residual — a fragment glued across an open/close
        would double-count the same blank lines at render time."""
        source = tmp_path / "macros.hpp"
        source.write_text(
            "namespace sample\n{\n\nbegin_scoped_guard(lock, mutex);\n\n}\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert [(f.start_line, f.end_line, f.text) for f in fragments] == [
            (4, 4, "begin_scoped_guard(lock, mutex);\n"),
        ]
        fragment = fragments[0]
        assert fragment.placement == "sample"

    def test_multiline_macro_with_adjacent_comments(self, tmp_path: Path):
        source = tmp_path / "macros.hpp"
        source.write_text(
            "// register the widget with the framework\n"
            "defer_setup(Widget,\n"
            "           options);\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert len(fragments) == 1
        assert fragments[0].text == (
            "// register the widget with the framework\n"
            "defer_setup(Widget,\n"
            "           options);\n"
        )
        assert (fragments[0].start_line, fragments[0].end_line) == (1, 3)

    def test_pragma_and_conditional_block(self, tmp_path: Path):
        source = tmp_path / "cond.cpp"
        source.write_text(
            "#include <vector>\n"
            "#pragma clang diagnostic push\n"
            "#ifdef ENABLE_FEATURE\n"
            "int enabled = 1;\n"
            "#else\n"
            "int enabled = 0;\n"
            "#endif\n"
            "void f() {}\n",
            encoding="utf-8",
        )
        result = ParseResult()
        from codegraph import FunctionNode

        result.functions.append(FunctionNode(
            refid="r", kind="function", name="f", qualified_name="f",
            file_path=str(source), line_number=8, start_line=8, end_line=8,
            source="demo",
        ))
        fragments = _extract(source, result)
        texts = [(f.start_line, f.end_line, f.text) for f in fragments]
        assert texts == [
            (2, 7, "#pragma clang diagnostic push\n"
                   "#ifdef ENABLE_FEATURE\nint enabled = 1;\n"
                   "#else\nint enabled = 0;\n#endif\n"),
        ]

    def test_compiler_attribute_is_captured(self, tmp_path: Path):
        """Same contract as above: the surrounding blank lines are FileNode
        layout metadata, the residual carries only the content span."""
        source = tmp_path / "attr.hpp"
        source.write_text(
            "namespace sample\n{\n\n__attribute__((visibility(\"default\"))) int counter = 0;\n\n}\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert len(fragments) == 1
        assert fragments[0].text == (
            "__attribute__((visibility(\"default\"))) int counter = 0;\n"
        )

    def test_file_with_no_residuals_produces_no_fragments(self, tmp_path: Path):
        result, header = _parse_fixture(tmp_path)
        fragments = _extract(header, result)
        assert fragments == []

    def test_structured_declarations_never_appear_in_fragments(self, tmp_path: Path):
        result, header = _parse_fixture(tmp_path)
        fragments = _extract(header, result)
        assert fragments == []
        # A fragment must never contain a modeled declaration — inject a
        # marker the source does not contain and confirm nothing captures it.
        assert all("struct Widget" not in (f.text or "") for f in fragments)

    def test_fragments_are_ordered_by_source_position(self, tmp_path: Path):
        source = tmp_path / "ordered.cpp"
        source.write_text(
            "FIRST_MACRO()\n"
            "void f() {}\n"
            "SECOND_MACRO()\n",
            encoding="utf-8",
        )
        from codegraph import FunctionNode

        result = ParseResult()
        result.functions.append(FunctionNode(
            refid="r", kind="function", name="f", qualified_name="f",
            file_path=str(source), line_number=2, start_line=2, end_line=2,
            source="demo",
        ))
        fragments = _extract(source, result)
        assert [f.start_line for f in fragments] == [1, 3]


class TestNestedNamespacePlacement:
    """Review fix 2: fragments inside nested namespaces keep the fully
    qualified namespace name so codegen emits them in the right scope."""

    def test_traditionally_nested_namespace_gets_qualified_placement(self, tmp_path: Path):
        source = tmp_path / "nested.hpp"
        source.write_text(
            "namespace outer {\n"
            "namespace inner {\n"
            "thing();\n"
            "}\n"
            "}\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert len(fragments) == 1
        assert fragments[0].placement == "outer::inner"
        assert (fragments[0].start_line, fragments[0].end_line) == (3, 3)

    def test_cpp17_qualified_namespace_syntax(self, tmp_path: Path):
        source = tmp_path / "c17.hpp"
        source.write_text(
            "namespace outer::inner {\n"
            "thing();\n"
            "}\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert len(fragments) == 1
        assert fragments[0].placement == "outer::inner"

    def test_sibling_namespaces_stay_independent(self, tmp_path: Path):
        source = tmp_path / "siblings.hpp"
        source.write_text(
            "namespace a {\n"
            "thing_a();\n"
            "}\n"
            "namespace b {\n"
            "thing_b();\n"
            "}\n",
            encoding="utf-8",
        )
        fragments = _extract(source, ParseResult())
        assert {f.placement: f.start_line for f in fragments} == {"a": 2, "b": 5}

    def test_serialization_preserves_qualified_placement(self, tmp_path: Path):
        fragment = SourceFragmentNode(
            qualified_name="nested.hpp#3-3",
            file_path="nested.hpp",
            start_line=3,
            end_line=3,
            placement="outer::inner",
            text="thing();\n",
            source="demo",
        )
        graph = result_to_graph_json(ParseResult(source_fragments=[fragment]), source="demo")
        entry = next(item for item in graph if item["type"] == "SourceFragmentNode")
        assert entry["placement"] == "outer::inner"

    def test_codegen_nests_fragment_under_qualified_placement(self):
        """The fragment's qualified placement recreates the namespace nesting
        in generated output (``outer::inner`` → two nested blocks)."""
        from types import SimpleNamespace

        from codegraph.codegen.context import _nest_by_namespace
        from codegraph.codegen.context.source_fragment import build_context

        node = SourceFragmentNode(
            qualified_name="nested.hpp#3-3",
            file_path="nested.hpp",
            start_line=3,
            end_line=3,
            placement="outer::inner",
            text="thing();\n",
            source="demo",
        )
        ctx = build_context(SimpleNamespace(node=node), None)
        assert ctx["qualified_name"].startswith("outer::inner::")
        nested = _nest_by_namespace([ctx])
        assert len(nested) == 1
        outer = nested[0]
        assert outer["name"] == "outer"
        assert outer["blocks"] == []
        assert [n["name"] for n in outer["namespaces"]] == ["inner"]
        assert outer["namespaces"][0]["blocks"] == [ctx]


# ---------------------------------------------------------------------------
# Phase 4.1 — round trip through graph JSON → LayerGraph
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_fragment_metadata_survives_parse_to_layergraph(self, tmp_path: Path):
        from codegraph.graph import LayerGraph

        source = tmp_path / "frag.hpp"
        source.write_text(
            "namespace sample\n{\n\n#pragma clang diagnostic push\n\n}\n",
            encoding="utf-8",
        )
        result = ParseResult()
        CppParser().parse_source_dir(_write_xml_only(tmp_path, source), source="demo",
                                    result=result, layer="as-built")
        assert len(result.source_fragments) == 1
        fragment = result.source_fragments[0]

        serialized = result_to_graph_json(result, source="demo")
        graph = LayerGraph.deserialize(serialized)
        entries = [
            e.node for e in graph._all_entries()
            if type(e.node).__name__ == "SourceFragmentNode"
        ]
        assert len(entries) == 1
        round_tripped = entries[0]
        assert round_tripped.text == fragment.text
        assert round_tripped.start_line == fragment.start_line
        assert round_tripped.end_line == fragment.end_line
        assert round_tripped.placement == fragment.placement
        assert round_tripped.file_path == fragment.file_path

    def test_fragment_serialization_is_stable(self, tmp_path: Path):
        fragment = SourceFragmentNode(
            qualified_name="frag.hpp#3-3",
            file_path="frag.hpp",
            start_line=3,
            end_line=3,
            placement="sample",
            text="#pragma once\n",
            source="demo",
        )
        graph = result_to_graph_json(ParseResult(source_fragments=[fragment]), source="demo")
        entry = next(item for item in graph if item["type"] == "SourceFragmentNode")
        assert entry["text"] == "#pragma once\n"
        assert entry["placement"] == "sample"
        assert entry["start_line"] == 3
        assert entry["end_line"] == 3


def _write_xml_only(tmp_path: Path, source: Path) -> Path:
    """Minimal file-compound XML for a source with a pragma residual."""
    xml_dir = tmp_path / "xml2"
    xml_dir.mkdir(exist_ok=True)
    rel = source.name
    abs_rel = str(source)
    (xml_dir / "index.xml").write_text(
        '<doxygenindex><compound refid="_frag_8hpp" kind="file">'
        f"<name>{rel}</name></compound></doxygenindex>",
        encoding="utf-8",
    )
    (xml_dir / "_frag_8hpp.xml").write_text(
        '<doxygen><compounddef id="_frag_8hpp" kind="file" language="C++">'
        f"<compoundname>{rel}</compoundname>"
        f'<location file="{abs_rel}"/>'
        "</compounddef></doxygen>",
        encoding="utf-8",
    )
    return xml_dir
