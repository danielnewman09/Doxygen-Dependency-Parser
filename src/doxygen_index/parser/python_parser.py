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
from doxygen_index.parser.model import ParseResult, IncludeEntry, ImplementationRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    ) -> None:
        """Parse all Python source files in *source_dir* and populate *result*.

        Walks the directory recursively for ``.py`` files.  Each file
        is parsed with ``ast`` and its symbols extracted.

        Args:
            source_dir: Root directory of the Python source tree.
            source: Provenance label.
            result: Accumulator for parsed entries.
            layer: Layer label.
            progress_interval: Print progress every N files. 0 disables.
        """
        source_dir = Path(source_dir)
        py_files = sorted(source_dir.rglob("*.py"))
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

        Extracts implementation source code for all methods and
        functions that have body_start/body_end line numbers.
        """
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

        # Add imports as IncludeEntry items
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