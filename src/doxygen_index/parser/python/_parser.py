"""
Python language parser — extracts symbols from Python source via ``ast``.

Implements :class:`~doxygen_index.parser.base.LanguageParser` using the
standard library ``ast`` module to parse ``.py`` files directly, without
requiring Sphinx or any external tool.  Produces the same
:class:`~doxygen_index.parser.model.ParseResult` as the C++ parser so
that both languages can share the same backend (Neo4j, JSON, etc.).

Mapping from Python constructs to codegraph node types:

=============  ===================  ==========================================
Python         Node type            Notes
=============  ===================  ==========================================
package        NamespaceNode        Directories with ``__init__.py``
module         FileNode + Namespace  ``.py`` files
class          ClassNode            Regular classes
ABC / Protocol InterfaceNode        ``abc.ABC``, ``typing.Protocol`` subclasses
Enum subclass  EnumNode             ``enum.Enum`` / ``enum.IntEnum`` / etc.
method         MethodNode           ``def`` inside a class
property       MethodNode           ``@property`` decorated methods
classmethod    MethodNode           ``@classmethod`` decorated methods
staticmethod   MethodNode           ``@staticmethod`` decorated methods
free function  FunctionNode         Top-level ``def``
class attr     AttributeNode        Annotated assignments & class-level vars
import          IncludeEntry        ``import`` / ``from ... import``
=============  ===================  ==========================================
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Optional

from codegraph import FileNode, NamespaceNode

from doxygen_index.parser.base import LanguageParser
from doxygen_index.parser.model import ParseResult, IncludeEntry
from doxygen_index.parser.python._ast_utils import DEFAULT_EXCLUDE_DIRS
from doxygen_index.parser.python._paths import is_excluded, module_path
from doxygen_index.parser.python._context import ParseContext
from doxygen_index.parser.python._visitor import _PythonVisitor
from doxygen_index.parser.python.postprocess import (
    derive_namespace_compositions,
    derive_inheritance,
    derive_type_dependencies,
    derive_invokes,
    derive_namespace_imports,
    derive_test_compositions,
    extract_implementations,
)


class PythonParser(LanguageParser):
    """Language parser for Python source files.

    Uses the ``ast`` module to parse ``.py`` files without requiring
    Sphinx or any external tool.  Walks a source directory, extracts
    classes, functions, methods, attributes, and imports, and populates
    a :class:`~doxygen_index.parser.model.ParseResult`.
    """

    # ------------------------------------------------------------------
    # LanguageParser interface
    # ------------------------------------------------------------------

    def parse_source_dir(
        self,
        source_dir: Path,
        source: str,
        result: ParseResult,
        layer: str = "codebase",
        progress_interval: int = 0,
        exclude_dirs: Optional[set[str]] = None,
    ) -> None:
        """Parse all Python source files in *source_dir* and populate *result*.

        Walks the directory recursively for ``.py`` files.  Each file
        is parsed with ``ast`` and its symbols extracted.

        Files whose path contains any directory in *exclude_dirs* are skipped.
        If *exclude_dirs* is ``None``, :data:`DEFAULT_EXCLUDE_DIRS` is used.

        Args:
            source_dir: Root directory of the Python source tree.
            source: Provenance label.
            result: Accumulator for parsed entries.
            layer: Layer label.
            progress_interval: Print progress every N files. 0 disables.
            exclude_dirs: Set of directory names to skip.  Defaults to
                :data:`DEFAULT_EXCLUDE_DIRS`.
        """
        source_dir = Path(source_dir)
        if exclude_dirs is None:
            exclude_dirs = set(DEFAULT_EXCLUDE_DIRS)

        py_files = sorted(
            f for f in source_dir.rglob("*.py")
            if not is_excluded(f, source_dir, exclude_dirs)
        )
        total = len(py_files)

        # First pass: register packages (directories with __init__.py)
        # so that namespace nodes exist before we parse their contents.
        for i, py_file in enumerate(py_files):
            if py_file.name == "__init__.py":
                mod_name = module_path(py_file, source_dir)
                # The package name excludes the __init__ module leaf
                package_name = mod_name if mod_name else py_file.parent.name
                self._register_package(py_file, package_name, source, result, layer)

            if progress_interval and (i + 1) % progress_interval == 0:
                print(f"  Parsed {i + 1}/{total} Python files...")

        # Second pass: parse each file for symbols
        for i, py_file in enumerate(py_files):
            mod_name = module_path(py_file, source_dir)
            self._parse_python_file(py_file, mod_name, source, result, layer)

            if progress_interval and (i + 1) % progress_interval == 0:
                print(f"  Parsed {i + 1}/{total} Python files...")

    def post_process(self, result: ParseResult) -> None:
        """Python-specific post-processing.

        Derives namespace ``COMPOSES`` relationships (recorded on
        ``result.compositions``) and extracts implementation source code
        for all methods and functions that have body_start/body_end line
        numbers.
        """
        derive_namespace_compositions(result)
        derive_inheritance(result)
        derive_type_dependencies(result)
        derive_invokes(result)
        derive_namespace_imports(result)
        derive_test_compositions(result)
        extract_implementations(result)

    # ------------------------------------------------------------------
    # Package registration
    # ------------------------------------------------------------------

    @staticmethod
    def _register_package(
        init_path: Path,
        package_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Register a package (directory with __init__.py) as a NamespaceNode.

        Only creates a node if one doesn't already exist for this
        qualified name (sub-modules may have already created it).
        """
        existing = {ns.qualified_name for ns in result.namespaces}
        if package_name in existing:
            return

        short_name = package_name.rsplit(".", 1)[-1] if "." in package_name else package_name
        result.namespaces.append(NamespaceNode(
            refid=package_name,
            name=short_name,
            qualified_name=package_name,
            source=source,
            layer=layer,
        ))

    # ------------------------------------------------------------------
    # File parsing
    # ------------------------------------------------------------------

    def _parse_python_file(
        self,
        file_path: Path,
        module_name: str,
        source: str,
        result: ParseResult,
        layer: str,
    ) -> None:
        """Parse a single Python file and populate *result*."""
        try:
            source_text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
            return

        try:
            tree = ast.parse(source_text, filename=str(file_path))
        except SyntaxError as e:
            print(f"Warning: Could not parse {file_path}: {e}", file=sys.stderr)
            return

        # __init__.py is a package marker, not a meaningful source file: its
        # re-exports just duplicate the package namespace's COMPOSES edges, so
        # it adds noise to the graph without information value.  Skip creating
        # a FileNode (and its re-export INCLUDES) for it, but still ensure the
        # namespace exists and visit the AST so any real definitions inside the
        # __init__.py are captured and composed by the namespace.
        is_init = file_path.name == "__init__.py"

        if not is_init:
            # Create a FileNode for this module
            result.files.append(FileNode(
                refid=module_name,
                name=file_path.name,
                path=str(file_path),
                language="Python",
                source=source,
            ))

        # Ensure a NamespaceNode exists for this module
        if module_name and module_name not in {ns.qualified_name for ns in result.namespaces}:
            short_name = module_name.rsplit(".", 1)[-1] if "." in module_name else module_name
            result.namespaces.append(NamespaceNode(
                refid=module_name,
                name=short_name,
                qualified_name=module_name,
                source=source,
                layer=layer,
            ))

        # Add imports as IncludeEntry items (skip for __init__.py — its
        # re-exports duplicate the namespace composition and would be orphaned
        # without a FileNode to attach to).
        if not is_init:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        result.includes.append(IncludeEntry(
                            file_refid=module_name,
                            included_file=alias.name,
                            included_refid=alias.name,
                            is_local=False,
                        ))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        qname = f"{module}.{alias.name}" if module else alias.name
                        result.includes.append(IncludeEntry(
                            file_refid=module_name,
                            included_file=alias.name,
                            included_refid=qname,
                            is_local=node.level > 0,
                        ))

        # Read any tagged ``# codegraph:test-desc <qn>`` comment blocks
        # from this file so the test handlers can restore enriched
        # descriptions that were written back into the source.
        from doxygen_index.parser.python.test_comments import read_test_comments

        # Visit top-level definitions
        ctx = ParseContext(
            module_name=module_name,
            file_path=str(file_path),
            source=source,
            layer=layer,
            result=result,
            test_comments=read_test_comments(file_path),
        )
        visitor = _PythonVisitor(ctx)
        visitor.visit(tree)