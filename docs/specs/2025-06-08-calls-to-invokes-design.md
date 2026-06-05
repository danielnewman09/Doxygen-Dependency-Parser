# Issue 1: CALLS → INVOKES Rename — Design

**Date:** 2025-06-08
**Status:** Approved
**Scope:** Write path only — rename CALLS→INVOKES in the ingestion pipeline (parser, backend, example). Query path (tools.py, MCP server) is a separate issue.

## Overview

codegraph defines `MethodNode.invokes = RelationshipTo('MethodNode', 'INVOKES')` for
call-callee relationships. doxygen-index writes `[:CALLS]` edges. This mismatch means
the dependency layer and design layer store the same concept under different
relationship types.

This spec covers the **write path** — renaming the parser data model and Neo4j
ingestion to use INVOKES terminology. The **query path** (tools.py, MCP server)
will be addressed separately by migrating query methods into codegraph's
`GraphRepository`, which also resolves the stale label issue (Issue 2).

**Migration:** Clean break — no backward compatibility with existing `[:CALLS]` edges.
Re-ingest after changes.

## 1. Parser data model — `parser.py`

### 1.1 Rename CallEntry → InvokeEntry

```python
@dataclass
class InvokeEntry:
    from_refid: str
    to_refid: str
    to_name: str
```

### 1.2 Rename ParseResult fields

- `calls` → `invokes: list[InvokeEntry]`
- `called_by` → `invoked_by: list[InvokeEntry]`

### 1.3 Update _parse_member() references

- `result.calls.append(CallEntry(...))` → `result.invokes.append(InvokeEntry(...))`
- `result.called_by.append(CallEntry(...))` → `result.invoked_by.append(InvokeEntry(...))`

## 2. Write path — `neo4j_backend.py`

### 2.1 Rename _write_call_relationships → _write_invoke_relationships

- Cypher: `MERGE (caller)-[:CALLS]->(callee)` → `MERGE (invoker)-[:INVOKES]->(invokee)`
- Cypher label fix: `MATCH (caller:Member)` / `MATCH (callee:Member)` → `MATCH (caller:Method|Function)` / `MATCH (callee:Method|Function)`
- Print: `"Calls:"` → `"Invokes:"`
- Parameter: `result.calls` → `result.invokes`

### 2.2 Update write_result() call site

- `_write_call_relationships(result)` → `_write_invoke_relationships(result)`

## 3. Example script — `examples/doxygen_to_neo4j.py`

- `_write_call_relationships()` → `_write_invoke_relationships()`
- `[:CALLS]` → `[:INVOKES]`
- `CallEntry` → `InvokeEntry`
- Print text updated accordingly

## 4. Cppreference parser — `src/doxygen_index/cppreference/__init__.py`

No changes needed. The `parse()` function creates a `ParseResult()` whose
`invokes`/`invoked_by` fields default to empty lists. Cppreference data doesn't
include call references.

## 5. Tests — `tests/test_parser.py`

- Update any `CallEntry` imports → `InvokeEntry`
- The XML fixture has no `<references>` elements, so no assertion changes needed
  for invoke data

## 6. Out of scope (separate issue)

The following are **not** changed in this issue and will be addressed when
query methods migrate to codegraph's `GraphRepository`:

- `tools.py` — `find_callers_and_callees()` stays as-is for now; will be
  replaced by codegraph query methods
- `mcp_neo4j_codebase_server.py` — `get_callers()`/`get_callees()` stay as-is;
  will be replaced by codegraph query methods
- Stale `Compound`/`Member`/`CONTAINS` labels in query Cypher — resolves
  naturally when queries move to codegraph

## Summary of file changes

| File | Change type |
|---|---|
| `src/doxygen_index/parser.py` | Rename CallEntry→InvokeEntry, calls→invokes, called_by→invoked_by |
| `src/doxygen_index/neo4j_backend.py` | Rename function, CALLS→INVOKES, result.calls→result.invokes, label fix |
| `examples/doxygen_to_neo4j.py` | Rename function/class, CALLS→INVOKES |
| `tests/test_parser.py` | Import rename CallEntry→InvokeEntry |