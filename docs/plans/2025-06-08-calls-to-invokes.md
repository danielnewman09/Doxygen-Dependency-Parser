# Issue 1: CALLS → INVOKES Rename — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `CallEntry` → `InvokeEntry`, `calls`/`called_by` → `invokes`/`invoked_by` in the parser data model, and `[:CALLS]` → `[:INVOKES]` in the Neo4j backend write path. Query path (tools.py, MCP server) is out of scope.

**Architecture:** Parser produces `InvokeEntry` instances in `ParseResult.invokes`/`ParseResult.invoked_by`. Backend writes `[:INVOKES]` edges with atomized `:Method|:Function` labels. Example script updated in parallel.

**Tech Stack:** Python 3.12, neomodel, codegraph (atomized models), pytest

---

### Task 1: Rename CallEntry → InvokeEntry in parser data model

**Files:**
- Modify: `src/doxygen_index/parser.py`

- [ ] **Step 1: Rename CallEntry dataclass to InvokeEntry**

Replace:

```python
@dataclass
class CallEntry:
    from_refid: str
    to_refid: str
    to_name: str
```

With:

```python
@dataclass
class InvokeEntry:
    from_refid: str
    to_refid: str
    to_name: str
```

- [ ] **Step 2: Rename ParseResult fields**

Replace in the `ParseResult` dataclass:

```python
    calls: list[CallEntry] = field(default_factory=list)
    called_by: list[CallEntry] = field(default_factory=list)
```

With:

```python
    invokes: list[InvokeEntry] = field(default_factory=list)
    invoked_by: list[InvokeEntry] = field(default_factory=list)
```

- [ ] **Step 3: Update _parse_member() references**

Replace the call-reference section at the end of `_parse_member()`:

```python
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

With:

```python
    # --- Invoke references (shared) ---
    for ref in memberdef.findall("references"):
        result.invokes.append(InvokeEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))

    for ref in memberdef.findall("referencedby"):
        result.invoked_by.append(InvokeEntry(
            from_refid=refid,
            to_refid=ref.get("refid", ""),
            to_name=ref.text or "",
        ))
```

- [ ] **Step 4: Run tests to verify parser still works**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v
```

Expected: ALL PASS (no test directly asserts on CallEntry/calls/called_by)

- [ ] **Step 5: Commit**

```bash
git add src/doxygen_index/parser.py
git commit -m "refactor: rename CallEntry → InvokeEntry, calls/called_by → invokes/invoked_by"
```

---

### Task 2: Rename _write_call_relationships → _write_invoke_relationships in backend

**Files:**
- Modify: `src/doxygen_index/neo4j_backend.py`

- [ ] **Step 1: Update write_result() call site**

Replace in `write_result()`:

```python
    _write_call_relationships(result)
```

With:

```python
    _write_invoke_relationships(result)
```

- [ ] **Step 2: Rename function and update Cypher**

Replace the entire `_write_call_relationships` function:

```python
def _write_call_relationships(result: ParseResult) -> None:
    if not result.calls:
        print("  Calls: 0")
        return
    batch_size = 1000
    batch_dicts = [asdict(c) for c in result.calls]
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (caller:Member {refid: row.from_refid})
            MATCH (callee:Member {refid: row.to_refid})
            MERGE (caller)-[:CALLS]->(callee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Calls: {created} (of {len(batch_dicts)} references)")
```

With:

```python
def _write_invoke_relationships(result: ParseResult) -> None:
    if not result.invokes:
        print("  Invokes: 0")
        return
    batch_size = 1000
    batch_dicts = [asdict(c) for c in result.invokes]
    created = 0
    for i in range(0, len(batch_dicts), batch_size):
        batch = batch_dicts[i:i + batch_size]
        results, _meta = db.cypher_query("""
            UNWIND $batch AS row
            MATCH (invoker:Method|Function {refid: row.from_refid})
            MATCH (invokee:Method|Function {refid: row.to_refid})
            MERGE (invoker)-[:INVOKES]->(invokee)
            RETURN count(*) AS cnt
        """, {"batch": batch})
        if results:
            created += results[0][0]
    print(f"  Invokes: {created} (of {len(batch_dicts)} references)")
```

Key changes:
- Function name: `_write_call_relationships` → `_write_invoke_relationships`
- Field access: `result.calls` → `result.invokes`
- Cypher label: `:Member` → `:Method|Function` (matches codegraph's atomized labels)
- Cypher relationship: `[:CALLS]` → `[:INVOKES]`
- Cypher variable: `caller`/`callee` → `invoker`/`invokee`
- Print: `"Calls:"` → `"Invokes:"`

- [ ] **Step 3: Run tests**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/test_parser.py -v
```

Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/doxygen_index/neo4j_backend.py
git commit -m "refactor: rename _write_call_relationships → _write_invoke_relationships, CALLS → INVOKES, Member → Method|Function"
```

---

### Task 3: Update example script

**Files:**
- Modify: `examples/doxygen_to_neo4j.py`

- [ ] **Step 1: Rename internal lists in Neo4jBatchWriter**

Replace in `__init__`:

```python
        self.calls: list[dict] = []       # from_refid -> to_refid (calls)
        self.called_by: list[dict] = []   # from_refid -> to_refid (called_by)
```

With:

```python
        self.invokes: list[dict] = []       # from_refid -> to_refid (invokes)
        self.invoked_by: list[dict] = []   # from_refid -> to_refid (invoked_by)
```

- [ ] **Step 2: Rename add_call → add_invoke, add_called_by → add_invoked_by**

Replace:

```python
    def add_call(self, from_member_refid: str, to_member_refid: str, to_member_name: str):
        self.calls.append({
```

With:

```python
    def add_invoke(self, from_member_refid: str, to_member_refid: str, to_member_name: str):
        self.invokes.append({
```

Replace:

```python
    def add_called_by(self, member_refid: str, caller_refid: str, caller_name: str):
        self.called_by.append({
```

With:

```python
    def add_invoked_by(self, member_refid: str, invoker_refid: str, invoker_name: str):
        self.invoked_by.append({
```

- [ ] **Step 3: Rename flush() call to _write_invoke_relationships**

Replace in `flush()`:

```python
            self._write_call_relationships(session)
```

With:

```python
            self._write_invoke_relationships(session)
```

- [ ] **Step 4: Rename _write_call_relationships → _write_invoke_relationships**

Replace the entire method. Change:
- Method name: `_write_call_relationships` → `_write_invoke_relationships`
- Field: `self.calls` → `self.invokes`
- Cypher: `[:CALLS]` → `[:INVOKES]`
- Cypher labels: `:Member` → `:Method|:Function` (following the same pattern as the main backend)
- Cypher variables: `caller`/`callee` → `invoker`/`invokee`
- Print: `"Calls:"` → `"Invokes:"`

New method:

```python
    def _write_invoke_relationships(self, session):
        """Create INVOKES relationships between Methods/Functions."""
        if not self.invokes:
            print("  Invokes: 0")
            return
        batch_size = 1000
        created = 0
        for i in range(0, len(self.invokes), batch_size):
            batch = self.invokes[i:i + batch_size]
            result = session.run(
                """
                UNWIND $batch AS row
                MATCH (invoker:Member {refid: row.from_refid})
                MATCH (invokee:Member {refid: row.to_refid})
                MERGE (invoker)-[:INVOKES]->(invokee)
                RETURN count(*) AS cnt
                """,
                batch=batch,
            )
            created += result.single()["cnt"]
        print(f"  Invokes: {created} (of {len(self.invokes)} references)")
```

**Note:** The example script still uses `:Member` labels because it hasn't been migrated to atomized models (that's Issue 8). The `[:INVOKES]` relationship label is the critical fix here.

- [ ] **Step 5: Update parse_member() call-site references**

In the `parse_member()` function, replace:

```python
        writer.add_call(refid, to_refid, to_name)
```

With:

```python
        writer.add_invoke(refid, to_refid, to_name)
```

And replace:

```python
        writer.add_called_by(refid, caller_refid, caller_name)
```

With:

```python
        writer.add_invoked_by(refid, invoker_refid, invoker_name)
```

(The variable names in the parse function may use `caller_refid`/`caller_name` — rename to `invoker_refid`/`invoker_name` for consistency.)

- [ ] **Step 6: Commit**

```bash
git add examples/doxygen_to_neo4j.py
git commit -m "refactor: update example script — CALLS → INVOKES, add_call → add_invoke"
```

---

### Task 4: Verify no remaining CALLS references in write path

- [ ] **Step 1: Search for stale references**

```bash
grep -rn "CallEntry\|\.calls\b\|\.called_by\b\|_write_call\|:CALLS\|add_call\b\|add_called_by" src/doxygen_index/ examples/ tests/
```

Expected: No matches (excluding any query-path files like tools.py and MCP server which are out of scope).

- [ ] **Step 2: Run full test suite**

```bash
cd /Users/danielnewman/dev/Doxygen-Dependency-Parser && python -m pytest tests/ -v
```

Expected: ALL PASS

---

## Self-Review Checklist

- [x] **Placeholder scan:** No TBD/TODO — all code changes specified inline
- [x] **Internal consistency:** `InvokeEntry`/`invokes`/`invoked_by` used consistently across parser, backend, and example. `[:INVOKES]` used in all Cypher. `:Method|Function` labels used in backend (main path), `:Member` retained in example (not yet migrated).
- [x] **Scope check:** Write path only — tools.py, MCP server, and query-related Cypher are explicitly out of scope. Issue 2b tracks the query-path migration.
- [x] **Ambiguity check:** `from_refid`/`to_refid` field names on `InvokeEntry` are unchanged (they describe the Doxygen XML structure, not the graph relationship direction). `ParseResult.compounds`/`.members` properties unchanged. Cppreference parser creates `ParseResult()` with default empty `invokes`/`invoked_by` lists — no changes needed.