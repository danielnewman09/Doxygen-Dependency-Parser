"""Namespace-scoped PlantUML diagrams for the doxygen-index dogfood
graph.

Scopes the as-built dogfood LayerGraph to a *namespace* (rather than a
single class) and exports + saves the PlantUML diagram, so the package
architecture can be inspected directly:

- ``tests/codegraph_output/doxygen_index_parser.puml`` — the diagram
- ``tests/codegraph_output/doxygen_index_parser.svg`` — validated render

The scoping is ``LayerGraph.subgraph(<namespace qname>)`` (full
composition subtree + 1-hop neighbours) followed by the standard
``export_plantuml`` — no codegraph changes needed.

Start with ``doxygen_index.parser``; add more namespace qnames to
``NS_CASES`` to cover the rest of the package tree (e.g.
``doxygen_index.cppreference``, ``doxygen_index.cli``).  The generic
checks (structure, no dangling arrows, no file nodes, artifact
saving) are parametrized over the whole list; the parser-specific
class pins the current extraction fidelity for the focal namespace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_CODEGRAPH_OUTPUT = Path(__file__).resolve().parent.parent / "codegraph_output"

#: Namespaces to diagram.  Extend this list to cover more of the
#: package tree — every entry gets the generic checks + saved artifacts.
NS_CASES = ["doxygen_index.parser"]
NS_IDS = [ns.rsplit(".", 1)[-1] for ns in NS_CASES]  # ["parser"]


def _out_stem(ns: str) -> str:
    """File stem for a namespace diagram: dots → underscores."""
    return "doxygen_index_" + ns.split("doxygen_index.", 1)[-1].replace(".", "_")


def _element_aliases(puml: str) -> set[str]:
    """Aliases of every rendered element (class/interface/enum/package)."""
    return set(
        m.group(1)
        for m in re.finditer(
            r'^\s*(?:class|interface|enum|union|package)\s+"[^"]*"\s+as\s+(\S+)',
            puml, re.M,
        )
    )


def _arrows(puml: str) -> list[tuple[str, str, str]]:
    """(source_alias, target_alias, label) for every relationship arrow."""
    return [
        (m.group(1), m.group(2), m.group(3))
        for m in re.finditer(r"^(\S+)\s+(?:\.\.>|<\|--)\s+(\S+)\s+:\s+(\S+)", puml, re.M)
    ]


@pytest.fixture(scope="module")
def ns_diagrams(codegraph_graph):
    """Build {ns: {"graph": LayerGraph, "puml": str}} for every NS_CASES.

    Built once per module from the session fixture's serialized data.
    """
    from codegraph.graph import LayerGraph
    from codegraph.export.plantuml import export_plantuml

    serialized, _uid_map = codegraph_graph
    full_graph = LayerGraph.deserialize(serialized)

    out: dict[str, dict] = {}
    for ns in NS_CASES:
        scoped = full_graph.subgraph(ns)
        out[ns] = {
            "graph": scoped,
            "puml": export_plantuml(scoped, fields="all"),
        }
    return out


class TestNamespaceScopedDiagrams:
    """Generic checks, parametrized over every namespace in NS_CASES."""

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_has_startuml(self, ns_diagrams, ns):
        assert "@startuml" in ns_diagrams[ns]["puml"]
        assert "@enduml" in ns_diagrams[ns]["puml"]

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_has_namespace_package(self, ns_diagrams, ns):
        """The focal namespace renders as a top-level package."""
        name = ns.rsplit(".", 1)[-1]
        assert any(f'package "{name}"' in L for L in ns_diagrams[ns]["puml"].splitlines())

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_no_dangling_arrows(self, ns_diagrams, ns):
        """Every arrow endpoint resolves to an emitted element.

        The subgraph pulls in 1-hop neighbours, so edges among 2-hop
        nodes could theoretically dangle — this pins that they never
        do for the rendered view.
        """
        puml = ns_diagrams[ns]["puml"]
        aliases = _element_aliases(puml)
        dangling = [
            (s, t, l) for s, t, l in _arrows(puml)
            if s not in aliases or t not in aliases
        ]
        assert not dangling, f"{len(dangling)} dangling arrows in {ns}"

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_has_relationship_arrows(self, ns_diagrams, ns):
        """The diagram carries the expected arrow vocabulary."""
        puml = ns_diagrams[ns]["puml"]
        assert any(" : invokes" in L for L in puml.splitlines())
        assert any(" : depends_on" in L for L in puml.splitlines())
        assert any(" : includes" in L for L in puml.splitlines())

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_no_file_nodes(self, ns_diagrams, ns):
        assert ".py" not in ns_diagrams[ns]["puml"]

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_scoped_smaller_than_full(self, ns_diagrams, ns, codegraph_graph):
        _serialized, full_uid_map = codegraph_graph
        scoped_entries = ns_diagrams[ns]["graph"].serialize(fields="all")
        assert len(scoped_entries) < len(full_uid_map), (
            f"namespace subgraph ({len(scoped_entries)}) should be "
            f"smaller than full graph ({len(full_uid_map)})"
        )
        print(f"\n  {ns}: {len(scoped_entries)} entries "
              f"({100*len(scoped_entries)/len(full_uid_map):.0f}% of full)")

    # ------------------------------------------------------------------
    # Artifact saving
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("ns", NS_CASES, ids=NS_IDS)
    def test_saves_puml_and_svg(self, ns_diagrams, ns):
        """Save the diagram to the codegraph_output directory and render
        a validated SVG alongside it (following the artifact convention
        of the one-hop suite)."""
        import shutil
        import tempfile

        from codegraph.export.plantuml import render_plantuml_to_svg

        puml_text = ns_diagrams[ns]["puml"]
        stem = _out_stem(ns)

        _CODEGRAPH_OUTPUT.mkdir(parents=True, exist_ok=True)
        puml_path = _CODEGRAPH_OUTPUT / f"{stem}.puml"
        puml_path.write_text(puml_text, encoding="utf-8")
        assert puml_path.stat().st_size > 1000, "saved puml is suspiciously small"

        if not shutil.which("plantuml"):
            pytest.skip("plantuml binary not found — SVG render skipped")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_puml = Path(tmpdir) / "ns.puml"
            tmp_puml.write_text(puml_text, encoding="utf-8")
            svg_path = render_plantuml_to_svg(tmp_puml, timeout=120)
            output_svg = _CODEGRAPH_OUTPUT / f"{stem}.svg"
            shutil.copy(str(svg_path), str(output_svg))
            print(f"\n  Saved: {puml_path} ({puml_path.stat().st_size:,} bytes)")
            print(f"  Saved: {output_svg} ({output_svg.stat().st_size:,} bytes)")

        text = output_svg.read_text(encoding="utf-8", errors="replace")
        assert "<svg" in text or "<?xml" in text


class TestParserNamespaceDiagram:
    """Extraction-fidelity checks for the focal ``doxygen_index.parser``
    namespace diagram."""

    NS = "doxygen_index.parser"

    @pytest.fixture(scope="class")
    def parser_puml(self, ns_diagrams):
        return ns_diagrams[self.NS]["puml"]

    def test_subpackages_present(self, parser_puml):
        """All five parser subpackages render as nested packages."""
        for pkg in ("base", "cpp_parser", "helpers", "model", "python"):
            assert any(f'package "{pkg}"' in L for L in parser_puml.splitlines()), (
                f"missing parser subpackage: {pkg}"
            )

    def test_key_symbols_present(self, parser_puml):
        """The parser surface is fully extracted: interface, both
        concrete parsers, entry points, and the core model type."""
        for sym in (
            "LanguageParser",     # interface
            "CppParser",          # concrete parser
            "PythonParser",       # concrete parser
            "parse_xml_dir",      # C++ entry point
            "parse_python_dir",   # Python entry point
            "ParseResult",        # core model type
        ):
            assert sym in parser_puml, f"missing symbol: {sym}"

    def test_inheritance_arrows(self, parser_puml):
        """Both concrete parsers inherit from the interface."""
        inherit_lines = [L for L in parser_puml.splitlines() if " : inherits_from" in L]
        assert len(inherit_lines) == 2, inherit_lines
        for L in inherit_lines:
            assert "LanguageParser" in L

    def test_model_types_show_members(self, parser_puml):
        """ParseResult renders its data fields as member lines."""
        assert "  +classes: list[ClassNode]" in parser_puml
        assert "  +methods: list[MethodNode]" in parser_puml

    def test_catch_clause_helpers_present(self, parser_puml):
        """The catch-clause DEPENDS_ON machinery (from the spdlog_ex
        work) is part of the extracted C++ parser surface."""
        assert "_scan_file_catch_clauses" in parser_puml
        assert "_resolve_catch_clauses" in parser_puml
