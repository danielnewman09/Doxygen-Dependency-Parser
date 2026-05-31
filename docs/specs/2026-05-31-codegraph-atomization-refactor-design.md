# Codegraph Atomization Refactor — Doxygen Parser Design

**Date:** 2026-05-31
**Status:** Draft
**Scope:** `parser.py` and `neo4j_backend.py` only (not MCP server, examples, or tests)

## Overview

Refactor the Doxygen Dependency Parser to use the atomized codegraph models. The
codegraph library replaced generic `CompoundNode`/`MemberNode` with kind-specific
neomodel classes (`ClassNode`, `InterfaceNode`, `EnumNode`, `UnionNode`, `ModuleNode`,
`MethodNode`, `AttributeNode`, `EnumValueNode`, `FunctionNode`, `DefineNode`).

### Architecture after

```
Doxygen XML → parser.py (kind dispatch) → typed ParseResult
                ↓
         neo4j_backend.py → .save() per typed batch
                           → .connect() for modeled relationships
                           → Cypher MERGE for unmodeled relationships
                           → clear_source() targets atomized labels
```

### Key design decisions

| Decision | Rationale |
|---|---|
| Append `argsstring` to `qualified_name` for methods/functions | Resolves overload collisions; `UniqueIdProperty` on `qualified_name` |
| Fresh start — clear all old data, re-ingest | Simplest path; no Neo4j migration needed |
| Use `.connect()` where models declare relationships | Neomodel-native where available; Cypher fallback otherwise |
| Skip unknown Doxygen kinds with warning | Don't silently create wrong node types |

## 1. Parser Changes (parser.py)

### 1.1 Kind-to-Class Mapping

| Doxygen compound `kind` | Target class | `qualified_name` source |
|---|---|---|
| `class`, `struct` | `ClassNode` | `<qualifiedname>` from XML |
| `enum` | `EnumNode` | `<qualifiedname>` |
| `union` | `UnionNode` | `<qualifiedname>` |
| `interface` | `InterfaceNode` | `<qualifiedname>` |
| `namespace` | `NamespaceNode` | `<qualifiedname>` |
| `file` | `FileNode` | `<compoundname>` |

| Doxygen member `kind` | Target class | `qualified_name` construction |
|---|---|---|
| `function` (in compound) | `MethodNode` | `CompoundQualifiedName::name(argsstring)` |
| `function` (standalone, no compound) | `FunctionNode` | `::name(argsstring)` |
| `variable` | `AttributeNode` | `CompoundQualifiedName::name` |
| `enumvalue` | `EnumValueNode` | `EnumQualifiedName::name` |
| `define` | `DefineNode` | `name` (unqualified; defines are global) |

### 1.2 qualified_name for Overloads

For `MethodNode` and `FunctionNode`:
- `qualified_name = "{parent_qualified_name}::{name}{normalized_argsstring}"`
- `argsstring` normalization: strip parameter names, keeping types only
  - Input: `(int x, const char* str)` → Output: `(int, const char*)`
  - Input: `()` → Output: `()`
  - Trailing `void`: `(void)` → `()`
- The full `argsstring` with names is stored on `MethodNode.argsstring` / `FunctionNode.argsstring`

For non-overloadable nodes (ClassNode, EnumNode, etc.):
- `qualified_name` = Doxygen `<qualifiedname>` as-is (unique by definition)

### 1.3 New Field Population

| Field | Source | Notes |
|---|---|---|
| `qualified_name` | See 1.1 / 1.2 | Now the `UniqueIdProperty` |
| `name` | Doxygen `<name>` | Unqualified name |
| `refid` | Doxygen `id` attribute | Now a regular StringProperty |
| `source_type` | `"header"` / `"source"` | Derived from Doxygen location `file` extension (`.h`/`.hpp` → header, `.c`/`.cpp` → source) |
| `definition` | Doxygen `<definition>` element | Full C++ definition string |
| `module` | Namespace prefix of `qualified_name` | `"ns::ns2::ClassName"` → `"ns::ns2"`; top-level → `""` |
| `component_id` | Not set by parser | Left as default (None) — for ticketing system use |
| `layer` | `"dependency"` | Unchanged from current behavior |

### 1.4 ParseResult Restructuring

Replace generic lists with typed lists:

```python
@dataclass
class ParseResult:
    files: list[FileNode]
    namespaces: list[NamespaceNode]
    classes: list[ClassNode]
    enums: list[EnumNode]
    unions: list[UnionNode]
    interfaces: list[InterfaceNode]
    methods: list[MethodNode]
    attributes: list[AttributeNode]
    enum_values: list[EnumValueNode]
    defines: list[DefineNode]
    functions: list[FunctionNode]
    parameters: list[ParameterNode]
    includes: list[IncludeEntry]
    calls: list[CallEntry]
    called_by: list[CallEntry]
```

### 1.5 Edge Cases

1. **Unknown compound kind** — log warning with kind and refid, skip the compound
2. **Unknown member kind** — log warning with kind and refid, skip the member
3. **Missing `<qualifiedname>`** — fall back to `<name>`; if both missing, skip with warning
4. **Missing `<argsstring>`** — omit parentheses from qualified_name; it won't collide
5. **Standalone functions** — file-level `memberdef kind="function"` → `FunctionNode`, no `compound_refid`

## 2. Neo4j Backend Changes (neo4j_backend.py)

### 2.1 Imports

Replace:
```python
from codegraph import CompoundNode, FileNode, MemberNode, NamespaceNode, ParameterNode
```

With:
```python
from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
)
```

### 2.2 Node Writing (write_result)

Replace generic loops with typed batches:

```python
def write_result(result: ParseResult) -> None:
    for node_list, label in [
        (result.files, "Files"), (result.namespaces, "Namespaces"),
        (result.classes, "Classes"), (result.enums, "Enums"),
        (result.unions, "Unions"), (result.methods, "Methods"),
        (result.attributes, "Attributes"), (result.enum_values, "EnumValues"),
        (result.defines, "Defines"), (result.functions, "Functions"),
    ]:
        if node_list:
            for node in node_list:
                node.save()
            print(f"  {label}: {len(node_list)}")

    _write_parameters(result)
    # relationships follow...
```

### 2.3 Relationship Writing

| Relationship | Method | Note |
|---|---|---|
| ClassNode → MethodNode (COMPOSES) | `class_node.methods.connect(method_node)` | Model descriptor exists |
| InterfaceNode → MethodNode (COMPOSES) | `interface_node.methods.connect(method_node)` | Model descriptor exists |
| ClassNode → AttributeNode (COMPOSES) | `class_node.attributes.connect(attr_node)` | Model descriptor exists |
| EnumNode → EnumValueNode (COMPOSES) | `enum_node.values.connect(value_node)` | Model descriptor exists |
| Compound → FileNode (DEFINED_IN) | Cypher MERGE | No relationship descriptor on FileNode |
| Member → FileNode (DEFINED_IN) | Cypher MERGE | No relationship descriptor on FileNode |
| FileNode → FileNode (INCLUDES) | Cypher MERGE | No relationship descriptor on FileNode |
| ClassNode → ClassNode (INHERITS_FROM) | Cypher MERGE | Pattern matching on `base_classes` array |
| MethodNode/FunctionNode → MethodNode/FunctionNode (CALLS) | Cypher MERGE | Cross-compound references |
| Member → ParameterNode (HAS_PARAMETER) | Cypher MERGE | ParameterNode has no UniqueIdProperty |

`.connect()` failures are counted and reported for visibility.

### 2.4 clear_source()

Update Cypher queries to target atomized labels:

```python
def clear_source(source: str) -> None:
    queries = [
        ("MATCH (p:Parameter)<-[:HAS_PARAMETER]-(m) WHERE m.source = $src DETACH DELETE p", ...),
        ("MATCH (m:Method|Attribute|EnumValue|Define|Function) WHERE m.source = $src DETACH DELETE m", ...),
        ("MATCH (c:Class|Interface|Enum|Union) WHERE c.source = $src DETACH DELETE c", ...),
        ("MATCH (n:Namespace) WHERE n.source = $src DETACH DELETE n", ...),
        ("MATCH (f:File) WHERE f.source = $src DETACH DELETE f", ...),
    ]
```

### 2.5 clear_all()

Same label update for bulk clear.

### 2.6 ensure_schema()

`db.install_all_labels()` discovers labels from the neomodel registry automatically. The atomized labels (`Class`, `Interface`, `Enum`, `Union`, `Method`, `Attribute`, `EnumValue`, `Define`, `Function`) will be installed alongside the old umbrella labels (`Compound`, `Member`) which remain via `__label__` overrides until codegraph removes them in a future release.

No changes needed in this method.

### 2.7 Summary query

The summary query at the end of `ingest()` should be updated or removed — querying by label will show both old umbrella labels and new atomized labels. Simplest fix: remove it, or query by source only without label breakdown.

## 3. Deleted Code

- `IncludeEntry` and `CallEntry` dataclasses stay (not codegraph models, parser-local)
- The `CompoundNode` and `MemberNode` imports — removed
- The `_parse_compound_file` function — refactored to dispatch by kind
- The `_parse_member` function — refactored to dispatch by kind

## 4. Benefits

- **Kind safety** — each node carries only its relevant fields (e.g., `EnumNode` has no `is_final`, `AttributeNode` has no `argsstring`)
- **Cleaner Neo4j schema** — specific labels enable targeted queries
- **Relationship readability** — `class_node.methods.connect(method_node)` vs. `MERGE (c)-[:COMPOSES]->(m)`
- **No duplicate model layers** — neomodel models are the single source of truth

## 5. Risks

- **Overload handling** — `qualified_name` with `argsstring` may produce keys that don't match design-layer nodes created by the ticketing system. Acceptable: design-layer nodes don't have overloads.
- **Performance** — `.save()` called per-node instead of batched Cypher MERGE. For typical Doxygen outputs (thousands of nodes), this is acceptable; neomodel `.save()` is a single write transaction per call.
- **`__label__` overrides** — codegraph currently writes atomized types to old umbrella labels (`ClassNode.__label__ = "Compound"`). If this override is removed in a future codegraph release, our queries that match by label (`MATCH (c:Class)`) will break. We target the atomized labels in advance, knowing they're currently aliased.
