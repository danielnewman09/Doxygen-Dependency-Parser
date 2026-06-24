"""
Convert a ParseResult to a codegraph LayerGraph-compatible JSON format.

The output is a flat list of serialized node dicts that
``codegraph.viz.export_html_from_json`` can consume to render an
interactive HTML graph.

Each node dict contains:
- ``type``: the codegraph node class name (e.g. ``"ClassNode"``)
- node properties (name, qualified_name, refid, etc.)
- ``tags``: provenance tags (set to the project name)
- ``edges``: a list of ``{relation_type, target_uid, target_type}`` dicts

Edges are built from the ParseResult's relationship lists (includes,
invokes, composition via ``compound_refid``, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

from doxygen_index.parser.model import ParseResult


def result_to_graph_json(result: ParseResult, source: str) -> list[dict]:
    """Convert a ParseResult to a list of serialized node dicts.

    Args:
        result: The parsed output from ``parse_xml_dir`` or ``parse_python_dir``.
        source: Provenance label (project name) — stored in the node's
            ``source`` field for traceability.

    Returns:
        A list of dicts, each a serialized node with ``type``,
        properties, ``tags``, and ``edges`` keys.  Suitable for
        ``json.dumps`` and consumption by
        ``codegraph.viz.export_html_from_json``.
    """
    # Collect all node lists
    node_lists = [
        result.files,
        result.namespaces,
        result.classes,
        result.enums,
        result.unions,
        result.interfaces,
        result.concepts,
        result.methods,
        result.attributes,
        result.enum_values,
        result.defines,
        result.functions,
        result.parameters,
        result.tests,
        result.assertions,
        result.test_steps,
        result.test_fixtures,
        result.literals,
    ]

    # Build refid → uid mapping for edge target resolution.
    # codegraph's LayerGraph._deserialize_flat resolves target_uid via
    # an identity_to_key lookup that checks qualified_name, refid, and path,
    # so we can use refid directly as target_uid.
    refid_to_uid: dict[str, str] = {}
    refid_to_type: dict[str, str] = {}
    for nodes in node_lists:
        for node in nodes:
            refid = getattr(node, "refid", None)
            if refid:
                uid = node._uid_value()
                if uid:
                    refid_to_uid[refid] = uid
                refid_to_type[refid] = type(node).__name__

    # Serialize each node and attach edges
    serialized: list[dict] = []

    for nodes in node_lists:
        for node in nodes:
            entry = node.serialize()
            entry["tags"] = ["as-built"]

            # Include the source file path so the visualisation can show
            # "Defined in" in the detail panel.  ``serialize()`` omits it
            # (file_path isn't in ``_llm_fields``), so add it explicitly;
            # ``LayerGraph.deserialize`` restores it onto the node since
            # file_path is a declared property on compounds/members.
            file_path = getattr(node, "file_path", "") or ""
            if file_path:
                entry["file_path"] = file_path

            # Build edges for this node
            edges = _build_node_edges(node, result, refid_to_uid, refid_to_type)
            if edges:
                entry["edges"] = edges

            serialized.append(entry)

    return serialized


def write_graph_json(
    result: ParseResult,
    output_path: Path,
    source: str,
) -> Path:
    """Write a ParseResult as a LayerGraph-compatible JSON file.

    Args:
        result: The parsed output.
        output_path: Where to write the JSON file.
        source: Provenance label (project name).

    Returns:
        The resolved output path.
    """
    data = result_to_graph_json(result, source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return output_path.resolve()


# ---------------------------------------------------------------------------
# Edge builders
# ---------------------------------------------------------------------------


def _build_node_edges(
    node,
    result: ParseResult,
    refid_to_uid: dict[str, str],
    refid_to_type: dict[str, str],
) -> list[dict]:
    """Build the edge list for a single node.

    Edge types:
    - COMPOSES: compound → member (via ``compound_refid`` on members)
    - COMPOSES: namespace → child namespace / directly-defined top-level
      compound or function (via ``result.compositions`` recorded by the
      language parser)
    - INHERITS_FROM: derived class → base compound (via
      ``result.inherits`` recorded by the language parser)
    - INCLUDES: file → file  (namespaces never include files)
    - INVOKES: method/function → method/function
    """
    edges: list[dict] = []
    node_refid = getattr(node, "refid", None)
    node_type = type(node).__name__

    # --- COMPOSES (outgoing: compound → member via compound_refid) ---
    # Check all members to see if any have compound_refid == this node's refid
    if node_refid:
        for member in result.members:
            member_compound = getattr(member, "compound_refid", None)
            if member_compound and member_compound == node_refid:
                member_refid = getattr(member, "refid", None)
                if member_refid and member_refid in refid_to_uid:
                    edges.append({
                        "relation_type": "COMPOSES",
                        "target_uid": member_refid,  # refid works via identity_to_key
                        "target_type": type(member).__name__,
                    })

    # --- COMPOSES (outgoing: namespace → child via parser compositions) ---
    # The language parser records namespace composition on
    # ``result.compositions``; emit a COMPOSES edge for each child whose
    # parent is this node.  Only namespaces emit these — a Python module's
    # FileNode shares the dotted module name as its refid with the
    # NamespaceNode, so matching by refid alone would otherwise make the
    # file wrongly compose the namespace's children too.  Only the
    # parent node's edge list is consulted, matching the flat-format
    # convention used above.
    if node_type == "NamespaceNode" and node_refid:
        for comp in result.compositions:
            if comp.parent_refid == node_refid and comp.child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": comp.child_refid,  # refid works via identity_to_key
                    "target_type": comp.child_type,
                })

    # --- INCLUDES (outgoing: this file includes other files) ---
    # Only files include other files.  A Python module's FileNode and its
    # NamespaceNode share the same refid (the dotted module name), so
    # matching includes by refid alone would duplicate every import onto
    # the namespace as a bogus INCLUDES edge.  Restricting this to FileNode
    # prevents that duplication.
    if node_type == "FileNode" and node_refid:
        for inc in result.includes:
            if inc.file_refid == node_refid and inc.included_refid:
                edges.append({
                    "relation_type": "INCLUDES",
                    "target_uid": inc.included_refid,
                    "target_type": "FileNode",
                })

    # --- INCLUDES (namespace imports — resolved by the parser) ---
    # The language parser resolves import statements to known compound
    # refids and records them on ``result.namespace_includes``; emit
    # INCLUDES edges from the namespace to each imported compound.
    # These are distinct from file-level includes above — they originate
    # from namespaces (not files) and target compounds (not files).
    if node_type == "NamespaceNode" and node_refid:
        for inc in result.namespace_includes:
            if inc.file_refid == node_refid and inc.included_refid in refid_to_uid:
                edges.append({
                    "relation_type": "INCLUDES",
                    "target_uid": inc.included_refid,
                    "target_type": "FileNode",  # reused; target_type is just a label
                })

    # --- INVOKES (outgoing: this method/function invokes others) ---
    if node_refid:
        for inv in result.invokes:
            if inv.from_refid == node_refid and inv.to_refid:
                target_type = refid_to_type.get(inv.to_refid, "MethodNode")
                edges.append({
                    "relation_type": "INVOKES",
                    "target_uid": inv.to_refid,
                    "target_type": target_type,
                })

    # --- INHERITS_FROM (outgoing: this compound derives from a base) ---
    # The language parser resolves base-class names to known compound
    # refids (recorded on ``result.inherits``); emit an INHERITS_FROM edge
    # from the derived compound to its base.  Bases that didn't resolve
    # (e.g. ``Exception``, ``ABC``) are absent from ``result.inherits`` and
    # thus never produce edges here -- and the graph layer drops any edge
    # whose target isn't a known node, so there are never dangling edges.
    if node_refid:
        for inh in result.inherits:
            if inh.from_refid == node_refid and inh.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "INHERITS_FROM",
                    "target_uid": inh.to_refid,
                    "target_type": inh.to_type,
                })

    # --- DEPENDS_ON (outgoing: function/method depends on a type) ---
    # The language parser resolves parameter and return types to known
    # compound refids (recorded on ``result.depends_on``); emit a
    # DEPENDS_ON edge from the callable to the type it uses.
    if node_refid:
        for dep in result.depends_on:
            if dep.from_refid == node_refid and dep.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEPENDS_ON",
                    "target_uid": dep.to_refid,
                    "target_type": dep.to_type,
                })

    # --- VERIFIES (outgoing: TestNode → tested code) ---
    if node_refid:
        for ver in result.verifies:
            if ver.from_refid == node_refid and ver.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "VERIFIES",
                    "target_uid": ver.to_refid,
                    "target_type": ver.to_type,
                })

    # --- LEFT_OPERAND / RIGHT_OPERAND (outgoing: AssertionNode → operand) ---
    if node_refid:
        for op in result.operands:
            if op.from_refid == node_refid and op.to_refid in refid_to_uid:
                relation = "LEFT_OPERAND" if op.side == "left" else "RIGHT_OPERAND"
                edges.append({
                    "relation_type": relation,
                    "target_uid": op.to_refid,
                    "target_type": op.to_type,
                })

    # --- CALLEE (outgoing: TestStepNode → called method/function) ---
    if node_refid:
        for cal in result.callees:
            if cal.from_refid == node_refid and cal.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CALLEE",
                    "target_uid": cal.to_refid,
                    "target_type": cal.to_type,
                })

    # --- COMPOSES (outgoing: TestNode → AssertionNode/TestStepNode) ---
    if node_refid:
        for tc in result.test_compositions:
            if tc.parent_refid == node_refid and tc.child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": tc.child_refid,
                    "target_type": tc.child_type,
                })

    # --- OF_TYPE (outgoing: TestFixtureNode → type definition) ---
    if node_refid:
        for fo in result.fixture_of_types:
            if fo.from_refid == node_refid and fo.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "OF_TYPE",
                    "target_uid": fo.to_refid,
                    "target_type": fo.to_type,
                })

    # --- CHECKED_BY (outgoing: TestFixtureNode → AssertionNode) ---
    if node_refid:
        for cb in result.fixture_checked_by:
            if cb.from_refid == node_refid and cb.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CHECKED_BY",
                    "target_uid": cb.to_refid,
                    "target_type": "AssertionNode",
                })

    # --- DEFINED_IN (outgoing: TestFixtureNode → TestStepNode) ---
    if node_refid:
        for di in result.fixture_defined_in:
            if di.from_refid == node_refid and di.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEFINED_IN",
                    "target_uid": di.to_refid,
                    "target_type": "TestStepNode",
                })

    return edges
