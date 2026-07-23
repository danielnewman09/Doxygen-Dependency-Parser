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


def merge_parse_results(*results: ParseResult) -> ParseResult:
    """Merge multiple ParseResults into one.

    All node lists and relationship lists are concatenated.  Duplicate
    nodes (same refid) from different results are preserved — downstream
    consumers are responsible for deduplication by uid.

    This is used to combine independently-parsed dependency results with
    a project parse result before calling :func:`result_to_graph_json`.
    """
    from dataclasses import fields

    merged = ParseResult()
    for fld in fields(ParseResult):
        target = getattr(merged, fld.name)
        for r in results:
            source_list = getattr(r, fld.name)
            if source_list:
                target.extend(source_list)
    return merged


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
    # Edges use refid_to_uid to translate Doxygen refids into the
    # deterministic uid (source + qualified_name hash) used on nodes.
    refid_to_uid: dict[str, str] = {}
    refid_to_type: dict[str, str] = {}
    # Secondary mapping: qualified_name → uid for resolving edges
    # whose target refid doesn't match (e.g. cross-ParseResult merges
    # where Doxygen generates different refids for the same symbol).
    qname_to_uid: dict[str, str] = {}
    qname_to_type: dict[str, str] = {}
    for nodes in node_lists:
        for node in nodes:
            refid = _get_prop(node, "refid")
            if refid:
                # Compute deterministic uid from source + identity fields
                # (e.g. source + qualified_name), not the auto-generated
                # UUID from UniqueIdProperty.  This ensures edge
                # target_uids resolve to the same uid used in node
                # serialization.
                try:
                    uid = node._compute_uid()
                except ValueError:
                    uid = node._uid_value()
                if uid:
                    refid_to_uid[refid] = uid
                    # Set the deterministic uid on the node so that
                    # serialize() emits it instead of the auto-generated
                    # UUID from UniqueIdProperty.
                    uid_prop = type(node)._uid_prop()
                    if uid_prop:
                        setattr(node, uid_prop, uid)
                    # Build qualified_name → uid mapping for cross-
                    # ParseResult resolution.
                    qn = _get_prop(node, "qualified_name") or ""
                    if qn:
                        qname_to_uid[qn] = uid
                        qname_to_type[qn] = type(node).__name__
                refid_to_type[refid] = type(node).__name__

    # Serialize each node and attach edges
    serialized: list[dict] = []

    for nodes in node_lists:
        for node in nodes:
            entry = node.serialize()
            # Use the node's tags as set by tag_nodes_by_source.
            # FileNode lacks a ``tags`` attribute, so fall back to
            # deriving from source.
            node_tags = _get_prop(node, "tags", is_list=True)
            if node_tags:
                entry["tags"] = list(node_tags)
            else:
                node_source = _get_prop(node, "source") or source
                entry["tags"] = ["as-built" if node_source == source else "dependency"]

            # Include source so downstream consumers can filter nodes by
            # project ownership.  ``source`` isn't in ``_llm_fields`` so
            # ``serialize()`` omits it; add it explicitly.
            node_source = _get_prop(node, "source") or ""
            if node_source:
                entry["source"] = node_source

            # Include the source file path so the visualisation can show
            # "Defined in" in the detail panel.  ``serialize()`` omits it
            # (file_path isn't in ``_llm_fields``), so add it explicitly;
            # ``LayerGraph.deserialize`` restores it onto the node since
            # file_path is a declared property on compounds/members.
            file_path = _get_prop(node, "file_path") or ""
            if file_path:
                entry["file_path"] = file_path

            # Build edges for this node
            edges = _build_node_edges(node, result, refid_to_uid,
                                      refid_to_type, qname_to_uid)
            # Filter out self-references (edge target_uid == this node's uid).
            node_uid = entry.get("uid", "")
            edges = [e for e in edges if e.get("target_uid", "") != node_uid]
            if edges:
                entry["edges"] = edges

            serialized.append(entry)

    # ------------------------------------------------------------------
    # Post-process: text-scanning for qualified-name references
    #
    # Doxygen's <ref> elements cover explicit type references in
    # declarations, but text fields (type_signature, brief_description,
    # description, argsstring) may reference dep/stdlib types that
    # Doxygen didn't link.  Scan project-node text for qualified names
    # (e.g. ``spdlog::logger``, ``std::vector``) and emit synthetic
    # DEPENDS_ON edges to matching nodes from cppreference or dep parses.
    # ------------------------------------------------------------------
    import re
    _QNAME_RE = re.compile(r'\b(\w+(?:::\w+)+)\b')
    for entry in serialized:
        node_source = entry.get("source", "")
        if node_source != source:
            continue  # Only scan project-owned nodes

        # Collect known target_uids to avoid duplicates
        existing_targets = {e["target_uid"] for e in entry.get("edges", [])}

        # Gather text from relevant fields
        texts: list[str] = []
        for field in ("type_signature", "definition", "argsstring",
                       "brief_description", "description"):
            val = entry.get(field, "")
            if val:
                texts.append(str(val))
        if not texts:
            continue
        combined = " ".join(texts)

        for match in _QNAME_RE.finditer(combined):
            qn = match.group(1)
            if qn in qname_to_uid and qname_to_uid[qn] not in existing_targets:
                existing_targets.add(qname_to_uid[qn])
                entry.setdefault("edges", []).append({
                    "relation_type": "DEPENDS_ON",
                    "target_uid": qname_to_uid[qn],
                    "target_type": qname_to_type.get(qn, "ClassNode"),
                })

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


def _get_prop(node, name: str, *, is_list: bool = False) -> str | list | None:
    """Extract a property from a codegraph node safely.

    neomodel nodes store properties in ``__dict__`` with a leading underscore.
    ``getattr(node, name)`` may return the neomodel ``StringProperty``
    descriptor rather than the value when the property was set via the
    descriptor on a brand-new (unsaved) instance.
    """
    if not hasattr(node, name):
        return None
    # Tags are stored in __dict__['_tags'] as a list
    val = node.__dict__.get(name, None)
    if is_list and isinstance(val, list):
        return val
    if isinstance(val, str):
        return val or None
    return None


def _build_node_edges(
    node,
    result: ParseResult,
    refid_to_uid: dict[str, str],
    refid_to_type: dict[str, str],
    qname_to_uid: dict[str, str],
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
    node_refid = _get_prop(node, "refid")
    node_type = type(node).__name__

    # --- COMPOSES (outgoing: compound → member via compound_refid) ---
    # Check all members to see if any have compound_refid == this node's refid
    if node_refid:
        for member in result.members:
            member_compound = _get_prop(member, "compound_refid")
            if member_compound and member_compound == node_refid:
                member_refid = _get_prop(member, "refid")
                if member_refid and member_refid in refid_to_uid:
                    edges.append({
                        "relation_type": "COMPOSES",
                        "target_uid": refid_to_uid[member_refid],
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
                    "target_uid": refid_to_uid[comp.child_refid],
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
                target_uid = refid_to_uid.get(inc.included_refid, inc.included_refid)
                edges.append({
                    "relation_type": "INCLUDES",
                    "target_uid": target_uid,
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
                    "target_uid": refid_to_uid[inc.included_refid],
                    "target_type": "FileNode",  # reused; target_type is just a label
                })

    # --- INVOKES (outgoing: this method/function invokes others) ---
    if node_refid:
        for inv in result.invokes:
            if inv.from_refid == node_refid and inv.to_refid:
                target_type = refid_to_type.get(inv.to_refid, "MethodNode")
                # Primary: resolve by refid (works within a single
                # Doxygen parse).  Fallback: resolve by qualified_name
                # (works across merged ParseResults where Doxygen
                # generates different refids for the same symbol).
                target_uid = refid_to_uid.get(inv.to_refid)
                if target_uid is None and inv.to_name:
                    target_uid = qname_to_uid.get(inv.to_name)
                # Skip edges that can't resolve — a dangling target_uid
                # (raw Doxygen refid) won't match any node uid.
                if target_uid is None:
                    continue
                edges.append({
                    "relation_type": "INVOKES",
                    "target_uid": target_uid,
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
                    "target_uid": refid_to_uid[inh.to_refid],
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
                    "target_uid": refid_to_uid[dep.to_refid],
                    "target_type": dep.to_type,
                })

    # --- VERIFIES (outgoing: TestNode → tested code) ---
    if node_refid:
        for ver in result.verifies:
            if ver.from_refid == node_refid and ver.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "VERIFIES",
                    "target_uid": refid_to_uid[ver.to_refid],
                    "target_type": ver.to_type,
                })

    # --- LEFT_OPERAND / RIGHT_OPERAND (outgoing: AssertionNode → operand) ---
    if node_refid:
        for op in result.operands:
            if op.from_refid == node_refid and op.to_refid in refid_to_uid:
                relation = "LEFT_OPERAND" if op.side == "left" else "RIGHT_OPERAND"
                edges.append({
                    "relation_type": relation,
                    "target_uid": refid_to_uid[op.to_refid],
                    "target_type": op.to_type,
                })

    # --- CALLEE (outgoing: TestStepNode → called method/function) ---
    if node_refid:
        for cal in result.callees:
            if cal.from_refid == node_refid and cal.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CALLEE",
                    "target_uid": refid_to_uid[cal.to_refid],
                    "target_type": cal.to_type,
                })

    # --- COMPOSES (outgoing: TestNode → AssertionNode/TestStepNode) ---
    if node_refid:
        for tc in result.test_compositions:
            if tc.parent_refid == node_refid and tc.child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": refid_to_uid[tc.child_refid],
                    "target_type": tc.child_type,
                })

    # --- OF_TYPE (outgoing: TestFixtureNode → type definition) ---
    if node_refid:
        for fo in result.fixture_of_types:
            if fo.from_refid == node_refid and fo.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "OF_TYPE",
                    "target_uid": refid_to_uid[fo.to_refid],
                    "target_type": fo.to_type,
                })

    # --- CHECKED_BY (outgoing: TestFixtureNode → AssertionNode) ---
    if node_refid:
        for cb in result.fixture_checked_by:
            if cb.from_refid == node_refid and cb.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CHECKED_BY",
                    "target_uid": refid_to_uid[cb.to_refid],
                    "target_type": "AssertionNode",
                })

    # --- DEFINED_IN (outgoing: TestFixtureNode → TestStepNode) ---
    if node_refid:
        for di in result.fixture_defined_in:
            if di.from_refid == node_refid and di.to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEFINED_IN",
                    "target_uid": refid_to_uid[di.to_refid],
                    "target_type": "TestStepNode",
                })

    return edges
