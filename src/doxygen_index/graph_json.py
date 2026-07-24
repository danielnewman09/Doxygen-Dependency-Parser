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

    # ==================================================================
    # Pre-build from_refid → [entries] index maps so _build_node_edges
    # can do O(1) lookups instead of O(n*m) full-table scans.
    # ==================================================================
    from collections import defaultdict

    # compound_refid → [(member_refid, member_type)]
    members_by_compound: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for member in result.members:
        compound = _get_prop(member, "compound_refid")
        mrefid = _get_prop(member, "refid")
        if compound and mrefid:
            members_by_compound[compound].append((mrefid, type(member).__name__))

    # parent_refid → [(child_refid, child_type)] — namespace composes
    composes_by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for comp in result.compositions:
        composes_by_parent[comp.parent_refid].append((comp.child_refid, comp.child_type))

    # file_refid → [(included_refid)] — file includes
    includes_by_file: dict[str, list[str]] = defaultdict(list)
    for inc in result.includes:
        if inc.included_refid:
            includes_by_file[inc.file_refid].append(inc.included_refid)

    # file_refid → [(included_refid)] — namespace includes
    ns_includes_by_file: dict[str, list[str]] = defaultdict(list)
    for inc in result.namespace_includes:
        if inc.included_refid:
            ns_includes_by_file[inc.file_refid].append(inc.included_refid)

    # from_refid → [(to_refid, to_name, to_type)] — invokes
    invokes_by_from: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for inv in result.invokes:
        if inv.to_refid:
            invokes_by_from[inv.from_refid].append(
                (inv.to_refid, inv.to_name or "",
                 refid_to_type.get(inv.to_refid, "MethodNode")))

    # from_refid → [(to_refid, to_name, to_type)] — inherits
    inherits_by_from: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for inh in result.inherits:
        inherits_by_from[inh.from_refid].append(
            (inh.to_refid, inh.to_type, inh.to_name or inh.to_refid or ""))

    # from_refid → [(to_refid, to_type)] — depends_on
    depends_on_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for dep in result.depends_on:
        depends_on_by_from[dep.from_refid].append((dep.to_refid, dep.to_type))

    # from_refid → [(to_refid, to_type)] — verifies
    verifies_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for ver in result.verifies:
        verifies_by_from[ver.from_refid].append((ver.to_refid, ver.to_type))

    # from_refid → [(to_refid, side)] — operands
    operands_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for op in result.operands:
        operands_by_from[op.from_refid].append((op.to_refid, op.side))

    # from_refid → [(to_refid, to_type)] — callees
    callees_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cal in result.callees:
        callees_by_from[cal.from_refid].append((cal.to_refid, cal.to_type))

    # parent_refid → [(child_refid, child_type)] — test compositions
    test_comp_by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for tc in result.test_compositions:
        test_comp_by_parent[tc.parent_refid].append((tc.child_refid, tc.child_type))

    # from_refid → [(to_refid, to_type)] — fixture-of-types
    fot_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for fo in result.fixture_of_types:
        fot_by_from[fo.from_refid].append((fo.to_refid, fo.to_type))

    # from_refid → [(to_refid)] — fixture-checked-by
    fcb_by_from: dict[str, list[str]] = defaultdict(list)
    for cb in result.fixture_checked_by:
        fcb_by_from[cb.from_refid].append(cb.to_refid)

    # from_refid → [(to_refid)] — fixture-defined-in
    fdi_by_from: dict[str, list[str]] = defaultdict(list)
    for di in result.fixture_defined_in:
        fdi_by_from[di.from_refid].append(di.to_refid)

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

            # Build edges for this node (uses pre-built index maps)
            edges = _build_node_edges(
                node, refid_to_uid, refid_to_type, qname_to_uid,
                members_by_compound,
                composes_by_parent,
                includes_by_file,
                ns_includes_by_file,
                invokes_by_from,
                inherits_by_from,
                depends_on_by_from,
                verifies_by_from,
                operands_by_from,
                callees_by_from,
                test_comp_by_parent,
                fot_by_from,
                fcb_by_from,
                fdi_by_from,
            )
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
    refid_to_uid: dict[str, str],
    refid_to_type: dict[str, str],
    qname_to_uid: dict[str, str],
    # Pre-built index maps (from_refid → list of targets)
    members_by_compound: dict[str, list[tuple[str, str]]],
    composes_by_parent: dict[str, list[tuple[str, str]]],
    includes_by_file: dict[str, list[str]],
    ns_includes_by_file: dict[str, list[str]],
    invokes_by_from: dict[str, list[tuple[str, str, str]]],
    inherits_by_from: dict[str, list[tuple[str, str, str]]],  # (to_refid, to_name, to_type)
    depends_on_by_from: dict[str, list[tuple[str, str]]],
    verifies_by_from: dict[str, list[tuple[str, str]]],
    operands_by_from: dict[str, list[tuple[str, str]]],
    callees_by_from: dict[str, list[tuple[str, str]]],
    test_comp_by_parent: dict[str, list[tuple[str, str]]],
    fot_by_from: dict[str, list[tuple[str, str]]],
    fcb_by_from: dict[str, list[str]],
    fdi_by_from: dict[str, list[str]],
) -> list[dict]:
    """Build the edge list for a single node using pre-built index maps.

    All indexes are ``from_refid → [target]`` so each edge type is a
    single dict lookup instead of a full-table scan.
    """
    edges: list[dict] = []
    node_refid = _get_prop(node, "refid")
    node_type = type(node).__name__

    # --- COMPOSES (compound → member) ---
    if node_refid:
        for mrefid, mtype in members_by_compound.get(node_refid, ()):
            if mrefid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": refid_to_uid[mrefid],
                    "target_type": mtype,
                })

    # --- COMPOSES (namespace → child) ---
    if node_type == "NamespaceNode" and node_refid:
        for child_refid, child_type in composes_by_parent.get(node_refid, ()):
            if child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": refid_to_uid[child_refid],
                    "target_type": child_type,
                })

    # --- INCLUDES (file → included file) ---
    if node_type == "FileNode" and node_refid:
        for inc_refid in includes_by_file.get(node_refid, ()):
            target_uid = refid_to_uid.get(inc_refid, inc_refid)
            edges.append({
                "relation_type": "INCLUDES",
                "target_uid": target_uid,
                "target_type": "FileNode",
            })

    # --- INCLUDES (namespace → imported compound) ---
    if node_type == "NamespaceNode" and node_refid:
        for inc_refid in ns_includes_by_file.get(node_refid, ()):
            if inc_refid in refid_to_uid:
                edges.append({
                    "relation_type": "INCLUDES",
                    "target_uid": refid_to_uid[inc_refid],
                    "target_type": "FileNode",
                })

    # --- INVOKES ---
    if node_refid:
        for to_refid, to_name, target_type in invokes_by_from.get(node_refid, ()):
            target_uid = refid_to_uid.get(to_refid)
            if target_uid is None and to_name:
                target_uid = qname_to_uid.get(to_name)
            if target_uid is None:
                continue
            edges.append({
                "relation_type": "INVOKES",
                "target_uid": target_uid,
                "target_type": target_type,
            })

    # --- INHERITS_FROM ---
    if node_refid:
        for to_refid, to_type, to_name in inherits_by_from.get(node_refid, ()):
            target_uid = refid_to_uid.get(to_refid)
            if target_uid is None and to_name:
                target_uid = qname_to_uid.get(to_name)
            if target_uid is not None:
                edges.append({
                    "relation_type": "INHERITS_FROM",
                    "target_uid": target_uid,
                    "target_type": to_type,
                })

    # --- DEPENDS_ON ---
    if node_refid:
        for to_refid, to_type in depends_on_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEPENDS_ON",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- VERIFIES ---
    if node_refid:
        for to_refid, to_type in verifies_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "VERIFIES",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- LEFT_OPERAND / RIGHT_OPERAND ---
    if node_refid:
        for to_refid, side in operands_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                relation = "LEFT_OPERAND" if side == "left" else "RIGHT_OPERAND"
                edges.append({
                    "relation_type": relation,
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": "MethodNode",
                })

    # --- CALLEE ---
    if node_refid:
        for to_refid, to_type in callees_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CALLEE",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- COMPOSES (test → assertions/steps) ---
    if node_refid:
        for child_refid, child_type in test_comp_by_parent.get(node_refid, ()):
            if child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_uid": refid_to_uid[child_refid],
                    "target_type": child_type,
                })

    # --- OF_TYPE ---
    if node_refid:
        for to_refid, to_type in fot_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "OF_TYPE",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- CHECKED_BY ---
    if node_refid:
        for to_refid in fcb_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CHECKED_BY",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": "AssertionNode",
                })

    # --- DEFINED_IN ---
    if node_refid:
        for to_refid in fdi_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEFINED_IN",
                    "target_uid": refid_to_uid[to_refid],
                    "target_type": "TestStepNode",
                })

    return edges
