"""
Post-processing passes for Python parsing.

These functions run after all source files have been parsed and derive
cross-referencing relationships (``COMPOSES``, ``INHERITS_FROM``,
``DEPENDS_ON``, ``INCLUDES``, test compositions) and extract
implementation source code.

Called from
:meth:`~doxygen_index.parser.python._parser.PythonParser.post_process`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from codegraph import ImplementationNode

from doxygen_index.parser.model import (
    ParseResult,
    CompositionEntry,
    InheritsEntry,
    DependsOnEntry,
    IncludeEntry,
    ImplementationRef,
)


# ---------------------------------------------------------------------------
# Utility
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


# ---------------------------------------------------------------------------
# Namespace composition
# ---------------------------------------------------------------------------


def derive_namespace_compositions(result: ParseResult) -> None:
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
    # Test nodes are handled separately by derive_test_compositions.
    child_sources = (
        result.namespaces + result.classes + result.interfaces
        + result.enums + result.unions + result.functions
    )

    for child in child_sources:
        child_qname = getattr(child, "qualified_name", None)
        child_refid = getattr(child, "refid", None)
        if not child_qname or not child_refid:
            continue
        # Skip test_fixture nodes — they are test-local instances,
        # not namespace-level classes.
        if hasattr(child, "has_tag") and child.has_tag("test_fixture"):
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


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def derive_inheritance(result: ParseResult) -> None:
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


def derive_type_dependencies(result: ParseResult) -> None:
    """Record ``DEPENDS_ON`` edges from functions/methods to the types
    they reference in parameters and return types.

    Iterates every method and free function, plus the ``ParameterNode``
    entries that belong to them, and emits a :class:`DependsOnEntry` for
    each type that resolves to a known parsed compound.

    Builtin / standard-library types (``int``, ``str``, ``float``,
    ``Optional``, …) and types with unsupported syntax (generics with
    bracket arguments) are silently skipped — the same way
    ``derive_inheritance`` skips ``Exception``.
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


def derive_namespace_imports(result: ParseResult) -> None:
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
# Test composition
# ---------------------------------------------------------------------------


def derive_test_compositions(result: ParseResult) -> None:
    """Record namespace ``COMPOSES`` edges for test nodes.

    Each TestNode is composed by its parent namespace (the module it
    was defined in).  This function adds :class:`CompositionEntry`
    items to ``result.compositions`` so that ``graph_json`` emits the
    appropriate ``COMPOSES`` edges.

    TestNode → AssertionNode / TestStepNode compositions are already
    recorded on ``result.test_compositions`` during parsing.
    """
    ns_refid_by_qname: dict[str, str] = {}
    for ns in result.namespaces:
        qname = getattr(ns, "qualified_name", None)
        if qname:
            ns_refid_by_qname[qname] = getattr(ns, "refid", None) or qname

    for test in result.tests:
        test_qname = getattr(test, "qualified_name", None)
        test_refid = getattr(test, "refid", None)
        if not test_qname or not test_refid:
            continue
        parent_qname = _parent_qualified_name(test_qname)
        parent_refid = ns_refid_by_qname.get(parent_qname)
        if not parent_refid:
            continue
        result.compositions.append(CompositionEntry(
            parent_refid=parent_refid,
            child_refid=test_refid,
            child_type="TestNode",
        ))


# ---------------------------------------------------------------------------
# Implementation extraction
# ---------------------------------------------------------------------------


def extract_implementations(result: ParseResult) -> None:
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