# Issue 1: CALLS → INVOKES Rename — Design

**Date:** 2025-06-08
**Status:** Approved
**Scope:** Rename all CALLS relationship labels, tool names, and terminology to INVOKES/invokers/invokees

## Overview

codegraph defines `MethodNode.invokes = RelationshipTo('MethodNode', 'INVOKES')` for
call-callee relationships. doxygen-index writes `[:CALLS]` edges and queries them as
`[:CALLS]`. This mismatch means the dependency layer and design layer store the same
concept under different relationship types.

This design renames everything to match: `CALLS` → `INVOKES` in Neo4j, and all
user-facing tool/method names shift from "callers/callees" to "invokers/invokees".

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
- Cypher label fix (scoped to this method): `MATCH (caller:Member)` / `MATCH (callee:Member)` → `MATCH (caller:Method|Function)` / `MATCH (callee:Method|Function)`
- Print: `"Calls:"` → `"Invokes:"`
- Parameter: `result.calls` → `result.invokes`

### 2.2 Update write_result() call site

- `_write_call_relationships(result)` → `_write_invoke_relationships(result)`

## 3. Query tools — `tools.py`

### 3.1 Rename find_callers_and_callees → find_invokers_and_invokees

- Method name, docstring, parameter names, variable names all shift to invoke terminology
- Parameter `direction` choices: `"invokees"`, `"invokers"`, `"both"` (was `"callees"`, `"callers"`, `"both"`)
- Default stays `"both"`
- Cypher: `[:CALLS]` → `[:INVOKES]`
- Cypher labels: `:Member` → `:Method|Function` (scoped to this method only; full label cleanup is Issue 2)
- Variable names: `caller_name` → `invoker_name`, `callee_name` → `invokee_name`, etc.
- Update `DependencyGraphTools.schemas()` method list: `self.find_callers_and_callees` → `self.find_invokers_and_invokees`

## 4. MCP server — `mcp/mcp_neo4j_codebase_server.py`

### 4.1 Method renames

- `get_callers()` → `get_invokers()`
- `get_callees()` → `get_invokees()`

### 4.2 MCP tool renames (in create_mcp_server)

- `get_callers` → `get_invokers`
- `get_callees` → `get_invokees`

### 4.3 Cypher updates in these two methods

- All `[:CALLS]` → `[:INVOKES]`
- `:Member` label → `:Method|Function`
- Remove `[:CONTAINS]` traversals that joined members to compounds (INVOKES is a method-to-method relationship only)
- Variable names: `caller`/`callee` → `invoker`/`invokee`

### 4.4 CLI command renames (in main())

- `get_callers` → `get_invokers`
- `get_callees` → `get_invokees`
- Help text updated accordingly

### 4.5 No changes to other methods

Other MCP server methods (find_class, get_class_members, search_symbols, etc.) still
use `:Compound`/`:Member`/`:CONTAINS` labels. Those are addressed in Issue 2.

## 5. Example script — `examples/doxygen_to_neo4j.py`

- `_write_call_relationships()` → `_write_invoke_relationships()`
- `[:CALLS]` → `[:INVOKES]`
- `CallEntry` → `InvokeEntry`
- Print text: `"Relationships: DEFINED_IN, CONTAINS"` → update if CONTAINS appears elsewhere (this is Issue 8 territory; minimal change here)

## 6. Cppreference parser — `src/doxygen_index/cppreference/__init__.py`

No changes needed. The `parse()` function creates a `ParseResult()` whose `invokes`/`invoked_by` fields default to empty lists. Cppreference data doesn't include call references.

## 7. Tests — `tests/test_parser.py`

- Update any `CallEntry` imports → `InvokeEntry`
- The XML fixture has no `<references>` elements, so no assertion changes needed for call data
- The `TestNormalizeArgsstring`, `TestDeriveModule`, `TestDeriveSourceType`, `TestDepsConfig` classes are unaffected

## 8. Changes NOT in scope

- `ParseResult.compounds` / `ParseResult.members` backward-compat properties — unchanged
- Full Compound/Member label cleanup across all Cypher queries — Issue 2
- Namespace COMPOSES writes — Issue 3
- FTS index — Issue 7
- Docstrings in MCP server methods outside get_invokers/get_invokees — Issue 2

## Summary of file changes

| File | Change type |
|---|---|
| `src/doxygen_index/parser.py` | Rename CallEntry→InvokeEntry, calls→invokes, called_by→invoked_by |
| `src/doxygen_index/neo4j_backend.py` | Rename function, CALLS→INVOKES, result.calls→result.invokes |
| `src/doxygen_index/tools.py` | Rename method, CALLS→INVOKES, callers/callees→invokers/invokees |
| `mcp/mcp_neo4j_codebase_server.py` | Rename methods/tools/CLI, CALLS→INVOKES, label fix scoped to invoke methods |
| `examples/doxygen_to_neo4j.py` | Rename function/class, CALLS→INVOKES |
| `tests/test_parser.py` | Import rename CallEntry→InvokeEntry |