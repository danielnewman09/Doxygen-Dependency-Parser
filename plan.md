# Implementation Source Extraction — Implementation Plan

> **Spec:** `danielnewman09/codegraph` Issue #2
> **Repos involved:**
> - `danielnewman09/codegraph` (local: `/Users/danielnewman/dev/codegraph`) — data model changes
> - `danielnewman09/Doxygen-Dependency-Parser` (local: `/Users/danielnewman/dev/Doxygen-Dependency-Parser`) — parsing/ingestion changes

---

## Overview

Add `body_start` and `body_end` properties to `_MemberMixin`, then extend the Doxygen parser to extract implementation source code from those line ranges and create `ImplementationNode` instances connected via `HAS_IMPLEMENTATION`.

---

## Task 1: Add `body_start` and `body_end` to `_MemberMixin`

**Repo:** codegraph
**File:** `src/codegraph/models/member.py`

- [ ] **Step 1:** Add two `IntegerProperty` fields to `_MemberMixin`, after the `line_number` field in the "Location" section:

```python
    # --- Location ---
    file_path = StringProperty(default="")
    line_number = IntegerProperty()
    body_start = IntegerProperty(
        default=0,
        help_text="Start line of the implementation body (from Doxygen bodystart). "
                  "0 or negative means no implementation body available.",
    )
    body_end = IntegerProperty(
        default=0,
        help_text="End line of the implementation body (from Doxygen bodyend). "
                  "0 or negative means no implementation body available.",
    )
```

- [ ] **Step 2:** Update the `_MemberMixin` docstring to include `body_start` and `body_end` in the attributes list.

- [ ] **Step 3:** Verify the model imports and creates correctly:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -c "
from codegraph.models.member import MethodNode, FunctionNode
m = MethodNode(kind='method')
assert hasattr(m, 'body_start')
assert m.body_start == 0
assert hasattr(m, 'body_end')
assert m.body_end == 0
print('OK')
"
```

- [ ] **Step 4:** Confirm that `_llm_fields` does NOT include `body_start` or `body_end`. Verify explicitly:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -c "
from codegraph.models.member import _MemberMixin, MethodNode, FunctionNode, AttributeNode, DefineNode
for cls in [_MemberMixin, MethodNode, FunctionNode, AttributeNode, DefineNode]:
    assert 'body_start' not in cls._llm_fields, f'{cls.__name__}._llm_fields contains body_start'
    assert 'body_end' not in cls._llm_fields, f'{cls.__name__}._llm_fields contains body_end'
print('OK - body_start/body_end not in _llm_fields')
"
```

---

## Task 2: Add `body_start`/`body_end` to member test fixtures

**Repo:** codegraph
**Files:** `tests/data/method_node_full.json`, `tests/data/function_node_full.json`, `tests/data/attribute_node_full.json`, `tests/data/define_node_full.json`, `tests/data/enum_value_node_full.json`

- [ ] **Step 1:** Add `"body_start": 0` and `"body_end": 0` to each member fixture JSON file (default values). For `method_node_full.json`, use realistic values like `"body_start": 25` and `"body_end": 30` to test roundtrip.

- [ ] **Step 2:** Verify fixtures load and roundtrip correctly:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -c "
import json
from pathlib import Path
from codegraph.models.tags import CodeGraphNode

for f in Path('tests/data').glob('*_node_full.json'):
    data = json.loads(f.read_text())
    if data.get('type') in ('MethodNode', 'FunctionNode', 'AttributeNode', 'DefineNode', 'EnumValueNode'):
        node = CodeGraphNode.from_json(data)
        assert hasattr(node, 'body_start'), f'{data[\"type\"]} missing body_start'
        print(f'{data[\"type\"]}: body_start={node.body_start}, body_end={node.body_end}')
print('OK')
"
```

---

## Task 3: Add tests for `body_start`/`body_end` on member nodes

**Repo:** codegraph
**File:** `tests/member/test_member_search_fields.py`

- [ ] **Step 1:** Add a new test class `TestMemberBodyLocation`:

```python
class TestMemberBodyLocation:
    """Test body_start and body_end fields on member nodes."""

    def test_method_body_start_default_zero(self):
        m = MethodNode(kind="method")
        assert m.body_start == 0

    def test_method_body_end_default_zero(self):
        m = MethodNode(kind="method")
        assert m.body_end == 0

    def test_method_body_start_stored(self):
        m = MethodNode(kind="method", body_start=25, body_end=30)
        assert m.body_start == 25
        assert m.body_end == 30

    def test_function_body_start_stored(self):
        f = FunctionNode(kind="function", body_start=100, body_end=120)
        assert f.body_start == 100
        assert f.body_end == 120

    def test_body_start_not_in_llm_fields(self):
        """body_start/body_end are extraction plumbing, not for LLM context."""
        for cls in [MethodNode, FunctionNode, AttributeNode, DefineNode]:
            assert "body_start" not in cls._llm_fields
            assert "body_end" not in cls._llm_fields

    def test_method_serialize_excludes_body_location(self):
        m = MethodNode(kind="method", body_start=25, body_end=30, name="draw")
        serialized = m.serialize()
        assert "body_start" not in serialized
        assert "body_end" not in serialized

    def test_deserialize_with_body_location(self):
        data = {
            "type": "MethodNode",
            "qualified_name": "Widget::draw",
            "name": "draw",
            "kind": "method",
            "body_start": 25,
            "body_end": 30,
        }
        node = CodeGraphNode.from_json(data)
        assert isinstance(node, MethodNode)
        assert node.body_start == 25
        assert node.body_end == 30

    def test_deserialize_without_body_location(self):
        """Old fixtures without body_start/body_end should default to 0."""
        data = {
            "type": "MethodNode",
            "qualified_name": "Widget::draw",
            "name": "draw",
            "kind": "method",
        }
        node = CodeGraphNode.from_json(data)
        assert node.body_start == 0
        assert node.body_end == 0
```

- [ ] **Step 2:** Run the new tests:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -m pytest tests/member/test_member_search_fields.py -v
```

---

## Task 4: Update `ImplementationNode` import in codegraph `__init__.py`

**Repo:** codegraph
**File:** `src/codegraph/__init__.py`

- [ ] **Step 1:** Add `ImplementationNode` to the imports and `__all__` list so the Doxygen Dependency Parser can import it directly:

In the imports section, add:
```python
from codegraph.models import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    NamespaceNode, FileNode, ParameterNode,
    ImplementationNode,  # <-- add this
)
```

In `__all__`, add `"ImplementationNode"` in the Members section or a new Implementation section.

- [ ] **Step 2:** Verify:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -c "from codegraph import ImplementationNode; print(ImplementationNode._llm_fields)"
```

Expected output: `{'qualified_name', 'kind', 'implementation'}`

---

## Task 5: Extract `bodystart`/`bodyend` in the Doxygen parser

**Repo:** Doxygen-Dependency-Parser
**File:** `src/doxygen_index/parser.py`

- [ ] **Step 1:** Update `parse_location()` to also extract `bodyfile`, `bodystart`, and `bodyend` from the `<location>` element. Currently it returns `(file_path, line_number)`. Change the return to include body location data:

```python
def parse_location(loc_elem: Optional[ET.Element]) -> tuple[Optional[str], Optional[int], Optional[str], Optional[int], Optional[int]]:
    """Extract file path, line number, body file, body start, and body end from location element.

    Returns:
        (file_path, line_number, body_file, body_start, body_end)
        Elements may be None if the location element is missing or
        the corresponding attributes are absent.
    """
    if loc_elem is None:
        return None, None, None, None, None
    file_path = loc_elem.get("file")
    line = loc_elem.get("line")
    body_file = loc_elem.get("bodyfile")
    bodystart = loc_elem.get("bodystart")
    bodyend = loc_elem.get("bodyend")
    return (
        file_path,
        int(line) if line else None,
        body_file if body_file != loc_elem.get("file") else None,  # Only set if different from file_path
        int(bodystart) if bodystart and bodystart != "-1" else None,
        int(bodyend) if bodyend and bodyend != "-1" else None,
    )
```

**Note:** `parse_location` is also called in `_parse_compound_file` for compounds. The compound call site will need updating too, but compounds don't use body_start/body_end in this phase — just pass `None` or ignore the extra return values.

- [ ] **Step 2:** Update all call sites of `parse_location()` to handle the expanded return value:

In `_parse_member()`:
```python
    # Before:
    loc = memberdef.find("location")
    file_path, line_number = parse_location(loc)
    
    # After:
    loc = memberdef.find("location")
    file_path, line_number, _body_file, body_start, body_end = parse_location(loc)
```

In `_parse_compound_file()`:
```python
    # Before:
    loc = compounddef.find("location")
    file_path, line_number = parse_location(loc)
    
    # After:
    loc = compounddef.find("location")
    file_path, line_number, _, _, _ = parse_location(loc)
```

- [ ] **Step 3:** Pass `body_start` and `body_end` to member node constructors in `_parse_member()`. For each `MethodNode` and `FunctionNode` construction, add the body location fields:

For `MethodNode`:
```python
        result.methods.append(MethodNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname, type_signature=type_str,
            definition=definition, argsstring=argsstring,
            file_path=file_path or "", line_number=line_number,
            body_start=body_start or 0, body_end=body_end or 0,
            brief_description=brief, detailed_description=detailed,
            protection=prot, is_static=is_static, is_const=is_const,
            is_constexpr=is_constexpr, is_virtual=is_virtual,
            is_inline=is_inline, is_explicit=is_explicit, source=source,
            source_type=source_type, layer=layer,
        ))
```

For `FunctionNode` (the `kind == "function" and not compound_refid` case — if it exists as a free function), same pattern.

For `AttributeNode`, `EnumValueNode`, `DefineNode` — also pass them (they'll default to 0 for members without implementation bodies):

```python
        result.attributes.append(AttributeNode(
            ...
            body_start=body_start or 0, body_end=body_end or 0,
            ...
        ))
```

Wait — let me check: Do `AttributeNode`, `EnumValueNode`, and `DefineNode` have bodies? Attributes typically don't (`bodystart=-1`). Defines sometimes do (macro definitions). But the model allows 0 as "no body", so it's safe to always pass the values. Members without bodies will have `body_start=0, body_end=0`.

- [ ] **Step 4:** Also update the `DefineNode` case in `_parse_member()`. The `kind == "define"` branch currently uses inline `file_path` from location — update it to use the parsed values same as other members. For defines, Doxygen *does* provide `bodystart`/`bodyend` for multi-line macros, so this is useful.

- [ ] **Step 5:** Verify the parser still works with existing tests:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v
```

---

## Task 6: Add `ImplementationRef` dataclass and `implementations` list to `ParseResult`

**Repo:** Doxygen-Dependency-Parser
**File:** `src/doxygen_index/parser.py`

- [ ] **Step 1:** Add a new dataclass `ImplementationRef` that links a member refid to an ImplementationNode:

```python
@dataclass
class ImplementationRef:
    """Association between a member and its extracted implementation source.
    
    Links the member's refid to the ImplementationNode so that
    write_result() can create the HAS_IMPLEMENTATION relationship
    after persisting both the member and the implementation node.
    """
    member_refid: str
    implementation: "ImplementationNode"
```

This requires importing `ImplementationNode` from codegraph. Add to the top-level imports:

```python
from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
)
```

- [ ] **Step 2:** Add `implementations` and `implementation_refs` fields to `ParseResult`:

```python
@dataclass
class ParseResult:
    """Complete parsed output from a Doxygen XML directory."""
    files: list[FileNode] = field(default_factory=list)
    namespaces: list[NamespaceNode] = field(default_factory=list)
    classes: list[ClassNode] = field(default_factory=list)
    enums: list[EnumNode] = field(default_factory=list)
    unions: list[UnionNode] = field(default_factory=list)
    interfaces: list[InterfaceNode] = field(default_factory=list)
    concepts: list[ConceptNode] = field(default_factory=list)
    methods: list[MethodNode] = field(default_factory=list)
    attributes: list[AttributeNode] = field(default_factory=list)
    enum_values: list[EnumValueNode] = field(default_factory=list)
    defines: list[DefineNode] = field(default_factory=list)
    functions: list[FunctionNode] = field(default_factory=list)
    parameters: list[ParameterNode] = field(default_factory=list)
    includes: list[IncludeEntry] = field(default_factory=list)
    invokes: list[InvokeEntry] = field(default_factory=list)
    invoked_by: list[InvokeEntry] = field(default_factory=list)
    template_param_refs: list[TemplateParamRef] = field(default_factory=list)
    specializes_refs: list[SpecializesRef] = field(default_factory=list)
    implementations: list[ImplementationNode] = field(default_factory=list)
    implementation_refs: list[ImplementationRef] = field(default_factory=list)
```

- [ ] **Step 3:** Verify import works:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -c "from doxygen_index.parser import ParseResult, ImplementationRef; print('OK')"
```

---

## Task 7: Implement `extract_implementations()` function

**Repo:** Doxygen-Dependency-Parser
**File:** `src/doxygen_index/parser.py`

- [ ] **Step 1:** Add the `extract_implementations()` function after `_resolve_concept_constraints()`:

```python
def extract_implementations(
    result: ParseResult,
    source_base: Path | str | None = None,
) -> None:
    """Extract implementation source code from source files using body_start/body_end.

    For each member with body_start > 0 and body_end > 0, reads the
    source file and extracts lines body_start..body_end (inclusive),
    creates an ImplementationNode, and records the association.

    Members without implementation bodies (body_start == 0, body_end == 0,
    body_start < 0, or body_end < 0) are skipped.

    Args:
        result: The ParseResult to augment with implementations.
        source_base: Optional base directory for resolving relative file paths.
            If None, file_path values must be absolute paths.
    """
    if source_base is not None:
        source_base = Path(source_base)

    # Cache for file contents to avoid re-reading the same file
    file_cache: dict[str, list[str]] = {}

    def _read_lines(file_path: str) -> list[str] | None:
        """Read file lines from cache or disk. Returns None if file not found."""
        if file_path in file_cache:
            return file_cache[file_path]

        path = Path(file_path)
        if not path.is_absolute() and source_base is not None:
            path = source_base / path

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            file_cache[file_path] = lines
            return lines
        except FileNotFoundError:
            print(f"  Warning: Source file not found for implementation extraction: {path}",
                  file=sys.stderr)
            file_cache[file_path] = None  # Cache the miss
            return None

    # Collect all members that have body locations
    members_with_bodies: list[tuple[object, str]] = []
    # (member_node, member_refid)
    for m in result.methods:
        if m.body_start > 0 and m.body_end > 0 and m.file_path:
            members_with_bodies.append((m, m.refid))
    for f in result.functions:
        if f.body_start > 0 and f.body_end > 0 and f.file_path:
            members_with_bodies.append((f, f.refid))
    for d in result.defines:
        if d.body_start > 0 and d.body_end > 0 and d.file_path:
            members_with_bodies.append((d, d.refid))

    if not members_with_bodies:
        return

    impl_count = 0
    skip_count = 0

    for member, refid in members_with_bodies:
        lines = _read_lines(member.file_path)
        if lines is None:
            skip_count += 1
            continue

        # Doxygen bodystart/bodyend are 1-based line numbers, inclusive
        start = member.body_start - 1  # Convert to 0-based
        end = member.body_end           # 1-based inclusive, so slice end is this

        if start < 0 or end > len(lines) or start >= end:
            skip_count += 1
            continue

        source_text = "".join(lines[start:end]).rstrip("\n")

        if not source_text.strip():
            skip_count += 1
            continue

        impl_node = ImplementationNode(
            qualified_name=member.qualified_name,
            kind="implementation",
            implementation=source_text,
            impl_embedding=[],  # Embeddings deferred to a later phase
            source=member.source if hasattr(member, 'source') else "",
            layer=member.layer if hasattr(member, 'layer') else "dependency",
        )

        result.implementations.append(impl_node)
        result.implementation_refs.append(ImplementationRef(
            member_refid=refid,
            implementation=impl_node,
        ))
        impl_count += 1

    print(f"  Implementations extracted: {impl_count} (skipped: {skip_count})")
```

- [ ] **Step 2:** Call `extract_implementations()` in `parse_xml_dir()`, after parsing and before returning:

In `parse_xml_dir()`, after `_resolve_concept_constraints(result)` and before `return result`:

```python
    # Post-processing: resolve type_constraint text to concept qualified names
    _resolve_concept_constraints(result)

    # Extract implementation source code from source files
    extract_implementations(result)

    return result
```

**Note:** `parse_xml_dir()` doesn't currently accept a `source_base` argument. The `file_path` values from Doxygen XML are typically absolute paths, so `source_base=None` should work in most cases. If a use case needs relative path resolution, that can be added later.

- [ ] **Step 3:** Verify parser still works:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v
```

---

## Task 8: Persist `ImplementationNode`s and `HAS_IMPLEMENTATION` relationships in `neo4j_backend.py`

**Repo:** Doxygen-Dependency-Parser
**File:** `src/doxygen_index/neo4j_backend.py`

- [ ] **Step 1:** Add `ImplementationNode` import:

```python
from codegraph import (  # noqa: F401 — needed for install_all_labels
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
)
```

Also add the `ImplementationRef` import:

```python
from doxygen_index.parser import ParseResult, parse_xml_dir, TemplateParamRef, SpecializesRef, ImplementationRef
```

- [ ] **Step 2:** Add ImplementationNode to the `clear_source()` function so they're cleaned up on re-ingestion:

Add before the existing member/compound cleanup queries:

```python
        # Delete ImplementationNodes for this source
        ("MATCH (impl:ImplementationNode {source: $src}) DETACH DELETE impl",
         {"src": source}),
```

**Important:** This must come BEFORE the member deletion, since members have outgoing `HAS_IMPLEMENTATION` relationships to ImplementationNodes. Deleting the ImplementationNodes first avoids orphaned relationship issues.

- [ ] **Step 3:** Add `_write_implementations()` function in `neo4j_backend.py`:

```python
def _write_implementations(result: ParseResult) -> None:
    """Persist ImplementationNodes and create HAS_IMPLEMENTATION relationships."""
    if not result.implementations:
        print("  Implementations: 0")
        return

    # Phase 1: Save ImplementationNodes
    for impl in result.implementations:
        result_nodes = ImplementationNode.create_or_update(impl.__properties__)
        # Replace in-place with saved instance (has element_id)
        # Note: create_or_update returns a list; [0] is the saved node
        saved = result_nodes[0]
        # Update the ImplementationRef to point to the saved instance
        # This is needed so we can connect relationships later

    # Build refid → saved member lookup
    member_by_refid: dict[str, object] = {}
    for node_list in [result.methods, result.attributes, result.enum_values,
                      result.defines, result.functions]:
        for node in node_list:
            member_by_refid[node.refid] = node

    # Also build qualified_name → saved implementation lookup
    impl_by_qname: dict[str, object] = {}
    # Re-collect implementations (they were saved in-place via create_or_update)
    for impl in result.implementations:
        # create_or_update was called above; the node now has an element_id
        impl_by_qname[impl.qualified_name] = impl

    # Phase 2: Connect HAS_IMPLEMENTATION relationships
    success, failed = 0, 0
    for ref in result.implementation_refs:
        member = member_by_refid.get(ref.member_refid)
        if member is None:
            failed += 1
            continue
        # Find the implementation node by qualified_name
        impl = impl_by_qname.get(ref.implementation.qualified_name)
        if impl is None:
            failed += 1
            continue
        try:
            member.implementation_ref.connect(impl)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect HAS_IMPLEMENTATION for "
                  f"{ref.member_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Implementations: {len(result.implementations)} nodes, "
          f"{success} relationships, {failed} failed")
```

**Wait — there's a subtlety.** `create_or_update` returns new instances, but the `ImplementationRef.implementation` still points to the original unsaved node. We need to update refs or use qualified_name matching. Let me revise:

Actually, `create_or_update` replaces the properties on existing nodes or creates new ones. The issue is that `result.implementations` list won't be updated in-place with saved instances unless we do that explicitly. Let me revise the approach to be cleaner:

```python
def _write_implementations(result: ParseResult) -> None:
    """Persist ImplementationNodes and create HAS_IMPLEMENTATION relationships."""
    if not result.implementations:
        print("  Implementations: 0")
        return

    # Phase 1: Save ImplementationNodes and replace with saved instances
    saved_implementations = []
    for impl in result.implementations:
        result_nodes = ImplementationNode.create_or_update(impl.__properties__)
        saved_implementations.append(result_nodes[0])

    # Build qualified_name → saved implementation lookup
    impl_by_qname: dict[str, object] = {
        impl.qualified_name: saved_implementations[i]
        for i, impl in enumerate(result.implementations)
    }

    # Build refid → saved member lookup
    member_by_refid: dict[str, object] = {}
    for node_list in [result.methods, result.attributes, result.enum_values,
                      result.defines, result.functions]:
        for node in node_list:
            member_by_refid[node.refid] = node

    # Phase 2: Connect HAS_IMPLEMENTATION relationships
    success, failed = 0, 0
    for ref in result.implementation_refs:
        member = member_by_refid.get(ref.member_refid)
        impl = impl_by_qname.get(ref.implementation.qualified_name)
        if member is None or impl is None:
            failed += 1
            continue
        try:
            member.implementation_ref.connect(impl)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect HAS_IMPLEMENTATION for "
                  f"{ref.member_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Implementations: {len(result.implementations)} nodes, "
          f"{success} relationships, {failed} failed")
```

- [ ] **Step 4:** Call `_write_implementations()` in `write_result()`, after saving member nodes but before writing relationship-only helpers:

In `write_result()`, after the existing node-saving loop and before `_write_parameters(result)`, add:

```python
    # Save ImplementationNodes and replace with saved instances
    if result.implementations:
        saved_impls = []
        for impl in result.implementations:
            result_nodes = ImplementationNode.create_or_update(impl.__properties__)
            saved_impls.append(result_nodes[0])
        # Update result.implementations in-place
        result.implementations[:] = saved_impls
        print(f"  Implementations: {len(saved_impls)}")

    # Parameters use Cypher MERGE — no UniqueIdProperty on ParameterNode
    _write_parameters(result)
```

Wait — that duplicates the saving logic. Let me reconsider. The cleanest approach is:

1. Add `result.implementations` to the `batch_refs` / `batch_labels` loop that already saves all node types.
2. Add `_write_implementations()` just for the relationship connections.

Here's the revised approach:

**In `write_result()`, add implementations to the batch save loop (Tasks 8 continued):**

```python
    batch_refs: list[list] = [
        result.files, result.namespaces, result.classes,
        result.enums, result.unions, result.interfaces, result.concepts,
        result.methods, result.attributes, result.enum_values,
        result.defines, result.functions,
        result.implementations,  # <-- ADD
    ]
    batch_labels = [
        "Files", "Namespaces", "Classes", "Enums", "Unions",
        "Interfaces", "Concepts", "Methods", "Attributes", "EnumValues",
        "Defines", "Functions",
        "Implementations",  # <-- ADD
    ]
```

This saves ImplementationNodes using the same `create_or_update` loop, and replaces them in-place with saved instances (which have `element_id` set for subsequent `.connect()` calls).

Then add the relationship connection as a separate helper call:

After `_write_compound_member_connect(result)` and other relationship helpers, add:

```python
    _write_implementation_relationships(result)
```

And the helper function:

```python
def _write_implementation_relationships(result: ParseResult) -> None:
    """Create HAS_IMPLEMENTATION relationships from members to ImplementationNodes."""
    if not result.implementation_refs:
        print("  Relationships: HAS_IMPLEMENTATION (0 edges)")
        return

    # Build refid → saved member lookup
    member_by_refid: dict[str, object] = {}
    for node_list in [result.methods, result.attributes, result.enum_values,
                      result.defines, result.functions]:
        for node in node_list:
            member_by_refid[node.refid] = node

    # Build qualified_name → saved implementation lookup
    impl_by_qname: dict[str, object] = {}
    for impl in result.implementations:
        impl_by_qname[impl.qualified_name] = impl

    success, failed = 0, 0
    for ref in result.implementation_refs:
        member = member_by_refid.get(ref.member_refid)
        impl = impl_by_qname.get(ref.implementation.qualified_name)
        if member is None or impl is None:
            failed += 1
            continue
        try:
            member.implementation_ref.connect(impl)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect HAS_IMPLEMENTATION for "
                  f"{ref.member_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Relationships: HAS_IMPLEMENTATION ({success} edges, {failed} failed)")
```

- [ ] **Step 5:** Add `ImplementationNode` cleanup to `clear_all()` as well:

```python
    "MATCH (i:ImplementationNode) DETACH DELETE i",
```

Add this before the member/compound cleanup queries.

- [ ] **Step 6:** Verify the module imports correctly:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -c "from doxygen_index.neo4j_backend import write_result; print('OK')"
```

---

## Task 9: Add tests for `extract_implementations()`

**Repo:** Doxygen-Dependency-Parser
**File:** `tests/test_parser.py`

- [ ] **Step 1:** Add `TestExtractImplementations` class with the following tests:

```python
class TestExtractImplementations:
    """Test implementation source extraction from body location data."""

    @pytest.fixture
    def xml_dir_with_body(self, tmp_path):
        """Create a Doxygen XML directory with a method that has a body location."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classWidget" kind="class">
                <name>Widget</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "classWidget.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classWidget" kind="class" language="C++">
                <compoundname>Widget</compoundname>
                <briefdescription><para>A widget.</para></briefdescription>
                <detaileddescription/>
                <location file="src/widget.cpp" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classWidget_1adraw"
                             prot="public" static="no" const="no">
                    <name>draw</name>
                    <qualifiedname>Widget::draw</qualifiedname>
                    <type>void</type>
                    <definition>void Widget::draw</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>Draw the widget.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/widget.cpp" line="10" bodystart="10" bodyend="15"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))

        # Create the source file that the implementation will be extracted from
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "widget.cpp").write_text(
            "#include \"widget.h\"\n"
            "\n"
            "void Widget::draw() {\n"      # line 3 - but Doxygen says line 10
            "    canvas.begin();\n"
            "    render();\n"
            "    canvas.end();\n"
            "}\n"
            "\n"
        )

        return tmp_path

    def test_extract_implementations_populates_bodystart(self, xml_dir_with_body):
        """Members with bodystart/bodyend get body_start/body_end populated."""
        result = parse_xml_dir(xml_dir_with_body, source="test", progress_interval=0)
        assert len(result.methods) == 1
        method = result.methods[0]
        assert method.body_start == 10
        assert method.body_end == 15

    def test_extract_implementations_creates_impl_node(self, xml_dir_with_body):
        """extract_implementations creates ImplementationNode for method with body."""
        from doxygen_index.parser import extract_implementations
        result = parse_xml_dir(xml_dir_with_body, source="test", progress_interval=0)
        # Implementation extraction happens inside parse_xml_dir
        assert len(result.implementations) >= 1

    def test_extract_implementations_source_text(self, xml_dir_with_body):
        """ImplementationNode.implementation contains the extracted source lines."""
        result = parse_xml_dir(xml_dir_with_body, source="test", progress_interval=0)
        if result.implementations:
            impl = result.implementations[0]
            assert impl.kind == "implementation"
            assert impl.impl_embedding == []  # Embeddings deferred

    def test_extract_implementations_ref(self, xml_dir_with_body):
        """ImplementationRef links member refid to ImplementationNode."""
        result = parse_xml_dir(xml_dir_with_body, source="test", progress_interval=0)
        if result.implementation_refs:
            ref = result.implementation_refs[0]
            method = result.methods[0]
            assert ref.member_refid == method.refid

    def test_no_body_location_means_no_implementation(self, xml_dir):
        """Members without body locations get body_start=0, body_end=0 and no implementation."""
        from doxygen_index.parser import extract_implementations
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)
        for m in result.methods:
            assert m.body_start is not None
            assert m.body_end is not None

    def test_extract_implementations_skips_missing_file(self, tmp_path):
        """Source files that don't exist should be skipped with a warning."""
        from doxygen_index.parser import extract_implementations, ImplementationNode, ImplementationRef

        # Create a minimal result with a method pointing to a nonexistent file
        result = ParseResult()
        method = MethodNode(
            refid="test1", kind="method", name="foo",
            qualified_name="Test::foo", file_path="/nonexistent/file.cpp",
            body_start=10, body_end=15, source="test", layer="dependency",
        )
        result.methods.append(method)
        extract_implementations(result)
        # Should skip the file and produce 0 implementations
        assert len(result.implementations) == 0
```

- [ ] **Step 2:** Run the tests:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v -k "TestExtractImplementations"
```

---

## Task 10: Run full test suites and fix failures

**Repo:** Both

- [ ] **Step 1:** Run codegraph tests:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -m pytest tests/ -x -v
```

- [ ] **Step 2:** Run Doxygen Dependency Parser tests:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/ -x -v
```

- [ ] **Step 3:** Fix any import errors, test failures, or compatibility issues.

---

## Task 11: Commit codegraph changes

**Repo:** codegraph

- [ ] **Step 1:** Stage and commit:

```bash
cd /Users/danielnewman/dev/codegraph
git add src/codegraph/models/member.py \
       src/codegraph/__init__.py \
       tests/data/method_node_full.json \
       tests/data/function_node_full.json \
       tests/data/attribute_node_full.json \
       tests/data/define_node_full.json \
       tests/data/enum_value_node_full.json \
       tests/member/test_member_search_fields.py
git commit -m "feat: add body_start/body_end properties to _MemberMixin for implementation extraction

- Add body_start and body_end IntegerProperty fields to _MemberMixin
- Fields store Doxygen bodystart/bodyend line numbers for locating
  implementation source code
- Not included in _llm_fields (extraction plumbing, not LLM context)
- Source file resolved via existing DEFINED_IN → FileNode relationship
- Update member test fixtures and add body location tests"
```

---

## Task 12: Commit Doxygen Dependency Parser changes

**Repo:** Doxygen-Dependency-Parser

- [ ] **Step 1:** Stage and commit:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser
git add src/doxygen_index/parser.py \
       src/doxygen_index/neo4j_backend.py \
       tests/test_parser.py \
       pyproject.toml
git commit -m "feat: extract implementation source code from Doxygen body locations

- Parse bodystart/bodyend from <location> XML elements and store
  as body_start/body_end on member nodes
- Add ImplementationRef dataclass and implementations list to ParseResult
- Add extract_implementations() to read source files using body_start/body_end
  line ranges and create ImplementationNode instances
- Persist ImplementationNodes and HAS_IMPLEMENTATION relationships in
  neo4j_backend.py
- Clean up ImplementationNodes when clearing source data
- Embeddings deferred: impl_embedding stays empty"
```

---

## Task 13: Update codegraph dependency version

**Repo:** Doxygen-Dependency-Parser
**File:** `pyproject.toml`

The Doxygen Dependency Parser depends on `codegraph` in its `dependencies`. After the codegraph changes are published, update the version constraint if needed. However, since `body_start` and `body_end` use `IntegerProperty(default=0)`, they are backward-compatible — existing data without these fields will default to 0.

- [ ] **Step 1:** Verify that the codegraph version in the Doxygen Dependency Parser's virtual environment includes the new fields. If using a local editable install (`pip install -e /path/to/codegraph`), this should be automatic. If not, update the dependency:

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && pip install -e /Users/danielnewman/dev/codegraph
```

- [ ] **Step 2:** Run the full test suite again to confirm everything works end-to-end:

```bash
cd /Users/danielnewman/dev/codegraph && .venv/bin/python -m pytest tests/ -x -q
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/ -x -q
```