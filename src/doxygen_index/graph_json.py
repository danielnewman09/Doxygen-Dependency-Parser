"""
Convert a ParseResult to a codegraph LayerGraph-compatible JSON format.

The output is a flat list of serialized node dicts suitable for
LayerGraph deserialization and backend-independent graph inspection.

Each node dict contains:
- ``type``: the codegraph node class name (e.g. ``"ClassNode"``)
- portable semantic node properties (name, qualified_name, etc.)
- ``tags``: provenance tags (set to the project name)
- ``edges``: a list of ``{relation_type, target_key, target_type}`` dicts

Edges are built from the ParseResult's relationship lists (includes,
invokes, composition via ``compound_refid``, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

from doxygen_index.parser.model import ParseResult

from codegraph.models.compound import CompoundNode


# Doxygen locators are extraction/provenance data.  They are deliberately
# retained in ParseResult DTOs and the lookup indexes below, but never cross
# the normal LayerGraph wire boundary.  Parent-relative identities are
# resolved into canonical_key before this policy is applied.
PARSER_LOCATOR_FIELDS = frozenset({
    "refid",
    "compound_refid",
    "member_refid",
    "parent_refid",
    "child_refid",
    "from_refid",
    "to_refid",
})


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


def result_to_graph_json(
    result: ParseResult,
    source: str,
    *,
    text_scan: bool = True,
    portable: bool = True,
) -> list[dict]:
    """Convert a ParseResult to a list of serialized node dicts.

    Args:
        result: The parsed output from ``parse_xml_dir`` or ``parse_python_dir``.
        source: Provenance label (project name) — stored in the node's
            ``source`` field for traceability.
        text_scan: When True (default), also scan project-node text
            fields (type_signature, definition, argsstring, brief
            description, description) for qualified names and emit
            synthetic DEPENDS_ON edges to matching nodes.  This
            enriches JSON exports.  The graph
            DATABASE write path passes ``text_scan=False``: the
            pre-decoupling Neo4j writer created DEPENDS_ON edges only
            from explicit doxygen ``<ref>`` references, and the
            synthetic edges (e.g. a ``definition`` field naming the
            same function in a canonical namespace) are noise in the
            stored graph.
        portable: When True (default), omit parser locator fields such as
            ``refid`` from the normal LayerGraph wire format.  Backend
            persistence passes False so incremental maintenance can retain
            its internal parser indexes.

    Returns:
        A list of dicts, each a serialized node with ``type``,
        properties, ``tags``, and ``edges`` keys.  Suitable for
        ``json.dumps`` and LayerGraph deserialization.
    """
    # Collect all node lists
    from codegraph import ClassNode  # type_param synthesis below

    # ── type_param ClassNode synthesis (TEMPLATE_PARAM targets) ──
    # The Cypher writer creates a lightweight ``kind='type_parameter'``
    # ClassNode per template-param slot (qname ``type_param:<parent>:<pos>``)
    # and links parent → slot via TEMPLATE_PARAM, slot → concept via
    # ENFORCES_CONCEPT.  Reproduce those nodes here so they round-trip
    # through the LayerGraph bridge like any other node.
    type_param_nodes: list = []
    type_param_concept_by_qn: dict[str, str] = {}
    if result.template_param_refs:
        source_by_refid: dict[str, object] = {}
        for lst in (result.classes, result.enums, result.unions,
                    result.interfaces, result.concepts,
                    result.methods, result.attributes, result.functions):
            for n in lst:
                refid = _get_prop(n, "refid")
                if refid:
                    source_by_refid.setdefault(refid, n)
        for tp in result.template_param_refs:
            parent = source_by_refid.get(tp.from_refid)
            if parent is None:
                continue
            parent_qn = _get_prop(parent, "qualified_name") or ""
            if not parent_qn:
                continue
            parent_source = _get_prop(parent, "source") or source
            tp_qn = f"type_param:{parent_qn}:{tp.position}"
            definition = f"position={tp.position}"
            if tp.defval:
                definition += f" defval={tp.defval}"
            type_param_nodes.append(ClassNode(
                qualified_name=tp_qn,
                name=tp.declname or tp.defname or "T",
                kind="type_parameter",
                source=parent_source,
                definition=definition,
                tags=["dependency" if parent_source != source else "as-built"],
            ))
            if tp.concept_qualified_name:
                type_param_concept_by_qn[tp_qn] = tp.concept_qualified_name

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
        result.source_fragments,
        result.functions,
        result.parameters,
        result.tests,
        result.assertions,
        result.test_steps,
        result.test_fixtures,
        result.literals,
        result.implementations,
        type_param_nodes,
    ]

    # ── Canonical-key computation (P2: cg:v1 canonical identity) ──
    #
    # Project nodes get their canonical key under the repository scope
    # ``(source, source)`` (the DDP project label is both project and
    # repository here).  Merged dependency parses retain their own source
    # as the repository component.  This is important for symbols such as
    # ``std``: the project may contain a namespace with that name while the
    # cppreference parse contains a separate namespace that composes the
    # dependency types.  Giving both nodes the project scope collapses them
    # during serialization and silently drops the dependency namespace.
    # Parent-relative types resolve their parent
    # context from the parse result graph:
    #   - ParameterNode / ImplementationNode: parent_callable_key (the
    #     owning member's key, via member_refid);
    #   - SourceFragmentNode: file_key (the declaring FileNode's key);
    #   - TestNode / TestFixtureNode / AssertionNode / TestStepNode:
    #     parent_key (the COMPOSES parent's key).
    # Keys are computed in dependency order (roots first, then
    # parent-relative), cached by node identity.
    from codegraph.identity import IdentityScope, resolve_identity_for

    def _canonical_key(node, *, parents=None) -> str | None:
        try:
            node_source = _get_prop(node, "source") or source
            node_scope = IdentityScope.repository(source, node_source)
            return resolve_identity_for(
                node, node_scope, parents=parents or {}
            ).key()
        except Exception as exc:
            node_type = type(node).__name__
            identifier = (
                _get_prop(node, "qualified_name")
                or _get_prop(node, "name")
                or _get_prop(node, "path")
                or "<unknown>"
            )
            raise ValueError(
                f"canonical-key generation failed for "
                f"{node_type} {identifier!r}"
            ) from exc

    # refid → node (first emission wins) and qname → key maps.
    refid_to_uid: dict[str, str] = {}
    refid_to_type: dict[str, str] = {}
    key_cache: dict[int, str] = {}
    qname_to_uid: dict[str, str] = {}
    qname_to_type: dict[str, str] = {}
    implementation_member_refid_by_id = {
        id(ref.implementation): ref.member_refid
        for ref in result.implementation_refs
    }

    # Pass A: keys for non-parent-relative nodes (roots of the identity
    # graph): files, namespaces, compounds, members, functions, defines,
    # enum values, literals, and synthesized type-param ClassNodes.
    for nodes in node_lists:
        for node in nodes:
            t = type(node).__name__
            if t in (
                "ParameterNode", "ImplementationNode",
                "SourceFragmentNode",
                "TestNode", "TestFixtureNode", "AssertionNode",
                "TestStepNode",
            ):
                continue
            key = _canonical_key(node)
            if not key:
                continue
            key_cache[id(node)] = key
            qn = _get_prop(node, "qualified_name") or ""
            if qn:
                qname_to_uid[qn] = key
                qname_to_type[qn] = t
            refid = _get_prop(node, "refid")
            if refid:
                refid_to_uid[refid] = key
                refid_to_type[refid] = t

    # refid → node for parent lookups (params/implementations/tests).
    refid_to_node: dict[str, object] = {}
    for nodes in node_lists:
        for node in nodes:
            refid = _get_prop(node, "refid")
            if refid:
                refid_to_node.setdefault(refid, node)

    # Pass B: parent-relative keys.
    # member_refid → parent member key (params, implementations).
    def _parent_member_key(member_refid: str) -> str:
        member = refid_to_node.get(member_refid)
        if member is None:
            return ""
        k = key_cache.get(id(member))
        if k:
            return k
        k = _canonical_key(member)
        if k:
            key_cache[id(member)] = k
        return k or ""

    # ParameterNode (parent_callable_key).  Implementations are resolved after
    # test nodes below because a test step can itself own an implementation.
    for nodes in node_lists:
        for node in nodes:
            t = type(node).__name__
            if t != "ParameterNode":
                continue
            prefid = _get_prop(node, "member_refid") or ""
            pk = _parent_member_key(prefid)
            key = _canonical_key(node, parents={"parent_callable_key": pk})
            if key:
                key_cache[id(node)] = key
                qn = _get_prop(node, "qualified_name") or ""
                if qn:
                    qname_to_uid[qn] = key
                    qname_to_type[qn] = t
                refid = _get_prop(node, "refid")
                if refid:
                    refid_to_uid[refid] = key
                    refid_to_type[refid] = t

    # SourceFragmentNode (file_key from the declaring file).
    # FileNode key derives from its repository path.
    for nodes in node_lists:
        for node in nodes:
            if type(node).__name__ != "SourceFragmentNode":
                continue
            fp = _get_prop(node, "file_path") or ""
            fk = ""
            for f in result.files:
                f_path = _get_prop(f, "path") or _get_prop(f, "file_path") or ""
                if f_path == fp:
                    fk = key_cache.get(id(f), "")
                    break
            key = _canonical_key(
                node, parents={"file_key": fk or "cg:v1:root"}
            )
            if key:
                key_cache[id(node)] = key
                qn = _get_prop(node, "qualified_name") or ""
                if qn:
                    qname_to_uid[qn] = key
                    qname_to_type[qn] = "SourceFragmentNode"
                refid = _get_prop(node, "refid")
                if refid:
                    refid_to_uid[refid] = key
                    refid_to_type[refid] = "SourceFragmentNode"

    # TestNode / TestFixtureNode / AssertionNode / TestStepNode
    # (parent_key from the COMPOSES parent's refid).
    for nodes in node_lists:
        for node in nodes:
            t = type(node).__name__
            if t not in ("TestNode", "TestFixtureNode",
                         "AssertionNode", "TestStepNode"):
                continue
            parent_key = ""
            node_refid = _get_prop(node, "refid") or ""
            for tc in result.test_compositions:
                if tc.child_refid == node_refid:
                    parent = refid_to_node.get(tc.parent_refid)
                    if parent is not None:
                        parent_key = key_cache.get(id(parent), "")
                    break
            key = _canonical_key(
                node, parents={"parent_key": parent_key or "cg:v1:root"}
            )
            if key:
                key_cache[id(node)] = key
                qn = _get_prop(node, "qualified_name") or ""
                if qn:
                    qname_to_uid[qn] = key
                    qname_to_type[qn] = t
                refid = _get_prop(node, "refid")
                if refid:
                    refid_to_uid[refid] = key
                    refid_to_type[refid] = t

    # ImplementationNode (parent_callable_key).  This pass must follow test
    # identity resolution: test steps and fixtures can own extracted source
    # implementations and are themselves parent-relative nodes.
    for node in result.implementations:
        prefid = implementation_member_refid_by_id.get(id(node), "")
        pk = _parent_member_key(prefid)
        key = _canonical_key(node, parents={"parent_callable_key": pk})
        if key:
            key_cache[id(node)] = key
            # Implementations share their owner's qualified name and must not
            # replace the callable in the general qualified-name lookup.
            refid = _get_prop(node, "refid")
            if refid:
                refid_to_uid[refid] = key
                refid_to_type[refid] = "ImplementationNode"

    def _key_of_node(node) -> str:
        return key_cache.get(id(node), "")

    # ==================================================================
    # Pre-build from_refid → [entries] index maps so _build_node_edges
    # can do O(1) lookups instead of O(n*m) full-table scans.
    # ==================================================================
    from collections import defaultdict

    # compound_refid → [(member_key, member_type)]
    # Uses the member's OWN key, not a refid→key lookup: doxygen reuses
    # one refid for a member declared in BOTH a primary template and its
    # specialization (different qualified_names ⇒ different keys).  A
    # refid lookup would collapse both to one key and COMPOSES both
    # parents to the same node — multi-parent tree → duplicate edges in
    # every exporter.  The pre-decoupling writer targeted ``m.uid``.
    members_by_compound: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for member in result.members:
        compound = _get_prop(member, "compound_refid")
        mkey = _key_of_node(member)
        if compound and mkey:
            members_by_compound[compound].append((mkey, type(member).__name__))
    # Nested compounds (class-scoped enums/structs) are composed by their
    # owning compound exactly like members — they are not namespace children.
    for enum in result.enums:
        compound = _get_prop(enum, "compound_refid")
        ekey = _key_of_node(enum)
        if compound and ekey:
            members_by_compound[compound].append((ekey, type(enum).__name__))

    # parent_refid → [(child_refid, child_type)] — namespace composes
    composes_by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for comp in result.compositions:
        composes_by_parent[comp.parent_refid].append((comp.child_refid, comp.child_type))

    # file_refid → [(included_refid, spelling, is_local)] — file includes
    includes_by_file: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for inc in result.includes:
        includes_by_file[inc.file_refid].append(
            (inc.included_refid, inc.included_file, inc.is_local)
        )

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

    # from_refid → [(to_refid, to_type)] — concept_constraints (CONSTRAINS)
    concept_constraints_by_from: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for cc in result.concept_constraints:
        concept_constraints_by_from[cc.from_refid].append((cc.to_refid, cc.to_type))

    # member_refid → [param_uid] — HAS_PARAMETER.  Also assigns the
    # deterministic param uid onto the node so serialize() emits it.
    param_uids_by_member_refid: dict[str, list[str]] = defaultdict(list)
    for p in result.parameters:
        prefid = _get_prop(p, "member_refid") or ""
        if not prefid:
            continue
        pkey = _key_of_node(p)
        if pkey:
            param_uids_by_member_refid[prefid].append(pkey)

    # file_path → FileNode uid — DEFINED_IN (location-based: compounds/
    # members/namespaces are "defined in" the file that declares them).
    #
    # Use each FileNode's OWN computed uid, NOT refid_to_uid: in Python
    # parses a FileNode and its module NamespaceNode share the same refid
    # (e.g. ``samplepkg.backend``), so the refid table resolves to whatever
    # node won the collision (the namespace) and DEFINED_IN would point at
    # the namespace instead of the file.
    file_uid_by_path: dict[str, str] = {}
    for f in result.files:
        fp = _get_prop(f, "path") or _get_prop(f, "file_path") or ""
        fkey = _key_of_node(f)
        if fp and fkey:
            file_uid_by_path[fp] = fkey

    # Implementation identities are parent-relative and already resolved
    # above.  Build this object-identity index once, rather than rebuilding a
    # dictionary containing every implementation for every serialized node.
    implementation_keys_by_id = {
        id(node): _key_of_node(node)
        for node in result.implementations
    }

    # Serialize each node and attach edges
    serialized: list[dict] = []
    # key → index into ``serialized`` (WP6.1 duplicate-key elimination)
    key_to_idx: dict[str, int] = {}

    for nodes in node_lists:
        for node in nodes:
            # WP A: the canonical key is the sole identity — stamp it on
            # the node (serialize()/deserialize() round-trip it) so
            # LayerGraph.deserialize (canonical-only) can rebuild the graph.
            node_key = _key_of_node(node)
            if node_key:
                try:
                    node.canonical_key = node_key
                except Exception:
                    pass
            entry = node.serialize()
            entry["canonical_key"] = node_key
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

            # ``serialize()`` only emits ``_llm_fields``; the DB round-trip
            # needs the FULL declared property set (refid, identity fields
            # like ParameterNode's member_refid/position, is_static,
            # body_start, ...) or ``save()`` writes defaults and the graph
            # loses data / collapses distinct nodes onto one uid.
            from codegraph.models.descriptors import PropertyRegistry

            declared = PropertyRegistry.properties_of(type(node))
            for pname in declared:
                if portable and pname in PARSER_LOCATOR_FIELDS:
                    continue
                if pname in entry:
                    continue
                val = _raw_prop(node, pname)
                if val is None or val == "" or val == [] or val == {}:
                    continue
                entry[pname] = val

            # Build edges for this node (uses pre-built index maps)
            edges = _build_node_edges(
                node, result, refid_to_uid, refid_to_type, qname_to_uid,
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
                concept_constraints_by_from,
                param_uids_by_member_refid,
                file_uid_by_path,
                implementation_keys_by_id,
                type_param_concept_by_qn,
            )
            # Filter out self-references (edge target_key == this node's key).
            node_key = _key_of_node(node)
            edges = [e for e in edges if e.get("target_key", "") != node_key]
            if edges:
                entry["edges"] = edges

            # Dedupe by uid (Priority 2, WP6.1): the doxygen XML and the
            # source text-scan can emit the SAME symbol twice under one
            # uid — e.g. a class whose malformed member span is also
            # scanned as a ``namespace``.  The authoritative compound
            # (any ``CompoundNode`` subclass: class/struct/interface/…)
            # wins over a NamespaceNode; otherwise the first emission
            # stands.  Mirrors codegraph's no-last-write-wins contract
            # (``LayerGraph._register_entry`` raises on distinct nodes
            # sharing one uid).
            entry_key = _key_of_node(node)
            existing_idx = key_to_idx.get(entry_key)
            if existing_idx is not None:
                existing = serialized[existing_idx]
                existing_is_namespace = existing.get("type") == "NamespaceNode"
                incoming_is_compound = issubclass(
                    type(node), CompoundNode
                )
                if existing_is_namespace and incoming_is_compound:
                    # Replace the text-scan namespace with the XML class.
                    serialized[existing_idx] = entry
                # else keep the existing entry (compound or equal kind)
                continue
            key_to_idx[entry_key] = len(serialized)
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
    #
    # The DATABASE write path disables this (text_scan=False) to match
    # the pre-decoupling writer, which emitted DEPENDS_ON only from
    # explicit references.
    if not text_scan:
        return serialized
    import re
    _QNAME_RE = re.compile(r'\b(\w+(?:::\w+)+)\b')
    for entry in serialized:
        node_source = entry.get("source", "")
        if node_source != source:
            continue  # Only scan project-owned nodes

        # Collect known target_keys to avoid duplicates
        existing_targets = {e.get("target_key", "") for e in entry.get("edges", [])}

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
                    "target_key": qname_to_uid[qn],
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


def _raw_prop(node, name: str):
    """Extract a raw property value (any type) from a codegraph node.

    Like :func:`_get_prop` but does not restrict to str/list — needed
    for int/bool identity fields (e.g. ParameterNode ``position``).
    """
    if not hasattr(node, name):
        return None
    val = node.__dict__.get(name, None)
    if val is None:
        props = node.__dict__.get("_props", None)
        if isinstance(props, dict):
            val = props.get(name)
    if val is None:
        val = node.__dict__.get(f"_{name}", None)
    if val is None:
        val = getattr(node, name, None)
        if isinstance(val, type(node)):  # descriptor leaked on unsaved instance
            return None
    return val


def _get_prop(node, name: str, *, is_list: bool = False) -> str | list | None:
    """Extract a property from a codegraph node safely.

    Handles both storage shapes:
    - legacy neomodel nodes: properties stored directly in ``__dict__``
      (possibly with a leading underscore, e.g. ``_tags``).
    - new Property-based codegraph models: declared properties stored in
      ``node._props``.

    ``getattr(node, name)`` may return the descriptor rather than the
    value on a brand-new (unsaved) instance, so the stores above are
    consulted first.
    """
    if not hasattr(node, name):
        return None
    val = node.__dict__.get(name, None)
    if val is None:
        # New Property-based codegraph model: declared properties live in
        # ``_props`` (a plain dict on the instance).
        props = node.__dict__.get("_props", None)
        if isinstance(props, dict):
            val = props.get(name)
    if val is None:
        # Legacy underscore-prefixed storage (e.g. ``_tags``).
        val = node.__dict__.get(f"_{name}", None)
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
    # Pre-built index maps (from_refid → list of targets)
    members_by_compound: dict[str, list[tuple[str, str, str]]],
    composes_by_parent: dict[str, list[tuple[str, str]]],
    includes_by_file: dict[str, list[tuple[str, str, bool]]],
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
    concept_constraints_by_from: dict[str, list[tuple[str, str]]],
    param_uids_by_member_refid: dict[str, list[str]],
    file_uid_by_path: dict[str, str],
    implementation_keys_by_id: dict[int, str],
    type_param_concept_by_qn: dict[str, str],
) -> list[dict]:
    """Build the edge list for a single node using pre-built index maps.

    All indexes are ``from_refid → [target]`` so each edge type is a
    single dict lookup instead of a full-table scan.
    """
    edges: list[dict] = []
    node_refid = _get_prop(node, "refid")
    node_type = type(node).__name__

    # --- COMPOSES (compound → member) ---
    # Only compounds (classes/interfaces/unions/enums/concepts) compose
    # members.  File-scope members (typedefs like ``sqlite3``, global
    # variables) carry a *file* compound_refid — composing them under
    # the FileNode absorbs them into the excluded file in every
    # exporter (they vanish from views and their incoming edges drop).
    # The pre-decoupling writer matched ``from_label="CompoundNode"``
    # and never created file→member COMPOSES.
    if node_refid and issubclass(type(node), CompoundNode):
        for mkey, mtype in members_by_compound.get(node_refid, ()):
            if mkey:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_key": mkey,
                    "target_type": mtype,
                })

    # --- COMPOSES (namespace → child) ---
    if node_type == "NamespaceNode" and node_refid:
        for child_refid, child_type in composes_by_parent.get(node_refid, ()):
            if child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_key": refid_to_uid[child_refid],
                    "target_type": child_type,
                })

    # --- INCLUDES (file → included file) ---
    if node_type == "FileNode" and node_refid:
        for inc_refid, inc_spelling, inc_local in includes_by_file.get(node_refid, ()):
            target_key_v = refid_to_uid.get(inc_refid)
            edge = {
                "relation_type": "INCLUDES",
                "target_type": "FileNode",
            }
            if target_key_v:
                edge["target_key"] = target_key_v
            else:
                # External include (system header not in the parse): a
                # human-readable ref that the canonical deserializer can
                # materialize as a scaffold when scoped; never stored as
                # a fabricated target_key.
                edge["target_ref"] = inc_refid or inc_spelling
            # Carry the include spelling as written in the source (the
            # ``<includes>`` element text, sans quotes/brackets) plus the
            # local/system flag — the relationship metadata codegen needs
            # to re-emit ``#include`` lines.
            if inc_spelling:
                edge["include"] = inc_spelling
                edge["local"] = inc_local
            edges.append(edge)

    # --- INCLUDES (namespace → imported compound) ---
    if node_type == "NamespaceNode" and node_refid:
        for inc_refid in ns_includes_by_file.get(node_refid, ()):
            if inc_refid in refid_to_uid:
                edges.append({
                    "relation_type": "INCLUDES",
                    "target_key": refid_to_uid[inc_refid],
                    "target_type": refid_to_type.get(inc_refid, "CompoundNode"),
                })

    # --- INVOKES ---
    if node_refid:
        for to_refid, to_name, target_type in invokes_by_from.get(node_refid, ()):
            target_key_v = refid_to_uid.get(to_refid)
            if target_key_v is None and to_name:
                target_key_v = qname_to_uid.get(to_name)
            if target_key_v is None:
                continue
            edges.append({
                "relation_type": "INVOKES",
                "target_key": target_key_v,
                "target_type": target_type,
            })

    # --- INHERITS_FROM ---
    if node_refid:
        for to_refid, to_type, to_name in inherits_by_from.get(node_refid, ()):
            target_key_v = refid_to_uid.get(to_refid)
            if target_key_v is None and to_name:
                target_key_v = qname_to_uid.get(to_name)
            if target_key_v is not None:
                edges.append({
                    "relation_type": "INHERITS_FROM",
                    "target_key": target_key_v,
                    "target_type": to_type,
                })

    # --- DEPENDS_ON ---
    if node_refid:
        for to_refid, to_type in depends_on_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEPENDS_ON",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- VERIFIES ---
    if node_refid:
        for to_refid, to_type in verifies_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "VERIFIES",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- LEFT_OPERAND / RIGHT_OPERAND ---
    if node_refid:
        for to_refid, side in operands_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                relation = "LEFT_OPERAND" if side == "left" else "RIGHT_OPERAND"
                edges.append({
                    "relation_type": relation,
                    "target_key": refid_to_uid[to_refid],
                    "target_type": "MethodNode",
                })

    # --- CALLEE ---
    if node_refid:
        for to_refid, to_type in callees_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CALLEE",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- COMPOSES (test → assertions/steps) ---
    if node_refid:
        for child_refid, child_type in test_comp_by_parent.get(node_refid, ()):
            if child_refid in refid_to_uid:
                edges.append({
                    "relation_type": "COMPOSES",
                    "target_key": refid_to_uid[child_refid],
                    "target_type": child_type,
                })

    # --- OF_TYPE ---
    if node_refid:
        for to_refid, to_type in fot_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "OF_TYPE",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- CHECKED_BY ---
    if node_refid:
        for to_refid in fcb_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CHECKED_BY",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": "AssertionNode",
                })

    # --- DEFINED_IN ---
    if node_refid:
        for to_refid in fdi_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "DEFINED_IN",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": "TestStepNode",
                })

    # --- CONSTRAINS (concept → compound/concept) ---
    # Direction is referenced → referencer.
    if node_refid:
        for to_refid, to_type in concept_constraints_by_from.get(node_refid, ()):
            if to_refid in refid_to_uid:
                edges.append({
                    "relation_type": "CONSTRAINS",
                    "target_key": refid_to_uid[to_refid],
                    "target_type": to_type,
                })

    # --- HAS_PARAMETER (member → parameter) ---
    if node_refid:
        for pkey in param_uids_by_member_refid.get(node_refid, ()):
            edges.append({
                "relation_type": "HAS_PARAMETER",
                "target_key": pkey,
                "target_type": "ParameterNode",
            })

    # --- TEMPLATE_PARAM (compound/member → type_param slot) ---
    node_qn = _get_prop(node, "qualified_name") or ""
    if node_refid and node_qn:
        for tp in result.template_param_refs:
            if tp.from_refid != node_refid:
                continue
            tp_qn = f"type_param:{node_qn}:{tp.position}"
            tp_key = qname_to_uid.get(tp_qn)
            if tp_key:
                edges.append({
                    "relation_type": "TEMPLATE_PARAM",
                    "target_key": tp_key,
                    "target_type": "ClassNode",
                })

    # --- ENFORCES_CONCEPT (type_param slot → concept) ---
    concept_qn = type_param_concept_by_qn.get(node_qn, "")
    if concept_qn:
        concept_key = qname_to_uid.get(concept_qn)
        if concept_key:
            edges.append({
                "relation_type": "ENFORCES_CONCEPT",
                "target_key": concept_key,
                "target_type": "ConceptNode",
            })

    # --- SPECIALIZES (specialization → primary template) ---
    if node_qn:
        for sr in result.specializes_refs:
            if sr.from_qualified_name != node_qn:
                continue
            prim_key = qname_to_uid.get(sr.primary_template_qualified_name)
            if prim_key:
                edges.append({
                    "relation_type": "SPECIALIZES",
                    "target_key": prim_key,
                    "target_type": "CompoundNode",
                })

    # --- HAS_IMPLEMENTATION (member → implementation node) ---
    if node_refid:
        for ref in result.implementation_refs:
            if ref.member_refid != node_refid:
                continue
            impl_key = implementation_keys_by_id.get(id(ref.implementation), "")
            if impl_key:
                edges.append({
                    "relation_type": "HAS_IMPLEMENTATION",
                    "target_key": impl_key,
                    "target_type": "ImplementationNode",
                })

    # --- DEFINED_IN (location-based: node → declaring file) ---
    fp = _get_prop(node, "file_path") or ""
    if fp:
        fkey = file_uid_by_path.get(fp)
        if fkey:
            edges.append({
                "relation_type": "DEFINED_IN",
                "target_key": fkey,
                "target_type": "FileNode",
            })

    return edges
