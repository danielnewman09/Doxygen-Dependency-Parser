The implementation plan has been written to `docs/plans/2025-06-08-calls-to-invokes.md` and is ready for execution. Here's a summary of the 4 tasks:

| Task | Scope | Key Changes |
|---|---|---|
| **1** | `parser.py` | `CallEntry` → `InvokeEntry`, `calls` → `invokes`, `called_by` → `invoked_by` |
| **2** | `neo4j_backend.py` | Rename function, `[:CALLS]` → `[:INVOKES]`, `:Member` → `:Method|Function`, `result.calls` → `result.invokes` |
| **3** | `examples/doxygen_to_neo4j.py` | Rename method/field, `[:CALLS]` → `[:INVOKES]`, `add_call` → `add_invoke` |
| **4** | Verification | grep for stale references, run test suite |

The plan is scoped to **write path only** — tools.py and the MCP server query methods remain unchanged and will be migrated to codegraph's `GraphRepository` in a separate issue.