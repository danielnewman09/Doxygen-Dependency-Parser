# doxygen-index

Index source code into graph databases.  Supports **C++** (via Doxygen XML) and
**Python** (via the standard library ``ast`` module — no external tools required).

Takes any project, generates/parses its source, and outputs structured results
(JSON or Neo4j).  Works with Conan-managed dependencies and standalone projects alike.

## Installation

```bash
# Development
pip install -e ".[dev]"
```

**System requirements:**
- `doxygen` on PATH — only needed for C++ projects
- `conan` — optional, only needed for Conan dependency mode
- Python projects need no external tools (uses the built-in ``ast`` module)
- A running **Neo4j** instance — only needed when using `--format neo4j` / `--neo4j`
- `pip install openai` + ``LLM_API_KEY`` env var — optional, only needed for ``--enrich``

### Neo4j connection via `.env`

The CLI and Python API automatically load a `.env` file from the current
working directory (or any parent).  Copy the template and adjust:

```bash
cp .env.example .env
# Edit .env with your credentials
```

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
# NEO4J_DATABASE=neo4j   # optional
```

Values in the real environment always take precedence over `.env`.
You can also override on the command line with `--neo4j-uri`,
`--neo4j-user`, and `--neo4j-password`.

## Quick Start: Index Your Own Project

1. Create a `.doxygen-index.toml` in your project root:

### C++ project

```toml
[project]
name = "myproject"
language = "cpp"          # default, can be omitted
input_paths = ["include", "src"]
# file_patterns = "*.h *.hpp *.cpp"   # default
# exclude_patterns = "*/test/* */build/*"
# predefined = "SOME_MACRO=1"
```

### Python project

```toml
[project]
name = "myproject"
language = "python"
input_paths = ["src"]
test_paths = ["tests"]          # also parse test dirs for TestNode extraction
# exclude_patterns = "build dist"   # additional dirs to skip
```

When ``test_paths`` is specified, the parser also walks those
directories and extracts ``test_*`` functions and ``Test*`` classes as
:class:`TestNode` instances with :class:`AssertionNode` and
:class:`TestStepNode` children, linked to the code they exercise via
``VERIFIES`` edges.  You can also pass ``--test-paths tests`` on the
command line to override the config.

For Python, virtual environments (`.venv`, `venv`, `env`), caches
(`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`), build
artifacts (`build`, `dist`), and other common non-source directories are
**automatically excluded** — no need to list them.

2. Run from inside the project directory:

```bash
cd /path/to/myproject
doxygen-index
```

Or specify the path explicitly:

```bash
doxygen-index project /path/to/myproject
```

3. Get a JSON output with all parsed symbols:

```bash
cat build/docs/doxygen-myproject/myproject.json | jq '.classes[] | .name'
```

### HTML Graph Visualization

Add a ``[codegraph-html]`` section to your ``.doxygen-index.toml`` to
automatically generate an interactive HTML graph alongside the JSON:

```toml
[project]
name = "myproject"
language = "python"
input_paths = ["src"]

[codegraph-html]
output_dir = "codegraph"   # where to write JSON + HTML (default: codegraph)
size = "large"             # "large" (full-page) or "small" (compact)
```

With this section present, running ``doxygen-index`` produces:

- ``codegraph/myproject.json`` — LayerGraph-compatible JSON for visualization
- ``codegraph/myproject.html`` — self-contained interactive Cytoscape.js graph

The HTML file is fully self-contained (no external dependencies) and can be
opened directly in any browser.

To regenerate just the HTML without re-parsing:

```bash
doxygen-index html
doxygen-index html --size small
```

### LLM Test Description Enrichment

When ``test_paths`` is configured, the parser extracts test nodes (fixtures,
steps, assertions) from your test suite.  You can optionally enrich these
nodes with human-readable descriptions using an LLM:

**Requirements:**
- ``LLM_API_KEY`` environment variable (Anthropic or OpenAI key)
- ``pip install openai`` (optional dependency)
- Optional: ``LLM_BASE_URL`` and ``LLM_MODEL`` for custom endpoints

```bash
# Dry-run: build prompts without calling the LLM (fast, safe)
doxygen-index project . --enrich --dry-run-enrich

# Full enrichment: call the LLM to generate descriptions
doxygen-index project . --enrich

# Overwrite existing descriptions (skip by default)
doxygen-index project . --enrich --overwrite-enrich

# Save enrichment summary to output dir
doxygen-index project . --enrich --output-enrich-summary
```

```python
from doxygen_index.enrich import enrich_result
from doxygen_index.parser import parse_python_dir

result = parse_python_dir("src", test_paths=["tests"])
summary = enrich_result(
    result,
    model="claude-sonnet-4-20250514",  # optional, default from LLM_MODEL env
    dry_run=False,
    overwrite=False,
)
print(f"Enriched {summary.total_enriched} test nodes")
```

The enrichment:
- Generates 1–2 sentence descriptions describing *purpose* and *why*.
- Links descriptions to code under test via ``VERIFIES`` edges.
- Groups peer context (other fixtures/steps/assertions in the same test).
- Is applied **in-memory** before writing to JSON or Neo4j.

The ``--dry-run-enrich`` flag is useful for previewing prompts and verifying
that the enrichment will target the right nodes before incurring API costs.

## CLI Usage

```bash
# Parse a project (requires .doxygen-index.toml in project dir)
# When run inside the project directory, the path can be omitted:
doxygen-index                          # auto-detects config in current dir
doxygen-index project                  # explicit subcommand
doxygen-index project /path/to/project # explicit path

# Parse with custom output
doxygen-index project --output-dir custom/docs

# Parse and ingest into Neo4j (incremental by default)
# Adds new nodes, updates changed ones, deletes stale ones
doxygen-index project --format neo4j

# Full re-index (clears existing data for this source first)
doxygen-index project --format neo4j --clear

# Just generate Doxygen XML (don't parse) — C++ only
doxygen-index project --generate-only

# Just parse existing XML — C++ only
doxygen-index project --parse-only --xml-dir build/docs/xml

# Regenerate HTML graph from existing JSON (requires [codegraph-html] in config)
doxygen-index html
doxygen-index html --size small

# -----------------------------------------------------------------------
# Conan dependency mode (requires conan):

# List known dependency configurations
doxygen-index list-deps

# Discover what's available in your Conan cache
doxygen-index discover --build-type Debug

# Generate Doxygen XML for all dependencies
doxygen-index generate --output-dir build/docs/deps

# Generate for specific deps only
doxygen-index generate --output-dir build/docs/deps --only eigen,sdl

# Ingest into Neo4j (incremental by default)
doxygen-index ingest --output-dir build/docs/deps --neo4j

# Full re-index of existing dependencies (clears first)
doxygen-index ingest --output-dir build/docs/deps --neo4j --clear

# All-in-one: discover + generate + ingest (incremental by default)
doxygen-index full --output-dir build/docs/deps --neo4j
```

## C++ Standard Library (cppreference)

Index the entire C++ standard library documentation from cppreference.com.

```bash
# Download, parse, and ingest into Neo4j
# First run downloads the HTML archive (~30 MB) and caches it
# Parsing ~18,000 pages takes 5-10 minutes

# Ingest into Neo4j (incremental by default)
doxygen-index cppreference --neo4j

# Full re-index (clears existing cppreference data first)
doxygen-index cppreference --neo4j --clear

# Custom cache location
doxygen-index cppreference --neo4j --cache-dir /path/to/cache

# Force re-download
doxygen-index cppreference --neo4j --force
```

```python
from doxygen_index.cppreference import download, parse
from doxygen_index.neo4j_backend import update_result, ensure_schema

# Download and parse
archive_root = download("~/.cache/doxygen-index/cppreference")
result = parse(archive_root)

# Incrementally ingest into Neo4j (default)
ensure_schema()
update_result(result, source="cppreference")
```

## Python API

```python
from doxygen_index import discover_packages, generate_xml
from doxygen_index.neo4j_backend import ingest as ingest_neo4j

# Discover Conan dependency include paths
packages = discover_packages(build_type="Debug")

# Generate Doxygen XML
xml_dirs = generate_xml(packages, output_dir="build/docs/deps")

# Ingest into Neo4j (incremental by default — no wipe)
for name, xml_dir in xml_dirs.items():
    ingest_neo4j(xml_dir, source=name, uri="bolt://localhost:7687")

# Full re-write (clears source first)
for name, xml_dir in xml_dirs.items():
    ingest_neo4j(xml_dir, source=name, uri="bolt://localhost:7687", clear=True)
```

### Incremental Re-indexing

Incremental update is the **default** behavior.  The :func:`update_result`
function re-indexes a source without destroying the existing graph.  It:

1. **Creates** new nodes that appeared in the source since the last index.
2. **Updates** existing nodes in place (via MERGE on deterministic uid +
   source) when their properties changed.
3. **Deletes** stale nodes — those that were removed or renamed in the
   source — and their edges.

Other sources are left untouched.

```python
from doxygen_index.parser import parse_python_dir
from doxygen_index.neo4j_backend import (
    connect_neo4j, ensure_schema, update_result,
)

connect_neo4j()
ensure_schema()

result = parse_python_dir("src/myproject", source="myproject")
deleted = update_result(result, source="myproject")
# deleted is a dict like {"ClassNode": 2, "FunctionNode": 1}
# showing how many stale nodes were removed.
```

The CLI simply does an incremental update by default;
pass ``--clear`` for a full re-write::

    doxygen-index project --format neo4j         # incremental (default)
    doxygen-index project --format neo4j --clear # full re-write
    doxygen-index ingest --output-dir build/docs/deps --neo4j          # incremental
    doxygen-index ingest --output-dir build/docs/deps --neo4j --clear  # full re-write
    doxygen-index full --output-dir build/docs/deps --neo4j            # incremental
    doxygen-index full --output-dir build/docs/deps --neo4j --clear    # full re-write

### Neo4j

```python
from doxygen_index.neo4j_backend import ingest as ingest_neo4j

ingest_neo4j(xml_dir, source="eigen", uri="bolt://localhost:7687")
```

### Custom dependency configs

```python
from doxygen_index.deps_config import DepConfig
from doxygen_index import discover_packages, generate_xml

custom = {
    "my-lib": DepConfig(
        file_patterns="*.h *.hpp",
        recursive=True,
        subdir="mylib",
    ),
}

packages = discover_packages(dep_configs=custom)
xml_dirs = generate_xml(packages, output_dir="build/docs/deps", dep_configs=custom)
```

## CMake Integration

```cmake
find_program(DOXYGEN_INDEX doxygen-index)
if(DOXYGEN_INDEX)
  add_custom_target(deps-index
    COMMAND doxygen-index full
            --output-dir ${CMAKE_BINARY_DIR}/docs/deps
            --build-type ${CMAKE_BUILD_TYPE}
            --neo4j
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
endif()
```

## Example Queries

### cppreference queries

```cypher
-- All std::vector members
MATCH (c:Compound)-[:COMPOSES]->(m:Member)
WHERE c.qualified_name = 'std::vector'
RETURN m.name, m.brief_description

-- Find all algorithms that work on ranges
MATCH (c:Compound {source: "cppreference"})
WHERE c.qualified_name STARTS WITH "std::ranges"
RETURN c.name, c.brief_description
```

### Neo4j (Cypher)

```cypher
-- What Eigen classes are available?
MATCH (c:Compound {source: "eigen"}) RETURN c.name, c.brief_description

-- Search across all sources
CALL db.index.fulltext.queryNodes("doc_search", "collision") YIELD node
RETURN node.source, node.name, node.brief_description

-- What sources are in the graph?
MATCH (n) WHERE n.source IS NOT NULL
RETURN DISTINCT n.source, labels(n)[0], count(*)
```
