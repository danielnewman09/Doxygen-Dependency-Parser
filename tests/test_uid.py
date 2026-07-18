"""Tests for deterministic UID computation across all node types.

Verifies that ``_ensure_deterministic_uid`` and ``_merge_by_keys`` in
``neo4j_backend.py`` produce uids consistent with ``codegraph.uid.compute_uid``,
the single canonical UID function.  Covers:

1. Unit tests: ``_ensure_deterministic_uid`` for every node type
2. Determinism: same inputs → same uid; different inputs → different uid
3. Argsstring normalisation: overloads get distinct uids, param names ignored
4. Source scoping: same qname in different sources → different uid
5. ``_merge_by_keys`` returns the correct merge spec
6. Integration: save to Neo4j, retrieve, verify uid matches expected
"""

from __future__ import annotations

import pytest

from codegraph.uid import compute_uid, normalize_argsstring
from codegraph import (
    ClassNode, InterfaceNode, EnumNode, UnionNode, ConceptNode,
    MethodNode, AttributeNode, EnumValueNode, FunctionNode, DefineNode,
    FileNode, NamespaceNode, ParameterNode, ImplementationNode,
)
from codegraph.models.test import (
    TestNode, AssertionNode, TestStepNode, TestFixtureNode,
)
from codegraph.models.literal import LiteralNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_uid(source: str, node) -> str:
    """Compute the expected uid for *node* using ``compute_uid``."""
    from doxygen_index.neo4j_backend import _ensure_deterministic_uid

    # Make a copy so we don't mutate the original
    node_copy = type(node)(**{k: v for k, v in node.__properties__.items()})
    node_copy.source = source
    _ensure_deterministic_uid(node_copy)
    return node_copy.uid


# ---------------------------------------------------------------------------
# Unit tests: _ensure_deterministic_uid for every node type
# ---------------------------------------------------------------------------


class TestEnsureDeterministicUid:
    """Verifies ``_ensure_deterministic_uid`` produces correct uids."""

    SOURCE = "test_project"

    # ── NamespaceNode ──────────────────────────────────────────────────

    def test_namespace_node(self):
        node = NamespaceNode(name="myns", qualified_name="myns", kind="namespace")
        expected = compute_uid(self.SOURCE, "myns")
        assert _expected_uid(self.SOURCE, node) == expected

    def test_namespace_node_nested(self):
        node = NamespaceNode(
            name="inner", qualified_name="outer::inner", kind="namespace"
        )
        expected = compute_uid(self.SOURCE, "outer::inner")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── ClassNode ──────────────────────────────────────────────────────

    def test_class_node(self):
        node = ClassNode(
            name="Widget", qualified_name="myns::Widget", kind="class"
        )
        expected = compute_uid(self.SOURCE, "myns::Widget")
        assert _expected_uid(self.SOURCE, node) == expected

    def test_class_node_nested(self):
        node = ClassNode(
            name="Inner", qualified_name="outer::Inner::Nested", kind="class"
        )
        expected = compute_uid(self.SOURCE, "outer::Inner::Nested")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── InterfaceNode ──────────────────────────────────────────────────

    def test_interface_node(self):
        node = InterfaceNode(
            name="IPrintable", qualified_name="myns::IPrintable", kind="interface"
        )
        expected = compute_uid(self.SOURCE, "myns::IPrintable")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── EnumNode ───────────────────────────────────────────────────────

    def test_enum_node(self):
        node = EnumNode(name="Color", qualified_name="myns::Color", kind="enum")
        expected = compute_uid(self.SOURCE, "myns::Color")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── UnionNode ──────────────────────────────────────────────────────

    def test_union_node(self):
        node = UnionNode(name="Variant", qualified_name="myns::Variant", kind="union")
        expected = compute_uid(self.SOURCE, "myns::Variant")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── ConceptNode ────────────────────────────────────────────────────

    def test_concept_node(self):
        node = ConceptNode(
            name="Serializable", qualified_name="myns::Serializable", kind="concept"
        )
        expected = compute_uid(self.SOURCE, "myns::Serializable")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── MethodNode ─────────────────────────────────────────────────────

    def test_method_node(self):
        node = MethodNode(
            name="doSomething",
            qualified_name="myns::Widget::doSomething",
            kind="method",
            argsstring="(double x, int y)",
        )
        expected = compute_uid(
            self.SOURCE,
            "myns::Widget::doSomething",
            normalize_argsstring("(double x, int y)"),
        )
        assert _expected_uid(self.SOURCE, node) == expected

    def test_method_node_no_args(self):
        node = MethodNode(
            name="init",
            qualified_name="myns::Widget::init",
            kind="method",
            argsstring="()",
        )
        expected = compute_uid(
            self.SOURCE,
            "myns::Widget::init",
            normalize_argsstring("()"),
        )
        assert _expected_uid(self.SOURCE, node) == expected

    def test_method_node_const_ref(self):
        node = MethodNode(
            name="setName",
            qualified_name="myns::Widget::setName",
            kind="method",
            argsstring="(const std::string& name)",
        )
        expected = compute_uid(
            self.SOURCE,
            "myns::Widget::setName",
            normalize_argsstring("(const std::string& name)"),
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── AttributeNode ──────────────────────────────────────────────────

    def test_attribute_node(self):
        node = AttributeNode(
            name="m_count",
            qualified_name="myns::Widget::m_count",
            kind="attribute",
        )
        expected = compute_uid(self.SOURCE, "myns::Widget::m_count")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── EnumValueNode ──────────────────────────────────────────────────

    def test_enum_value_node(self):
        node = EnumValueNode(
            name="RED",
            qualified_name="myns::Color::RED",
            kind="enumvalue",
        )
        expected = compute_uid(self.SOURCE, "myns::Color::RED")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── FunctionNode ───────────────────────────────────────────────────

    def test_function_node(self):
        node = FunctionNode(
            name="parseConfig",
            qualified_name="parseConfig",
            kind="function",
            argsstring="(const char* path, int flags)",
        )
        expected = compute_uid(
            self.SOURCE,
            "parseConfig",
            normalize_argsstring("(const char* path, int flags)"),
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── DefineNode ─────────────────────────────────────────────────────

    def test_define_node(self):
        node = DefineNode(
            name="MAX_SIZE",
            qualified_name="MAX_SIZE",
            kind="define",
        )
        expected = compute_uid(self.SOURCE, "MAX_SIZE")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── FileNode ───────────────────────────────────────────────────────

    def test_file_node(self):
        node = FileNode(name="widget.h", path="src/widget.h")
        expected = compute_uid(self.SOURCE, "src/widget.h")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── ParameterNode ──────────────────────────────────────────────────

    def test_parameter_node(self):
        node = ParameterNode(
            name="x",
            kind="parameter",
            type_signature="double",
            member_refid="classWidget_1aDoSomething",
            position=0,
        )
        expected = compute_uid(
            self.SOURCE, "classWidget_1aDoSomething", "0"
        )
        assert _expected_uid(self.SOURCE, node) == expected

    def test_parameter_node_position_1(self):
        node = ParameterNode(
            name="y",
            kind="parameter",
            type_signature="int",
            member_refid="classWidget_1aDoSomething",
            position=1,
        )
        expected = compute_uid(
            self.SOURCE, "classWidget_1aDoSomething", "1"
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── ImplementationNode ─────────────────────────────────────────────

    def test_implementation_node(self):
        node = ImplementationNode(
            qualified_name="myns::Widget::doSomething",
            source_code="void doSomething() {}",
        )
        expected = compute_uid(self.SOURCE, "myns::Widget::doSomething")
        assert _expected_uid(self.SOURCE, node) == expected

    # ── TestNode ───────────────────────────────────────────────────────

    def test_test_node(self):
        node = TestNode(
            name="test_widget",
            qualified_name="tests.test_widget::test_widget_creation",
            kind="test",
        )
        expected = compute_uid(
            self.SOURCE, "tests.test_widget::test_widget_creation"
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── AssertionNode ──────────────────────────────────────────────────

    def test_assertion_node(self):
        node = AssertionNode(
            qualified_name="tests.test_widget::test_widget_creation::assert_0",
            assertion_type="assertEqual",
        )
        expected = compute_uid(
            self.SOURCE,
            "tests.test_widget::test_widget_creation::assert_0",
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── TestStepNode ───────────────────────────────────────────────────

    def test_test_step_node(self):
        node = TestStepNode(
            qualified_name="tests.test_widget::test_widget_creation::step_0",
        )
        expected = compute_uid(
            self.SOURCE, "tests.test_widget::test_widget_creation::step_0"
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── TestFixtureNode ────────────────────────────────────────────────

    def test_test_fixture_node(self):
        node = TestFixtureNode(
            name="widget",
            qualified_name="tests.test_widget::test_widget_creation::widget",
            kind="fixture",
        )
        expected = compute_uid(
            self.SOURCE, "tests.test_widget::test_widget_creation::widget"
        )
        assert _expected_uid(self.SOURCE, node) == expected

    # ── LiteralNode ────────────────────────────────────────────────────

    def test_literal_node(self):
        node = LiteralNode(
            qualified_name="literal::42", kind="literal", value="42"
        )
        expected = compute_uid(self.SOURCE, "literal::42")
        assert _expected_uid(self.SOURCE, node) == expected


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestUidDeterminism:
    """Verifies uids are deterministic and distinguish different identities."""

    def test_same_inputs_produce_same_uid(self):
        """Repeated calls with identical inputs produce identical uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        node1 = NamespaceNode(name="ns", qualified_name="ns", kind="namespace")
        node2 = NamespaceNode(name="ns", qualified_name="ns", kind="namespace")

        node1.source = src
        node2.source = src
        _ensure_deterministic_uid(node1)
        _ensure_deterministic_uid(node2)

        assert node1.uid == node2.uid
        assert node1.uid == compute_uid(src, "ns")

    def test_different_qualified_name_produces_different_uid(self):
        """Two namespaces with different qnames get different uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        node_a = NamespaceNode(name="ns_a", qualified_name="ns_a", kind="namespace")
        node_b = NamespaceNode(name="ns_b", qualified_name="ns_b", kind="namespace")

        node_a.source = src
        node_b.source = src
        _ensure_deterministic_uid(node_a)
        _ensure_deterministic_uid(node_b)

        assert node_a.uid != node_b.uid

    def test_different_source_produces_different_uid(self):
        """Same qname in different sources produces different uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        node_a = NamespaceNode(name="ns", qualified_name="ns", kind="namespace")
        node_b = NamespaceNode(name="ns", qualified_name="ns", kind="namespace")

        node_a.source = "proj_a"
        node_b.source = "proj_b"
        _ensure_deterministic_uid(node_a)
        _ensure_deterministic_uid(node_b)

        assert node_a.uid != node_b.uid

    def test_overloaded_methods_get_distinct_uids(self):
        """Methods with the same name but different arg types get distinct uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        method_int = MethodNode(
            name="f",
            qualified_name="ns::C::f",
            kind="method",
            argsstring="(int x)",
        )
        method_float = MethodNode(
            name="f",
            qualified_name="ns::C::f",
            kind="method",
            argsstring="(float x)",
        )

        method_int.source = src
        method_float.source = src
        _ensure_deterministic_uid(method_int)
        _ensure_deterministic_uid(method_float)

        assert method_int.uid != method_float.uid

    def test_normalized_argsstrings_produce_same_uid(self):
        """Same parameter types with different param names → same uid."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        m1 = MethodNode(
            name="f",
            qualified_name="ns::C::f",
            kind="method",
            argsstring="(int a, const char* str)",
        )
        m2 = MethodNode(
            name="f",
            qualified_name="ns::C::f",
            kind="method",
            argsstring="(int b, const char* name)",
        )

        m1.source = src
        m2.source = src
        _ensure_deterministic_uid(m1)
        _ensure_deterministic_uid(m2)

        assert m1.uid == m2.uid


# ---------------------------------------------------------------------------
# Cross-codebase consistency tests
# ---------------------------------------------------------------------------


class TestUidCrossCodebase:
    """Verifies doxygen-index uids match codegraph's ``compute_uid``."""

    def test_namespace_uid_matches_compute_uid(self):
        """Ensured uid == compute_uid(source, qualified_name)."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "cpp_sqlite"
        node = NamespaceNode(
            name="cpp_sqlite", qualified_name="cpp_sqlite", kind="namespace"
        )
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, "cpp_sqlite")

    def test_class_uid_matches_compute_uid(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "myproject"
        node = ClassNode(
            name="Foo", qualified_name="ns::Foo", kind="class"
        )
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, "ns::Foo")

    def test_method_uid_matches_compute_uid(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "myproject"
        qn = "ns::Foo::bar"
        args = "(int x, double y)"
        node = MethodNode(
            name="bar", qualified_name=qn, kind="method", argsstring=args
        )
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, qn, normalize_argsstring(args))

    def test_function_uid_matches_compute_uid(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "myproject"
        qn = "free_func"
        args = "(const char* s, size_t n)"
        node = FunctionNode(
            name="free_func", qualified_name=qn, kind="function", argsstring=args
        )
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, qn, normalize_argsstring(args))

    def test_file_uid_matches_compute_uid(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "myproject"
        node = FileNode(name="main.cpp", path="src/main.cpp")
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, "src/main.cpp")

    def test_parameter_uid_matches_compute_uid(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "myproject"
        node = ParameterNode(
            name="x",
            kind="parameter",
            type_signature="int",
            member_refid="func_abc",
            position=2,
        )
        node.source = src
        _ensure_deterministic_uid(node)
        assert node.uid == compute_uid(src, "func_abc", "2")


# ---------------------------------------------------------------------------
# _merge_by_keys tests
# ---------------------------------------------------------------------------


class TestMergeByKeys:
    """Verifies ``_merge_by_keys`` returns correct merge spec."""

    def test_returns_uid_key(self):
        from doxygen_index.neo4j_backend import _merge_by_keys, _ensure_deterministic_uid

        node = NamespaceNode(name="ns", qualified_name="ns", kind="namespace", source="proj")
        _ensure_deterministic_uid(node)
        result = _merge_by_keys(node)
        assert result == {"keys": ["uid"]}, f"Got {result}"

    def test_uid_key_for_all_node_types(self):
        from doxygen_index.neo4j_backend import _merge_by_keys, _ensure_deterministic_uid

        nodes = [
            NamespaceNode(name="x", qualified_name="x", kind="namespace", source="p"),
            ClassNode(name="C", qualified_name="ns::C", kind="class", source="p"),
            MethodNode(
                name="m", qualified_name="ns::C::m", kind="method",
                argsstring="()", source="p",
            ),
            FunctionNode(
                name="f", qualified_name="f", kind="function", argsstring="()", source="p",
            ),
            FileNode(name="f.h", path="f.h", source="p"),
            ParameterNode(
                name="p", kind="parameter", type_signature="int",
                member_refid="ref", position=0, source="p",
            ),
            TestNode(
                name="t", qualified_name="test::t", kind="test", source="p",
            ),
            AssertionNode(
                qualified_name="test::t::assert_0", source="p",
            ),
            LiteralNode(qualified_name="literal::0", source="p"),
        ]
        for node in nodes:
            _ensure_deterministic_uid(node)
            result = _merge_by_keys(node)
            assert result == {"keys": ["uid"]}, (
                f"{type(node).__name__} got {result}"
            )


# ---------------------------------------------------------------------------
# Argsstring normalization tests
# ---------------------------------------------------------------------------


class TestArgsstringNormalization:
    """Verifies argsstring normalization in uid computation."""

    def test_strips_cpp_param_names(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        a = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int x, const char* str)",
        )
        b = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int count, const char* message)",
        )
        a.source = src
        b.source = src
        _ensure_deterministic_uid(a)
        _ensure_deterministic_uid(b)
        assert a.uid == b.uid

    def test_strips_cpp_defaults(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        a = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int x = 0)",
        )
        b = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int x)",
        )
        a.source = src
        b.source = src
        _ensure_deterministic_uid(a)
        _ensure_deterministic_uid(b)
        assert a.uid == b.uid

    def test_preserves_pointers_and_references(self):
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        a = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int* ptr, const Foo& obj)",
        )
        b = MethodNode(
            name="f", qualified_name="ns::C::f", kind="method",
            argsstring="(int* p, const Foo& x)",
        )
        a.source = src
        b.source = src
        _ensure_deterministic_uid(a)
        _ensure_deterministic_uid(b)
        assert a.uid == b.uid

    def test_overloaded_function_uids_differ(self):
        """Two free functions with the same name but different signatures."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "proj"
        f1 = FunctionNode(
            name="process", qualified_name="process", kind="function",
            argsstring="(int x)",
        )
        f2 = FunctionNode(
            name="process", qualified_name="process", kind="function",
            argsstring="(double x)",
        )
        f1.source = src
        f2.source = src
        _ensure_deterministic_uid(f1)
        _ensure_deterministic_uid(f2)
        assert f1.uid != f2.uid


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestUidEdgeCases:
    """Edge cases for UID computation."""

    def test_special_characters_in_qualified_name(self):
        """Qualified names with special characters produce stable uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        qn = "std::vector<int, std::allocator<int>>"
        node = ClassNode(name="vector", qualified_name=qn, kind="class", source="proj")
        _ensure_deterministic_uid(node)
        expected = compute_uid("proj", qn)
        assert node.uid == expected

    def test_unicode_in_name(self):
        """Nodes with unicode names produce stable uids."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        node = ClassNode(
            name="café", qualified_name="myns::café", kind="class", source="proj"
        )
        _ensure_deterministic_uid(node)
        expected = compute_uid("proj", "myns::café")
        assert node.uid == expected

    def test_empty_qualified_name_still_hashes(self):
        """Node with empty qualified_name but valid source still produces a uid."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        node = NamespaceNode(name="", qualified_name="", kind="namespace", source="proj")
        _ensure_deterministic_uid(node)
        expected = compute_uid("proj", "")
        assert node.uid == expected
        assert len(node.uid) == 40


# ---------------------------------------------------------------------------
# Integration tests (require Neo4j)
# ---------------------------------------------------------------------------


class TestUidNeo4jIntegration:
    """Integration tests: save to Neo4j, retrieve, verify uid matches.

    These tests run against a real Neo4j instance (the test container
    started by ``tests/conftest.py``).
    """

    def test_namespace_save_has_deterministic_uid(self):
        """Saving a NamespaceNode produces the expected deterministic uid."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "test_integration"
        qn = "test_ns_uid"
        node = NamespaceNode(
            name="test_ns_uid", qualified_name=qn, kind="namespace", source=src,
        )
        _ensure_deterministic_uid(node)
        expected_uid = node.uid
        saved = node.save()
        try:
            assert saved.uid == expected_uid
            assert saved.uid == compute_uid(src, qn)
        finally:
            saved.delete()

    def test_class_save_has_deterministic_uid(self):
        """Saving a ClassNode produces the expected deterministic uid."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "test_integration"
        qn = "test_ns::TestClass"
        node = ClassNode(
            name="TestClass", qualified_name=qn, kind="class", source=src,
        )
        _ensure_deterministic_uid(node)
        expected_uid = node.uid
        saved = node.save()
        try:
            assert saved.uid == expected_uid
            assert saved.uid == compute_uid(src, qn)
        finally:
            saved.delete()

    def test_method_save_has_deterministic_uid(self):
        """Saving a MethodNode produces the expected deterministic uid."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "test_integration"
        qn = "test_ns::TestClass::method"
        args = "(int x, const char* s)"
        node = MethodNode(
            name="method", qualified_name=qn, kind="method",
            argsstring=args, source=src,
        )
        _ensure_deterministic_uid(node)
        expected_uid = node.uid
        saved = node.save()
        try:
            assert saved.uid == expected_uid
            assert saved.uid == compute_uid(src, qn, normalize_argsstring(args))
        finally:
            saved.delete()

    def test_re_save_is_idempotent(self):
        """Re-saving with the same identity updates rather than duplicates."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        src = "test_integration"
        qn = "test_ns_idempotent"
        node1 = NamespaceNode(
            name=qn, qualified_name=qn, kind="namespace", source=src,
        )
        _ensure_deterministic_uid(node1)
        saved1 = node1.save()
        try:
            uid1 = saved1.uid

            # Re-save with same identity
            node2 = NamespaceNode(
                name=qn, qualified_name=qn, kind="namespace", source=src,
            )
            _ensure_deterministic_uid(node2)
            saved2 = node2.save()
            try:
                # Same uid (deterministic)
                assert saved2.uid == uid1
                # Should be the same Neo4j node (MERGE, not CREATE)
                assert saved2.element_id == saved1.element_id
            finally:
                saved2.delete()
        finally:
            saved1.delete()

    def test_different_sources_get_different_nodes(self):
        """Same qname, different source → different Neo4j nodes."""
        from doxygen_index.neo4j_backend import _ensure_deterministic_uid

        qn = "shared_ns"
        node_a = NamespaceNode(
            name=qn, qualified_name=qn, kind="namespace", source="src_a",
        )
        node_b = NamespaceNode(
            name=qn, qualified_name=qn, kind="namespace", source="src_b",
        )
        _ensure_deterministic_uid(node_a)
        _ensure_deterministic_uid(node_b)

        assert node_a.uid != node_b.uid

        saved_a = node_a.save()
        try:
            saved_b = node_b.save()
            try:
                assert saved_a.element_id != saved_b.element_id
                assert saved_a.uid != saved_b.uid
            finally:
                saved_b.delete()
        finally:
            saved_a.delete()
