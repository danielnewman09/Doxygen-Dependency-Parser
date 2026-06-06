# doxygen-index

Index Doxygen XML output and Conan C++ dependencies into Neo4j graph databases.

Takes any C++ project, generates Doxygen XML, and outputs structured results (JSON or Neo4j).
Works with Conan-managed dependencies and standalone projects alike.

## Installation

```bash
# Development
pip install -e ".[dev]"
```

**System requirements:** `doxygen` on PATH. `conan` is optional (only needed for Conan dependency mode).

## Quick Start: Index Your Own Project

1. Create a `.doxygen-index.toml` in your project root:

```toml
[project]
name = "myproject"
input_paths = ["include", "src"]
# file_patterns = "*.h *.hpp *.cpp"   # default
# exclude_patterns = "*/test/* */build/*"
# predefined = "SOME_MACRO=1"
```

2. Run:

```bash
doxygen-index project /path/to/myproject
```

3. Get a JSON output with all parsed symbols:

```bash
cat build/docs/doxygen-myproject/myproject.json | jq '.classes[] | .name'
```

## CLI Usage

```bash
# Parse an arbitrary C++ project (requires .doxygen-index.toml)
doxygen-index project /path/to/project

# Parse with custom output
doxygen-index project /path/to/project --output-dir custom/docs

# Parse and ingest into Neo4j
doxygen-index project /path/to/project --format neo4j

# Just generate Doxygen XML (don't parse)
doxygen-index project /path/to/project --generate-only

# Just parse existing XML
doxygen-index project /path/to/project --parse-only --xml-dir build/docs/xml

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

# Ingest into Neo4j
doxygen-index ingest --output-dir build/docs/deps --neo4j

# All-in-one: discover + generate + ingest
doxygen-index full --output-dir build/docs/deps --neo4j
```

## C++ Standard Library (cppreference)

Index the entire C++ standard library documentation from cppreference.com.

```bash
# Download, parse, and ingest into Neo4j
# First run downloads the HTML archive (~30 MB) and caches it
# Parsing ~18,000 pages takes 5-10 minutes

# Fresh ingest (clears existing cppreference data first)
doxygen-index cppreference --neo4j --clear

# Subsequent runs (appends, skip download if cached)
doxygen-index cppreference --neo4j

# Custom cache location
doxygen-index cppreference --neo4j --cache-dir /path/to/cache

# Force re-download
doxygen-index cppreference --neo4j --force
```

```python
from doxygen_index.cppreference import download, parse
from doxygen_index.neo4j_backend import write_result, ensure_schema, clear_source

# Download and parse
archive_root = download("~/.cache/doxygen-index/cppreference")
result = parse(archive_root)

# Ingest into Neo4j
ensure_schema()
clear_source("cppreference")
write_result(result)
```

## Python API

```python
from doxygen_index import discover_packages, generate_xml
from doxygen_index.neo4j_backend import ingest as ingest_neo4j

# Discover Conan dependency include paths
packages = discover_packages(build_type="Debug")

# Generate Doxygen XML
xml_dirs = generate_xml(packages, output_dir="build/docs/deps")

# Ingest into Neo4j
for name, xml_dir in xml_dirs.items():
    ingest_neo4j(xml_dir, source=name, uri="bolt://localhost:7687")
```

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
