#!/usr/bin/env python3
"""
MCP Neo4j Codebase Server

A Model Context Protocol (MCP) server that provides codebase navigation
using the Neo4j graph database populated by doxygen_to_neo4j.py.

Provides the same tool surface as mcp_codebase_server.py (SQLite) but
leverages graph traversals for relationship-heavy queries (call chains,
inheritance hierarchies, transitive dependencies).

Usage:
    python mcp_neo4j_codebase_server.py [options]

Environment variables:
    NEO4J_URI       Bolt URI       (default: bolt://localhost:7687)
    NEO4J_USER      Username       (default: neo4j)
    NEO4J_PASSWORD  Password       (default: msd-local-dev)
    NEO4J_DATABASE  Database name  (default: neo4j)

Tools provided:
    - search_symbols: Full-text search across all symbols
    - find_class: Find a class/struct by name
    - find_function: Find a function by name
    - get_class_hierarchy: Get inheritance hierarchy for a class
    - get_callers: Find all functions that call a given function
    - get_callees: Find all functions called by a given function
    - get_file_symbols: List all symbols defined in a file
    - get_includes: Get include dependencies for a file
    - get_class_members: Get all members of a class
    - get_function_parameters: Get parameters for a function
    - search_documentation: Full-text search in documentation
    - get_statistics: Get database statistics
    - list_namespaces: List all namespaces
    - list_classes: List all classes
"""

import argparse
import json
import os
import sys
from typing import Any, Optional

from neo4j import GraphDatabase

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP = True
except ImportError:
    HAS_MCP = False


class Neo4jCodebaseServer:
    """MCP server for codebase navigation queries backed by Neo4j."""

    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        self.driver.verify_connectivity()

    def close(self):
        self.driver.close()

    def _run(self, cypher: str, **params) -> list[dict[str, Any]]:
        """Run a Cypher query and return results as list of dicts."""
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher, parameters=params)
            return [dict(record) for record in result]

    def _run_single(self, cypher: str, **params) -> Optional[dict[str, Any]]:
        """Run a Cypher query and return the first result or None."""
        results = self._run(cypher, **params)
        return results[0] if results else None

    # =========================================================================
    # Symbol Search Tools
    # =========================================================================

    def search_symbols(self, query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
        """
        Full-text search across all symbols (classes, functions, variables).

        Args:
            query: Search term
            kind: Optional filter by kind (class, struct, function, variable, etc.)
            limit: Maximum number of results (default: 20)

        Returns:
            List of matching symbols with file location and description
        """
        # Try full-text index first
        try:
            if kind:
                results = self._run(
                    """
                    CALL db.index.fulltext.queryNodes('doc_search', $query)
                    YIELD node, score
                    WHERE (node:Compound AND node.kind = $kind) OR (node:Member AND node.kind = $kind)
                    RETURN
                        node.name AS name,
                        node.qualified_name AS qualified_name,
                        CASE WHEN node:Compound THEN node.kind ELSE node.kind END AS kind,
                        node.brief_description AS description,
                        node.file_path AS file_path,
                        node.line_number AS line_number,
                        node.definition AS definition,
                        score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    query=query, kind=kind, limit=limit,
                )
            else:
                results = self._run(
                    """
                    CALL db.index.fulltext.queryNodes('doc_search', $query)
                    YIELD node, score
                    RETURN
                        node.name AS name,
                        node.qualified_name AS qualified_name,
                        CASE WHEN node:Compound THEN node.kind ELSE node.kind END AS kind,
                        node.brief_description AS description,
                        node.file_path AS file_path,
                        node.line_number AS line_number,
                        node.definition AS definition,
                        score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    query=query, limit=limit,
                )
            if results:
                return results
        except Exception:
            pass  # FTS index may not exist; fall through to CONTAINS

        # Fallback: CONTAINS matching on name/qualified_name
        if kind:
            return self._run(
                """
                MATCH (n)
                WHERE (n:Compound OR n:Member)
                  AND (n.name CONTAINS $query OR n.qualified_name CONTAINS $query)
                  AND n.kind = $kind
                RETURN
                    n.name AS name, n.qualified_name AS qualified_name,
                    n.kind AS kind, n.brief_description AS description,
                    n.file_path AS file_path, n.line_number AS line_number,
                    n.definition AS definition
                LIMIT $limit
                """,
                query=query, kind=kind, limit=limit,
            )
        else:
            return self._run(
                """
                MATCH (n)
                WHERE (n:Compound OR n:Member)
                  AND (n.name CONTAINS $query OR n.qualified_name CONTAINS $query)
                RETURN
                    n.name AS name, n.qualified_name AS qualified_name,
                    n.kind AS kind, n.brief_description AS description,
                    n.file_path AS file_path, n.line_number AS line_number,
                    n.definition AS definition
                LIMIT $limit
                """,
                query=query, limit=limit,
            )

    def find_class(self, name: str, exact: bool = False) -> list[dict]:
        """
        Find a class or struct by name.

        Args:
            name: Class/struct name to search for
            exact: If True, match exactly; if False, use CONTAINS matching

        Returns:
            List of matching classes with their details
        """
        if exact:
            return self._run(
                """
                MATCH (c:Compound)
                WHERE (c.name = $name OR c.qualified_name = $name) AND c.kind IN ['class', 'struct']
                OPTIONAL MATCH (c)-[:DEFINED_IN]->(f:File)
                RETURN
                    c.name AS name, c.qualified_name AS qualified_name,
                    c.kind AS kind, c.brief_description AS brief_description,
                    c.detailed_description AS detailed_description,
                    c.base_classes AS base_classes,
                    c.is_final AS is_final, c.is_abstract AS is_abstract,
                    f.path AS file_path, c.line_number AS line_number
                """,
                name=name,
            )
        else:
            return self._run(
                """
                MATCH (c:Compound)
                WHERE (c.name CONTAINS $name OR c.qualified_name CONTAINS $name)
                  AND c.kind IN ['class', 'struct']
                OPTIONAL MATCH (c)-[:DEFINED_IN]->(f:File)
                RETURN
                    c.name AS name, c.qualified_name AS qualified_name,
                    c.kind AS kind, c.brief_description AS brief_description,
                    c.detailed_description AS detailed_description,
                    c.base_classes AS base_classes,
                    c.is_final AS is_final, c.is_abstract AS is_abstract,
                    f.path AS file_path, c.line_number AS line_number
                """,
                name=name,
            )

    def find_function(self, name: str, class_name: str | None = None, exact: bool = False) -> list[dict]:
        """
        Find a function by name.

        Args:
            name: Function name to search for
            class_name: Optional class name to scope the search
            exact: If True, match exactly; if False, use CONTAINS matching

        Returns:
            List of matching functions with their signatures and locations
        """
        if class_name:
            name_match = "m.name = $name" if exact else "(m.name CONTAINS $name)"
            class_match = "c.name = $class_name" if exact else "(c.name CONTAINS $class_name)"
            return self._run(
                f"""
                MATCH (c:Compound)-[:CONTAINS]->(m:Member)
                WHERE {name_match} AND {class_match} AND m.kind = 'function'
                OPTIONAL MATCH (m)-[:DEFINED_IN]->(f:File)
                RETURN
                    m.name AS name, m.qualified_name AS qualified_name,
                    m.type AS type, m.definition AS definition,
                    m.argsstring AS argsstring,
                    m.brief_description AS brief_description,
                    m.protection AS protection,
                    m.is_static AS is_static, m.is_const AS is_const,
                    m.is_virtual AS is_virtual, m.is_constexpr AS is_constexpr,
                    f.path AS file_path, m.line_number AS line_number,
                    c.name AS class_name
                """,
                name=name, class_name=class_name,
            )
        else:
            name_match = "m.name = $name" if exact else "(m.name CONTAINS $name OR m.qualified_name CONTAINS $name)"
            return self._run(
                f"""
                MATCH (m:Member)
                WHERE {name_match} AND m.kind = 'function'
                OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(m)
                OPTIONAL MATCH (m)-[:DEFINED_IN]->(f:File)
                RETURN
                    m.name AS name, m.qualified_name AS qualified_name,
                    m.type AS type, m.definition AS definition,
                    m.argsstring AS argsstring,
                    m.brief_description AS brief_description,
                    m.protection AS protection,
                    m.is_static AS is_static, m.is_const AS is_const,
                    m.is_virtual AS is_virtual, m.is_constexpr AS is_constexpr,
                    f.path AS file_path, m.line_number AS line_number,
                    c.name AS class_name
                """,
                name=name,
            )

    # =========================================================================
    # Hierarchy and Relationship Tools
    # =========================================================================

    def get_class_hierarchy(self, class_name: str) -> dict:
        """
        Get the inheritance hierarchy for a class.

        Uses graph traversal — much more natural than the SQLite LIKE-based approach.

        Args:
            class_name: Name of the class to get hierarchy for

        Returns:
            Dictionary with class info, base classes, and derived classes
        """
        # Get the class itself
        class_info = self._run_single(
            """
            MATCH (c:Compound)
            WHERE (c.name = $name OR c.qualified_name = $name) AND c.kind IN ['class', 'struct']
            OPTIONAL MATCH (c)-[:DEFINED_IN]->(f:File)
            RETURN
                c.name AS name, c.qualified_name AS qualified_name,
                c.kind AS kind, c.base_classes AS base_classes,
                c.is_final AS is_final, c.is_abstract AS is_abstract,
                f.path AS file_path, c.line_number AS line_number
            """,
            name=class_name,
        )

        if not class_info:
            return {"error": f"Class '{class_name}' not found"}

        # Get base classes (direct and transitive) via graph traversal
        bases = self._run(
            """
            MATCH (c:Compound {name: $name})-[:INHERITS_FROM*1..]->(base:Compound)
            OPTIONAL MATCH (base)-[:DEFINED_IN]->(f:File)
            RETURN DISTINCT
                base.name AS name, base.qualified_name AS qualified_name,
                base.kind AS kind, f.path AS file_path,
                base.line_number AS line_number
            """,
            name=class_name,
        )

        # Get derived classes (direct and transitive) via graph traversal
        derived = self._run(
            """
            MATCH (derived:Compound)-[:INHERITS_FROM*1..]->(c:Compound {name: $name})
            OPTIONAL MATCH (derived)-[:DEFINED_IN]->(f:File)
            RETURN DISTINCT
                derived.name AS name, derived.qualified_name AS qualified_name,
                derived.kind AS kind, f.path AS file_path,
                derived.line_number AS line_number
            """,
            name=class_name,
        )

        class_info["base_class_hierarchy"] = bases
        class_info["derived_classes"] = derived
        return class_info

    def get_callers(self, function_name: str, class_name: str | None = None) -> list[dict]:
        """
        Find all functions that call a given function.

        Args:
            function_name: Name of the function to find callers for
            class_name: Optional class name to scope the search

        Returns:
            List of calling functions with their locations
        """
        if class_name:
            return self._run(
                """
                MATCH (caller:Member)-[:CALLS]->(callee:Member)<-[:CONTAINS]-(c:Compound)
                WHERE callee.name = $function_name AND c.name = $class_name
                  AND callee.kind = 'function'
                OPTIONAL MATCH (caller_class:Compound)-[:CONTAINS]->(caller)
                OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File)
                RETURN DISTINCT
                    caller.name AS caller_name,
                    caller.qualified_name AS caller_qualified_name,
                    caller.definition AS definition,
                    f.path AS file_path,
                    caller.line_number AS line_number,
                    caller_class.name AS class_name
                """,
                function_name=function_name, class_name=class_name,
            )
        else:
            return self._run(
                """
                MATCH (caller:Member)-[:CALLS]->(callee:Member)
                WHERE callee.name = $function_name AND callee.kind = 'function'
                OPTIONAL MATCH (caller_class:Compound)-[:CONTAINS]->(caller)
                OPTIONAL MATCH (caller)-[:DEFINED_IN]->(f:File)
                RETURN DISTINCT
                    caller.name AS caller_name,
                    caller.qualified_name AS caller_qualified_name,
                    caller.definition AS definition,
                    f.path AS file_path,
                    caller.line_number AS line_number,
                    caller_class.name AS class_name
                """,
                function_name=function_name,
            )

    def get_callees(self, function_name: str, class_name: str | None = None) -> list[dict]:
        """
        Find all functions called by a given function.

        Args:
            function_name: Name of the function to find callees for
            class_name: Optional class name to scope the search

        Returns:
            List of called functions
        """
        if class_name:
            return self._run(
                """
                MATCH (caller:Member)-[:CALLS]->(callee:Member)
                MATCH (c:Compound)-[:CONTAINS]->(caller)
                WHERE caller.name = $function_name AND c.name = $class_name
                  AND caller.kind = 'function'
                OPTIONAL MATCH (callee)-[:DEFINED_IN]->(f:File)
                RETURN DISTINCT
                    callee.name AS callee_name,
                    callee.qualified_name AS callee_qualified_name,
                    callee.definition AS callee_definition,
                    f.path AS callee_file_path,
                    callee.line_number AS callee_line_number
                """,
                function_name=function_name, class_name=class_name,
            )
        else:
            return self._run(
                """
                MATCH (caller:Member)-[:CALLS]->(callee:Member)
                WHERE caller.name = $function_name AND caller.kind = 'function'
                OPTIONAL MATCH (callee)-[:DEFINED_IN]->(f:File)
                RETURN DISTINCT
                    callee.name AS callee_name,
                    callee.qualified_name AS callee_qualified_name,
                    callee.definition AS callee_definition,
                    f.path AS callee_file_path,
                    callee.line_number AS callee_line_number
                """,
                function_name=function_name,
            )

    # =========================================================================
    # File Navigation Tools
    # =========================================================================

    def get_file_symbols(self, file_path: str) -> dict:
        """
        List all symbols defined in a file.

        Args:
            file_path: Path to the file (can be partial match)

        Returns:
            Dictionary with classes, functions, and variables in the file
        """
        # Find the file
        file_info = self._run_single(
            """
            MATCH (f:File)
            WHERE f.path CONTAINS $file_path
            RETURN f.name AS name, f.path AS path
            LIMIT 1
            """,
            file_path=file_path,
        )

        if not file_info:
            return {"error": f"File matching '{file_path}' not found"}

        resolved_path = file_info["path"]

        # Get classes defined in this file
        classes = self._run(
            """
            MATCH (c:Compound)-[:DEFINED_IN]->(f:File {path: $path})
            WHERE c.kind IN ['class', 'struct']
            RETURN c.name AS name, c.qualified_name AS qualified_name,
                   c.kind AS kind, c.brief_description AS brief_description,
                   c.line_number AS line_number,
                   c.is_final AS is_final, c.is_abstract AS is_abstract
            ORDER BY c.line_number
            """,
            path=resolved_path,
        )

        # Get functions defined in this file
        functions = self._run(
            """
            MATCH (m:Member)-[:DEFINED_IN]->(f:File {path: $path})
            WHERE m.kind = 'function'
            OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(m)
            RETURN m.name AS name, m.qualified_name AS qualified_name,
                   m.definition AS definition, m.argsstring AS argsstring,
                   m.brief_description AS brief_description,
                   m.line_number AS line_number, m.protection AS protection,
                   m.is_static AS is_static, m.is_const AS is_const,
                   m.is_virtual AS is_virtual,
                   c.name AS class_name
            ORDER BY m.line_number
            """,
            path=resolved_path,
        )

        # Get variables defined in this file
        variables = self._run(
            """
            MATCH (m:Member)-[:DEFINED_IN]->(f:File {path: $path})
            WHERE m.kind = 'variable'
            OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(m)
            RETURN m.name AS name, m.qualified_name AS qualified_name,
                   m.type AS type, m.definition AS definition,
                   m.brief_description AS brief_description,
                   m.line_number AS line_number, m.protection AS protection,
                   m.is_static AS is_static, m.is_const AS is_const,
                   c.name AS class_name
            ORDER BY m.line_number
            """,
            path=resolved_path,
        )

        # Get typedefs defined in this file
        typedefs = self._run(
            """
            MATCH (m:Member)-[:DEFINED_IN]->(f:File {path: $path})
            WHERE m.kind = 'typedef'
            RETURN m.name AS name, m.qualified_name AS qualified_name,
                   m.type AS type, m.definition AS definition,
                   m.brief_description AS brief_description,
                   m.line_number AS line_number
            ORDER BY m.line_number
            """,
            path=resolved_path,
        )

        return {
            "file": file_info,
            "classes": classes,
            "functions": functions,
            "variables": variables,
            "typedefs": typedefs,
        }

    def get_includes(self, file_path: str) -> dict:
        """
        Get include dependencies for a file.

        Args:
            file_path: Path to the file (can be partial match)

        Returns:
            Dictionary with includes and included_by relationships
        """
        file_info = self._run_single(
            """
            MATCH (f:File)
            WHERE f.path CONTAINS $file_path
            RETURN f.name AS name, f.path AS path, f.refid AS refid
            LIMIT 1
            """,
            file_path=file_path,
        )

        if not file_info:
            return {"error": f"File matching '{file_path}' not found"}

        refid = file_info["refid"]

        # Files this file includes (outgoing INCLUDES relationships)
        includes = self._run(
            """
            MATCH (src:File {refid: $refid})-[r:INCLUDES]->(dst:File)
            RETURN r.included_file AS included_file, dst.refid AS included_refid,
                   r.is_local AS is_local
            ORDER BY r.included_file
            """,
            refid=refid,
        )

        # Files that include this file (incoming INCLUDES relationships)
        included_by = self._run(
            """
            MATCH (src:File)-[:INCLUDES]->(dst:File {refid: $refid})
            RETURN src.name AS name, src.path AS path
            ORDER BY src.name
            """,
            refid=refid,
        )

        return {
            "file": file_info,
            "includes": includes,
            "included_by": included_by,
        }

    # =========================================================================
    # Class Member Tools
    # =========================================================================

    def get_class_members(
        self,
        class_name: str,
        include_private: bool = True,
        kind: str | None = None,
    ) -> dict:
        """
        Get all members of a class.

        Args:
            class_name: Name of the class
            include_private: Include private members (default: True)
            kind: Filter by kind (function, variable, typedef, etc.)

        Returns:
            Dictionary with class info and categorized members
        """
        # Find the class
        class_info = self._run_single(
            """
            MATCH (c:Compound)
            WHERE (c.name = $name OR c.qualified_name = $name) AND c.kind IN ['class', 'struct']
            RETURN c.name AS name, c.qualified_name AS qualified_name,
                   c.kind AS kind, c.brief_description AS brief_description,
                   c.detailed_description AS detailed_description,
                   c.base_classes AS base_classes,
                   c.is_final AS is_final, c.is_abstract AS is_abstract
            """,
            name=class_name,
        )

        if not class_info:
            return {"error": f"Class '{class_name}' not found"}

        # Build filters
        protection_filter = ""
        if not include_private:
            protection_filter = "AND m.protection IN ['public', 'protected']"

        kind_filter = ""
        if kind:
            kind_filter = f"AND m.kind = '{kind}'"

        # Get all members
        members = self._run(
            f"""
            MATCH (c:Compound)-[:CONTAINS]->(m:Member)
            WHERE (c.name = $name OR c.qualified_name = $name) AND c.kind IN ['class', 'struct'] {protection_filter} {kind_filter}
            RETURN m.name AS name, m.qualified_name AS qualified_name,
                   m.kind AS kind, m.type AS type, m.definition AS definition,
                   m.argsstring AS argsstring, m.brief_description AS brief_description,
                   m.protection AS protection,
                   m.is_static AS is_static, m.is_const AS is_const,
                   m.is_constexpr AS is_constexpr, m.is_virtual AS is_virtual,
                   m.is_inline AS is_inline, m.is_explicit AS is_explicit,
                   m.line_number AS line_number
            ORDER BY m.line_number
            """,
            name=class_name,
        )

        # Categorize members
        result = {
            "class": class_info,
            "constructors": [],
            "destructor": None,
            "methods": [],
            "static_methods": [],
            "variables": [],
            "static_variables": [],
            "typedefs": [],
            "enums": [],
        }

        for member in members:
            if member["kind"] == "function":
                if member["name"] == class_name:
                    result["constructors"].append(member)
                elif member["name"] == f"~{class_name}":
                    result["destructor"] = member
                elif member["is_static"]:
                    result["static_methods"].append(member)
                else:
                    result["methods"].append(member)
            elif member["kind"] == "variable":
                if member["is_static"]:
                    result["static_variables"].append(member)
                else:
                    result["variables"].append(member)
            elif member["kind"] == "typedef":
                result["typedefs"].append(member)
            elif member["kind"] == "enum":
                result["enums"].append(member)

        return result

    def get_function_parameters(self, function_name: str, class_name: str | None = None) -> list[dict]:
        """
        Get parameters for a function.

        Args:
            function_name: Name of the function
            class_name: Optional class name to scope the search

        Returns:
            List of functions with their parameters
        """
        if class_name:
            functions = self._run(
                """
                MATCH (c:Compound)-[:CONTAINS]->(m:Member)
                WHERE m.name = $function_name AND c.name = $class_name AND m.kind = 'function'
                RETURN m.refid AS refid, m.name AS name, m.qualified_name AS qualified_name,
                       m.definition AS definition, m.argsstring AS argsstring,
                       c.name AS class_name
                """,
                function_name=function_name, class_name=class_name,
            )
        else:
            functions = self._run(
                """
                MATCH (m:Member)
                WHERE m.name = $function_name AND m.kind = 'function'
                OPTIONAL MATCH (c:Compound)-[:CONTAINS]->(m)
                RETURN m.refid AS refid, m.name AS name, m.qualified_name AS qualified_name,
                       m.definition AS definition, m.argsstring AS argsstring,
                       c.name AS class_name
                """,
                function_name=function_name,
            )

        for func in functions:
            params = self._run(
                """
                MATCH (m:Member {refid: $refid})-[:HAS_PARAMETER]->(p:Parameter)
                RETURN p.position AS position, p.name AS name,
                       p.type AS type, p.default_value AS default_value
                ORDER BY p.position
                """,
                refid=func["refid"],
            )
            func["parameters"] = params
            del func["refid"]  # Don't expose internal ID

        return functions

    # =========================================================================
    # Documentation Search Tools
    # =========================================================================

    def search_documentation(self, query: str, limit: int = 20) -> list[dict]:
        """
        Full-text search in documentation.

        Args:
            query: Search term (uses Neo4j full-text index)
            limit: Maximum number of results

        Returns:
            List of matching symbols with their documentation
        """
        try:
            return self._run(
                """
                CALL db.index.fulltext.queryNodes('doc_search', $query)
                YIELD node, score
                RETURN
                    node.name AS name,
                    node.qualified_name AS qualified_name,
                    node.brief_description AS description,
                    score
                ORDER BY score DESC
                LIMIT $limit
                """,
                query=query, limit=limit,
            )
        except Exception:
            # Fallback to CONTAINS if FTS not available
            return self._run(
                """
                MATCH (n)
                WHERE (n:Compound OR n:Member)
                  AND (n.name CONTAINS $query
                    OR n.qualified_name CONTAINS $query
                    OR n.brief_description CONTAINS $query)
                RETURN n.name AS name, n.qualified_name AS qualified_name,
                       n.brief_description AS description
                LIMIT $limit
                """,
                query=query, limit=limit,
            )

    # =========================================================================
    # Statistics and Overview Tools
    # =========================================================================

    def get_statistics(self) -> dict:
        """
        Get database statistics.

        Returns:
            Dictionary with counts of various node and relationship types
        """
        stats = {}

        # Node counts by label
        node_counts = self._run(
            """
            MATCH (n)
            WITH labels(n)[0] AS label
            RETURN label, count(*) AS count
            ORDER BY count DESC
            """
        )
        stats["nodes"] = {r["label"]: r["count"] for r in node_counts}

        # Relationship counts by type
        rel_counts = self._run(
            """
            MATCH ()-[r]->()
            WITH type(r) AS rel_type
            RETURN rel_type, count(*) AS count
            ORDER BY count DESC
            """
        )
        stats["relationships"] = {r["rel_type"]: r["count"] for r in rel_counts}

        # Breakdown of compounds by kind
        compound_kinds = self._run(
            """
            MATCH (c:Compound)
            RETURN c.kind AS kind, count(*) AS count
            ORDER BY count DESC
            """
        )
        stats["compounds_by_kind"] = {r["kind"]: r["count"] for r in compound_kinds}

        # Breakdown of members by kind
        member_kinds = self._run(
            """
            MATCH (m:Member)
            RETURN m.kind AS kind, count(*) AS count
            ORDER BY count DESC
            """
        )
        stats["members_by_kind"] = {r["kind"]: r["count"] for r in member_kinds}

        return stats

    def list_namespaces(self) -> list[dict]:
        """
        List all namespaces in the codebase.

        Returns:
            List of namespaces with their qualified names
        """
        return self._run(
            """
            MATCH (n:Namespace)
            RETURN n.name AS name, n.qualified_name AS qualified_name
            ORDER BY n.qualified_name
            """
        )

    def list_classes(self, namespace: str | None = None) -> list[dict]:
        """
        List all classes in the codebase.

        Args:
            namespace: Optional namespace to filter by

        Returns:
            List of classes with their basic info
        """
        if namespace:
            return self._run(
                """
                MATCH (c:Compound)
                WHERE c.kind IN ['class', 'struct']
                  AND c.qualified_name STARTS WITH $ns_prefix
                OPTIONAL MATCH (c)-[:DEFINED_IN]->(f:File)
                RETURN c.name AS name, c.qualified_name AS qualified_name,
                       c.kind AS kind, c.brief_description AS brief_description,
                       f.path AS file_path, c.line_number AS line_number
                ORDER BY c.qualified_name
                """,
                ns_prefix=f"{namespace}::",
            )
        else:
            return self._run(
                """
                MATCH (c:Compound)
                WHERE c.kind IN ['class', 'struct']
                OPTIONAL MATCH (c)-[:DEFINED_IN]->(f:File)
                RETURN c.name AS name, c.qualified_name AS qualified_name,
                       c.kind AS kind, c.brief_description AS brief_description,
                       f.path AS file_path, c.line_number AS line_number
                ORDER BY c.qualified_name
                """
            )


def create_mcp_server(uri: str, user: str, password: str, database: str) -> "FastMCP":
    """Create a FastMCP server wrapping the Neo4jCodebaseServer."""
    server = Neo4jCodebaseServer(uri, user, password, database)
    mcp = FastMCP("codebase-neo4j")

    @mcp.tool()
    def search_symbols(query: str, kind: str | None = None, limit: int = 20) -> str:
        """Full-text search across all symbols (classes, functions, variables)."""
        return json.dumps(server.search_symbols(query, kind, limit), indent=2, default=str)

    @mcp.tool()
    def find_class(name: str, exact: bool = False) -> str:
        """Find a class or struct by name."""
        return json.dumps(server.find_class(name, exact), indent=2, default=str)

    @mcp.tool()
    def find_function(name: str, class_name: str | None = None, exact: bool = False) -> str:
        """Find a function by name, optionally scoped to a class."""
        return json.dumps(server.find_function(name, class_name, exact), indent=2, default=str)

    @mcp.tool()
    def get_class_hierarchy(class_name: str) -> str:
        """Get the inheritance hierarchy (base and derived classes) for a class. Uses graph traversal for transitive resolution."""
        return json.dumps(server.get_class_hierarchy(class_name), indent=2, default=str)

    @mcp.tool()
    def get_callers(function_name: str, class_name: str | None = None) -> str:
        """Find all functions that call a given function."""
        return json.dumps(server.get_callers(function_name, class_name), indent=2, default=str)

    @mcp.tool()
    def get_callees(function_name: str, class_name: str | None = None) -> str:
        """Find all functions called by a given function."""
        return json.dumps(server.get_callees(function_name, class_name), indent=2, default=str)

    @mcp.tool()
    def get_file_symbols(file_path: str) -> str:
        """List all symbols (classes, functions, variables) defined in a file."""
        return json.dumps(server.get_file_symbols(file_path), indent=2, default=str)

    @mcp.tool()
    def get_includes(file_path: str) -> str:
        """Get include dependencies for a file (what it includes and what includes it)."""
        return json.dumps(server.get_includes(file_path), indent=2, default=str)

    @mcp.tool()
    def get_class_members(class_name: str, include_private: bool = True, kind: str | None = None) -> str:
        """Get all members of a class, categorized by type."""
        return json.dumps(server.get_class_members(class_name, include_private, kind), indent=2, default=str)

    @mcp.tool()
    def get_function_parameters(function_name: str, class_name: str | None = None) -> str:
        """Get parameters for a function."""
        return json.dumps(server.get_function_parameters(function_name, class_name), indent=2, default=str)

    @mcp.tool()
    def search_documentation(query: str, limit: int = 20) -> str:
        """Full-text search in documentation (uses Neo4j full-text index)."""
        return json.dumps(server.search_documentation(query, limit), indent=2, default=str)

    @mcp.tool()
    def get_statistics() -> str:
        """Get database statistics (counts of nodes, relationships, etc.)."""
        return json.dumps(server.get_statistics(), indent=2, default=str)

    @mcp.tool()
    def list_namespaces() -> str:
        """List all namespaces in the codebase."""
        return json.dumps(server.list_namespaces(), indent=2, default=str)

    @mcp.tool()
    def list_classes(namespace: str | None = None) -> str:
        """List all classes in the codebase, optionally filtered by namespace."""
        return json.dumps(server.list_classes(namespace), indent=2, default=str)

    return mcp


def format_output(data: Any) -> str:
    """Format output as pretty-printed JSON."""
    return json.dumps(data, indent=2, default=str)


def main():
    parser = argparse.ArgumentParser(
        description="MCP Neo4j Codebase Server - Navigate your codebase via graph database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
    NEO4J_URI       Bolt URI       (default: bolt://localhost:7687)
    NEO4J_USER      Username       (default: neo4j)
    NEO4J_PASSWORD  Password       (default: msd-local-dev)
    NEO4J_DATABASE  Database name  (default: neo4j)

Examples:
    # Start as MCP server (default)
    python mcp_neo4j_codebase_server.py

    # CLI mode: search symbols
    python mcp_neo4j_codebase_server.py search_symbols ConvexHull

    # CLI mode: get class hierarchy
    python mcp_neo4j_codebase_server.py get_class_hierarchy Constraint
        """
    )
    parser.add_argument("command", nargs="?", help="Command to execute (omit for MCP server mode)")
    parser.add_argument("args", nargs="*", help="Command arguments")
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", "msd-local-dev"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--cli", action="store_true", help="Force CLI mode (show help)")

    args = parser.parse_args()

    if not args.command and not args.cli:
        # MCP server mode
        if not HAS_MCP:
            print("Error: mcp package not installed. Run: pip install mcp", file=sys.stderr)
            sys.exit(1)
        mcp_server = create_mcp_server(args.uri, args.user, args.password, args.database)
        mcp_server.run(transport="stdio")
        return

    server = Neo4jCodebaseServer(args.uri, args.user, args.password, args.database)

    try:
        if args.cli or not args.command:
            print("Available commands:")
            commands = [
                ("search_symbols <query> [kind]", "Search for symbols by name"),
                ("find_class <name> [--exact]", "Find a class by name"),
                ("find_function <name> [class_name]", "Find a function by name"),
                ("get_class_hierarchy <class_name>", "Get inheritance hierarchy"),
                ("get_callers <function_name> [class_name]", "Find callers of a function"),
                ("get_callees <function_name> [class_name]", "Find functions called by a function"),
                ("get_file_symbols <file_path>", "List symbols in a file"),
                ("get_includes <file_path>", "Get include dependencies"),
                ("get_class_members <class_name>", "Get all members of a class"),
                ("get_function_parameters <function_name> [class_name]", "Get function parameters"),
                ("search_documentation <query>", "Full-text search in docs"),
                ("get_statistics", "Get database statistics"),
                ("list_namespaces", "List all namespaces"),
                ("list_classes [namespace]", "List all classes"),
            ]
            for cmd, desc in commands:
                print(f"  {cmd:50} {desc}")
            sys.exit(0)

        command = args.command
        cmd_args = args.args

        if command == "search_symbols":
            query = cmd_args[0] if cmd_args else ""
            kind = cmd_args[1] if len(cmd_args) > 1 else None
            result = server.search_symbols(query, kind)
        elif command == "find_class":
            name = cmd_args[0] if cmd_args else ""
            exact = "--exact" in cmd_args
            result = server.find_class(name, exact)
        elif command == "find_function":
            name = cmd_args[0] if cmd_args else ""
            class_name = cmd_args[1] if len(cmd_args) > 1 and not cmd_args[1].startswith("-") else None
            exact = "--exact" in cmd_args
            result = server.find_function(name, class_name, exact)
        elif command == "get_class_hierarchy":
            result = server.get_class_hierarchy(cmd_args[0] if cmd_args else "")
        elif command == "get_callers":
            function_name = cmd_args[0] if cmd_args else ""
            class_name = cmd_args[1] if len(cmd_args) > 1 else None
            result = server.get_callers(function_name, class_name)
        elif command == "get_callees":
            function_name = cmd_args[0] if cmd_args else ""
            class_name = cmd_args[1] if len(cmd_args) > 1 else None
            result = server.get_callees(function_name, class_name)
        elif command == "get_file_symbols":
            result = server.get_file_symbols(cmd_args[0] if cmd_args else "")
        elif command == "get_includes":
            result = server.get_includes(cmd_args[0] if cmd_args else "")
        elif command == "get_class_members":
            class_name = cmd_args[0] if cmd_args else ""
            include_private = "--public-only" not in cmd_args
            kind = None
            for i, arg in enumerate(cmd_args):
                if arg == "--kind" and i + 1 < len(cmd_args):
                    kind = cmd_args[i + 1]
            result = server.get_class_members(class_name, include_private, kind)
        elif command == "get_function_parameters":
            function_name = cmd_args[0] if cmd_args else ""
            class_name = cmd_args[1] if len(cmd_args) > 1 else None
            result = server.get_function_parameters(function_name, class_name)
        elif command == "search_documentation":
            result = server.search_documentation(cmd_args[0] if cmd_args else "")
        elif command == "get_statistics":
            result = server.get_statistics()
        elif command == "list_namespaces":
            result = server.list_namespaces()
        elif command == "list_classes":
            namespace = cmd_args[0] if cmd_args else None
            result = server.list_classes(namespace)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            sys.exit(1)

        print(format_output(result))

    finally:
        server.close()


if __name__ == "__main__":
    main()
