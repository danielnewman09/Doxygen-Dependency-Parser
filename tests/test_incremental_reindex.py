"""Incremental re-indexing tests — verify that re-indexing a source
captures additions, deletions, renames, and property updates without
destroying and re-writing the whole database.

These tests use the ``tests/languages/python/samplepkg`` fixture as a
base, copy it to a temp directory, write it to Neo4j, then modify the
source files and call :func:`update_result` to incrementally update the
graph.  After the update, they fetch actual :class:`CodeGraphNode`
objects from Neo4j and inspect their neomodel relationship managers
(e.g. ``ns.classes.all()``, ``cls.methods.all()``,
``meth.parent_compound.all()``) to verify that:

* **Added** nodes appear with their edges (parent_namespace, methods, …).
* **Deleted** nodes are gone and no edges point to them.
* **Renamed** nodes: the old node is gone; the new one has the same
  edges the old one had.
* **Updated** nodes retain all their edges.
* **Preservation**: nodes from *other* sources are untouched.

Requires a running Neo4j instance.  Skipped automatically when Neo4j
is not reachable.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path

import pytest

from doxygen_index.parser import parse_python_dir
from doxygen_index.graph_json import result_to_graph_json


#: Root of the language-specific test fixtures.
LANGUAGES_DIR = Path(__file__).parent / "languages"
PYTHON_FIXTURE_DIR = LANGUAGES_DIR / "python"  # contains the ``samplepkg`` package

#: Source label used throughout these tests.
TEST_SOURCE = "reindex_test"

#: Source label for an "other" source — used to verify that incremental
#: updates for one source don't affect nodes from another.
OTHER_SOURCE = "reindex_other"


# ---------------------------------------------------------------------------
# Neo4j helpers — raw Cypher
# ---------------------------------------------------------------------------

def _neo4j_available() -> bool:
    """Check if Neo4j is reachable and credentials are configured."""
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / "codegraph" / ".env", override=False)
    except Exception:
        pass
    if not os.getenv("NEO4J_URI"):
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).parent.parent / ".env", override=False)
        except Exception:
            pass
    if not os.getenv("NEO4J_URI"):
        return False
    try:
        from neomodel import db
        from doxygen_index.neo4j_backend import connect_neo4j
        connect_neo4j()
        db.cypher_query("RETURN 1")
        return True
    except Exception:
        return False


def _file_exists(refid: str, source: str = TEST_SOURCE) -> bool:
    from neomodel import db
    results, _ = db.cypher_query(
        "MATCH (f:FileNode {refid: $refid, source: $src}) RETURN count(f)",
        {"refid": refid, "src": source},
    )
    return results[0][0] > 0


def _node_count(label: str, source: str = TEST_SOURCE) -> int:
    from neomodel import db
    results, _ = db.cypher_query(
        f"MATCH (n:{label} {{source: $src}}) RETURN count(n)",
        {"src": source},
    )
    return results[0][0]


def _node_exists(label: str, qualified_name: str, source: str = TEST_SOURCE) -> bool:
    from neomodel import db
    results, _ = db.cypher_query(
        f"MATCH (n:{label} {{qualified_name: $qn, source: $src}}) RETURN count(n)",
        {"qn": qualified_name, "src": source},
    )
    return results[0][0] > 0


def _node_property(label: str, qualified_name: str, prop: str, source: str = TEST_SOURCE):
    from neomodel import db
    results, _ = db.cypher_query(
        f"MATCH (n:{label} {{qualified_name: $qn, source: $src}}) "
        f"RETURN n.{prop}",
        {"qn": qualified_name, "src": source},
    )
    if results and results[0]:
        return results[0][0]
    return None


def _total_node_count(source: str = TEST_SOURCE) -> int:
    from neomodel import db
    results, _ = db.cypher_query(
        "MATCH (n {source: $src}) RETURN count(n)",
        {"src": source},
    )
    return results[0][0]


def _incoming_edge_count(qualified_name: str, source: str = TEST_SOURCE) -> int:
    """Count ALL incoming edges to a node, regardless of type.

    Used to verify that a deleted node has zero dangling edges.
    """
    from neomodel import db
    results, _ = db.cypher_query(
        "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) "
        "RETURN count(r)",
        {"qn": qualified_name, "src": source},
    )
    return results[0][0]


def _outgoing_edge_count(qualified_name: str, source: str = TEST_SOURCE) -> int:
    """Count ALL outgoing edges from a node, regardless of type."""
    from neomodel import db
    results, _ = db.cypher_query(
        "MATCH (n {qualified_name: $qn, source: $src})-[r]->() "
        "RETURN count(r)",
        {"qn": qualified_name, "src": source},
    )
    return results[0][0]


def _clear_test_sources():
    from neomodel import db
    for src in [TEST_SOURCE, OTHER_SOURCE]:
        db.cypher_query(
            "MATCH (n {source: $src}) DETACH DELETE n",
            {"src": src},
        )


def _write_file(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Neo4j helpers — neomodel object fetchers and edge inspectors
# ---------------------------------------------------------------------------

def _get_node(model_cls, qualified_name: str, source: str = TEST_SOURCE):
    """Fetch a single node by qualified_name and source, or None."""
    return model_cls.nodes.get_or_none(
        qualified_name=qualified_name, source=source,
    )


def _node_identity(node) -> str:
    """Return a string identity for *node*, preferring qualified_name,
    then refid, then path.

    FileNode doesn't have ``qualified_name``, so we fall back to ``refid``
    (the module name) or ``path``.
    """
    for attr in ("qualified_name", "refid", "path"):
        val = getattr(node, attr, None)
        if val:
            return val
    return str(id(node))


def _rel_qnames(node, rel_manager_name: str) -> set[str]:
    """Return the set of identity strings of nodes reachable via a
    relationship manager on *node*.

    Uses :func:`_node_identity` so that FileNode targets (which lack
    ``qualified_name``) are identified by ``refid`` or ``path``.

    Example::

        _rel_qnames(ns, "classes")  → {"samplepkg.backend.Evaluator"}
        _rel_qnames(cls, "defined_in") → {"samplepkg.backend"}  # FileNode refid
    """
    rm = getattr(node, rel_manager_name, None)
    if rm is None:
        return set()
    return {_node_identity(n) for n in rm.all()}


def _rel_count(node, rel_manager_name: str) -> int:
    """Return the number of nodes reachable via a relationship manager."""
    rm = getattr(node, rel_manager_name, None)
    if rm is None:
        return 0
    return len(rm.all())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _neo4j_setup():
    if not _neo4j_available():
        pytest.skip("Neo4j not reachable — skipping incremental re-index tests")

    from doxygen_index.neo4j_backend import connect_neo4j, ensure_schema
    from neomodel import db

    connect_neo4j()
    ensure_schema()
    _clear_test_sources()

    yield

    _clear_test_sources()


@pytest.fixture
def fixture_copy(tmp_path) -> Path:
    """Copy the Python fixture (containing samplepkg) to a temp directory."""
    dest = tmp_path / "python"
    shutil.copytree(PYTHON_FIXTURE_DIR, dest)
    return dest


SAMPLEPKG = "samplepkg"


@pytest.fixture
def initial_index(fixture_copy):
    """Parse the fixture, write it to Neo4j, and return the directory path."""
    from doxygen_index.neo4j_backend import write_result
    from neomodel import db

    db.cypher_query(
        "MATCH (n {source: $src}) DETACH DELETE n",
        {"src": TEST_SOURCE},
    )

    result = parse_python_dir(fixture_copy, source=TEST_SOURCE, progress_interval=0)
    write_result(result)

    yield fixture_copy

    db.cypher_query(
        "MATCH (n {source: $src}) DETACH DELETE n",
        {"src": TEST_SOURCE},
    )


# ---------------------------------------------------------------------------
# Tests — Node Addition
# ---------------------------------------------------------------------------

class TestNodeAddition:
    """Adding new nodes to the source creates them in Neo4j on re-index."""

    def test_new_class_added(self, initial_index):
        """A new class added to a source file appears with its edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, NamespaceNode, MethodNode

        qname = "samplepkg.backend.Calculator"
        assert not _node_exists("ClassNode", qname)

        # Add a new class to backend.py
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        original = backend_file.read_text()
        _write_file(backend_file, original + """

class Calculator:
    \"\"\"A simple calculator that wraps an Evaluator.\"\"\"

    def __init__(self, initial: float = 0.0):
        self._evaluator = Evaluator(initial)

    def run(self, op: Operator, operand: float) -> float:
        return self._evaluator.step(op, operand)
""")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Node exists
        assert _node_exists("ClassNode", qname)
        assert _node_exists("MethodNode", qname + ".run")
        assert _node_exists("MethodNode", qname + ".__init__")

        # Edges: the namespace composes the new class
        ns = _get_node(NamespaceNode, "samplepkg.backend")
        assert qname in _rel_qnames(ns, "classes")

        # Edges: the class composes its methods
        cls = _get_node(ClassNode, qname)
        assert cls is not None
        method_qnames = _rel_qnames(cls, "methods")
        assert qname + ".run" in method_qnames
        assert qname + ".__init__" in method_qnames

        # Edges: the class has parent_namespace pointing back to the namespace
        assert "samplepkg.backend" in _rel_qnames(cls, "parent_namespace")

    def test_new_function_added(self, initial_index):
        """A new free function added to a source file appears with edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import FunctionNode, NamespaceNode

        qname = "samplepkg.operations.multiply_all"
        assert not _node_exists("FunctionNode", qname)

        ops_file = initial_index / SAMPLEPKG / "operations.py"
        original = ops_file.read_text()
        _write_file(ops_file, original + """


def multiply_all(values: list[float]) -> float:
    \"\"\"Multiply all values together and return the product.\"\"\"
    result = 1.0
    for v in values:
        result *= v
    return result
""")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        assert _node_exists("FunctionNode", qname)

        # Edges: the namespace composes the new function
        ns = _get_node(NamespaceNode, "samplepkg.operations")
        assert qname in _rel_qnames(ns, "functions")

        # Edges: the function has parent_namespace pointing back
        func = _get_node(FunctionNode, qname)
        assert func is not None
        assert "samplepkg.operations" in _rel_qnames(func, "parent_namespace")

    def test_new_file_added(self, initial_index):
        """A new source file with new classes/functions appears with edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, NamespaceNode, MethodNode

        assert not _node_exists("ClassNode", "samplepkg.logger.Logger")
        assert not _node_exists("NamespaceNode", "samplepkg.logger")

        _write_file(initial_index / SAMPLEPKG / "logger.py", '''
"""Logging utilities for the calculator."""

class Logger:
    """A simple logger that records operations."""

    def __init__(self):
        self._entries: list[str] = []

    def log(self, message: str) -> None:
        """Record a log message."""
        self._entries.append(message)

    def entries(self) -> list[str]:
        """Return all logged messages."""
        return list(self._entries)
''')

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Nodes
        assert _node_exists("NamespaceNode", "samplepkg.logger")
        assert _node_exists("ClassNode", "samplepkg.logger.Logger")
        assert _node_exists("MethodNode", "samplepkg.logger.Logger.log")
        assert _file_exists("samplepkg.logger")

        # Edges: top-level namespace composes the new sub-namespace
        top_ns = _get_node(NamespaceNode, "samplepkg")
        assert "samplepkg.logger" in _rel_qnames(top_ns, "namespaces")

        # Edges: the new namespace composes the Logger class
        ns = _get_node(NamespaceNode, "samplepkg.logger")
        assert ns is not None
        assert "samplepkg.logger.Logger" in _rel_qnames(ns, "classes")

        # Edges: the class composes its methods
        cls = _get_node(ClassNode, "samplepkg.logger.Logger")
        assert cls is not None
        method_qnames = _rel_qnames(cls, "methods")
        assert "samplepkg.logger.Logger.log" in method_qnames
        assert "samplepkg.logger.Logger.__init__" in method_qnames

        # Edges: the class has parent_namespace
        assert "samplepkg.logger" in _rel_qnames(cls, "parent_namespace")


# ---------------------------------------------------------------------------
# Tests — Node Deletion
# ---------------------------------------------------------------------------

class TestNodeDeletion:
    """Removing nodes from the source deletes them from Neo4j on re-index."""

    def test_class_removed(self, initial_index):
        """A deleted class is gone, no edges point to it, and siblings survive."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import NamespaceNode, InterfaceNode, FunctionNode

        old_qname = "samplepkg.verify.ToleranceVerifier"
        old_method_qname = old_qname + ".verify"

        assert _node_exists("ClassNode", old_qname)
        assert _node_exists("MethodNode", old_method_qname)

        # Record edges before deletion
        ns = _get_node(NamespaceNode, "samplepkg.verify")
        assert old_qname in _rel_qnames(ns, "classes")
        ns_classes_before = _rel_qnames(ns, "classes")

        # Remove the ToleranceVerifier class from verify.py
        verify_file = initial_index / SAMPLEPKG / "verify.py"
        _write_file(verify_file, '''
"""Verification of calculator results."""

from abc import ABC, abstractmethod
from enum import Enum


class VerificationLevel(Enum):
    """Strictness of result verification."""
    LENIENT = 0
    STRICT = 1


class Verifier(ABC):
    """Interface for verifying a computed result against an expectation."""

    @abstractmethod
    def verify(self, expected: float, actual: float) -> bool:
        """Return True if *actual* is an acceptable result for *expected*."""


def assert_close(
    expected: float,
    actual: float,
    level: VerificationLevel = VerificationLevel.STRICT,
) -> None:
    """Raise ``AssertionError`` if *actual* is not close to *expected*."""
    tolerance = 1e-9 if level is VerificationLevel.STRICT else 1e-6
    if abs(expected - actual) > tolerance:
        raise AssertionError(f"{actual} is not close to {expected}")
''')

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Nodes are gone
        assert not _node_exists("ClassNode", old_qname)
        assert not _node_exists("MethodNode", old_method_qname)

        # No edges point to the deleted class or its method
        # (the node doesn't exist, so incoming_edge_count should be 0)
        from neomodel import db
        results, _ = db.cypher_query(
            "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) RETURN count(r)",
            {"qn": old_qname, "src": TEST_SOURCE},
        )
        assert results[0][0] == 0, f"Dangling edges point to deleted {old_qname}"

        # Siblings survive
        assert _node_exists("InterfaceNode", "samplepkg.verify.Verifier")
        assert _node_exists("FunctionNode", "samplepkg.verify.assert_close")

        # Edges: the namespace no longer has the deleted class
        ns_after = _get_node(NamespaceNode, "samplepkg.verify")
        ns_classes_after = _rel_qnames(ns_after, "classes")
        assert old_qname not in ns_classes_after

        # Edges: the surviving siblings are still composed by the namespace
        assert "samplepkg.verify.Verifier" not in ns_classes_after  # Interface, not Class
        ns_interfaces = _rel_qnames(ns_after, "interfaces")
        assert "samplepkg.verify.Verifier" in ns_interfaces
        ns_functions = _rel_qnames(ns_after, "functions")
        assert "samplepkg.verify.assert_close" in ns_functions

    def test_file_removed(self, initial_index):
        """Removing an entire source file deletes all its nodes and edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import NamespaceNode

        ns_qname = "samplepkg.long_signatures"
        func_qname = ns_qname + ".process_data"
        cls_qname = ns_qname + ".ReportingService"

        assert _node_exists("FunctionNode", func_qname)
        assert _node_exists("ClassNode", cls_qname)
        assert _node_exists("NamespaceNode", ns_qname)
        assert _file_exists(ns_qname)

        # Record the top-level namespace's sub-namespaces before
        top_ns = _get_node(NamespaceNode, "samplepkg")
        assert ns_qname in _rel_qnames(top_ns, "namespaces")

        # Delete the file
        (initial_index / SAMPLEPKG / "long_signatures.py").unlink()

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # All nodes from the deleted file should be gone
        assert not _node_exists("FunctionNode", func_qname)
        assert not _node_exists("ClassNode", cls_qname)
        assert not _node_exists("NamespaceNode", ns_qname)
        assert not _file_exists(ns_qname)

        # No edges point to the deleted namespace
        from neomodel import db
        for qn in [ns_qname, func_qname, cls_qname]:
            results, _ = db.cypher_query(
                "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) RETURN count(r)",
                {"qn": qn, "src": TEST_SOURCE},
            )
            assert results[0][0] == 0, f"Dangling edges point to deleted {qn}"

        # Edges: the top-level namespace no longer composes the deleted namespace
        top_ns_after = _get_node(NamespaceNode, "samplepkg")
        assert ns_qname not in _rel_qnames(top_ns_after, "namespaces")

        # Other nodes should still be present with their edges
        assert _node_exists("ClassNode", "samplepkg.backend.Evaluator")

    def test_method_removed(self, initial_index):
        """Removing a method deletes only that method and its edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, MethodNode

        cls_qname = "samplepkg.backend.Evaluator"
        deleted_meth_qname = cls_qname + ".reset"

        assert _node_exists("MethodNode", deleted_meth_qname)

        # Record edges before deletion
        cls = _get_node(ClassNode, cls_qname)
        methods_before = _rel_qnames(cls, "methods")
        assert deleted_meth_qname in methods_before

        # Remove the reset method from backend.py
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        _write_file(backend_file, '''
"""Backend evaluation engine for the calculator."""

from samplepkg.errors import DivisionByZeroError
from samplepkg.operations import Operator, apply_operator


class Evaluator:
    """Evaluates a sequence of ``(operator, operand)`` steps."""

    DEFAULT_INITIAL: float = 0.0

    def __init__(self, initial: float = DEFAULT_INITIAL):
        self.value = initial

    @classmethod
    def from_zero(cls) -> "Evaluator":
        return cls(0.0)

    @property
    def current(self) -> float:
        return self.value

    def step(self, op: Operator, operand: float) -> float:
        if op is Operator.DIVIDE and operand == 0:
            raise DivisionByZeroError(
                "division by zero", expression=f"{self.value} / 0",
            )
        self.value = apply_operator(op, self.value, operand)
        return self.value
''')

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # The reset method should be gone
        assert not _node_exists("MethodNode", deleted_meth_qname)

        # No edges point to the deleted method
        from neomodel import db
        results, _ = db.cypher_query(
            "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) RETURN count(r)",
            {"qn": deleted_meth_qname, "src": TEST_SOURCE},
        )
        assert results[0][0] == 0, f"Dangling edges point to deleted {deleted_meth_qname}"

        # Edges: the class no longer composes the deleted method
        cls_after = _get_node(ClassNode, cls_qname)
        methods_after = _rel_qnames(cls_after, "methods")
        assert deleted_meth_qname not in methods_after

        # Edges: the surviving methods are still composed by the class
        assert cls_qname + ".step" in methods_after
        assert cls_qname + ".from_zero" in methods_after
        assert cls_qname + ".current" in methods_after
        assert cls_qname + ".__init__" in methods_after

        # Edges: surviving methods still have parent_compound pointing back
        step = _get_node(MethodNode, cls_qname + ".step")
        assert step is not None
        assert cls_qname in _rel_qnames(step, "parent_compound")


# ---------------------------------------------------------------------------
# Tests — Node Renaming
# ---------------------------------------------------------------------------

class TestNodeRenaming:
    """Renaming a node in source creates the new node and deletes the old one.

    The new node must retain the same edges the old one had.
    """

    def test_class_renamed(self, initial_index):
        """A renamed class: old gone, new exists with same parent_namespace edge."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, NamespaceNode, MethodNode

        old_qname = "samplepkg.verify.ToleranceVerifier"
        new_qname = "samplepkg.verify.ToleranceChecker"

        assert _node_exists("ClassNode", old_qname)

        # Record edges before rename
        ns_before = _get_node(NamespaceNode, "samplepkg.verify")
        old_methods_before = set()
        cls_before = _get_node(ClassNode, old_qname)
        if cls_before:
            old_methods_before = _rel_qnames(cls_before, "methods")

        # Rename ToleranceVerifier → ToleranceChecker in verify.py
        verify_file = initial_index / SAMPLEPKG / "verify.py"
        content = verify_file.read_text()
        renamed = content.replace("ToleranceVerifier", "ToleranceChecker")
        verify_file.write_text(renamed, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Old node is gone; new node exists
        assert not _node_exists("ClassNode", old_qname)
        assert not _node_exists("MethodNode", old_qname + ".verify")
        assert _node_exists("ClassNode", new_qname)
        assert _node_exists("MethodNode", new_qname + ".verify")

        # Edges: the new class has parent_namespace pointing to the same ns
        cls_after = _get_node(ClassNode, new_qname)
        assert cls_after is not None
        assert "samplepkg.verify" in _rel_qnames(cls_after, "parent_namespace")

        # Edges: the namespace now composes the new class, not the old
        ns_after = _get_node(NamespaceNode, "samplepkg.verify")
        ns_classes = _rel_qnames(ns_after, "classes")
        assert new_qname in ns_classes
        assert old_qname not in ns_classes

        # Edges: the new class composes its methods (same as before, renamed)
        new_methods = _rel_qnames(cls_after, "methods")
        expected_new_methods = {m.replace(old_qname, new_qname) for m in old_methods_before}
        assert expected_new_methods == new_methods, (
            f"Methods on renamed class mismatch: expected {expected_new_methods}, "
            f"got {new_methods}"
        )

        # Edges: the new method has parent_compound pointing to the new class
        new_meth = _get_node(MethodNode, new_qname + ".verify")
        assert new_meth is not None
        assert new_qname in _rel_qnames(new_meth, "parent_compound")

        # No edges point to the old node
        from neomodel import db
        results, _ = db.cypher_query(
            "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) RETURN count(r)",
            {"qn": old_qname, "src": TEST_SOURCE},
        )
        assert results[0][0] == 0, f"Dangling edges point to old {old_qname}"

    def test_function_renamed(self, initial_index):
        """A renamed function: old gone, new exists with parent_namespace edge."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import FunctionNode, NamespaceNode

        old_qname = "samplepkg.operations.apply_operator"
        new_qname = "samplepkg.operations.execute_operator"

        assert _node_exists("FunctionNode", old_qname)

        # Record edges before rename
        ns_before = _get_node(NamespaceNode, "samplepkg.operations")
        assert old_qname in _rel_qnames(ns_before, "functions")

        # Rename apply_operator → execute_operator in operations.py
        ops_file = initial_index / SAMPLEPKG / "operations.py"
        content = ops_file.read_text()
        renamed = content.replace("apply_operator", "execute_operator")
        ops_file.write_text(renamed, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        assert not _node_exists("FunctionNode", old_qname)
        assert _node_exists("FunctionNode", new_qname)

        # Edges: the namespace composes the new function, not the old
        ns_after = _get_node(NamespaceNode, "samplepkg.operations")
        ns_functions = _rel_qnames(ns_after, "functions")
        assert new_qname in ns_functions
        assert old_qname not in ns_functions

        # Edges: the new function has parent_namespace pointing back
        func_after = _get_node(FunctionNode, new_qname)
        assert func_after is not None
        assert "samplepkg.operations" in _rel_qnames(func_after, "parent_namespace")

        # Edges: the new function has implementation_ref
        assert _rel_count(func_after, "implementation_ref") == 1

    def test_method_renamed(self, initial_index):
        """A renamed method: old gone, new exists with parent_compound edge."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, MethodNode

        cls_qname = "samplepkg.backend.Evaluator"
        old_qname = cls_qname + ".step"
        new_qname = cls_qname + ".apply"

        assert _node_exists("MethodNode", old_qname)
        assert not _node_exists("MethodNode", new_qname)

        # Record edges before rename
        cls_before = _get_node(ClassNode, cls_qname)
        methods_before = _rel_qnames(cls_before, "methods")
        assert old_qname in methods_before

        # Rename step → apply in backend.py
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        content = backend_file.read_text()
        renamed = content.replace("def step(", "def apply(")
        # Also update the call in test_calculator.py
        test_file = initial_index / SAMPLEPKG / "test_calculator.py"
        test_content = test_file.read_text()
        test_renamed = test_content.replace(".step(", ".apply(")
        test_file.write_text(test_renamed, encoding="utf-8")
        backend_file.write_text(renamed, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        assert not _node_exists("MethodNode", old_qname)
        assert _node_exists("MethodNode", new_qname)

        # Edges: the class composes the new method, not the old
        cls_after = _get_node(ClassNode, cls_qname)
        methods_after = _rel_qnames(cls_after, "methods")
        assert new_qname in methods_after
        assert old_qname not in methods_after

        # Edges: the new method has parent_compound pointing to the class
        meth_after = _get_node(MethodNode, new_qname)
        assert meth_after is not None
        assert cls_qname in _rel_qnames(meth_after, "parent_compound")

        # Edges: the new method has implementation_ref (HAS_IMPLEMENTATION)
        assert _rel_count(meth_after, "implementation_ref") == 1


# ---------------------------------------------------------------------------
# Tests — Node Updating
# ---------------------------------------------------------------------------

class TestNodeUpdate:
    """Updating a node's properties in source updates them in Neo4j.

    All edges must be retained when a node is merely updated (not
    renamed or deleted).
    """

    def test_class_description_updated(self, initial_index):
        """Changing a class docstring updates the property; edges are retained."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode

        qname = "samplepkg.backend.Evaluator"

        # Record state before update
        cls_before = _get_node(ClassNode, qname)
        orig_desc = cls_before.brief_description
        orig_methods = _rel_qnames(cls_before, "methods")
        orig_parent = _rel_qnames(cls_before, "parent_namespace")
        orig_attributes = _rel_qnames(cls_before, "attributes")
        orig_impl_count = _rel_count(cls_before, "implementation_ref")

        # Change the docstring
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        content = backend_file.read_text()
        updated = content.replace(
            'Evaluates a sequence of ``(operator, operand)`` steps.',
            'A **modified** evaluator that accumulates results across steps.',
        )
        backend_file.write_text(updated, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Property is updated
        cls_after = _get_node(ClassNode, qname)
        assert cls_after is not None
        assert "modified" in cls_after.brief_description.lower()
        assert cls_after.brief_description != orig_desc

        # No duplicates
        from neomodel import db
        results, _ = db.cypher_query(
            "MATCH (n:ClassNode {qualified_name: $qn, source: $src}) RETURN count(n)",
            {"qn": qname, "src": TEST_SOURCE},
        )
        assert results[0][0] == 1, "Should have exactly one node (no duplicates)"

        # Edges are retained — same methods, same parent, same attributes
        assert _rel_qnames(cls_after, "methods") == orig_methods, (
            "Methods changed after a description-only update"
        )
        assert _rel_qnames(cls_after, "parent_namespace") == orig_parent, (
            "parent_namespace changed after a description-only update"
        )
        assert _rel_qnames(cls_after, "attributes") == orig_attributes, (
            "Attributes changed after a description-only update"
        )

    def test_method_implementation_updated(self, initial_index):
        """Changing a method's body updates ImplementationNode; edges retained."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import MethodNode

        qname = "samplepkg.backend.Evaluator.step"

        # Record state before update
        meth_before = _get_node(MethodNode, qname)
        orig_impl = meth_before.implementation_ref.all()
        orig_impl_text = orig_impl[0].implementation if orig_impl else None
        orig_parent = _rel_qnames(meth_before, "parent_compound")
        assert orig_impl_text is not None

        # Change the step method implementation
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        content = backend_file.read_text()
        updated = content.replace(
            'self.value = apply_operator(op, self.value, operand)',
            'result = apply_operator(op, self.value, operand)\n        self.value = result',
        )
        backend_file.write_text(updated, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Implementation text is updated
        meth_after = _get_node(MethodNode, qname)
        assert meth_after is not None
        new_impl = meth_after.implementation_ref.all()
        assert len(new_impl) == 1
        assert new_impl[0].implementation != orig_impl_text

        # Edges retained: parent_compound still points to the class
        assert _rel_qnames(meth_after, "parent_compound") == orig_parent, (
            "parent_compound changed after an implementation-only update"
        )

        # Edges retained: implementation_ref still has exactly 1 edge
        assert _rel_count(meth_after, "implementation_ref") == 1

    def test_no_duplicate_nodes_on_reindex(self, initial_index):
        """Re-indexing the same source does not create duplicate nodes."""
        from doxygen_index.neo4j_backend import update_result

        initial_total = _total_node_count(TEST_SOURCE)

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        final_total = _total_node_count(TEST_SOURCE)
        assert final_total == initial_total, (
            f"Expected {initial_total} nodes, got {final_total} — "
            "duplicates were created"
        )

    def test_unchanged_nodes_preserved(self, initial_index):
        """Unchanged nodes retain all their properties and edges."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, NamespaceNode

        qname = "samplepkg.errors.CalculatorError"
        cls_before = _get_node(ClassNode, qname)
        orig_kind = cls_before.kind
        orig_name = cls_before.name
        orig_desc = cls_before.brief_description
        orig_methods = _rel_qnames(cls_before, "methods")
        orig_parent = _rel_qnames(cls_before, "parent_namespace")

        # Modify a different file (operations.py)
        ops_file = initial_index / SAMPLEPKG / "operations.py"
        content = ops_file.read_text()
        _write_file(ops_file, content + "\n\ndef new_func():\n    pass\n")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        cls_after = _get_node(ClassNode, qname)
        assert cls_after.kind == orig_kind
        assert cls_after.name == orig_name
        assert cls_after.brief_description == orig_desc

        # All edges retained
        assert _rel_qnames(cls_after, "methods") == orig_methods
        assert _rel_qnames(cls_after, "parent_namespace") == orig_parent

        # The namespace that composes this class still has it
        ns = _get_node(NamespaceNode, "samplepkg.errors")
        assert qname in _rel_qnames(ns, "classes")


# ---------------------------------------------------------------------------
# Tests — Cross-source isolation
# ---------------------------------------------------------------------------

class TestCrossSourceIsolation:
    """Incremental updates for one source don't affect nodes from another."""

    def test_other_source_untouched(self, initial_index):
        """Updating TEST_SOURCE doesn't affect nodes from OTHER_SOURCE."""
        from doxygen_index.neo4j_backend import write_result, update_result
        from neomodel import db

        # Write a second source
        other_dir = initial_index.parent / "other_samplepkg"
        shutil.copytree(PYTHON_FIXTURE_DIR, other_dir)
        other_result = parse_python_dir(other_dir, source=OTHER_SOURCE, progress_interval=0)
        write_result(other_result)

        other_count = _total_node_count(OTHER_SOURCE)
        assert other_count > 0

        # Modify TEST_SOURCE by deleting a file
        (initial_index / SAMPLEPKG / "long_signatures.py").unlink()

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # OTHER_SOURCE should be completely unaffected
        assert _total_node_count(OTHER_SOURCE) == other_count
        assert _node_exists("ClassNode", "samplepkg.long_signatures.ReportingService",
                            source=OTHER_SOURCE)
        assert not _node_exists("ClassNode", "samplepkg.long_signatures.ReportingService",
                                source=TEST_SOURCE)

        # Cleanup
        db.cypher_query(
            "MATCH (n {source: $src}) DETACH DELETE n",
            {"src": OTHER_SOURCE},
        )
        shutil.rmtree(other_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests — Relationship integrity (edge-focused)
# ---------------------------------------------------------------------------

class TestRelationshipIntegrity:
    """Relationships are correctly maintained after incremental updates.

    These tests use neomodel relationship managers on fetched
    :class:`CodeGraphNode` objects to inspect edges directly.
    """

    def test_composes_edge_recreated_after_class_rename(self, initial_index):
        """After renaming a class, the new class has the COMPOSES edge from its namespace."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode, NamespaceNode, MethodNode

        # Rename ToleranceVerifier → ToleranceChecker
        verify_file = initial_index / SAMPLEPKG / "verify.py"
        content = verify_file.read_text()
        renamed = content.replace("ToleranceVerifier", "ToleranceChecker")
        verify_file.write_text(renamed, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # The namespace composes the new class (check via neomodel object)
        ns = _get_node(NamespaceNode, "samplepkg.verify")
        assert "samplepkg.verify.ToleranceChecker" in _rel_qnames(ns, "classes")
        assert "samplepkg.verify.ToleranceVerifier" not in _rel_qnames(ns, "classes")

        # The new class has parent_namespace pointing back
        cls = _get_node(ClassNode, "samplepkg.verify.ToleranceChecker")
        assert "samplepkg.verify" in _rel_qnames(cls, "parent_namespace")

        # The new class composes its methods
        new_methods = _rel_qnames(cls, "methods")
        assert "samplepkg.verify.ToleranceChecker.verify" in new_methods

        # The new method has parent_compound pointing to the new class
        meth = _get_node(MethodNode, "samplepkg.verify.ToleranceChecker.verify")
        assert "samplepkg.verify.ToleranceChecker" in _rel_qnames(meth, "parent_compound")

    def test_no_dangling_edges_after_file_deletion(self, initial_index):
        """After deleting a file, no edges of any type point to its nodes."""
        from doxygen_index.neo4j_backend import update_result
        from neomodel import db

        # Delete the entire long_signatures.py file
        (initial_index / SAMPLEPKG / "long_signatures.py").unlink()

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Check that no edges of ANY type point to the deleted namespace
        # or its children
        deleted_qnames = [
            "samplepkg.long_signatures",
            "samplepkg.long_signatures.process_data",
            "samplepkg.long_signatures.ReportingService",
            "samplepkg.long_signatures.ReportingService.generate_report",
        ]
        for qn in deleted_qnames:
            results, _ = db.cypher_query(
                "MATCH ()-[r]->(n {qualified_name: $qn, source: $src}) RETURN count(r)",
                {"qn": qn, "src": TEST_SOURCE},
            )
            assert results[0][0] == 0, (
                f"Dangling edge points to deleted node {qn}: {results[0][0]} edges"
            )

        # Also check no outgoing edges from the deleted nodes exist
        # (the nodes themselves should be gone)
        for qn in deleted_qnames:
            results, _ = db.cypher_query(
                "MATCH (n {qualified_name: $qn, source: $src}) RETURN count(n)",
                {"qn": qn, "src": TEST_SOURCE},
            )
            assert results[0][0] == 0, f"Deleted node {qn} still exists"

    def test_verifies_edges_preserved_for_unchanged_tests(self, initial_index):
        """VERIFIES edges for unchanged test nodes are preserved after update."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph.models.test import TestNode

        test_qname = "samplepkg.test_calculator.test_evaluator_step"

        # Record VERIFIES edges before update
        test_before = _get_node(TestNode, test_qname)
        verifies_before = (
            _rel_qnames(test_before, "verifies_methods")
            | _rel_qnames(test_before, "verifies_classes")
        )
        assert len(verifies_before) > 0, "Expected VERIFIES edges before update"

        # Re-index without changes
        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # VERIFIES edges are the same after update
        test_after = _get_node(TestNode, test_qname)
        verifies_after = (
            _rel_qnames(test_after, "verifies_methods")
            | _rel_qnames(test_after, "verifies_classes")
        )
        assert verifies_after == verifies_before, (
            f"VERIFIES edges changed: {verifies_before} → {verifies_after}"
        )

        # Test composition edges (assertions, steps) are also preserved
        assert _rel_qnames(test_after, "assertions") == _rel_qnames(test_before, "assertions")
        assert _rel_qnames(test_after, "steps") == _rel_qnames(test_before, "steps")

    def test_enriched_descriptions_preserved_on_reindex(self, initial_index):
        """Enriched descriptions on test children survive a re-index.

        The parser auto-generates placeholder descriptions (e.g.
        ``"assert =="`` for assertions).  If an LLM enrichment run
        wrote richer descriptions, re-indexing must not overwrite them
        with the parser's placeholders.
        """
        from doxygen_index.neo4j_backend import update_result
        from codegraph.models.test import TestNode

        test_qname = "samplepkg.test_calculator.test_evaluator_step"
        enriched_desc = "LLM-enriched: evaluates step with confidence"

        # Write a rich description directly on a test child node
        from neomodel import db
        child_qname = f"{test_qname}::evaluator"
        db.cypher_query(
            "MATCH (n {qualified_name: $qname}) SET n.description = $desc",
            {"qname": child_qname, "desc": enriched_desc},
        )

        # Re-index without changes
        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Description must be preserved — not overwritten with placeholder
        results, _ = db.cypher_query(
            "MATCH (n {qualified_name: $qname}) RETURN n.description",
            {"qname": child_qname},
        )
        assert results and results[0][0] == enriched_desc, (
            f"Enriched description was overwritten! "
            f"Expected '{enriched_desc}', got '{results[0][0] if results else 'N/A'}'"
        )

    def test_enriched_assertion_descriptions_preserved(self, initial_index):
        """Enriched descriptions on assertion nodes survive a re-index.

        Assertion descriptions are auto-generated as ``"assert <operator>"``
        (a placeholder).  Enriched values must survive.
        """
        from doxygen_index.neo4j_backend import update_result, _is_placeholder_description
        from codegraph.models.test import TestNode

        test_qname = "samplepkg.test_calculator.test_evaluator_step"

        # Find an assertion child
        test_node = _get_node(TestNode, test_qname)
        assertions = _rel_qnames(test_node, "assertions")
        assert assertions, "Test has no assertion children"
        assertion_qname = sorted(assertions)[0]

        # Verify the current description IS a placeholder
        from neomodel import db
        results, _ = db.cypher_query(
            "MATCH (n {qualified_name: $qname}) RETURN n.description",
            {"qname": assertion_qname},
        )
        current_desc = results[0][0] if results else ""
        assert _is_placeholder_description(current_desc), (
            f"Expected placeholder, got: {current_desc!r}"
        )

        # Write a rich description
        enriched = "LLM: Verifies that the step produces the expected result"
        db.cypher_query(
            "MATCH (n {qualified_name: $qname}) SET n.description = $desc",
            {"qname": assertion_qname, "desc": enriched},
        )

        # Re-index
        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # Must be preserved
        results, _ = db.cypher_query(
            "MATCH (n {qualified_name: $qname}) RETURN n.description",
            {"qname": assertion_qname},
        )
        assert results and results[0][0] == enriched, (
            f"Enriched assertion overwritten! "
            f"Expected '{enriched}', got '{results[0][0] if results else 'N/A'}'"
        )

    def test_inherits_from_edge_preserved_after_update(self, initial_index):
        """INHERITS_FROM edges are preserved when a derived class is updated."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode

        # CalculatorError has no INHERITS_FROM (inherits from Exception,
        # which isn't a parsed compound).  DivisionByZeroError inherits
        # from CalculatorError.
        qname = "samplepkg.errors.DivisionByZeroError"
        cls_before = _get_node(ClassNode, qname)
        base_before = _rel_qnames(cls_before, "base")
        assert "samplepkg.errors.CalculatorError" in base_before

        # Update the docstring (property change only, no rename)
        errors_file = initial_index / SAMPLEPKG / "errors.py"
        content = errors_file.read_text()
        updated = content.replace(
            "Raised when a division by zero is attempted.",
            "Raised when a division by zero is attempted. Updated.",
        )
        errors_file.write_text(updated, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # INHERITS_FROM edge is preserved
        cls_after = _get_node(ClassNode, qname)
        base_after = _rel_qnames(cls_after, "base")
        assert base_after == base_before, (
            f"INHERITS_FROM edges changed: {base_before} → {base_after}"
        )

    def test_defined_in_edge_preserved_after_update(self, initial_index):
        """DEFINED_IN edges are preserved when a class is updated."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode

        qname = "samplepkg.backend.Evaluator"
        cls_before = _get_node(ClassNode, qname)

        # Record the DEFINED_IN edge (points to a FileNode, identified by refid)
        defined_in_before = _rel_qnames(cls_before, "defined_in")
        assert len(defined_in_before) > 0, "Expected DEFINED_IN edge before update"
        assert "samplepkg.backend" in defined_in_before, (
            f"Expected DEFINED_IN → samplepkg.backend, got {defined_in_before}"
        )

        # Update the docstring
        backend_file = initial_index / SAMPLEPKG / "backend.py"
        content = backend_file.read_text()
        updated = content.replace(
            'Evaluates a sequence of ``(operator, operand)`` steps.',
            'A **modified** evaluator that accumulates results across steps.',
        )
        backend_file.write_text(updated, encoding="utf-8")

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # DEFINED_IN edge is preserved
        cls_after = _get_node(ClassNode, qname)
        defined_in_after = _rel_qnames(cls_after, "defined_in")
        assert defined_in_after == defined_in_before, (
            f"DEFINED_IN edges changed: {defined_in_before} → {defined_in_after}"
        )

    def test_full_edge_snapshot_unchanged_on_noop_reindex(self, initial_index):
        """Re-indexing without changes preserves all edges on a class."""
        from doxygen_index.neo4j_backend import update_result
        from codegraph import ClassNode

        qname = "samplepkg.backend.Evaluator"
        cls_before = _get_node(ClassNode, qname)

        # Snapshot all relationship managers
        snapshot_before = {
            "methods": _rel_qnames(cls_before, "methods"),
            "attributes": _rel_qnames(cls_before, "attributes"),
            "parent_namespace": _rel_qnames(cls_before, "parent_namespace"),
            "defined_in": _rel_qnames(cls_before, "defined_in"),
            "implementation_ref": _rel_count(cls_before, "implementation_ref"),
        }

        # Re-index without changes
        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        update_result(result, source=TEST_SOURCE)

        # All edges unchanged
        cls_after = _get_node(ClassNode, qname)
        snapshot_after = {
            "methods": _rel_qnames(cls_after, "methods"),
            "attributes": _rel_qnames(cls_after, "attributes"),
            "parent_namespace": _rel_qnames(cls_after, "parent_namespace"),
            "defined_in": _rel_qnames(cls_after, "defined_in"),
            "implementation_ref": _rel_count(cls_after, "implementation_ref"),
        }
        assert snapshot_after == snapshot_before, (
            f"Edge snapshot changed on no-op reindex:\n"
            f"  before: {snapshot_before}\n"
            f"  after:  {snapshot_after}"
        )


# ---------------------------------------------------------------------------
# Tests — Summary and return value
# ---------------------------------------------------------------------------

class TestUpdateResultSummary:
    """The update_result return value correctly reports deleted stale nodes."""

    def test_returns_empty_dict_when_nothing_deleted(self, initial_index):
        """When no nodes are deleted, update_result returns an empty dict."""
        from doxygen_index.neo4j_backend import update_result

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        deleted = update_result(result, source=TEST_SOURCE)

        assert deleted == {}, f"Expected no deletions, got: {deleted}"

    def test_returns_deletion_counts(self, initial_index):
        """When nodes are deleted, update_result returns counts by label."""
        from doxygen_index.neo4j_backend import update_result

        # Delete long_signatures.py (has a class and a function)
        (initial_index / SAMPLEPKG / "long_signatures.py").unlink()

        result = parse_python_dir(initial_index, source=TEST_SOURCE, progress_interval=0)
        deleted = update_result(result, source=TEST_SOURCE)

        assert "CompoundNode" in deleted, f"Expected CompoundNode deletions, got: {deleted}"
        assert "MemberNode" in deleted, f"Expected MemberNode deletions, got: {deleted}"
        assert "FileNode" in deleted, f"Expected FileNode deletions, got: {deleted}"
        assert deleted["CompoundNode"] >= 1  # DataProcessor class
        assert deleted["MemberNode"] >= 1  # process_data function