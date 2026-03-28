# doxygen-index

Index Doxygen XML output and Conan C++ dependencies into SQLite and Neo4j graph databases.

Takes any Conan-managed C++ project, discovers dependency headers, generates Doxygen XML, and ingests the results into searchable databases alongside your own codebase documentation.

## Installation

```bash
# SQLite only (no external dependencies)
pip install doxygen-index

# Development
pip install -e ".[dev]"
```

**System requirements:** `doxygen` and `conan` on PATH.

## CLI Usage

```bash
# List known dependency configurations
doxygen-index list-deps

# Discover what's available in your Conan cache
doxygen-index discover --build-type Debug

# Generate Doxygen XML for all dependencies
doxygen-index generate --output-dir build/docs/deps

# Generate for specific deps only
doxygen-index generate --output-dir build/docs/deps --only eigen,sdl

# Ingest into SQLite
doxygen-index ingest --output-dir build/docs/deps --sqlite codebase.db

# Ingest into Neo4j
doxygen-index ingest --output-dir build/docs/deps --neo4j

# All-in-one: discover + generate + ingest
doxygen-index full --output-dir build/docs/deps --sqlite codebase.db --neo4j
```

## Python API

```python
from doxygen_index import discover_packages, generate_xml, ingest_sqlite

# Discover Conan dependency include paths
packages = discover_packages(build_type="Debug")

# Generate Doxygen XML
xml_dirs = generate_xml(packages, output_dir="build/docs/deps")

# Ingest into SQLite
for name, xml_dir in xml_dirs.items():
    ingest_sqlite(xml_dir, db_path="codebase.db", source=name)
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
            --sqlite ${CMAKE_BINARY_DIR}/docs/codebase.db
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
endif()
```

## Example Queries

### SQLite

```sql
-- All Eigen classes
SELECT name, brief_description FROM compounds WHERE source = 'eigen';

-- Search across everything
SELECT * FROM fts_docs WHERE fts_docs MATCH 'matrix';

-- What sources are indexed?
SELECT source, COUNT(*) FROM compounds GROUP BY source;
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
