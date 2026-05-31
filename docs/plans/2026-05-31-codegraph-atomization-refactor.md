# Codegraph Atomization Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `parser.py` and `neo4j_backend.py` to use atomized codegraph models (ClassNode, InterfaceNode, EnumNode, UnionNode, MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode) replacing the generic CompoundNode/MemberNode.

**Architecture:** Dispatches Doxygen XML compound/member `kind` values to specific codegraph model constructors. ParseResult gains typed lists (`classes`, `methods`, etc.) with backward-compat aggregation properties (`compounds`, `members`). Neo4j backend writes typed batches with `.save()`, uses `.connect()` for modeled relationships, falls back to Cypher for unmodeled ones.

**Tech Stack:** Python 3.12, neomodel, codegraph (atomized models), pytest

---

### Task 1: Restructure ParseResult

**Files:**
- Modify: `src/doxygen_index/parser.py:1-35` (dataclass + imports)

- [ ] **Step 1: Update imports and ParseResult dataclass**

Replace the current imports and ParseResult:

```python
# at top of file, replace the import block
from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
)
```

Replace the ParseResult dataclass entirely:

```python
@dataclass
class ParseResult:
    """Complete parsed output from a Doxygen XML directory.

    Each Doxygen kind maps to a typed list. Backward-compat ``compounds`` and
    ``members`` properties aggregate the typed lists for consumers (e.g. SQLite
    backend) that haven't been updated yet.
    """
    files: list[FileNode] = field(default_factory=list)
    namespaces: list[NamespaceNode] = field(default_factory=list)
    classes: list[ClassNode] = field(default_factory=list)
    enums: list[EnumNode] = field(default_factory=list)
    unions: list[UnionNode] = field(default_factory=list)
    interfaces: list[InterfaceNode] = field(default_factory=list)
    methods: list[MethodNode] = field(default_factory=list)
    attributes: list[AttributeNode] = field(default_factory=list)
    enum_values: list[EnumValueNode] = field(default_factory=list)
    defines: list[DefineNode] = field(default_factory=list)
    functions: list[FunctionNode] = field(default_factory=list)
    parameters: list[ParameterNode] = field(default_factory=list)
    includes: list[IncludeEntry] = field(default_factory=list)
    calls: list[CallEntry] = field(default_factory=list)
    called_by: list[CallEntry] = field(default_factory=list)

    @property
    def compounds(self) -> list:
        """Aggregate all compound-type nodes for backward compat."""
        return self.classes + self.enums + self.unions + self.interfaces

    @property
    def members(self) -> list:
        """Aggregate all member-type nodes for backward compat."""
        return self.methods + self.attributes + self.enum_values + self.defines + self.functions
```

- [ ] **Step 2: Run existing tests to confirm the parse still works with old logic**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py::TestParseXmlDir -v
```

Expected: PASS (ParseResult still has `.compounds`/`.members` properties, and old code still writes to those lists even though they're now aggregation properties — but wait, the old code was writing to `result.compounds.append(...)` which won't work on a computed `@property`. This means we must update the parser logic in the same commit, or carefully order the changes.)

**Correction:** The restructure and parser logic refactor must happen atomically since `result.compounds.append(foo)` will break once `compounds` becomes a property. So Tasks 1+2+3 will be committed together.

- [ ] **Step 3: Commit**

```bash
git add src/doxygen_index/parser.py tests/test_parser.py
git commit -m "refactor: atomize ParseResult with typed lists and backward-compat properties"
```

This commit also includes Task 2 and 3 changes — see below.

---

### Task 2: Refactor _parse_compound_file with Kind Dispatch

**Files:**
- Modify: `src/doxygen_index/parser.py:_parse_compound_file` (lines ~125-180)

- [ ] **Step 1: Add a helper for module derivation**

Add at module level, after `parse_location`:

```python
def _derive_module(qualified_name: str) -> str:
    """Extract the namespace prefix from a qualified name.

    ``"ns::ns2::ClassName"`` → ``"ns::ns2"``
    ``"ClassName"`` → ``""``
    """
    if "::" not in qualified_name:
        return ""
    return qualified_name.rsplit("::", 1)[0]


def _derive_source_type(file_path: str) -> str:
    """Derive source type from file extension.

    ``.h``/``.hpp`` → ``"header"``, ``.c``/``.cpp`` → ``"source"``, else ``""``.
    """
    if not file_path:
        return ""
    ext = Path(file_path).suffix.lower()
    if ext in (".h", ".hpp", ".hxx", ".h++"):
        return "header"
    if ext in (".c", ".cpp", ".cxx", ".cc", ".c++"):
        return "source"
    return ""
```

- [ ] **Step 2: Replace compound creation in _parse_compound_file**

Replace the compounddef processing section (from `# --- Files ---` through the `# --- Classes, structs, unions ---` block). The file and namespace handling stays the same; only the compound creation changes:

```python
def _parse_compound_file(xml_path: Path, source: str, result: ParseResult) -> None:
    """Parse a compound (class/struct/file) XML file."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"Warning: Could not parse {xml_path}: {e}", file=sys.stderr)
        return

    for compounddef in root.findall(".//compounddef"):
        refid = compounddef.get("id", "")
        kind = compounddef.get("kind", "")
        language = compounddef.get("language", "")
        compoundname = compounddef.findtext("compoundname", "")

        # --- Files ---
        if kind == "file":
            loc = compounddef.find("location")
            file_path = loc.get("file") if loc is not None else None
            result.files.append(FileNode(
                refid=refid, name=compoundname,
                path=file_path or "", language=language, source=source,
            ))

            for inc in compounddef.findall("includes"):
                result.includes.append(IncludeEntry(
                    file_refid=refid,
                    included_file=inc.text or "",
                    included_refid=inc.get("refid") or "",
                    is_local=inc.get("local") == "yes",
                ))
            continue

        # --- Namespaces ---
        if kind == "namespace":
            name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
            result.namespaces.append(NamespaceNode(
                refid=refid, name=name,
                qualified_name=compoundname, source=source,
                layer="dependency",
            ))
            continue

        # --- Common fields for all compound types ---
        name = compoundname.split("::")[-1] if "::" in compoundname else compoundname
        qualified_name = compoundname

        loc = compounddef.find("location")
        file_path, line_number = parse_location(loc)

        brief = parse_description(compounddef.find("briefdescription"))
        detailed = parse_description(compounddef.find("detaileddescription"))
        definition = compounddef.findtext("definition", "")

        module = _derive_module(qualified_name)
        source_type = _derive_source_type(file_path or "")

        # --- Kind dispatch ---
        if kind in ("class", "struct"):
            base_classes = [
                baseref.text or ""
                for baseref in compounddef.findall("basecompoundref")
            ]
            is_final = compounddef.get("final") == "yes"
            is_abstract = compounddef.get("abstract") == "yes"

            result.classes.append(ClassNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                base_classes=base_classes, is_final=is_final,
                is_abstract=is_abstract, source=source,
                source_type=source_type, layer="dependency",
            ))

        elif kind == "enum":
            result.enums.append(EnumNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        elif kind == "union":
            result.unions.append(UnionNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        elif kind == "interface":
            result.interfaces.append(InterfaceNode(
                refid=refid, kind=kind, name=name,
                qualified_name=qualified_name,
                file_path=file_path or "", line_number=line_number,
                brief_description=brief, detailed_description=detailed,
                definition=definition, module=module,
                source=source, source_type=source_type, layer="dependency",
            ))

        else:
            print(f"Warning: Unknown compound kind '{kind}' for refid={refid}, skipping",
                  file=sys.stderr)
            continue

        # --- Parse members (shared across compound types) ---
        for sectiondef in compounddef.findall("sectiondef"):
            for memberdef in sectiondef.findall("memberdef"):
                _parse_member(memberdef, refid, qualified_name, source, result)
```

Note: `_parse_member` signature changes — it now takes `parent_qualified_name` instead of just `compound_refid`.

- [ ] **Step 3: Commit (combined with Tasks 1, 2, 3)**

```bash
git add src/doxygen_index/parser.py
git commit -m "refactor: dispatch Doxygen kinds to atomized codegraph compound models"
```

---

### Task 3: Refactor _parse_member with Kind Dispatch and qualified_name

**Files:**
- Modify: `src/doxygen_index/parser.py:_parse_member` (lines ~65-130)

- [ ] **Step 1: Add argsstring normalization helper**

Add at module level:

```python
def _normalize_argsstring(argsstring: str) -> str:
    """Strip parameter names from argsstring, keeping types only.

    ``(int x, const char* str)`` → ``(int, const char*)``
    ``(void)`` → ``()``
    ``"()"`` → ``()``
    Empty or missing → ``"()"``
    """
    if not argsstring:
        return "()"
    # Remove outer parens
    inner = argsstring.strip().strip("()")
    if not inner or inner == "void":
        return "()"
    # Split on commas, strip each token
    parts = [p.strip() for p in inner.split(",")]
    normalized = []
    for part in parts:
        # Split into tokens; the last token is the parameter name if it
        # starts with a letter/underscore and is not a C++ keyword.
        tokens = part.split()
        # Filter out tokens that look like parameter names (start with
        # letter/underscore, are the last word, and aren't types/keywords)
        type_keywords = {"const", "volatile", "static", "register",
                         "unsigned", "signed", "short", "long",
                         "struct", "class", "enum", "union", "typename",
                         "auto", "decltype", "noexcept", "override",
                         "virtual", "final", "explicit", "mutable",
                         "constexpr", "consteval", "constinit",
                         "throw", "restrict", "__restrict",
                         "&&", "&", "*", "**", "...", "..."}
        type_tokens = []
        for tok in tokens:
            if tok not in type_keywords:
                type_tokens.append(tok)
        # If last token looks like a param name (not all-uppercase, not
        # starting with a known type pattern), check more carefully.
        # Simple heuristic: if more than one token and the last token
        # doesn't contain <, >, *, &, ::, it's likely a param name.
        if len(type_tokens) > 1:
            last = type_tokens[-1]
            if not any(c in last for c in "<>*&::") and last.isidentifier():
                type_tokens.pop()
        normalized.append(" ".join(type_tokens))
    return "(" + ", ".join(normalized) + ")"
```

- [ ] **Step 2: Rewrite _parse_member**

Replace the entire `_parse_member` function:

```python
def _parse_member(memberdef: ET.Element, compound_refid: str,
                  parent_qualified_name: str,
                  source: str, result: ParseResult) -> None:
    """Parse a member definition element into the result using kind dispatch."""
    refid = memberdef.get("id", "")
    kind = memberdef.get("kind", "")
    prot = memberdef.get("prot", "public")

    name = memberdef.findtext("name", "")
    type_str = get_text(memberdef.find("type"))
    definition = memberdef.findtext("definition", "")
    argsstring = memberdef.findtext("argsstring", "")

    loc = memberdef.find("location")
    file_path, line_number = parse_location(loc)

    brief = parse_description(memberdef.find("briefdescription"))
    detailed = parse_description(memberdef.find("detaileddescription"))

    source_type = _derive_source_type(file_path or "")

    # --- Kind dispatch ---
    if kind == "function" and compound_refid:
        # Method — belongs to a compound
        normalized_args = _normalize_argsstring(argsstring)
        qname = f"{parent_qualified_name}::{name}{normalized_args}"

        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"
        is_constexpr = memberdef.get("constexpr") == "yes"
        is_virtual = memberdef.get("virt") in ("virtual", "pure-virtual")
        is_inline = memberdef.get("inline") == "yes"
        is_explicit = memberdef.get("explicit") == "yes"

        result.methods.append(MethodNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname, type_signature=type_str,
            definition=definition, argsstring=argsstring,
            file_path=file_path or "", line_number=line_number,
            brief_description=brief, detailed_description=detailed,
            protection=prot, is_static=is_static, is_const=is_const,
            is_constexpr=is_constexpr, is_virtual=is_virtual,
            is_inline=is_inline, is_explicit=is_explicit, source=source,
            source_type=source_type, layer="dependency",
        ))

    elif kind == "function":
        # Free function — no compound parent
        normalized_args = _normalize_argsstring(argsstring)
        qname = f"::{name}{normalized_args}"

        result.functions.append(FunctionNode(
            refid=refid, kind=kind, name=name, qualified_name=qname,
            type_signature=type_str, definition=definition,
            argsstring=argsstring, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, source=source, layer="dependency",
        ))

    elif kind == "variable":
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        is_static = memberdef.get("static") == "yes"
        is_const = memberdef.get("const") == "yes"

        result.attributes.append(AttributeNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname, type_signature=type_str,
            definition=definition, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, protection=prot,
            is_static=is_static, is_const=is_const, source=source,
            layer="dependency",
        ))

    elif kind == "enumvalue":
        qname = f"{parent_qualified_name}::{name}" if parent_qualified_name else name
        result.enum_values.append(EnumValueNode(
            refid=refid, compound_refid=compound_refid, kind=kind,
            name=name, qualified_name=qname,
            file_path=file_path or "", line_number=line_number,
            brief_description=brief, detailed_description=detailed,
            source=source, layer="dependency",
        ))

    elif kind == "define":
        result.defines.append(DefineNode(
            refid=refid, kind=kind, name=name, qualified_name=name,
            definition=definition, file_path=file_path or "",
            line_number=line_number, brief_description=brief,
            detailed_description=detailed, source=source, layer="dependency",
        ))

    else:
        print(f"Warning: Unknown member kind '{kind}' for refid={refid}, name={name}, skipping",
              file=sys.stderr)
        return

    # --- Parameters (shared) ---
    for i, param in enumerate(memberdef.findall("param")):
        param_name = param.findtext("declname", "")
        param_type = get_text(param.find("type"))
        default_value = param.findtext("defval")
        result.parameters.append(ParameterNode(
            member_refid=refid, position=i, name=param_name or "",
            type=param_type, default_value=default_value or "",
        ))

    # --- Call references (shared) ---
    for ref in memberdef.findall("references"):
        result.calls.append(CallEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))

    for ref in memberdef.findall("referencedby"):
        result.called_by.append(CallEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))
```

- [ ] **Step 3: Run tests to verify parse works with atomized types**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py::TestParseXmlDir -v
```

Expected: PASS (backward-compat `.compounds`/`.members` properties aggregate from typed lists)

- [ ] **Step 4: Commit (combined with Tasks 1, 2, 3)**

```bash
git add src/doxygen_index/parser.py
git commit -m "refactor: dispatch Doxygen member kinds to atomized codegraph member models with overload-safe qualified_name"
```

---

### Task 4: Update neo4j_backend — Imports, write_result, and .connect() Relationships

**Files:**
- Modify: `src/doxygen_index/neo4j_backend.py` (imports, write_result, new _write_compound_member_connect, updated _write_file_relationships)

- [ ] **Step 1: Update imports**

Replace the import block at the top:

```python
from codegraph import (  # noqa: F401 — needed for install_all_labels
    ClassNode, InterfaceNode, EnumNode, UnionNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode,
)
```

- [ ] **Step 2: Rewrite write_result with typed batches**

Replace the `write_result` function:

```python
def write_result(result: ParseResult) -> None:
    """Write a ParseResult to Neo4j.

    Nodes are saved via neomodel .save() (MERGE on unique identifier).
    Relationships use .connect() where models declare them, Cypher otherwise.
    """
    typed_batches = [
        (result.files, "Files"),
        (result.namespaces, "Namespaces"),
        (result.classes, "Classes"),
        (result.enums, "Enums"),
        (result.unions, "Unions"),
        (result.interfaces, "Interfaces"),
        (result.methods, "Methods"),
        (result.attributes, "Attributes"),
        (result.enum_values, "EnumValues"),
        (result.defines, "Defines"),
        (result.functions, "Functions"),
    ]
    for node_list, label in typed_batches:
        if node_list:
            for node in node_list:
                node.save()
            print(f"  {label}: {len(node_list)}")

    # Parameters use Cypher MERGE — no UniqueIdProperty on ParameterNode
    _write_parameters(result)

    # Relationships
    _write_compound_member_connect(result)
    _write_file_relationships()
    _write_include_relationships(result)
    _write_inheritance_relationships()
    _write_call_relationships(result)
```

- [ ] **Step 3: Add _write_compound_member_connect helper**

Add before the existing `_write_file_relationships`:

```python
def _write_compound_member_connect(result: ParseResult) -> None:
    """Create COMPOSES relationships using neomodel .connect().

    Builds lookup dicts by refid, then connects:
      - ClassNode/InterfaceNode → MethodNode via .methods.connect()
      - ClassNode → AttributeNode via .attributes.connect()
      - EnumNode → EnumValueNode via .values.connect()

    Failures are counted and reported.
    """
    compound_by_refid: dict[str, object] = {}
    for c in result.classes + result.enums + result.unions + result.interfaces:
        compound_by_refid[c.refid] = c

    success, skipped, failed = 0, 0, 0

    for m in result.methods:
        parent = compound_by_refid.get(m.compound_refid)
        if parent is None or not hasattr(parent, 'methods'):
            skipped += 1
            continue
        try:
            parent.methods.connect(m)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect MethodNode {m.qualified_name} "
                  f"to parent {m.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    for a in result.attributes:
        parent = compound_by_refid.get(a.compound_refid)
        if parent is None or not hasattr(parent, 'attributes'):
            skipped += 1
            continue
        try:
            parent.attributes.connect(a)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect AttributeNode {a.qualified_name} "
                  f"to parent {a.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    for v in result.enum_values:
        parent = compound_by_refid.get(v.compound_refid)
        if parent is None or not hasattr(parent, 'values'):
            skipped += 1
            continue
        try:
            parent.values.connect(v)
            success += 1
        except Exception as e:
            print(f"Warning: Could not connect EnumValueNode {v.qualified_name} "
                  f"to parent {v.compound_refid}: {e}", file=sys.stderr)
            failed += 1

    print(f"  Relationships via .connect(): {success} connected, "
          f"{skipped} skipped, {failed} failed")
```

- [ ] **Step 4: Update _write_file_relationships to remove COMPOSES Cypher**

`.connect()` now handles COMPOSES; remove the duplicate Cypher:

```python
def _write_file_relationships() -> None:
    db.cypher_query("""
        MATCH (c:Compound) WHERE c.file_path <> ''
        MATCH (f:File {path: c.file_path})
        MERGE (c)-[:DEFINED_IN]->(f)
    """)
    db.cypher_query("""
        MATCH (m:Member) WHERE m.file_path <> ''
        MATCH (f:File {path: m.file_path})
        MERGE (m)-[:DEFINED_IN]->(f)
    """)
    print("  Relationships: DEFINED_IN")
```

- [ ] **Step 5: Commit**

```bash
git add src/doxygen_index/neo4j_backend.py
git commit -m "refactor: update neo4j_backend for atomized models — typed batches, .connect() for COMPOSES"
```



---

### Task 5: Update clear_source and clear_all Labels

**Files:**
- Modify: `src/doxygen_index/neo4j_backend.py:clear_source` and `clear_all` functions

- [ ] **Step 1: Update clear_source to target atomized labels**

Replace `clear_source`:

```python
def clear_source(source: str) -> None:
    """Remove all nodes with a specific source label.

    Uses db.cypher_query for fast bulk deletion.
    Targets atomized labels (Class, Interface, Enum, Union, Method, etc.).
    """
    queries = [
        ("MATCH ()-[r:HAS_PARAMETER]->(p:Parameter) "
         "WHERE p.member_refid IN [(m:Method|Attribute|EnumValue|Define|Function "
         "{source: $src}) | m.refid] DETACH DELETE r, p",
         {"src": source}),
        ("MATCH (m:Method|Attribute|EnumValue|Define|Function {source: $src}) "
         "DETACH DELETE m",
         {"src": source}),
        ("MATCH (c:Class|Interface|Enum|Union {source: $src}) "
         "DETACH DELETE c",
         {"src": source}),
        ("MATCH (n:Namespace {source: $src}) DETACH DELETE n",
         {"src": source}),
        ("MATCH (f:File {source: $src}) DETACH DELETE f",
         {"src": source}),
    ]
    for query, params in queries:
        db.cypher_query(query, params)
    print(f"  Cleared existing '{source}' data from Neo4j.")
```

Wait — the first query is a bit complex. Let's simplify to a two-step approach:

```python
def clear_source(source: str) -> None:
    """Remove all nodes with a specific source label.

    Uses db.cypher_query for fast bulk deletion.
    Targets atomized labels (Class, Interface, Enum, Union, Method, etc.).
    """
    queries = [
        # Parameters first (no source property, delete via member refid relationship)
        ("MATCH (p:Parameter) WHERE EXISTS { MATCH (m:Method|Attribute|EnumValue|Define|Function {source: $src}) WHERE m.refid = p.member_refid } DETACH DELETE p",
         {"src": source}),
        # Members
        ("MATCH (m:Method|Attribute|EnumValue|Define|Function {source: $src}) DETACH DELETE m",
         {"src": source}),
        # Compounds
        ("MATCH (c:Class|Interface|Enum|Union {source: $src}) DETACH DELETE c",
         {"src": source}),
        # Namespaces
        ("MATCH (n:Namespace {source: $src}) DETACH DELETE n",
         {"src": source}),
        # Files
        ("MATCH (f:File {source: $src}) DETACH DELETE f",
         {"src": source}),
    ]
    for query, params in queries:
        db.cypher_query(query, params)
    print(f"  Cleared existing '{source}' data from Neo4j.")
```

Ah wait — `EXISTS { ... }` is Neo4j 5.x syntax. Let me use a simpler subquery approach:

```python
def clear_source(source: str) -> None:
    """Remove all nodes with a specific source label.

    Uses db.cypher_query for fast bulk deletion.
    Targets atomized labels (Class, Interface, Enum, Union, Method, etc.).
    """
    queries = [
        # Parameters — collect member refids, then delete
        ("MATCH (m:Method|Attribute|EnumValue|Define|Function {source: $src}) "
         "WITH collect(m.refid) AS refids "
         "MATCH (p:Parameter) WHERE p.member_refid IN refids "
         "DETACH DELETE p",
         {"src": source}),
        # Members
        ("MATCH (m:Method|Attribute|EnumValue|Define|Function {source: $src}) "
         "DETACH DELETE m",
         {"src": source}),
        # Compounds
        ("MATCH (c:Class|Interface|Enum|Union {source: $src}) "
         "DETACH DELETE c",
         {"src": source}),
        # Namespaces
        ("MATCH (n:Namespace {source: $src}) DETACH DELETE n",
         {"src": source}),
        # Files
        ("MATCH (f:File {source: $src}) DETACH DELETE f",
         {"src": source}),
    ]
    for query, params in queries:
        db.cypher_query(query, params)
    print(f"  Cleared existing '{source}' data from Neo4j.")
```

- [ ] **Step 2: Update clear_all similarly**

Replace `clear_all`:

```python
def clear_all() -> None:
    """Remove all codebase nodes and relationships.

    Uses db.cypher_query for fast bulk deletion.
    Targets atomized labels.
    """
    queries = [
        "MATCH (p:Parameter) DETACH DELETE p",
        "MATCH (m:Method|Attribute|EnumValue|Define|Function) DETACH DELETE m",
        "MATCH (c:Class|Interface|Enum|Union) DETACH DELETE c",
        "MATCH (n:Namespace) DETACH DELETE n",
        "MATCH (f:File) DETACH DELETE f",
        "MATCH (md:Metadata) DETACH DELETE md",
    ]
    for query in queries:
        db.cypher_query(query)
    print("  Cleared all codebase data from Neo4j.")
```

- [ ] **Step 3: Commit**

```bash
git add src/doxygen_index/neo4j_backend.py
git commit -m "refactor: update clear_source and clear_all for atomized Neo4j labels"
```

---

### Task 6: Update Tests

**Files:**
- Modify: `tests/test_parser.py`

- [ ] **Step 1: Update test_parse_xml_dir assertions**

Replace the `test_parse_xml_dir` method:

```python
    def test_parse_xml_dir(self, xml_dir):
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)

        assert isinstance(result, ParseResult)

        # Files
        assert len(result.files) == 1
        assert result.files[0].name == "math.h"
        assert result.files[0].source == "test"

        # Typed compound lists
        assert len(result.classes) == 1
        assert len(result.enums) == 0
        assert len(result.unions) == 0
        assert len(result.interfaces) == 0

        cls = result.classes[0]
        assert cls.name == "MyClass"
        assert cls.qualified_name == "myns::MyClass"
        assert cls.kind == "class"
        assert cls.source == "test"
        assert "test class" in cls.brief_description

        # Backward-compat properties
        assert len(result.compounds) == 1
        assert result.compounds[0] is cls

        # Typed member lists
        assert len(result.methods) == 1
        assert len(result.attributes) == 0
        assert len(result.enum_values) == 0
        assert len(result.defines) == 0
        assert len(result.functions) == 0

        fn = result.methods[0]
        assert fn.name == "doSomething"
        assert fn.compound_refid == "classMyClass"
        assert fn.source == "test"
        # qualified_name includes normalized argsstring for overload safety
        assert fn.qualified_name == "myns::MyClass::doSomething(double, int)"

        # Backward-compat members property
        assert len(result.members) == 1
        assert result.members[0] is fn

        # Parameters
        assert len(result.parameters) == 2
        assert result.parameters[0].name == "x"
        assert result.parameters[0].type == "double"
        assert result.parameters[1].name == "y"
        assert result.parameters[1].default_value == "0"

        # Includes
        assert len(result.includes) == 1
        assert result.includes[0].included_file == "MyClass.h"
        assert result.includes[0].is_local is True
```

- [ ] **Step 2: Update TestSqliteRoundTrip to use atomized types**

Replace the `test_round_trip` method:

```python
    def test_round_trip(self, tmp_path):
        from codegraph import ClassNode, AttributeNode, FileNode, NamespaceNode
        from doxygen_index.parser import ParseResult
        from doxygen_index.sqlite_backend import create_schema, write_result

        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)

        result = ParseResult(
            files=[FileNode(refid="f1", name="test.h", path="src/test.h", language="C++", source="mylib")],
            namespaces=[NamespaceNode(refid="ns1", name="myns", qualified_name="myns", source="mylib", layer="dependency")],
            classes=[ClassNode(
                refid="c1", kind="class", name="Foo",
                qualified_name="myns::Foo",
                file_path="", line_number=None,
                brief_description="A class.", detailed_description="",
                definition="", module="myns", base_classes=[],
                is_final=False, is_abstract=False,
                source="mylib", source_type="", layer="dependency",
            )],
            attributes=[AttributeNode(
                refid="m1", compound_refid="c1", kind="variable",
                name="bar", qualified_name="myns::Foo::bar",
                type_signature="int", definition="int myns::Foo::bar",
                file_path="", line_number=None,
                brief_description="A member.", detailed_description="",
                protection="public", is_static=False, is_const=False,
                source="mylib", layer="dependency",
            )],
        )

        counts = write_result(conn, result)
        assert counts["files"] == 1
        assert counts["compounds"] == 1
        assert counts["members"] == 1

        # Verify source column
        row = conn.execute("SELECT source FROM compounds WHERE name = 'Foo'").fetchone()
        assert row[0] == "mylib"

        row = conn.execute("SELECT source FROM members WHERE name = 'bar'").fetchone()
        assert row[0] == "mylib"

        conn.close()
```

- [ ] **Step 3: Run all tests**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/ -v
```

Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_parser.py
git commit -m "test: update parser tests for atomized models and qualified_name with argsstring"
```

---

### Task 7: Add Unit Tests for Helper Functions

**Files:**
- Modify: `tests/test_parser.py` (add new test classes)

- [ ] **Step 1: Add tests for _normalize_argsstring, _derive_module, _derive_source_type**

Add at the end of the file, before the existing test classes:

```python
class TestNormalizeArgsstring:
    def test_empty(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("") == "()"
        assert _normalize_argsstring("()") == "()"
        assert _normalize_argsstring("(void)") == "()"

    def test_simple_types(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(int)") == "(int)"
        assert _normalize_argsstring("(int, float)") == "(int, float)"

    def test_strips_param_names(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(int x, const char* str)") == "(int, const char*)"
        assert _normalize_argsstring("(double val, int count)") == "(double, int)"

    def test_preserves_qualifiers(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(const Foo& foo)") == "(const Foo&)"
        assert _normalize_argsstring("(volatile int* ptr)") == "(volatile int*)"


class TestDeriveModule:
    def test_namespaced(self):
        from doxygen_index.parser import _derive_module
        assert _derive_module("myns::MyClass") == "myns"
        assert _derive_module("ns1::ns2::ClassName") == "ns1::ns2"

    def test_top_level(self):
        from doxygen_index.parser import _derive_module
        assert _derive_module("MyClass") == ""
        assert _derive_module("") == ""


class TestDeriveSourceType:
    def test_header(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("src/Foo.h") == "header"
        assert _derive_source_type("include/bar.hpp") == "header"

    def test_source(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("src/Foo.cpp") == "source"
        assert _derive_source_type("tests/test.c") == "source"

    def test_unknown(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("") == ""
        assert _derive_source_type("README.md") == ""
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v
```

Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_parser.py
git commit -m "test: add unit tests for normalize_argsstring, derive_module, derive_source_type"
```

---

## Self-Review Checklist

1. **Spec coverage:** Each spec requirement mapped to a task above ✓
2. **Placeholder scan:** No TBD/TODO — all code shown inline ✓
3. **Type consistency:** `qualified_name` construction consistent across Task 3 and Task 7 assertions ✓
