# TODO: Sync doxygen-index with codegraph updates

Codegraph recently gained substantial new features. This file tracks every
area where doxygen-index is out of sync and needs updating.

---

## 1. Missing Namespace COMPOSES relationship writes

**Area:** `src/doxygen_index/neo4j_backend.py`

**Current state:** `_write_compound_member_connect()` writes COMPOSES edges from
compounds to their members (Class→Method, Class→Attribute, Interface→Method,
Enum→EnumValue). But there is **no code** to write NamespaceNode→children
COMPOSES edges.

**Codegraph now declares these NamespaceNode COMPOSES descriptors:**
- `NamespaceNode.classes` → ClassNode
- `NamespaceNode.interfaces` → InterfaceNode
- `NamespaceNode.enums` → EnumNode
- `NamespaceNode.unions` → UnionNode
- `NamespaceNode.modules` → ModuleNode
- `NamespaceNode.functions` → FunctionNode
- `NamespaceNode.namespaces` → NamespaceNode (nested)

**Available data in the ParseResult:**
- `result.namespaces` — each NamespaceNode has `qualified_name` (e.g. `"myns"`)
- Compounds have `module` derived from their `qualified_name` prefix (e.g. `"myns"`)
- Functions have `module` derived similarly
- Nested namespaces can be derived from `qualified_name` structure

**Approach options:**
- (A) Use `.connect()` matching codegraph's typed descriptors (consistent with
  existing compound→member pattern). Requires building a namespace lookup by
  `qualified_name` and matching each compound/function's `module` field to its
  parent namespace.
- (B) Use Cypher MERGE (simpler, no neomodel dispatch needed, but less
  type-safe).

---

## 2. CALLS → INVOKES relationship label mismatch

**Area:** `src/doxygen_index/neo4j_backend.py`, `src/doxygen_index/tools.py`,
`mcp/mcp_neo4j_codebase_server.py`

**Current state:** doxygen-index writes `[:CALLS]` edges between members:
- `neo4j_backend.py:_write_call_relationships()` creates `MERGE (caller)-[:CALLS]->(callee)`
- `tools.py:find_callers_and_callees()` queries `MATCH (m:Member)-[:CALLS]->(callee:Member)`
- `mcp_neo4j_codebase_server.py` queries `[:CALLS]` in multiple places

**Codegraph now declares:**
- `MethodNode.invokes = RelationshipTo('MethodNode', 'INVOKES')` — the
  relationship label is `INVOKES`, not `CALLS`.

**Impact:** If any design-layer or future code uses `MethodNode.invokes.connect()`,
it will create `[:INVOKES]` edges. The dependency layer writes `[:CALLS]`. Two
different relationship types for the same semantic relationship.

**Approach:** Rename all `[:CALLS]` references to `[:INVOKES]` to match codegraph's
model. Update `_write_call_relationships()`, all Cypher queries in `tools.py`,
and all Cypher queries in `mcp_neo4j_codebase_server.py`.

---

## 3. Stale Compound/Member umbrella labels and CONTAINS relationship

**Area:** `src/doxygen_index/tools.py`, `mcp/mcp_neo4j_codebase_server.py`

**Current state:** Both query modules still use old umbrella labels and the
old relationship name throughout their Cypher queries:

| Stale pattern | Should become |
|---|---|
| `MATCH (c:Compound)` | Type-specific: `Class`, `Interface`, `Enum`, `Union`, `Module`, or `MATCH (c)` with `c.kind` filter |
| `MATCH (m:Member)` | Type-specific: `Method`, `Attribute`, `EnumValue`, `Function`, `Define` |
| `MATCH (n:NamespaceNode)` | `MATCH (n:Namespace)` (label override) |
| `MATCH (f:FileNode)` | `MATCH (f:File)` (label override) |
| `(c)-[:CONTAINS]->(m)` | `(c)-[:COMPOSES]->(m)` with type-specific targets |
| `:ParameterNode` | `:Parameter` |

**Note:** codegraph uses `__label__` overrides so `ClassNode` gets label
`:Class` (not `:Compound`). The `neo4j_backend.py` was already updated for
the write path (typed batches, `.connect()`), but the **query path** still
assumes the old umbrella labels.

**Tool-by-tool impact:**
- `tools.py:DependencyGraphTools` — 8 tools, all using `Compound`/`Member`
- `mcp_neo4j_codebase_server.py:Neo4jCodebaseServer` — 14 tools, all
  using `Compound`/`Member`/`CONTAINS`

**Approach options:**
- (A) Replace umbrella labels with union queries: `MATCH (c:Class|Interface|Enum|Union|Module)`
- (B) Add atomized-label variants alongside umbrella queries (backward compat)
- (C) Full refactor to type-specific queries (clean but large diff)

---

## 4. clear_source() and clear_all() label alignment

**Area:** `src/doxygen_index/neo4j_backend.py`

**Current state:** The recent refactor already updated these to use pipe-syntax
atomized labels (`Method|Attribute|EnumValue|Define|Function`,
`Class|Interface|Enum|Union`). However, they don't cover:
- `Module` — the new `ModuleNode` type
- Nested `Parameter` deletion could use codegraph's registry instead of
  hardcoded label lists

**Impact:** If a Doxygen docset includes namespace-level `kind="module"` entries
in the future, they won't be cleaned up by `clear_source()`.

**Approach:** Add `Module` to the compound deletion query, and consider
building deletion queries from codegraph's `_registry` to auto-include
future node types.

---

## 5. tools.py and MCP server don't leverage incoming COMPOSES descriptors

**Area:** `src/doxygen_index/tools.py`, `mcp/mcp_neo4j_codebase_server.py`

**Current state:** codegraph now has incoming `RelationshipFrom` descriptors
on every compound and key member type:
- ClassNode: `parent_namespace` (from NamespaceNode)
- InterfaceNode: `parent_namespace`
- EnumNode: `parent_namespace`
- UnionNode: `parent_namespace`
- MethodNode: `parent_compound`, `parent_interface`, `parent_namespace`
- AttributeNode: `parent_compound`
- EnumValueNode: `parent_enum`
- FunctionNode: `parent_namespace`

**Impact:** The query tools could use these for simpler "find parent" queries
instead of traversing COMPOSES in the outgoing direction. For example,
"give me the namespace for this class" becomes a single-hop traversal via
`parent_namespace` rather than scanning all namespaces for a COMPOSES match.

**Approach:** Update `get_compound()`, `get_member()`, `browse_namespace()`
and related queries to use incoming COMPOSES. This is an optimization, not
a correctness issue — can be deferred.

---

## 6. ModuleNode not produced by the parser

**Area:** `src/doxygen_index/parser.py`

**Current state:** The parser produces `ClassNode`, `InterfaceNode`, `EnumNode`,
`UnionNode`, `NamespaceNode`, `FileNode` for compounds. But `ModuleNode` is
never created — there's no `case kind == "module"` in the kind dispatch.

**Impact:** Doxygen can produce `kind="module"` compounds for C++20 module
interface units. Right now these are logged as unknown kind and skipped.

**Approach:** Add `kind == "module"` dispatch in `_parse_compound_file()` to
produce `ModuleNode` instances, and update `ParseResult` to include a
`modules` list.

---

## 7. Full-text search index uses old labels

**Area:** `codegraph/constants.py` (schema DDL), `tools.py`, `mcp/mcp_neo4j_codebase_server.py`

**Current state:** The full-text index definition in codegraph is:
```cypher
CREATE FULLTEXT INDEX doc_search IF NOT EXISTS
FOR (n:Compound|Member) ON EACH [n.name, n.qualified_name, ...]
```

This uses the umbrella labels `Compound` and `Member`. As codegraph nodes now
get atomized labels (`Class`, `Method`, etc.), the FTS index may not match
them unless neomodel's `__label__` mechanism also applies the old labels.

**Impact:** `search_symbols()` and `search_documentation()` may not find nodes
that only have atomized labels and lack the umbrella labels.

**Approach:** Update the FTS index to cover atomized labels:
```cypher
FOR (n:Class|Interface|Enum|Union|Module|Method|Attribute|EnumValue|Function|Define|Namespace|File)
```
Or verify that `__label__` overrides ensure umbrella labels are still applied.

---

## 8. examples/doxygen_to_neo4j.py uses old schema entirely

**Area:** `examples/doxygen_to_neo4j.py`

**Current state:** The example script still references `CompoundNode`,
`MemberNode`, `CONTAINS` relationships, and old Cypher patterns throughout.

**Impact:** Anyone trying the example will get errors or create inconsistent
data.

**Approach:** Rewrite the example to use the atomized models and `.connect()`
pattern, matching the current `neo4j_backend.py`.

---

## Priority order (suggested)

1. **Issue 2** (CALLS→INVOKES) — correctness: different rel types for same concept
2. **Issue 3** (stale labels & CONTAINS) — correctness: queries return nothing
3. **Issue 1** (Namespace COMPOSES) — completeness: missing graph edges
4. **Issue 4** (clear_source labels) — completeness: Module cleanup gap
5. **Issue 6** (ModuleNode parser) — completeness: skipped Doxygen kind
6. **Issue 7** (FTS index) — correctness: search may miss nodes
7. **Issue 8** (example script) — usability: broken example
8. **Issue 5** (incoming COMPOSES in queries) — optimization, deferred