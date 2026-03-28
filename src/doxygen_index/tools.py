"""
LLM tool definitions for querying the dependency graph database.

Each public function is a self-contained tool that an LLM can invoke to
explore C++ dependency APIs stored in Neo4j.  Every tool returns plain
dicts/lists so the results serialise trivially to JSON.

Quick start::

    from doxygen_index.tools import create_toolset

    tools = create_toolset()                 # connect with defaults
    tools = create_toolset(uri="bolt://localhost:7687")

    # Use a tool directly
    results = tools.search_symbols("window create")

    # Export schemas for an LLM tool-calling API
    schemas = tools.schemas()                # list[dict] — one per tool
"""

from __future__ import annotations

import inspect
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Toolset
# ---------------------------------------------------------------------------

@dataclass
class DependencyGraphTools:
    """Collection of LLM-callable tools backed by a Neo4j dependency graph."""

    _uri: str = ""
    _user: str = ""
    _password: str = ""
    _database: str = "neo4j"
    _driver: Any = field(default=None, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        if self._driver:
            self._driver.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- helpers -------------------------------------------------------------

    def _session(self):
        return self._driver.session(database=self._database)

    def _run(self, cypher: str, params: dict | None = None) -> list[dict]:
        with self._session() as session:
            result = session.run(cypher, parameters=params or {})
            return [dict(r) for r in result]

    # -----------------------------------------------------------------------
    # Tools — each method is one LLM tool
    # -----------------------------------------------------------------------

    def list_sources(self) -> list[dict]:
        """List all indexed dependency sources and their symbol counts.

        Returns one entry per source with node-type breakdown so the LLM
        knows which dependencies are available and how large they are.
        """
        return self._run("""
            MATCH (n) WHERE n.source IS NOT NULL
            WITH n.source AS source, labels(n)[0] AS node_type
            RETURN source, node_type, count(*) AS count
            ORDER BY source, node_type
        """)

    def search_symbols(
        self,
        query: str,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search across symbol names and documentation.

        Use this as the primary discovery tool when mapping a requirement
        to relevant classes, functions, or enums.  Supports natural-language
        terms (e.g. "window create", "font rendering", "draw list").

        Args:
            query:  Search terms (supports Lucene syntax — AND, OR, quotes
                    for exact phrases, ``~`` for fuzzy).
            source: Optional dependency name to restrict results (e.g. "imgui").
            limit:  Maximum number of results.
        """
        cypher = """
            CALL db.index.fulltext.queryNodes('doc_search', $query)
            YIELD node, score
        """
        if source:
            cypher += "WHERE node.source CONTAINS $source\n"
        cypher += """
            RETURN labels(node)[0] AS node_type,
                   node.name AS name,
                   node.qualified_name AS qualified_name,
                   node.kind AS kind,
                   node.brief_description AS brief,
                   node.source AS source,
                   score
            ORDER BY score DESC
            LIMIT $limit
        """
        return self._run(cypher, {"query": query, "source": source, "limit": limit})

    def get_compound(self, name: str, source: Optional[str] = None) -> list[dict]:
        """Get full details of a class, struct, or union and its members.

        Use this after ``search_symbols`` identifies a compound of interest.
        Returns the compound metadata plus all of its members (functions,
        variables, enums, typedefs) with signatures.

        Args:
            name:   Exact or qualified name (e.g. "ImGuiWindow", "ImGui::IO").
            source: Optional dependency name filter.
        """
        where = "(c.name = $name OR c.qualified_name = $name)"
        if source:
            where += " AND c.source CONTAINS $source"
        return self._run(f"""
            MATCH (c:Compound) WHERE {where}
            OPTIONAL MATCH (c)-[:CONTAINS]->(m:Member)
            OPTIONAL MATCH (c)-[:INHERITS_FROM]->(base:Compound)
            OPTIONAL MATCH (derived:Compound)-[:INHERITS_FROM]->(c)
            WITH c, m,
                 collect(DISTINCT base.qualified_name) AS base_classes,
                 collect(DISTINCT derived.qualified_name) AS derived_classes
            RETURN c.name AS name,
                   c.qualified_name AS qualified_name,
                   c.kind AS kind,
                   c.brief_description AS brief,
                   c.detailed_description AS detailed,
                   c.source AS source,
                   c.is_abstract AS is_abstract,
                   c.is_final AS is_final,
                   base_classes,
                   derived_classes,
                   m.name AS member_name,
                   m.kind AS member_kind,
                   m.definition AS member_definition,
                   m.argsstring AS member_args,
                   m.protection AS member_protection,
                   m.brief_description AS member_brief,
                   m.refid AS member_refid
            ORDER BY m.kind, m.name
        """, {"name": name, "source": source})

    def get_member(
        self,
        name: str,
        source: Optional[str] = None,
        fuzzy: bool = False,
    ) -> list[dict]:
        """Get detailed information about a specific function, variable, or enum.

        Returns full signature, parameters, documentation, and the owning
        compound.  Use this to get the exact API surface for code generation.

        Args:
            name:   Exact or qualified name (e.g. "CreateContext", "ImGui::Begin").
            source: Optional dependency name filter.
            fuzzy:  If true, match names containing the search term instead
                    of requiring an exact match.
        """
        if fuzzy:
            where = "(m.name CONTAINS $name OR m.qualified_name CONTAINS $name)"
        else:
            where = "(m.name = $name OR m.qualified_name = $name)"
        if source:
            where += " AND m.source CONTAINS $source"
        return self._run(f"""
            MATCH (m:Member) WHERE {where}
            OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(m)
            OPTIONAL MATCH (m)-[:HAS_PARAMETER]->(p:Parameter)
            WITH m, c, p ORDER BY p.position
            WITH m, c, collect({{
                name: p.name, type: p.type,
                default_value: p.default_value, position: p.position
            }}) AS params
            RETURN m.name AS name,
                   m.qualified_name AS qualified_name,
                   m.kind AS kind,
                   m.type AS return_type,
                   m.definition AS definition,
                   m.argsstring AS argsstring,
                   m.brief_description AS brief,
                   m.detailed_description AS detailed,
                   m.protection AS protection,
                   m.is_static AS is_static,
                   m.is_const AS is_const,
                   m.is_virtual AS is_virtual,
                   m.source AS source,
                   c.name AS compound_name,
                   c.qualified_name AS compound_qualified_name,
                   params
        """, {"name": name, "source": source})

    def browse_namespace(
        self,
        name: str,
        source: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """List classes, free functions, and other symbols within a namespace.

        Returns both nested compounds (classes/structs) and namespace-level
        members (free functions, variables, typedefs).

        Args:
            name:   Namespace name (e.g. "ImGui").
            source: Optional dependency name filter.
            limit:  Maximum results.
        """
        where = "(n.name = $name OR n.qualified_name = $name)"
        if source:
            where += " AND n.source CONTAINS $source"
        # Compounds nested inside the namespace
        compounds = self._run(f"""
            MATCH (n:Namespace) WHERE {where}
            WITH n.qualified_name AS ns_prefix
            OPTIONAL MATCH (c:Compound)
                WHERE c.qualified_name STARTS WITH ns_prefix + '::'
            WITH ns_prefix, c
            ORDER BY c.kind, c.name
            LIMIT $limit
            RETURN ns_prefix AS namespace,
                   'compound' AS node_type,
                   c.name AS name,
                   c.qualified_name AS qualified_name,
                   c.kind AS kind,
                   c.brief_description AS brief,
                   c.source AS source
        """, {"name": name, "source": source, "limit": limit})
        # Members whose qualified_name starts with namespace::
        members = self._run(f"""
            MATCH (n:Namespace) WHERE {where}
            WITH n.qualified_name AS ns_prefix
            MATCH (m:Member)
                WHERE m.qualified_name STARTS WITH ns_prefix + '::'
            WITH ns_prefix, m
            ORDER BY m.kind, m.name
            LIMIT $limit
            RETURN ns_prefix AS namespace,
                   'member' AS node_type,
                   m.name AS name,
                   m.qualified_name AS qualified_name,
                   m.kind AS kind,
                   m.brief_description AS brief,
                   m.source AS source
        """, {"name": name, "source": source, "limit": limit})
        return compounds + members

    def find_inheritance(
        self,
        name: str,
        direction: str = "both",
        max_depth: int = 5,
    ) -> list[dict]:
        """Explore the inheritance hierarchy of a class.

        Args:
            name:      Exact or qualified class name.
            direction: "up" (base classes), "down" (derived), or "both".
            max_depth: Maximum inheritance depth to traverse.
        """
        results = []
        if direction in ("up", "both"):
            results.extend(self._run("""
                MATCH (c:Compound)
                WHERE c.name = $name OR c.qualified_name = $name
                MATCH path = (c)-[:INHERITS_FROM*1..]->(base:Compound)
                WHERE length(path) <= $max_depth
                RETURN 'base' AS direction,
                       base.name AS name,
                       base.qualified_name AS qualified_name,
                       base.kind AS kind,
                       length(path) AS depth
                ORDER BY depth
            """, {"name": name, "max_depth": max_depth}))
        if direction in ("down", "both"):
            results.extend(self._run("""
                MATCH (c:Compound)
                WHERE c.name = $name OR c.qualified_name = $name
                MATCH path = (derived:Compound)-[:INHERITS_FROM*1..]->(c)
                WHERE length(path) <= $max_depth
                RETURN 'derived' AS direction,
                       derived.name AS name,
                       derived.qualified_name AS qualified_name,
                       derived.kind AS kind,
                       length(path) AS depth
                ORDER BY depth
            """, {"name": name, "max_depth": max_depth}))
        return results

    def find_callers_and_callees(
        self,
        name: str,
        direction: str = "both",
        limit: int = 30,
    ) -> list[dict]:
        """Explore the call graph around a function.

        Args:
            name:      Exact or qualified function name.
            direction: "callees" (what it calls), "callers" (what calls it),
                       or "both".
            limit:     Maximum results per direction.
        """
        results = []
        if direction in ("callees", "both"):
            results.extend(self._run("""
                MATCH (m:Member)-[:CALLS]->(callee:Member)
                WHERE m.name = $name OR m.qualified_name = $name
                OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(callee)
                RETURN 'callee' AS direction,
                       callee.name AS name,
                       callee.qualified_name AS qualified_name,
                       callee.definition AS definition,
                       c.name AS compound_name
                LIMIT $limit
            """, {"name": name, "limit": limit}))
        if direction in ("callers", "both"):
            results.extend(self._run("""
                MATCH (caller:Member)-[:CALLS]->(m:Member)
                WHERE m.name = $name OR m.qualified_name = $name
                OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(caller)
                RETURN 'caller' AS direction,
                       caller.name AS name,
                       caller.qualified_name AS qualified_name,
                       caller.definition AS definition,
                       c.name AS compound_name
                LIMIT $limit
            """, {"name": name, "limit": limit}))
        return results

    def get_include_chain(self, header: str) -> list[dict]:
        """Find which header files are needed for a given header.

        Useful for generating correct ``#include`` directives.

        Args:
            header: Header file name (e.g. "imgui.h").
        """
        return self._run("""
            MATCH (f:File)
            WHERE f.name = $header OR f.path ENDS WITH $header
            OPTIONAL MATCH (f)-[:INCLUDES]->(inc:File)
            RETURN f.name AS file,
                   f.path AS path,
                   f.source AS source,
                   collect(inc.name) AS includes
        """, {"header": header})

    # -----------------------------------------------------------------------
    # Schema export
    # -----------------------------------------------------------------------

    def schemas(self) -> list[dict]:
        """Export tool definitions suitable for LLM tool-calling APIs.

        Returns a list of tool schemas compatible with both the Anthropic
        and OpenAI function-calling formats.
        """
        tool_methods = [
            self.list_sources,
            self.search_symbols,
            self.get_compound,
            self.get_member,
            self.browse_namespace,
            self.find_inheritance,
            self.find_callers_and_callees,
            self.get_include_chain,
        ]
        return [_method_to_schema(m) for m in tool_methods]


# ---------------------------------------------------------------------------
# Schema generation helpers
# ---------------------------------------------------------------------------

_PY_TYPE_TO_JSON = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


def _method_to_schema(method) -> dict:
    """Convert a bound method into an LLM tool schema dict."""
    sig = inspect.signature(method)
    hints = method.__annotations__ if hasattr(method, '__annotations__') else {}
    # Fall back to the underlying function for hints
    func = method.__func__ if hasattr(method, '__func__') else method
    hints = func.__annotations__ if hasattr(func, '__annotations__') else {}

    doc = inspect.getdoc(method) or ""
    # Split into description and args section
    desc_lines = []
    for line in doc.split("\n"):
        if line.strip().startswith("Args:"):
            break
        desc_lines.append(line)
    description = textwrap.dedent("\n".join(desc_lines)).strip()

    # Parse arg descriptions from docstring
    arg_docs: dict[str, str] = {}
    in_args = False
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Args:"):
            in_args = True
            continue
        if in_args:
            if stripped and not stripped.startswith(" ") and ":" in stripped:
                # Could be next section header
                parts = stripped.split(":", 1)
                if parts[0].strip() in ("Returns", "Raises", "Yields", "Note", "Example"):
                    break
            if ":" in stripped and not stripped.startswith(" "):
                param_name, param_desc = stripped.split(":", 1)
                param_name = param_name.strip()
                arg_docs[param_name] = param_desc.strip()

    properties: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname == "self":
            continue
        prop: dict[str, Any] = {}

        # Type
        annotation = hints.get(pname)
        type_name = ""
        is_optional = False
        if annotation:
            type_str = str(annotation)
            # Handle Optional[X]
            if "Optional" in type_str:
                is_optional = True
                # Extract inner type
                inner = type_str.replace("typing.Optional[", "").rstrip("]")
                type_name = inner
            else:
                type_name = getattr(annotation, "__name__", str(annotation))
        json_type = _PY_TYPE_TO_JSON.get(type_name, "string")
        prop["type"] = json_type

        # Description from docstring
        if pname in arg_docs:
            prop["description"] = arg_docs[pname]

        # Default value
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default

        # Enum for known constrained params
        if pname == "direction" and "both" in str(param.default):
            prop["enum"] = ["up", "down", "both"] if "up" in doc else ["callers", "callees", "both"]

        properties[pname] = prop

        if param.default is inspect.Parameter.empty and not is_optional:
            required.append(pname)

    schema = {
        "name": method.__name__,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }
    return schema


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_toolset(
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
    database: str = "neo4j",
) -> DependencyGraphTools:
    """Create a connected toolset instance.

    Args:
        uri:      Neo4j Bolt URI (default: ``$NEO4J_URI`` or ``bolt://localhost:7687``).
        user:     Neo4j username (default: ``$NEO4J_USER`` or ``neo4j``).
        password: Neo4j password (default: ``$NEO4J_PASSWORD`` or ``msd-local-dev``).
        database: Neo4j database name.
    """
    from neo4j import GraphDatabase

    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD", "msd-local-dev")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()

    return DependencyGraphTools(
        _uri=uri,
        _user=user,
        _password=password,
        _database=database,
        _driver=driver,
    )
