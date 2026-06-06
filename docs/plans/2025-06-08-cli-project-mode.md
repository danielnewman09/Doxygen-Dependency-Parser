# CLI Project Mode: Standalone Doxygen Parsing for Any Repository

## Problem

`doxygen-index` currently only works for Conan-managed dependencies. There's no way to:

1. Point at an arbitrary repo (e.g. `../cpp-sqlite`) and parse its own source code
2. Run it as a standalone CLI on any C++ codebase without Conan
3. Get structured output without a running Neo4j instance

Goal: `pip install doxygen-index`, then `doxygen-index project` reads `.doxygen-index.toml` from the target repo and produces parseable results.

---

## Current Architecture

| Module | Role | Coupling |
|--------|------|----------|
| `conan.py` | Discover deps via `conan graph info` | Conan-only |
| `deps_config.py` | Per-dep Doxygen settings (subdir, patterns, predefined) | Conan-oriented |
| `doxygen.py` | Generate Doxyfile, run `doxygen` binary | Works on any `Path` — **reusable** |
| `parser.py` | Parse Doxygen XML → `ParseResult` dataclasses | Totally generic — **reusable** |
| `neo4j_backend.py` | Write `ParseResult` → Neo4j | Neo4j-only |
| `cli.py` | Orchestrate discovery → generation → ingestion | Conan-locked subcommands |

**Key insight**: The core (`doxygen.py` + `parser.py`) is already generic. The coupling is in `conan.py` (input) and `neo4j_backend.py` (output). The middle is portable.

---

## Design Principle: Explicit Configuration, No Auto-Detection

**No guessing.** Anything that depends on project layout — header locations, source directories, exclude patterns, preprocessor defines — must be explicitly specified in a config file. The CLI provides sensible defaults for the *mechanics* (Doxygen flags, output location), but never infers *what to index*.

This means:
- `doxygen-index project /path/to/repo` **requires** a `.doxygen-index.toml` in the repo (or a `--config` flag)
- No CMake parser, no conanfile parser, no heuristic directory scanning
- The config file is the single source of truth

---

## Proposed Changes

### 1. Config File: `.doxygen-index.toml`

Placed at the root of the target repository. All project-specific settings live here.

```toml
# .doxygen-index.toml — required for `doxygen-index project`

[project]
name = "cpp_sqlite"
input_paths = ["include", "cpp_sqlite/src"]   # relative to this file's directory
file_patterns = "*.h *.hpp *.cpp"
recursive = true
exclude_patterns = ["*/test/*", "*/build/*", "*/.git/*"]
predefined = "BOOST_DESCRIBE_CPP14=1"

[output]
# format = "json"        # default; also "neo4j"
# neo4j_uri = "bolt://localhost:7687"
```

The `[project]` section maps 1:1 to a `ProjectConfig` dataclass. The `[output]` section controls where results go. All fields have defaults except `input_paths` and `name` — those **must** be specified.

Example for `../cpp-sqlite`:

```toml
# ../cpp-sqlite/.doxygen-index.toml
[project]
name = "cpp_sqlite"
input_paths = ["include", "cpp_sqlite/src"]
file_patterns = "*.h *.hpp *.cpp"
exclude_patterns = ["*/test/*", "*/build/*"]
predefined = "BOOST_DESCRIBE_CPP14=1 SQLITE_USECPP20=1"
```

### 2. `ProjectConfig` Dataclass

New file `src/doxygen_index/project.py`:

```python
@dataclass
class ProjectConfig:
    """Configuration for indexing a project's source code.

    Loaded from .doxygen-index.toml — no auto-detection.
    """
    name: str
    input_paths: list[str]             # Required: dirs relative to config file
    file_patterns: str = "*.h *.hpp *.hxx *.cpp *.cxx *.cc"
    recursive: bool = True
    exclude_patterns: str = ""          # Doxygen EXCLUDE_PATTERNS
    predefined: str = ""                # Doxygen PREDEFINED macros
```

The `input_paths` are relative to the directory containing the config file, resolved at load time. This keeps the config file portable — you can commit it to the repo and it works regardless of where the repo is checked out.

### 3. Config Loading

```python
def load_config(project_dir: Path) -> ProjectConfig:
    """Load .doxygen-index.toml from the project directory.

    Raises FileNotFoundError if no config file found.
    """
    config_path = project_dir / ".doxygen-index.toml"
    if not config_path.exists():
        print(f"Error: no .doxygen-index.toml found in {project_dir}", file=sys.stderr)
        print("Create one with:", file=sys.stderr)
        print('  [project]', file=sys.stderr)
        print('  name = "myproject"', file=sys.stderr)
        print('  input_paths = ["include", "src"]', file=sys.stderr)
        sys.exit(1)

    # Python 3.11+ has tomllib in stdlib
    import tomllib
    data = tomllib.loads(config_path.read_text())
    proj = data.get("project", {})

    return ProjectConfig(
        name=proj["name"],                          # required
        input_paths=proj["input_paths"],             # required
        file_patterns=proj.get("file_patterns", "*.h *.hpp *.hxx *.cpp *.cxx *.cc"),
        recursive=proj.get("recursive", True),
        exclude_patterns=proj.get("exclude_patterns", ""),
        predefined=proj.get("predefined", ""),
    )
```

### 4. Generalize `doxygen.py` — Accept Multiple Input Paths

Current signature:
```python
def generate_doxyfile(dep_name: str, include_path: Path, ...) -> str
```

New signature (backward compatible):
```python
def generate_doxyfile(
    name: str,
    input_paths: Path | list[Path],   # Accept single path (dep mode) or multiple (project mode)
    xml_output_dir: Path,
    config: DepConfig | ProjectConfig | None = None,
) -> str:
```

The Doxyfile `INPUT` field becomes a space-separated list of resolved paths. For the existing Conan dependency path, `input_paths` is just `[include_path]` — no behavioral change.

Similarly, `run_doxygen` and `generate_xml` get updated to handle `ProjectConfig` alongside `DepConfig`.

### 5. CLI Subcommand: `project`

```bash
# Must have .doxygen-index.toml in the project directory
doxygen-index project /path/to/cpp-sqlite

# Or specify config file explicitly
doxygen-index project /path/to/cpp-sqlite --config my-config.toml

# With Neo4j output
doxygen-index project /path/to/cpp-sqlite --neo4j

# Just generate Doxygen XML (skip parsing)
doxygen-index project /path/to/cpp-sqlite --generate-only --output-dir build/docs

# Just parse existing XML (skip Doxygen run)
doxygen-index project /path/to/cpp-sqlite --parse-only --xml-dir build/docs/xml

# Override output format
doxygen-index project /path/to/cpp-sqlite --format json --output result.json
```

Argument parsing:

```
doxygen-index project <project-dir>
  --config <path>           # Config file path (default: <project-dir>/.doxygen-index.toml)
  --output-dir <dir>        # Override output directory (default: <project-dir>/build/docs/doxygen-<name>)
  --format <json|neo4j>     # Output format (default: json)
  --source <label>          # Source label for provenance (default: project name from config)
  --generate-only           # Only run Doxygen, don't parse
  --parse-only --xml-dir    # Only parse existing XML, don't run Doxygen
  --neo4j / --neo4j-uri / --neo4j-user / --neo4j-password
```

If no config file is found and no `--config` is specified, print a helpful error message with a template.

### 6. JSON Output Backend

New file `src/doxygen_index/json_backend.py`:

```python
def write_result(result: ParseResult, output_path: Path, source: str = "") -> None:
    """Write ParseResult to a JSON file."""
    data = {
        "metadata": {
            "source": source,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "format_version": 1,
        },
        "files": [f.__properties__ for f in result.files],
        "namespaces": [n.__properties__ for n in result.namespaces],
        "classes": [c.__properties__ for c in result.classes],
        "enums": [e.__properties__ for e in result.enums],
        "unions": [u.__properties__ for u in result.unions],
        "interfaces": [i.__properties__ for i in result.interfaces],
        "methods": [m.__properties__ for m in result.methods],
        "attributes": [a.__properties__ for a in result.attributes],
        "enum_values": [e.__properties__ for e in result.enum_values],
        "defines": [d.__properties__ for d in result.defines],
        "functions": [f.__properties__ for f in result.functions],
        "parameters": [p.__properties__ for p in result.parameters],
        "includes": [asdict(i) for i in result.includes],
        "invokes": [asdict(i) for i in result.invokes],
        "invoked_by": [asdict(i) for i in result.invoked_by],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, default=str))
```

This is the **default** output format. No external dependencies. Immediately useful for piping to jq, feeding to analysis scripts, or using as LLM context.

### 7. Summary Printer

Print a human-readable summary to stdout regardless of output format:

```
$ doxygen-index project ../cpp-sqlite

Reading config: ../cpp-sqlite/.doxygen-index.toml
Project: cpp_sqlite
Input paths: include, cpp_sqlite/src
File patterns: *.h *.hpp *.cpp

Running Doxygen for cpp_sqlite...
  cpp_sqlite: 42 XML files generated

Parsing XML...
  Classes:       12
  Methods:       45
  Functions:      8
  Enums:          3
  Namespaces:     2
  Files:         15
  Includes:      23
  Invokes:       67

Output: build/docs/doxygen-cpp_sqlite/cpp_sqlite.json
```

---

## Implementation Plan

### Phase 1: Minimum Viable Project Command

1. **`src/doxygen_index/project.py`** — `ProjectConfig` dataclass + `load_config()`
2. **Generalize `doxygen.py`** — `generate_doxyfile` accepts `Path | list[Path]` for `input_paths`
3. **`src/doxygen_index/json_backend.py`** — `write_result()` serialization
4. **`cli.py`** — Add `project` subcommand with `cmd_project()`
5. **Test** against `../cpp-sqlite` with a `.doxygen-index.toml` config

### Phase 2: Conan Integration

Add `--conan` flag to the `project` subcommand. When specified:
1. Run existing Conan discovery alongside project indexing
2. Merge `ParseResult`s, tagging with different `source` labels
3. Project code gets `layer="codebase"`, deps get `layer="dependency"`

### Phase 3: Additional Backends

1. SQLite backend (local queryable, no server needed)
2. Markdown summary (human-readable)
3. Dot/graphviz visualization (call graphs, inheritance)

---

## Key Design Decisions

### Q: Should `ProjectConfig` be separate from `DepConfig`?

**Yes.** Different shapes:

- `DepConfig`: single `subdir` under a Conan package's `include/`. Narrow, dependency-oriented.
- `ProjectConfig`: multiple `input_paths`, project-level name, broader defaults. Wide, project-oriented.

They both feed into `generate_doxyfile` but represent different use cases.

### Q: Why not auto-detect project structure from CMakeLists.txt?

Too fragile. CMake project structures vary wildly — `target_include_directories` can reference build directories, generator expressions, aliased targets, etc. The config file is explicit, version-controllable, and debuggable. If you move your headers, you change one line in `.doxygen-index.toml`, not debug why the heuristics broke.

### Q: What about projects that already have a Doxyfile?

Two options:
1. **Override mode** (default): We generate our own minimal Doxyfile. We control the output.
2. **Respect mode** (`--use-existing-doxyfile`): Find and use the project's Doxyfile, just ensuring `GENERATE_XML = YES`.

Start with override only. Add `--use-existing-doxyfile` later if needed.

### Q: Why TOML for config?

- Python 3.11+ has `tomllib` in stdlib (no new dependency for Python >= 3.10 with `tomli` backport)
- Human-friendly: comments, no significant whitespace issues like YAML
- Unambiguous: one way to represent a list of strings
- The `pyproject.toml` precedent — Python developers already know TOML

For Python 3.10, we'd need `tomli` as a dependency. That's acceptable.

### Q: Should `--format json` also print to stdout?

JSON goes to a file by default (`<output-dir>/<name>.json`). Add `--format json --stdout` to pipe to stdout for scripting:
```bash
doxygen-index project . --format json --stdout | jq '.classes[] | .name'
```

---

## Example: `../cpp-sqlite`

Create config file:

```toml
# ../cpp-sqlite/.doxygen-index.toml
[project]
name = "cpp_sqlite"
input_paths = ["include", "cpp_sqlite/src"]
file_patterns = "*.h *.hpp *.cpp"
exclude_patterns = "*/test/* */build/* */.git/*"
predefined = "BOOST_DESCRIBE_CPP14=1"
```

Run:

```bash
doxygen-index project ../cpp-sqlite
```

What happens:
1. Load `.doxygen-index.toml` → `ProjectConfig`
2. Resolve `input_paths` relative to `../cpp-sqlite/` → `["../cpp-sqlite/include", "../cpp-sqlite/cpp_sqlite/src"]`
3. Generate Doxyfile with `INPUT = ../cpp-sqlite/include ../cpp-sqlite/cpp_sqlite/src`
4. Run `doxygen` → XML at `build/docs/doxygen-cpp_sqlite/xml/`
5. Parse XML → `ParseResult`
6. Write `build/docs/doxygen-cpp_sqlite/cpp_sqlite.json`
7. Print summary

---

## Open Questions

1. **Python 3.10 + TOML**: Use `tomli` as conditional dependency (`import tomllib; except ImportError: import tomli as tomllib`), or require Python 3.11+?

2. **Config file in Conan dependency mode**: Should the existing `full` / `generate` / `ingest` commands also support a config file for per-project dependency overrides? (Currently uses `BUILTIN_CONFIGS` in `deps_config.py`.)

3. **JSON schema versioning**: Include a `format_version` field in the JSON output so consumers can handle format evolution?

4. **MCP server integration**: The `tools.py` module defines LLM-callable tools against Neo4j. Could `project` mode register the parse result as an MCP resource, making it queryable without Neo4j?