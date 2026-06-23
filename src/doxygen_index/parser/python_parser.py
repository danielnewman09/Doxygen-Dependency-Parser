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

from codegraph import (
    ClassNode, InterfaceNode, EnumNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode,
    FileNode, NamespaceNode, ParameterNode,
    ImplementationNode,
)

from doxygen_index.parser.base import LanguageParser
from doxygen_index.parser.model import ParseResult, IncludeEntry, CompositionEntry, InheritsEntry, DependsOnEntry, ImplementationRef


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

#: Directory names that are always excluded when parsing Python source.
#: These cover virtual environments, caches, build artifacts, and tool
#: directories that should never be indexed.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "build",
    "dist",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".eggs",
    ".idea",
    ".vscode",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_excluded(file_path: Path, base_dir: Path, exclude_dirs: set[str]) -> bool:
    """Check whether *file_path* should be skipped.

    Returns ``True`` if any component of the path (relative to *base_dir*)
    matches a name in *exclude_dirs*.
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        rel = file_path
    return any(part in exclude_dirs for part in rel.parts[:-1])


def _module_path(file_path: Path, base_dir: Path) -> str:
    """Convert a file path to a dotted Python module name.

    Example: ``base_dir/mypackage/sub/mod.py`` → ``mypackage.sub.mod``
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        rel = file_path
    parts = list(rel.with_suffix("").parts)
    # If the file is __init__.py, the module name is the package name
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _qualified_name(module: str, name: str) -> str:
    """Build a dotted qualified name from module and local name."""
    if module:
        return f"{module}.{name}"
    return name


def _annotation_to_str(node: Optional[ast.expr]) -> str:
    """Convert an AST annotation node to a string representation."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant):
        return repr(node.value) if isinstance(node.value, str) else str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_annotation_to_str(node.value)}[{_annotation_to_str(node.slice)}]"
    if isinstance(node, ast.List):
        inner = ", ".join(_annotation_to_str(e) for e in node.elts)
        return f"[{inner}]"
    if isinstance(node, ast.Tuple):
        inner = ", ".join(_annotation_to_str(e) for e in node.elts)
        return f"({inner})"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_to_str(node.left)} | {_annotation_to_str(node.right)}"
    return ast.dump(node)


def _is_interface(bases: list[ast.expr]) -> bool:
    """Return True if the class inherits from a known interface base."""
    interface_bases = {"ABC", "Protocol"}
    for base in bases:
        name = _annotation_to_str(base)
        if name in interface_bases:
            return True
    return False


def _is_enum(bases: list[ast.expr]) -> bool:
    """Return True if the class inherits from a known enum base."""
    enum_bases = {"Enum", "IntEnum", "Flag", "IntFlag", "StrEnum", "ReprEnum"}
    for base in bases:
        name = _annotation_to_str(base)
        # Match both "Enum" and "enum.Enum"
        short = name.rsplit(".", 1)[-1] if "." in name else name
        if short in enum_bases:
            return True
    return False


def _decorator_names(decorator_list: list[ast.expr]) -> set[str]:
    """Extract the simple names from a list of decorators."""
    names = set()
    for dec in decorator_list:
        if isinstance(dec, ast.Name):
            names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.add(dec.attr)
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                names.add(dec.func.id)
            elif isinstance(dec.func, ast.Attribute):
                names.add(dec.func.attr)
    return names


def _get_docstring(node: ast.AST) -> str:
    """Extract the docstring from an AST node, cleaning indentation."""
    ds = ast.get_docstring(node, clean=True)
    return ds if ds else ""


def _is_private(name: str) -> bool:
    """Return True if a Python name is private (starts with _ but not __)."""
    return name.startswith("_") and not name.startswith("__")


# ---------------------------------------------------------------------------
# PythonParser
# ---------------------------------------------------------------------------


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
            if not _is_excluded(f, source_dir, exclude_dirs)
        )
        total = len(py_files)

        # First pass: register packages (directories with __init__.py)
        # so that namespace nodes exist before we parse their contents.
        for i, py_file in enumerate(py_files):
            if py_file.name == "__init__.py":
                module_name = _module_path(py_file, source_dir)
                # The package name excludes the __init__ module leaf
                package_name = module_name if module_name else py_file.parent.name
                self._register_package(py_file, package_name, source, result, layer)

            if progress_interval and (i + 1) % progress_interval == 0:
                print(f"  Parsed {i + 1}/{total} Python files...")

        # Second pass: parse each file for symbols
        for i, py_file in enumerate(py_files):
            module_name = _module_path(py_file, source_dir)
            self._parse_python_file(py_file, module_name, source, result, layer)

            if progress_interval and (i + 1) % progress_interval == 0:
                print(f"  Parsed {i + 1}/{total} Python files...")

    def post_process(self, result: ParseResult) -> None:
        """Python-specific post-processing.

        Derives namespace ``COMPOSES`` relationships (recorded on
        ``result.compositions``) and extracts implementation source code
        for all methods and functions that have body_start/body_end line
        numbers.
        """
        _derive_namespace_compositions(result)
        _derive_inheritance(result)
        _derive_type_dependencies(result)
        _derive_namespace_imports(result)
        _extract_implementations(result)

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

        # Visit top-level definitions
        visitor = _PythonVisitor(
            module_name=module_name,
            file_path=str(file_path),
            source=source,
            layer=layer,
            result=result,
        )
        visitor.visit(tree)

        # Register enum values if we found any Enum classes
        self._register_enum_values(result)


    @staticmethod
    def _register_enum_values(result: ParseResult) -> None:
        """Create EnumValueNode entries for EnumNode members that don't
        have enum_values yet. Called after each file is parsed."""
        # This is handled inline in _visit_ClassDef for enums


# ---------------------------------------------------------------------------
# AST Visitor
# ---------------------------------------------------------------------------


class _PythonVisitor(ast.NodeVisitor):
    """Walk an AST tree and extract symbol definitions into a ParseResult."""

    def __init__(
        self,
        module_name: str,
        file_path: str,
        source: str,
        layer: str,
        result: ParseResult,
    ):
        self.module_name = module_name
        self.file_path = file_path
        self.source = source
        self.layer = layer
        self.result = result
        # Stack of (refid, qualified_name) for the containing class
        self._class_stack: list[tuple[str, str]] = []

    def _current_class(self) -> tuple[str, str] | None:
        """Return (refid, qualified_name) of the innermost class, or None."""
        return self._class_stack[-1] if self._class_stack else None

    # ------------------------------------------------------------------
    # Class definitions
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_qname = _qualified_name(self.module_name, node.name)
        refid = class_qname
        docstring = _get_docstring(node)
        brief = docstring.split("\n")[0] if docstring else ""
        detailed = docstring
        decs = _decorator_names(node.decorator_list)
        base_classes = [_annotation_to_str(b) for b in node.bases]

        # Determine the node type
        if _is_interface(node.bases):
            self._add_interface(node, refid, class_qname, brief, detailed, base_classes, decs)
        elif _is_enum(node.bases):
            self._add_enum(node, refid, class_qname, brief, detailed, base_classes, decs)
        else:
            self._add_class(node, refid, class_qname, brief, detailed, base_classes, decs)

        # Push class context and visit children
        self._class_stack.append((refid, class_qname))
        self.generic_visit(node)
        self._class_stack.pop()

    def _add_class(
        self,
        node: ast.ClassDef,
        refid: str,
        qname: str,
        brief: str,
        detailed: str,
        base_classes: list[str],
        decs: set[str],
    ) -> None:
        is_abstract = "abstractmethod" in decs or "ABC" in {b for b in base_classes}
        module = qname.rsplit(".", 1)[0] if "." in qname else ""
        self.result.classes.append(ClassNode(
            refid=refid,
            kind="class",
            name=node.name,
            qualified_name=qname,
            file_path=self.file_path,
            line_number=node.lineno,
            body_start=node.lineno,
            body_end=node.end_lineno or node.lineno,
            brief_description=brief,
            detailed_description=detailed,
            definition=f"class {node.name}",
            module=module,
            base_classes=base_classes,
            is_final=False,
            is_abstract=is_abstract,
            source=self.source,
            source_type="source",
            layer=self.layer,
        ))

    def _add_interface(
        self,
        node: ast.ClassDef,
        refid: str,
        qname: str,
        brief: str,
        detailed: str,
        base_classes: list[str],
        decs: set[str],
    ) -> None:
        module = qname.rsplit(".", 1)[0] if "." in qname else ""
        self.result.interfaces.append(InterfaceNode(
            refid=refid,
            kind="interface",
            name=node.name,
            qualified_name=qname,
            file_path=self.file_path,
            line_number=node.lineno,
            brief_description=brief,
            detailed_description=detailed,
            definition=f"class {node.name}",
            module=module,
            is_abstract=True,
            source=self.source,
            source_type="source",
            layer=self.layer,
        ))

    def _add_enum(
        self,
        node: ast.ClassDef,
        refid: str,
        qname: str,
        brief: str,
        detailed: str,
        base_classes: list[str],
        decs: set[str],
    ) -> None:
        module = qname.rsplit(".", 1)[0] if "." in qname else ""
        self.result.enums.append(EnumNode(
            refid=refid,
            kind="enum",
            name=node.name,
            qualified_name=qname,
            file_path=self.file_path,
            line_number=node.lineno,
            brief_description=brief,
            detailed_description=detailed,
            definition=f"class {node.name}",
            module=module,
            source=self.source,
            source_type="source",
            layer=self.layer,
        ))
        # Extract enum values from class body
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        value_qname = f"{qname}.{target.id}"
                        self.result.enum_values.append(EnumValueNode(
                            refid=value_qname,
                            compound_refid=refid,
                            kind="enumvalue",
                            name=target.id,
                            qualified_name=value_qname,
                            file_path=self.file_path,
                            line_number=stmt.lineno,
                            body_start=stmt.lineno,
                            body_end=stmt.end_lineno or stmt.lineno,
                            brief_description="",
                            detailed_description="",
                            source=self.source,
                            layer=self.layer,
                        ))

    # ------------------------------------------------------------------
    # Function / method definitions
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_def(node)

    def _visit_function_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self._current_class()
        decs = _decorator_names(node.decorator_list)
        docstring = _get_docstring(node)
        brief = docstring.split("\n")[0] if docstring else ""

        # Return type annotation
        return_type = _annotation_to_str(node.returns) if node.returns else ""

        # Parameters
        args_info = self._extract_args(node)

        # Build argsstring
        argsstring = f"({args_info['signature']})"

        if parent:
            # Method inside a class
            compound_refid, parent_qname = parent
            qname = f"{parent_qname}.{node.name}"

            is_static = "staticmethod" in decs
            is_classmethod = "classmethod" in decs
            is_abstract = "abstractmethod" in decs
            is_property = "property" in decs
            is_private = _is_private(node.name)
            protection = "private" if is_private else "public"

            # Determine method kind
            if is_property:
                kind = "property"
            elif is_classmethod:
                kind = "classmethod"
            elif is_static:
                kind = "staticmethod"
            elif is_abstract:
                kind = "method"  # abstract method is still a method
            else:
                kind = "method"

            definition = f"def {node.name}{argsstring}"
            if return_type:
                definition = f"def {node.name}{argsstring} -> {return_type}"

            self.result.methods.append(MethodNode(
                refid=qname,
                compound_refid=compound_refid,
                kind=kind,
                name=node.name,
                qualified_name=qname,
                type_signature=return_type,
                definition=definition,
                argsstring=argsstring,
                file_path=self.file_path,
                line_number=node.lineno,
                body_start=node.lineno,
                body_end=node.end_lineno or node.lineno,
                brief_description=brief,
                detailed_description=docstring,
                protection=protection,
                visibility=protection,
                is_static=is_static,
                is_const=False,
                is_constexpr=False,
                is_virtual=is_abstract,
                is_inline=False,
                is_explicit=False,
                source=self.source,
                source_type="source",
                layer=self.layer,
            ))

            # Add parameters
            for i, (pname, ptype, pdefault) in enumerate(args_info["params"]):
                self.result.parameters.append(ParameterNode(
                    member_refid=qname,
                    position=i,
                    name=pname,
                    type=ptype,
                    default_value=pdefault,
                ))
        else:
            # Free function at module level
            qname = _qualified_name(self.module_name, node.name)
            definition = f"def {node.name}{argsstring}"
            if return_type:
                definition = f"def {node.name}{argsstring} -> {return_type}"

            self.result.functions.append(FunctionNode(
                refid=qname,
                kind="function",
                name=node.name,
                qualified_name=qname,
                type_signature=return_type,
                definition=definition,
                argsstring=argsstring,
                file_path=self.file_path,
                line_number=node.lineno,
                body_start=node.lineno,
                body_end=node.end_lineno or node.lineno,
                brief_description=brief,
                detailed_description=docstring,
                source=self.source,
                source_type="source",
                layer=self.layer,
            ))

            # Add parameters
            for i, (pname, ptype, pdefault) in enumerate(args_info["params"]):
                self.result.parameters.append(ParameterNode(
                    member_refid=qname,
                    position=i,
                    name=pname,
                    type=ptype,
                    default_value=pdefault,
                ))

    # ------------------------------------------------------------------
    # Class-level attributes (assignments and annotated assignments)
    # ------------------------------------------------------------------

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Handle annotated assignments (x: int = 5) inside classes."""
        parent = self._current_class()
        if parent is None:
            return  # Module-level annotated assignments are not attributes
        if not isinstance(node.target, ast.Name):
            return

        compound_refid, parent_qname = parent
        name = node.target.id
        if name.startswith("_") and not name.startswith("__"):
            protection = "private"
        else:
            protection = "public"

        type_str = _annotation_to_str(node.annotation)
        qname = f"{parent_qname}.{name}"

        self.result.attributes.append(AttributeNode(
            refid=qname,
            compound_refid=compound_refid,
            kind="variable",
            name=name,
            qualified_name=qname,
            type_signature=type_str,
            definition=f"{name}: {type_str}",
            file_path=self.file_path,
            line_number=node.lineno,
            body_start=node.lineno,
            body_end=node.end_lineno or node.lineno,
            brief_description="",
            detailed_description="",
            protection=protection,
            visibility=protection,
            is_static=True,  # class-level attributes are static
            is_const=False,
            source=self.source,
            layer=self.layer,
        ))

    def visit_Assign(self, node: ast.Assign) -> None:
        """Handle simple assignments (x = 5) inside classes."""
        parent = self._current_class()
        if parent is None:
            return  # Module-level assignments → skip for now

        compound_refid, parent_qname = parent
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            # Skip dunder attributes
            if name.startswith("__") and name.endswith("__"):
                continue

            # Skip enum members — they are handled by _add_enum as EnumValueNode
            # Check if the parent class is an enum
            for enum_node in self.result.enums:
                if enum_node.refid == compound_refid:
                    return

            protection = "private" if _is_private(name) else "public"
            qname = f"{parent_qname}.{name}"

            self.result.attributes.append(AttributeNode(
                refid=qname,
                compound_refid=compound_refid,
                kind="variable",
                name=name,
                qualified_name=qname,
                type_signature="",
                definition=f"{name} = ...",
                file_path=self.file_path,
                line_number=node.lineno,
                body_start=node.lineno,
                body_end=node.end_lineno or node.lineno,
                brief_description="",
                detailed_description="",
                protection=protection,
                visibility=protection,
                is_static=True,
                is_const=False,
                source=self.source,
                layer=self.layer,
            ))

    # ------------------------------------------------------------------
    # Argument extraction
    # ------------------------------------------------------------------

    def _extract_args(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
        """Extract parameter info from a function definition.

        Returns a dict with:
            - signature: the argument signature string (e.g. "self, x: int, y: int = 0")
            - params: list of (name, type_str, default_str) tuples
        """
        args = node.args
        param_parts: list[str] = []
        params: list[tuple[str, str, str]] = []

        # Positional args
        for i, arg in enumerate(args.args):
            name = arg.arg
            type_str = _annotation_to_str(arg.annotation) if arg.annotation else ""

            # Find default value
            default_idx = i - (len(args.args) - len(args.defaults))
            if default_idx >= 0:
                default_str = _annotation_to_str(args.defaults[default_idx])
                params.append((name, type_str, default_str))
                param_parts.append(f"{name}: {type_str} = {default_str}" if type_str else f"{name}={default_str}")
            else:
                params.append((name, type_str, ""))
                param_parts.append(f"{name}: {type_str}" if type_str else name)

        # *args
        if args.vararg:
            name = args.vararg.arg
            type_str = _annotation_to_str(args.vararg.annotation) if args.vararg.annotation else ""
            params.append((name, type_str, ""))
            param_parts.append(f"*{name}: {type_str}" if type_str else f"*{name}")

        # Keyword-only args
        for i, arg in enumerate(args.kwonlyargs):
            name = arg.arg
            type_str = _annotation_to_str(arg.annotation) if arg.annotation else ""
            default_idx = i - (len(args.kwonlyargs) - len(args.kw_defaults))
            if default_idx >= 0 and args.kw_defaults[default_idx] is not None:
                default_str = _annotation_to_str(args.kw_defaults[default_idx])
                params.append((name, type_str, default_str))
                param_parts.append(f"{name}: {type_str} = {default_str}" if type_str else f"{name}={default_str}")
            else:
                params.append((name, type_str, ""))
                param_parts.append(f"{name}: {type_str}" if type_str else name)

        # **kwargs
        if args.kwarg:
            name = args.kwarg.arg
            type_str = _annotation_to_str(args.kwarg.annotation) if args.kwarg.annotation else ""
            params.append((name, type_str, ""))
            param_parts.append(f"**{name}: {type_str}" if type_str else f"**{name}")

        return {
            "signature": ", ".join(param_parts),
            "params": params,
        }


# ---------------------------------------------------------------------------
# Namespace composition derivation
# ---------------------------------------------------------------------------


def _parent_qualified_name(qname: str) -> str:
    """Return the parent qualified name of a dotted Python qualified name.

    Splits on the last ``.`` so that, e.g., ``samplepkg.operations.Operator``
    yields ``samplepkg.operations``.  Returns an empty string for a
    top-level name with no separator.
    """
    idx = qname.rfind(".")
    if idx != -1:
        return qname[:idx]
    return ""


def _derive_namespace_compositions(result: ParseResult) -> None:
    """Record namespace ``COMPOSES`` relationships on ``result.compositions``.

    A namespace composes its *immediate* children: direct child
    namespaces and the top-level classes / interfaces / enums / unions /
    functions defined directly within it (i.e. whose parent qualified
    name equals the namespace's qualified name).

    Only immediate children are composed so that, e.g., a class's
    methods remain composed by their class (via ``compound_refid``) and
    a deeply-nested member is composed by its own parent rather than by
    every ancestor namespace.

    The recorded :class:`CompositionEntry` items are consumed by
    ``graph_json._build_node_edges`` to emit ``COMPOSES`` edges.
    """
    # Map namespace qualified_name -> refid (for python these are equal,
    # but look them up to stay robust).
    ns_refid_by_qname: dict[str, str] = {}
    for ns in result.namespaces:
        qname = getattr(ns, "qualified_name", None)
        if qname:
            ns_refid_by_qname[qname] = getattr(ns, "refid", None) or qname

    if not ns_refid_by_qname:
        return

    # Candidate children: sub-namespaces + top-level compounds/functions.
    child_sources = (
        result.namespaces + result.classes + result.interfaces
        + result.enums + result.unions + result.functions
    )

    for child in child_sources:
        child_qname = getattr(child, "qualified_name", None)
        child_refid = getattr(child, "refid", None)
        if not child_qname or not child_refid:
            continue
        parent_qname = _parent_qualified_name(child_qname)
        parent_refid = ns_refid_by_qname.get(parent_qname)
        if not parent_refid:
            continue
        # Skip self-composition (a namespace whose own parent is empty).
        if parent_refid == child_refid:
            continue
        result.compositions.append(CompositionEntry(
            parent_refid=parent_refid,
            child_refid=child_refid,
            child_type=type(child).__name__,
        ))


def _derive_inheritance(result: ParseResult) -> None:
    """Record ``INHERITS_FROM`` relationships on ``result.inherits``.

    For every class (and interface) that declares base classes, resolve each
    base-class name to a parsed compound (class / interface / enum) and emit
    an :class:`InheritsEntry`.  Bases that don't resolve to any parsed
    compound — e.g. ``Exception``, ``ABC``, ``Enum``, or anything from the
    standard library / third parties — are silently skipped; they never
    produce dangling edges because ``graph_json`` only emits edges whose
    ``to_refid`` is a known node, and the graph layer drops unresolvable
    targets anyway.

    Name resolution prefers a same-namespace match (mirroring Python's
    own class-body name lookup), then falls back to a unique global match
    by short name, then by the trailing component of a dotted base name
    (e.g. ``errors.CalculatorError``).

    Only :class:`ClassNode` stores ``base_classes`` today, so only classes
    originate inheritance edges; a base can still be an interface (e.g.
    ``ToleranceVerifier`` inherits ``Verifier``), since interfaces are in
    the resolution index.
    """
    # Index every parsed compound by short name -> list of (refid, namespace, type).
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    # Also index by qualified name for exact dotted-base matches.
    by_qname: dict[str, tuple[str, str]] = {}
    for comp in result.classes + result.interfaces + result.enums + result.unions:
        name = getattr(comp, "name", None)
        refid = getattr(comp, "refid", None)
        qname = getattr(comp, "qualified_name", None)
        if not name or not refid or not qname:
            continue
        ns = _parent_qualified_name(qname)
        type_name = type(comp).__name__
        by_name.setdefault(name, []).append((refid, ns, type_name))
        by_qname[qname] = (refid, type_name)

    def _resolve(base: str, child_ns: str) -> tuple[str, str] | None:
        # Exact qualified-name match (dotted base like "pkg.Mod.Cls").
        if base in by_qname:
            refid, type_name = by_qname[base]
            return refid, type_name
        candidates = by_name.get(base)
        if not candidates:
            # Trailing-component fallback for "module.Name" style bases.
            short = base.rsplit(".", 1)[-1]
            candidates = by_name.get(short)
        if not candidates:
            return None
        # Prefer a base defined in the same namespace as the child.
        same_ns = [c for c in candidates if c[1] == child_ns]
        if same_ns:
            refid, _ns, type_name = same_ns[0]
            return refid, type_name
        # Fall back to a unique global match.
        if len(candidates) == 1:
            refid, _ns, type_name = candidates[0]
            return refid, type_name
        return None

    # Only ClassNode carries base_classes; iterate classes for the source side.
    for cls in result.classes:
        bases = getattr(cls, "base_classes", None) or []
        if not bases:
            continue
        child_refid = getattr(cls, "refid", None)
        child_qname = getattr(cls, "qualified_name", None)
        if not child_refid or not child_qname:
            continue
        child_ns = _parent_qualified_name(child_qname)
        for base in bases:
            resolved = _resolve(base, child_ns)
            if resolved is None:
                continue
            to_refid, to_type = resolved
            # Don't emit self-inheritance.
            if to_refid == child_refid:
                continue
            result.inherits.append(InheritsEntry(
                from_refid=child_refid,
                to_refid=to_refid,
                to_type=to_type,
            ))


# ---------------------------------------------------------------------------
# Type dependencies
# ---------------------------------------------------------------------------

# Python builtin / standard-library type names that should not produce
# ``DEPENDS_ON`` edges (there is no parseable definition for them).
_PYTHON_BUILTIN_TYPES: frozenset[str] = frozenset({
    # Builtins
    "bool", "int", "float", "complex", "str", "bytes", "bytearray",
    "list", "tuple", "dict", "set", "frozenset", "range", "slice",
    "type", "object", "None", "NoneType", "Ellipsis", "NotImplemented",
    "memoryview", "property", "staticmethod", "classmethod",
    # typing builtins
    "Any", "Optional", "Union", "Literal", "Type", "Callable",
    "List", "Dict", "Set", "Tuple", "FrozenSet", "Iterable", "Iterator",
    "Sequence", "Mapping", "MutableMapping", "Generator", "Coroutine",
    "Awaitable", "Protocol", "TypedDict", "NamedTuple",
    # Other common stdlib
    "Self", "Exception", "BaseException", "TypeVar",
})


def _strip_generic_args(type_str: str) -> str:
    """Strip generic arguments, returning the base type name.

    ``"Optional[VerificationLevel]"`` → ``"Optional"``
    ``"dict[str, Operator]"`` → ``"dict"``
    """
    idx = type_str.find("[")
    if idx != -1:
        return type_str[:idx].strip()
    idx = type_str.find("<")
    if idx != -1:
        return type_str[:idx].strip()
    return type_str.strip()


def _derive_type_dependencies(result: ParseResult) -> None:
    """Record ``DEPENDS_ON`` edges from functions/methods to the types
    they reference in parameters and return types.

    Iterates every method and free function, plus the ``ParameterNode``
    entries that belong to them, and emits a :class:`DependsOnEntry` for
    each type that resolves to a known parsed compound.

    Builtin / standard-library types (``int``, ``str``, ``float``,
    ``Optional``, …) and types with unsupported syntax (generics with
    bracket arguments) are silently skipped — the same way
    ``_derive_inheritance`` skips ``Exception``.
    """
    # Index every parsed compound for type-name resolution.
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    by_qname: dict[str, tuple[str, str]] = {}
    for comp in result.classes + result.interfaces + result.enums + result.unions:
        name = getattr(comp, "name", None)
        refid = getattr(comp, "refid", None)
        qname = getattr(comp, "qualified_name", None)
        if not name or not refid or not qname:
            continue
        ns = _parent_qualified_name(qname)
        type_name = type(comp).__name__
        by_name.setdefault(name, []).append((refid, ns, type_name))
        by_qname[qname] = (refid, type_name)

    def _resolve(type_str: str, caller_ns: str) -> tuple[str, str] | None:
        base = _strip_generic_args(type_str)
        if not base or base.lower() in _PYTHON_BUILTIN_TYPES:
            return None
        # Exact dotted-name match (e.g. "samplepkg.errors.CalculatorError").
        if base in by_qname:
            refid, type_name = by_qname[base]
            return refid, type_name
        candidates = by_name.get(base)
        if not candidates:
            # Trailing-component fallback for "module.Name" style.
            short = base.rsplit(".", 1)[-1]
            candidates = by_name.get(short)
        if not candidates:
            return None
        # Prefer same-namespace match.
        same_ns = [c for c in candidates if c[1] == caller_ns]
        if same_ns:
            refid, _ns, type_name = same_ns[0]
            return refid, type_name
        if len(candidates) == 1:
            refid, _ns, type_name = candidates[0]
            return refid, type_name
        return None

    # Collect all callables (methods + free functions) with their namespace.
    callables: list[tuple[str, str]] = []  # (refid, ns)
    for m in result.methods:
        qname = getattr(m, "qualified_name", None)
        refid = getattr(m, "refid", None)
        if qname and refid:
            callables.append((refid, _parent_qualified_name(qname)))
    for f in result.functions:
        qname = getattr(f, "qualified_name", None)
        refid = getattr(f, "refid", None)
        if qname and refid:
            callables.append((refid, _parent_qualified_name(qname)))

    callable_ns = {refid: ns for refid, ns in callables}
    seen: set[tuple[str, str]] = set()  # (from_refid, to_refid)

    # Parameter types
    for param in result.parameters:
        refid = getattr(param, "member_refid", None)
        ptype = (getattr(param, "type", "") or "").strip()
        if not refid or not ptype or refid not in callable_ns:
            continue
        resolved = _resolve(ptype, callable_ns[refid])
        if resolved is None:
            continue
        to_refid, to_type = resolved
        if to_refid == refid:  # skip self-references (e.g. classmethod returning cls)
            continue
        pair = (refid, to_refid)
        if pair in seen:
            continue
        seen.add(pair)
        result.depends_on.append(DependsOnEntry(
            from_refid=refid, to_refid=to_refid, to_type=to_type,
        ))

    # Return types
    for m in result.methods:
        refid = getattr(m, "refid", None)
        ret = (getattr(m, "type_signature", "") or "").strip()
        if not refid or not ret or refid not in callable_ns:
            continue
        resolved = _resolve(ret, callable_ns[refid])
        if resolved is None:
            continue
        to_refid, to_type = resolved
        if to_refid == refid:
            continue
        pair = (refid, to_refid)
        if pair in seen:
            continue
        seen.add(pair)
        result.depends_on.append(DependsOnEntry(
            from_refid=refid, to_refid=to_refid, to_type=to_type,
        ))
    for f in result.functions:
        refid = getattr(f, "refid", None)
        ret = (getattr(f, "type_signature", "") or "").strip()
        if not refid or not ret or refid not in callable_ns:
            continue
        resolved = _resolve(ret, callable_ns[refid])
        if resolved is None:
            continue
        to_refid, to_type = resolved
        if to_refid == refid:
            continue
        pair = (refid, to_refid)
        if pair in seen:
            continue
        seen.add(pair)
        result.depends_on.append(DependsOnEntry(
            from_refid=refid, to_refid=to_refid, to_type=to_type,
        ))


# ---------------------------------------------------------------------------
# Namespace-level import derivation
# ---------------------------------------------------------------------------


def _derive_namespace_imports(result: ParseResult) -> None:
    """Derive ``INCLUDES`` edges from namespaces to the compounds they import.

    Uses the existing ``result.includes`` data (file-level import records)
    to resolve imported symbols to known compound refids, then emits them
    as namespace-level ``INCLUDES`` edges via ``result.namespace_includes``.

    Only non-excluded files (i.e. not ``__init__.py``) are considered,
    since ``__init__.py`` re-exports duplicate namespace composition.
    The source of each edge is the namespace that owns the importing file.

    Relative imports are resolved against the parent package; absolute
    imports are matched directly against known compounds.
    """
    # Build a compound name index for resolution.
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    by_qname: dict[str, tuple[str, str]] = {}
    for comp in result.classes + result.interfaces + result.enums + result.unions:
        name = getattr(comp, "name", None)
        refid = getattr(comp, "refid", None)
        qname = getattr(comp, "qualified_name", None)
        if not name or not refid or not qname:
            continue
        ns = _parent_qualified_name(qname)
        type_name = type(comp).__name__
        by_name.setdefault(name, []).append((refid, ns, type_name))
        by_qname[qname] = (refid, type_name)

    # Namespace refid set (for quick lookup of valid source namespaces).
    ns_refids = {getattr(ns, "refid", None) for ns in result.namespaces}

    seen: set[tuple[str, str]] = set()

    for inc in result.includes:
        file_refid = inc.file_refid
        if not file_refid or file_refid not in ns_refids:
            continue

        target_name = inc.included_refid or ""
        if not target_name:
            continue

        # Resolve relative imports: parent namespace + relative name.
        if inc.is_local:
            parent_ns = _parent_qualified_name(file_refid)
            if parent_ns:
                target_name = f"{parent_ns}.{target_name}"

        resolved: tuple[str, str] | None = None

        # Exact qualified-name match.
        if target_name in by_qname:
            resolved = by_qname[target_name]
        else:
            # Short-name match (prefer same-parent-namespace).
            short = target_name.rsplit(".", 1)[-1]
            candidates = by_name.get(short)
            if candidates:
                parent_ns = _parent_qualified_name(file_refid)
                same_ns = [c for c in candidates if c[1] == parent_ns]
                if same_ns:
                    resolved = (same_ns[0][0], same_ns[0][2])
                elif len(candidates) == 1:
                    resolved = (candidates[0][0], candidates[0][2])

        if resolved is None:
            continue
        to_refid, to_type = resolved
        pair = (file_refid, to_refid)
        if pair in seen:
            continue
        seen.add(pair)

        # The source namespace IS the file_refid (module name).
        result.namespace_includes.append(IncludeEntry(
            file_refid=file_refid,
            included_file=inc.included_file,
            included_refid=to_refid,
            is_local=inc.is_local,
        ))


# ---------------------------------------------------------------------------
# Implementation extraction
# ---------------------------------------------------------------------------


def _extract_implementations(result: ParseResult) -> None:
    """Extract implementation source code for methods and functions.

    For each member with ``body_start`` > 0 and ``body_end`` > 0, reads
    the source file and extracts lines ``body_start`` .. ``body_end``
    (inclusive), creates an :class:`ImplementationNode`, and records
    the association via :class:`ImplementationRef`.

    Members without implementation bodies or with missing source files
    are skipped.
    """
    # Collect all members that have body locations and a file_path
    members_with_bodies: list[tuple[object, str]] = []
    for m in result.methods:
        if m.body_start > 0 and m.body_end > 0 and m.file_path:
            members_with_bodies.append((m, m.refid))
    for f in result.functions:
        if f.body_start > 0 and f.body_end > 0 and f.file_path:
            members_with_bodies.append((f, f.refid))

    if not members_with_bodies:
        return

    # Cache for file contents to avoid re-reading
    file_cache: dict[str, list[str] | None] = {}

    def _read_lines(file_path: str) -> list[str] | None:
        if file_path in file_cache:
            return file_cache[file_path]
        try:
            lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            file_cache[file_path] = lines
            return lines
        except FileNotFoundError:
            print(f"  Warning: Source file not found for implementation extraction: {file_path}",
                  file=sys.stderr)
            file_cache[file_path] = None
            return None

    impl_count = 0
    skip_count = 0

    for member, refid in members_with_bodies:
        lines = _read_lines(member.file_path)
        if lines is None:
            skip_count += 1
            continue

        # body_start/body_end are 1-based line numbers, inclusive
        start = member.body_start - 1  # Convert to 0-based index
        end = member.body_end            # 1-based inclusive → slice end

        if start < 0 or end > len(lines) or start >= end:
            skip_count += 1
            continue

        source_text = "".join(lines[start:end]).rstrip("\n")

        if not source_text.strip():
            skip_count += 1
            continue

        impl_node = ImplementationNode(
            qualified_name=member.qualified_name,
            kind="implementation",
            implementation=source_text,
            impl_embedding=[],
            source=member.source if hasattr(member, "source") else "",
            layer=member.layer if hasattr(member, "layer") else "codebase",
        )

        result.implementations.append(impl_node)
        result.implementation_refs.append(ImplementationRef(
            member_refid=refid,
            implementation=impl_node,
        ))
        impl_count += 1

    print(f"  Implementations extracted: {impl_count} (skipped: {skip_count})", file=sys.stderr)