"""Tests for Python invoke-graph derivation."""

import ast

from codegraph import (
    MethodNode, FunctionNode, ParameterNode, NamespaceNode,
)

from doxygen_index.parser.model import ParseResult, InvokeEntry
from doxygen_index.parser.python.postprocess import (
    derive_invokes,
    derive_namespace_compositions,
)
from doxygen_index.parser.python._ast_utils import collect_calls


class TestCollectCalls:
    """Unit tests for _CallCollector / collect_calls."""

    def test_simple_call(self):
        code = """
def caller():
    helper()
"""
        tree = ast.parse(code)
        calls = collect_calls(tree)
        assert calls == [("helper", 3)]

    def test_self_call(self):
        code = """
class Foo:
    def method(self):
        self._helper()
"""
        tree = ast.parse(code)
        calls = collect_calls(tree)
        assert calls == [("self._helper", 4)]

    def test_dotted_call(self):
        code = """
def caller():
    mod.func(x, y)
"""
        tree = ast.parse(code)
        calls = collect_calls(tree)
        assert calls == [("mod.func", 3)]

    def test_no_calls(self):
        code = """
def noop():
    x = 1 + 1
"""
        tree = ast.parse(code)
        calls = collect_calls(tree)
        assert calls == []

    def test_multiple_calls(self):
        code = """
def caller():
    a()
    b()
    c(a())
"""
        tree = ast.parse(code)
        calls = collect_calls(tree)
        assert len(calls) == 4  # a, b, c, a (nested)
        assert ("a", 3) in calls
        assert ("b", 4) in calls
        assert ("c", 5) in calls


class TestDeriveInvokes:
    """Integration tests for derive_invokes post-process step."""

    def _make_result(self, methods=None, functions=None, pending_calls=None):
        """Helper to build a minimal ParseResult with callable nodes."""
        return ParseResult(
            methods=methods or [],
            functions=functions or [],
            pending_calls=pending_calls or [],
        )

    def _make_method(self, refid, name, compound_refid="module.MyClass"):
        qname = f"{compound_refid}.{name}" if "." in compound_refid else f"module.{compound_refid}.{name}"
        return MethodNode(
            refid=qname,
            compound_refid=compound_refid,
            name=name,
            qualified_name=qname,
            type_signature="",
            definition=f"def {name}()",
            argsstring="()",
            file_path="/fake/module.py",
            line_number=1,
            body_start=1,
            body_end=2,
            source="test",
            layer="codebase",
        )

    def _make_func(self, refid, name):
        qname = refid if "." in refid else f"module.{refid}"
        return FunctionNode(
            refid=qname,
            name=name,
            qualified_name=qname,
            type_signature="",
            definition=f"def {name}()",
            argsstring="()",
            file_path="/fake/module.py",
            line_number=1,
            body_start=1,
            body_end=2,
            source="test",
            layer="codebase",
        )

    def test_direct_call_same_module(self):
        """caller() calls helper() in the same module — should resolve."""
        helper = self._make_func("module.helper", "helper")
        caller = self._make_func("module.caller", "caller")
        result = self._make_result(
            functions=[helper, caller],
            pending_calls=[("module.caller", "helper", 5)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 1
        inv = result.invokes[0]
        assert inv.from_refid == "module.caller"
        assert inv.to_refid == "module.helper"

    def test_method_calls_same_class_method(self):
        """method_a() calls self.method_b() — should resolve."""
        method_b = self._make_method("module.MyClass.method_b", "method_b")
        method_a = self._make_method("module.MyClass.method_a", "method_a")
        result = self._make_result(
            methods=[method_b, method_a],
            pending_calls=[("module.MyClass.method_a", "self.method_b", 10)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 1
        assert result.invokes[0].to_refid == "module.MyClass.method_b"

    def test_call_to_function_in_other_module(self):
        """caller() calls othermod.helper() — should resolve via dotted name."""
        helper = self._make_func("othermod.helper", "helper")
        caller = self._make_func("module.caller", "caller")
        result = self._make_result(
            functions=[helper, caller],
            pending_calls=[("module.caller", "othermod.helper", 5)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 1
        assert result.invokes[0].to_refid == "othermod.helper"

    def test_builtin_ignored(self):
        """Calls to len(), print(), etc. should be ignored."""
        caller = self._make_func("module.caller", "caller")
        result = self._make_result(
            functions=[caller],
            pending_calls=[
                ("module.caller", "len", 3),
                ("module.caller", "print", 4),
                ("module.caller", "isinstance", 5),
            ],
        )
        derive_invokes(result)
        assert len(result.invokes) == 0

    def test_unresolved_call_ignored(self):
        """Call to a function not in the index is silently skipped."""
        caller = self._make_func("module.caller", "caller")
        result = self._make_result(
            functions=[caller],
            pending_calls=[("module.caller", "nonexistent_func", 3)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 0

    def test_duplicate_calls_deduplicated(self):
        """Multiple calls to the same target produce only one edge."""
        helper = self._make_func("module.helper", "helper")
        caller = self._make_func("module.caller", "caller")
        result = self._make_result(
            functions=[helper, caller],
            pending_calls=[
                ("module.caller", "helper", 3),
                ("module.caller", "helper", 6),
                ("module.caller", "helper", 9),
            ],
        )
        derive_invokes(result)
        assert len(result.invokes) == 1

    def test_empty_pending_calls(self):
        """No pending calls → no invokes."""
        result = self._make_result(functions=[])
        derive_invokes(result)
        assert len(result.invokes) == 0

    def test_self_call_ignored(self):
        """Recursive self-call should be skipped (to_refid == from_refid)."""
        func = self._make_func("module.recurse", "recurse")
        result = self._make_result(
            functions=[func],
            pending_calls=[("module.recurse", "recurse", 3)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 0

    def test_standalone_function_calls_class_method(self):
        """A free function calls ClassName.method_name()."""
        method = self._make_method("module.MyClass.method_b", "method_b")
        func = self._make_func("module.caller", "caller")
        result = self._make_result(
            methods=[method],
            functions=[func],
            pending_calls=[("module.caller", "module.MyClass.method_b", 5)],
        )
        derive_invokes(result)
        assert len(result.invokes) == 1
        assert result.invokes[0].to_refid == "module.MyClass.method_b"
