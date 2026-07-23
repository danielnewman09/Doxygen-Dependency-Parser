"""
CSV export — serialise a ParseResult to CSV files for Neo4j import.

Produces files compatible with ``neo4j-admin database import full``
(Neo4j 5.x):

* ``nodes.csv`` — all nodes with ``uid:ID``, ``:LABEL``, and properties.
* ``relationships.csv`` — all edges with ``:START_ID``, ``:END_ID``,
  and ``:TYPE``.

Arrays (``base_classes``, ``tags``) are serialised as JSON strings;
load them into Neo4j with ``apoc.convert.fromJsonList`` or a post-import
Cypher script.

Basic usage::

    from doxygen_index.csv_export import export_csv

    export_csv(result, source="cppreference", output_dir="import/csv")
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from doxygen_index.parser import ParseResult
from doxygen_index.parser.model import (
    CompositionEntry,
    InheritsEntry,
    IncludeEntry,
)


def _ensure_uid(node, default_source: str = "") -> str:
    """Compute and set the deterministic ``uid`` on *node*, return it.

    If the node has no ``source`` attribute, *default_source* is used.
    """
    from codegraph.uid import compute_uid, normalize_argsstring

    # Build identity values from the canonical _identity_fields tuple.
    identity_fields = list(getattr(node, "_identity_fields", ()) or ())
    identity_values: list[str] = []
    for field in identity_fields:
        val = getattr(node, field, "")
        val_str = str(val) if val is not None else ""
        if field == "argsstring":
            val_str = normalize_argsstring(val_str)
        identity_values.append(val_str)

    source = str(getattr(node, "source", "") or default_source or "")
    # source must be non-empty for compute_uid to produce a valid hash
    if not source:
        # Fallback: derive from qualified_name or name
        source = str(getattr(node, "qualified_name", "") or getattr(node, "name", "") or "unknown")
    uid = compute_uid(source, *identity_values)

    # Set in-place so the object carries it for the rest of the pipeline.
    if hasattr(node, "uid"):
        node.uid = uid
    return uid


def _serialize_list(val) -> str:
    """Serialize a list value for CSV storage.

    Neo4j-admin import uses ``;`` as the default array delimiter.
    We use JSON arrays so that the values are unambiguous even when
    individual items contain semicolons.
    """
    if not val:
        return ""
    if isinstance(val, list):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _node_label(node) -> str:
    """Return the Neo4j label for a codegraph node."""
    return type(node).__name__


# ---------------------------------------------------------------------------
# Node row builders
# ---------------------------------------------------------------------------

def _node_row(node) -> dict[str, str]:
    """Build a flat dict of CSV-safe strings for a single node."""
    uid = _ensure_uid(node)
    row: dict[str, str] = {
        "uid:ID": uid,
        ":LABEL": _node_label(node),
    }

    # Collect all neomodel property names
    prop_names = getattr(node.__class__, "__all_properties__", None)
    if prop_names is None:
        # Fallback for codegraph nodes that don't expose __all_properties__
        prop_names = [
            k for k in dir(node)
            if not k.startswith("_") and k not in ("DoesNotExist", "objects", "save", "delete")
        ]

    for name, _prop in prop_names if isinstance(prop_names[0] if prop_names else None, tuple) else [(n, None) for n in prop_names]:
        if name in ("uid",):  # already handled
            continue
        val = getattr(node, name, None)
        if val is None or val == "":
            row[name] = ""
        elif isinstance(val, list):
            row[name] = _serialize_list(val)
        elif isinstance(val, bool):
            row[name] = str(val).lower()
        else:
            row[name] = str(val)

    return row


def _export_nodes(result: ParseResult, output_dir: Path) -> int:
    """Write ``nodes.csv`` from all node lists in *result*."""
    node_lists: list[tuple[str, list]] = [
        ("FileNode", result.files),
        ("NamespaceNode", result.namespaces),
        ("ClassNode", result.classes),
        ("EnumNode", result.enums),
        ("UnionNode", result.unions),
        ("InterfaceNode", result.interfaces),
        ("ConceptNode", result.concepts),
        ("MethodNode", result.methods),
        ("AttributeNode", result.attributes),
        ("EnumValueNode", result.enum_values),
        ("DefineNode", result.defines),
        ("FunctionNode", result.functions),
        ("ParameterNode", result.parameters),
        ("ImplementationNode", result.implementations),
        ("TestNode", result.tests),
        ("AssertionNode", result.assertions),
        ("TestStepNode", result.test_steps),
        ("TestFixtureNode", result.test_fixtures),
        ("LiteralNode", result.literals),
    ]

    # First pass: collect all field names across all nodes.
    all_fields: set[str] = set()
    rows: list[dict[str, str]] = []
    for label, nodes in node_lists:
        for node in nodes:
            row = _node_row(node)
            rows.append(row)
            all_fields.update(row.keys())

    if not rows:
        print("  No nodes to export.")
        return 0

    # Stable field order: uid, label, then alphabetical properties.
    ordered_fields = ["uid:ID", ":LABEL"] + sorted(
        f for f in all_fields if f not in ("uid:ID", ":LABEL")
    )

    nodes_path = output_dir / "nodes.csv"
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=ordered_fields, extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"  Nodes: {nodes_path} ({len(rows)} rows, {len(ordered_fields)} columns)")
    return len(rows)


# ---------------------------------------------------------------------------
# Relationship helpers
# ---------------------------------------------------------------------------

def _ensure_uid_for_refid(refid: str, refid_to_uid: dict[str, str], nodes) -> str:
    """Look up the uid for a refid, computing it if needed."""
    if refid in refid_to_uid:
        return refid_to_uid[refid]
    # Try to find by refid in a node list
    for node in nodes:
        if getattr(node, "refid", "") == refid:
            uid = _ensure_uid(node)
            refid_to_uid[refid] = uid
            return uid
    return ""


def _export_relationships(result: ParseResult, output_dir: Path) -> int:
    """Write ``relationships.csv`` from all relationship lists in *result*.

    Relationships derived:
    - COMPOSES: namespace → child (namespace, class, function, etc.)
    - DEFINED_IN: compound/member → file
    - INHERITS_FROM: class → base class (via base_classes names)
    - INCLUDES: file → file
    - HAS_PARAMETER: method/function → parameter
    - INVOKES: method/function → method/function
    - TEMPLATE_PARAM: class → type_parameter
    """
    rel_rows: list[dict[str, str]] = []

    # Build refid → uid index across all node types
    refid_to_uid: dict[str, str] = {}
    uid_set: set[str] = set()

    all_node_lists = (
        result.files + result.namespaces +
        result.classes + result.enums + result.unions +
        result.interfaces + result.concepts +
        result.methods + result.attributes +
        result.enum_values + result.defines +
        result.functions + result.parameters +
        result.implementations + result.tests +
        result.assertions + result.test_steps +
        result.test_fixtures + result.literals
    )

    for node in all_node_lists:
        uid = _ensure_uid(node)
        uid_set.add(uid)
        refid = getattr(node, "refid", "")
        if refid:
            refid_to_uid[refid] = uid

    # Build name/qualified_name → uid index for inheritance resolution
    compound_by_name: dict[str, str] = {}
    for node in result.classes + result.interfaces:
        uid = _ensure_uid(node)
        name = getattr(node, "name", "")
        qn = getattr(node, "qualified_name", "")
        if name:
            compound_by_name[name] = uid
        if qn:
            compound_by_name[qn] = uid

    def _add_rel(start_uid: str, end_uid: str, rel_type: str, **props) -> None:
        if not start_uid or not end_uid:
            return
        if start_uid not in uid_set or end_uid not in uid_set:
            return
        row = {":START_ID": start_uid, ":END_ID": end_uid, ":TYPE": rel_type}
        row.update({k: _serialize_list(v) if isinstance(v, list) else str(v) for k, v in props.items()})
        rel_rows.append(row)

    # ── 1. COMPOSES: namespace → child ──────────────────────────
    ns_by_qname: dict[str, str] = {}
    for ns in result.namespaces:
        uid = _ensure_uid(ns)
        ns_by_qname[ns.qualified_name] = uid

    def _ns_for(qn: str, module: str = "") -> str | None:
        if module and "." in qn:
            return module
        separator = "::" if "::" in qn else "."
        parts = qn.rsplit(separator, 1)
        return parts[0] if len(parts) > 1 else None

    for cls in result.classes:
        ns_qn = _ns_for(cls.qualified_name, getattr(cls, "module", ""))
        ns_uid = ns_by_qname.get(ns_qn or "")
        cls_uid = _ensure_uid(cls)
        if ns_uid:
            _add_rel(ns_uid, cls_uid, "COMPOSES")

    for func in result.functions:
        ns_qn = _ns_for(func.qualified_name)
        ns_uid = ns_by_qname.get(ns_qn or "")
        func_uid = _ensure_uid(func)
        if ns_uid:
            _add_rel(ns_uid, func_uid, "COMPOSES")

    for ns in result.namespaces:
        parent_qn = _ns_for(ns.qualified_name)
        parent_uid = ns_by_qname.get(parent_qn or "")
        child_uid = _ensure_uid(ns)
        if parent_uid:
            _add_rel(parent_uid, child_uid, "COMPOSES")

    # Namespace → enum, interface, union, concept
    for entities in (result.enums, result.unions, result.interfaces, result.concepts):
        for ent in entities:
            ns_qn = _ns_for(ent.qualified_name, getattr(ent, "module", ""))
            ns_uid = ns_by_qname.get(ns_qn or "")
            ent_uid = _ensure_uid(ent)
            if ns_uid:
                _add_rel(ns_uid, ent_uid, "COMPOSES")

    print(f"  COMPOSES: {sum(1 for r in rel_rows if r[':TYPE'] == 'COMPOSES')} edges")

    # ── 2. DEFINED_IN: compound/member → file ───────────────────
    file_by_path: dict[str, str] = {}
    for fnode in result.files:
        file_by_path[fnode.path] = _ensure_uid(fnode)

    for node_list in (result.classes, result.methods, result.functions,
                      result.enums, result.interfaces):
        for node in node_list:
            fp = getattr(node, "file_path", "") or ""
            file_uid = file_by_path.get(fp)
            node_uid = _ensure_uid(node)
            if file_uid and node_uid:
                _add_rel(node_uid, file_uid, "DEFINED_IN")

    # Also namespaces → file
    for ns in result.namespaces:
        fp = getattr(ns, "file_path", "") or ""
        file_uid = file_by_path.get(fp)
        if not file_uid:
            continue
        ns_uid = _ensure_uid(ns)
        _add_rel(ns_uid, file_uid, "DEFINED_IN")

    print(f"  DEFINED_IN: {sum(1 for r in rel_rows if r[':TYPE'] == 'DEFINED_IN')} edges")

    # ── 3. INHERITS_FROM: class → base ──────────────────────────
    for cls in result.classes + result.interfaces:
        bases = getattr(cls, "base_classes", []) or []
        cls_uid = _ensure_uid(cls)
        for base_name in bases:
            base_uid = compound_by_name.get(base_name)
            if base_uid:
                _add_rel(cls_uid, base_uid, "INHERITS_FROM")

    print(f"  INHERITS_FROM: {sum(1 for r in rel_rows if r[':TYPE'] == 'INHERITS_FROM')} edges")

    # ── 4. INCLUDES: file → file ────────────────────────────────
    for inc in result.includes:
        src_uid = refid_to_uid.get(inc.file_refid, "")
        tgt_uid = refid_to_uid.get(inc.included_refid, "")
        _add_rel(src_uid, tgt_uid, "INCLUDES",
                 is_local=str(inc.is_local).lower())

    # Also namespace-level includes
    for inc in result.namespace_includes:
        src_uid = refid_to_uid.get(inc.file_refid, "")
        tgt_uid = refid_to_uid.get(inc.included_refid, "")
        _add_rel(src_uid, tgt_uid, "INCLUDES",
                 is_local=str(inc.is_local).lower())

    print(f"  INCLUDES: {sum(1 for r in rel_rows if r[':TYPE'] == 'INCLUDES')} edges")

    # ── 5. HAS_PARAMETER: member → parameter ────────────────────
    for param in result.parameters:
        member_refid = getattr(param, "member_refid", "")
        member_uid = refid_to_uid.get(member_refid, "")
        param_uid = _ensure_uid(param)
        if member_uid:
            _add_rel(member_uid, param_uid, "HAS_PARAMETER",
                     position=str(getattr(param, "position", 0)))

    print(f"  HAS_PARAMETER: {sum(1 for r in rel_rows if r[':TYPE'] == 'HAS_PARAMETER')} edges")

    # ── 6. INVOKES: caller → callee ─────────────────────────────
    for inv in result.invokes:
        from_uid = refid_to_uid.get(inv.from_refid, "")
        to_uid = refid_to_uid.get(inv.to_refid, "")
        _add_rel(from_uid, to_uid, "INVOKES")

    print(f"  INVOKES: {sum(1 for r in rel_rows if r[':TYPE'] == 'INVOKES')} edges")

    # ── 7. DEPENDS_ON: caller → type ───────────────────────────
    for dep in result.depends_on:
        from_uid = refid_to_uid.get(dep.from_refid, "")
        to_uid = refid_to_uid.get(dep.to_refid, "")
        _add_rel(from_uid, to_uid, "DEPENDS_ON")

    print(f"  DEPENDS_ON: {sum(1 for r in rel_rows if r[':TYPE'] == 'DEPENDS_ON')} edges")

    # ── Write CSV ───────────────────────────────────────────────
    if not rel_rows:
        print("  No relationships to export.")
        return 0

    # Collect all property columns across relationship rows
    rel_fields: set[str] = {":START_ID", ":END_ID", ":TYPE"}
    for row in rel_rows:
        rel_fields.update(row.keys())

    ordered_rel_fields = [":START_ID", ":END_ID", ":TYPE"] + sorted(
        f for f in rel_fields if f not in (":START_ID", ":END_ID", ":TYPE")
    )

    rels_path = output_dir / "relationships.csv"
    with open(rels_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=ordered_rel_fields, extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rel_rows)

    print(f"  Relationships: {rels_path} ({len(rel_rows)} rows, {len(ordered_rel_fields)} columns)")
    return len(rel_rows)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_csv(
    result: ParseResult,
    source: str,
    output_dir: Path | str,
) -> tuple[Path, Path]:
    """Export a ParseResult to Neo4j-import-compatible CSV files.

    Args:
        result: The parsed data from any parser.
        source: Source label (e.g. ``"cppreference"``).
        output_dir: Directory to write CSV files into.

    Returns:
        ``(nodes_csv_path, relationships_csv_path)``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nExporting CSV to {output_dir} ...")

    node_count = _export_nodes(result, output_dir)
    rel_count = _export_relationships(result, output_dir)

    print(f"\nDone: {node_count} nodes, {rel_count} relationships.")

    # Write a helper import script
    _write_load_script(output_dir)

    return output_dir / "nodes.csv", output_dir / "relationships.csv"


def _write_load_script(output_dir: Path) -> None:
    """Write a ``load.cypher`` script that uses LOAD CSV to import data.

    This is an alternative to ``neo4j-admin import`` — it can be run
    against a live Neo4j database via ``cypher-shell`` or the Neo4j
    Browser.
    """
    script = output_dir / "load.cypher"
    script.write_text("""\
// ---------------------------------------------------------------------------
// LOAD CSV import script for cppreference CSV export.
//
// Usage:
//   cypher-shell -u neo4j -p <password> -f load.cypher
//
// Or paste into Neo4j Browser one section at a time.
//
// Prerequisites:
//   1. Place nodes.csv and relationships.csv in the Neo4j import/ directory.
//   2. Ensure apoc is installed (for JSON array conversion) or adjust
//      the array-handling on END_ID markers below.
// ---------------------------------------------------------------------------

// ── 1. Create uniqueness constraint ──────────────────────────────────
CREATE CONSTRAINT unique_node_uid IF NOT EXISTS
FOR (n:Node) REQUIRE n.uid IS UNIQUE;

// ── 2. Load nodes ────────────────────────────────────────────────────
LOAD CSV WITH HEADERS FROM 'file:///nodes.csv' AS row
CALL {
  WITH row
  CREATE (n)
  SET n = row
  SET n.uid = row.`uid:ID`
  // Convert JSON arrays to Neo4j string arrays
  FOREACH (_ IN CASE WHEN row.base_classes IS NOT NULL AND row.base_classes <> ''
    THEN [1] ELSE [] END |
    SET n.base_classes = apoc.convert.fromJsonList(row.base_classes)
  )
  FOREACH (_ IN CASE WHEN row.tags IS NOT NULL AND row.tags <> ''
    THEN [1] ELSE [] END |
    SET n.tags = apoc.convert.fromJsonList(row.tags)
  )
  // Set the label dynamically
  CALL apoc.create.addLabels(n, [row.`:LABEL`]) YIELD node
  RETURN count(*) AS cnt
} IN TRANSACTIONS OF 5000 ROWS;

// ── 3. Load relationships ───────────────────────────────────────────
LOAD CSV WITH HEADERS FROM 'file:///relationships.csv' AS row
CALL {
  WITH row
  MATCH (start {uid: row.`:START_ID`})
  MATCH (end {uid: row.`:END_ID`})
  CALL apoc.create.relationship(start, row.`:TYPE`, {}, end)
  YIELD rel
  RETURN count(*) AS cnt
} IN TRANSACTIONS OF 5000 ROWS;

// ── 4. Clean up label-less nodes from step 2 ──────────────────────
// (The CALL subquery creates nodes without labels first; we remove
//  any that didn't get a label assigned, though in practice all should.)
MATCH (n) WHERE labels(n) = []
DETACH DELETE n;
""")
    print(f"  Load script: {script}")
