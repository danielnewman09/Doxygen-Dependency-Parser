"""Focused CLI behavior for database-only codegraph ingestion."""

from __future__ import annotations

from types import SimpleNamespace

from doxygen_index.cli import cmd_codegraph
from doxygen_index.parser.model import ParseResult


def _args(tmp_path, *, no_csv: bool, neo4j: bool = False):
    return SimpleNamespace(
        project_dir=str(tmp_path / "project"),
        output_dir=str(tmp_path / "output"),
        csv_dir=None,
        build_type="Debug",
        only=None,
        cppreference=False,
        cppreference_cache_dir=str(tmp_path / "cppreference"),
        cppreference_archive_url=None,
        cppreference_force=False,
        neo4j=neo4j,
        no_csv=no_csv,
        clear=False,
        yes=True,
    )


def _stub_pipeline(monkeypatch, tmp_path):
    import doxygen_index.conan as conan
    import doxygen_index.doxygen as doxygen
    import doxygen_index.graph_json as graph_json
    import doxygen_index.parser as parser
    import doxygen_index.parser.cpp_parser as cpp_parser

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir()

    monkeypatch.setattr(conan, "discover_packages", lambda **kwargs: {})
    monkeypatch.setattr(doxygen, "run_unified_doxygen", lambda **kwargs: xml_dir)
    monkeypatch.setattr(parser, "parse_xml_dir", lambda *args, **kwargs: ParseResult())
    monkeypatch.setattr(doxygen, "tag_nodes_by_source", lambda *args: None)
    monkeypatch.setattr(doxygen, "resolve_namespace_type_deps", lambda *args: None)
    monkeypatch.setattr(cpp_parser, "_derive_namespace_compositions", lambda *args: None)
    monkeypatch.setattr(graph_json, "result_to_graph_json", lambda *args, **kwargs: [])


def test_codegraph_default_still_exports_csv(monkeypatch, tmp_path):
    import doxygen_index.csv_export as csv_export

    _stub_pipeline(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        csv_export,
        "export_csv",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    cmd_codegraph(_args(tmp_path, no_csv=False))

    assert len(calls) == 1
    assert calls[0]["normalize"] is False
    assert (tmp_path / "output" / "csv").is_dir()


def test_codegraph_no_csv_suppresses_all_csv_artifacts(monkeypatch, tmp_path):
    import doxygen_index.csv_export as csv_export
    import doxygen_index.cli as cli
    import doxygen_index.neo4j_backend as backend

    _stub_pipeline(monkeypatch, tmp_path)
    csv_calls = []
    monkeypatch.setattr(csv_export, "export_csv", lambda *args, **kwargs: csv_calls.append(1))
    monkeypatch.setattr(cli, "get_backend", lambda: SimpleNamespace(health_check=lambda: True))
    monkeypatch.setattr(backend, "ensure_schema", lambda: None)
    monkeypatch.setattr(backend, "clear_source", lambda source: None)

    def fake_write(result, source=None, *, timings=None):
        timings.update(serialization=0.01, persistence=0.02)

    monkeypatch.setattr(backend, "write_result", fake_write)

    cmd_codegraph(_args(tmp_path, no_csv=True, neo4j=True))

    assert csv_calls == []
    assert not (tmp_path / "output" / "csv").exists()


def test_codegraph_csv_summary_accepts_large_fields(monkeypatch, tmp_path):
    import doxygen_index.csv_export as csv_export

    _stub_pipeline(monkeypatch, tmp_path)

    def fake_export(_result, *, output_dir, **_kwargs):
        output_dir.mkdir(parents=True)
        large_value = "x" * 200_000
        (output_dir / "nodes.csv").write_text(
            f'value\n"{large_value}"\n', encoding="utf-8"
        )
        (output_dir / "relationships.csv").write_text(
            "value\nsmall\n", encoding="utf-8"
        )

    monkeypatch.setattr(csv_export, "export_csv", fake_export)

    cmd_codegraph(_args(tmp_path, no_csv=False))
