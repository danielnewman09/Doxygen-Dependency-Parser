"""Unit tests for C++ GoogleTest parsing (Doxygen XML → TestNode).

These run entirely without Doxygen or Conan: a hand-written Doxygen XML
file compound (the shape Doxygen emits for ``TEST_F``/``TEST``/``TEST_P``
macros — file-level functions named after the macro, suite + test name
as ``<param>`` elements) is parsed together with the real source file
containing the test bodies.

Covers:
* test-node creation (name, qualified name, module, method),
* assertion extraction (ASSERT_*/EXPECT_* → AssertionNode with the
  macro as ``operator``),
* step extraction (setup/action blocks → TestStepNode with
  body_start/body_end),
* CALLEE / VERIFIES resolution against parsed methods,
* COMPOSES (test → assertion/step),
* non-gtest functions staying regular FunctionNodes.
"""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from doxygen_index.parser import parse_xml_dir


def _write_source(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "testDatabase.cpp"
    src.write_text(textwrap.dedent("""\
        #include "testDatabase.hpp"

        TEST_F(DatabaseTest, InsertProduct)
        {
          auto& db = cpp_sqlite::Database{":memory:", true};
          auto& dao = db.getDAO<Product>();
          dao.addToBuffer(Product{});
          ASSERT_NO_THROW(dao.insert());
          ASSERT_TRUE(dao.isInitialized())
            << "should be initialized";
        }

        TEST(Standalone, ChecksSomething)
        {
          auto result = compute(2);
          EXPECT_EQ(result, 4);
        }

        TEST_P(ParamTest, WithParams)
        {
          EXPECT_TRUE(true);
        }

        int plain_helper(int x) { return x + 1; }
    """))
    return src


def _write_xml(tmp_path: Path, src: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    xml_dir = tmp_path / "xml"
    xml_dir.mkdir(exist_ok=True)

    (xml_dir / "index.xml").write_text(textwrap.dedent("""\
        <?xml version="1.0"?>
        <doxygenindex>
          <compound refid="test__database_8cpp" kind="file">
            <name>testDatabase.cpp</name>
          </compound>
          <compound refid="classcpp__sqlite_1_1_dao" kind="class">
            <name>cpp_sqlite::DAO</name>
          </compound>
        </doxygenindex>
    """))

    # A small class whose methods the test calls — gives the name-based
    # CALLEE/VERIFIES resolution something to resolve against.
    (xml_dir / "classcpp__sqlite_1_1_dao.xml").write_text(textwrap.dedent("""\
        <?xml version="1.0"?>
        <doxygen>
          <compounddef id="classcpp__sqlite_1_1_dao" kind="class" language="C++">
            <compoundname>cpp_sqlite::DAO</compoundname>
            <sectiondef kind="public-func">
              <memberdef kind="function" id="classcpp__sqlite_1_1_dao_1a_getdao"
                         prot="public" static="no" const="no" virt="non-virtual">
                <name>getDAO</name>
                <type>auto</type>
                <argsstring>()</argsstring>
                <location file="{src}" line="1"/>
              </memberdef>
              <memberdef kind="function" id="classcpp__sqlite_1_1_dao_1a_insert"
                         prot="public" static="no" const="no" virt="non-virtual">
                <name>insert</name>
                <type>void</type>
                <argsstring>()</argsstring>
                <location file="{src}" line="1"/>
              </memberdef>
              <memberdef kind="function" id="classcpp__sqlite_1_1_dao_1a_isinit"
                         prot="public" static="no" const="no" virt="non-virtual">
                <name>isInitialized</name>
                <type>bool</type>
                <argsstring>()</argsstring>
                <location file="{src}" line="1"/>
              </memberdef>
            </sectiondef>
          </compounddef>
        </doxygen>
    """))

    (xml_dir / "test__database_8cpp.xml").write_text(textwrap.dedent(f"""\
        <?xml version="1.0"?>
        <doxygen>
          <compounddef id="test__database_8cpp" kind="file" language="C++">
            <compoundname>testDatabase.cpp</compoundname>
            <location file="{src}"/>
            <sectiondef kind="func">
              <memberdef kind="function" id="test__database_8cpp_1a_insert"
                         prot="public" static="no" const="no" virt="non-virtual">
                <definition>TEST_F</definition>
                <argsstring>(DatabaseTest, InsertProduct)</argsstring>
                <name>TEST_F</name>
                <param><type>DatabaseTest</type></param>
                <param><type>InsertProduct</type></param>
                <location file="{src}" line="3" bodyfile="{src}"
                          bodystart="3" bodyend="11"/>
              </memberdef>
              <memberdef kind="function" id="test__database_8cpp_1a_standalone"
                         prot="public" static="no" const="no" virt="non-virtual">
                <definition>TEST</definition>
                <argsstring>(Standalone, ChecksSomething)</argsstring>
                <name>TEST</name>
                <param><type>Standalone</type></param>
                <param><type>ChecksSomething</type></param>
                <location file="{src}" line="13" bodyfile="{src}"
                          bodystart="13" bodyend="17"/>
              </memberdef>
              <memberdef kind="function" id="test__database_8cpp_1a_param"
                         prot="public" static="no" const="no" virt="non-virtual">
                <definition>TEST_P</definition>
                <argsstring>(ParamTest, WithParams)</argsstring>
                <name>TEST_P</name>
                <param><type>ParamTest</type></param>
                <param><type>WithParams</type></param>
                <location file="{src}" line="19" bodyfile="{src}"
                          bodystart="19" bodyend="21"/>
              </memberdef>
              <memberdef kind="function" id="test__database_8cpp_1a_helper"
                         prot="public" static="no" const="no" virt="non-virtual">
                <definition>int plain_helper</definition>
                <argsstring>(int x)</argsstring>
                <name>plain_helper</name>
                <param><type>int</type><declname>x</declname></param>
                <location file="{src}" line="23" bodyfile="{src}"
                          bodystart="23" bodyend="23"/>
              </memberdef>
            </sectiondef>
          </compounddef>
        </doxygen>
    """))
    return xml_dir


@pytest.fixture
def parsed(tmp_path):
    src = _write_source(tmp_path)
    xml_dir = _write_xml(tmp_path, src)
    return parse_xml_dir(xml_dir, source="probe", layer="codebase")


class TestTestNodeExtraction:
    """gtest macros become TestNodes, not plain functions."""

    def test_test_nodes_created(self, parsed):
        assert len(parsed.tests) == 3
        names = {t.name for t in parsed.tests}
        assert names == {"InsertProduct", "ChecksSomething", "WithParams"}

    def test_qualified_names(self, parsed):
        qns = {t.qualified_name for t in parsed.tests}
        assert qns == {
            "testDatabase::DatabaseTest::InsertProduct",
            "testDatabase::Standalone::ChecksSomething",
            "testDatabase::ParamTest::WithParams",
        }

    def test_test_metadata(self, parsed):
        t = next(t for t in parsed.tests if t.name == "InsertProduct")
        assert t.kind == "test"
        assert t.test_name == "InsertProduct"
        assert t.test_module == "testDatabase"
        assert t.method == "automated"
        assert t.file_path.endswith("testDatabase.cpp")
        assert t.line_number == 3

    def test_plain_functions_unaffected(self, parsed):
        """Non-macro functions stay FunctionNodes (no test nodes)."""
        assert [f.name for f in parsed.functions] == ["plain_helper"]
        assert len(parsed.tests) == 3


class TestAssertionExtraction:
    """ASSERT_*/EXPECT_* statements become AssertionNodes."""

    def test_assertions_created(self, parsed):
        # InsertProduct: 2 (ASSERT_NO_THROW, ASSERT_TRUE)
        # ChecksSomething: 1 (EXPECT_EQ)
        # WithParams: 1 (EXPECT_TRUE)
        assert len(parsed.assertions) == 4

    def test_assertion_operators_and_order(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        insert_asserts = [
            a for a in parsed.assertions
            if a.qualified_name.startswith(insert.qualified_name)
        ]
        assert [a.operator for a in insert_asserts] == [
            "ASSERT_NO_THROW", "ASSERT_TRUE",
        ]
        assert [a.order for a in insert_asserts] == [0, 1]
        assert all(a.phase == "post" for a in insert_asserts)

    def test_assertion_line_number_and_source(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        first = next(
            a for a in parsed.assertions
            if a.qualified_name.startswith(insert.qualified_name)
            and a.order == 0
        )
        assert first.line_number == 8  # ASSERT_NO_THROW line


class TestStepExtraction:
    """Setup/action blocks between assertions become TestStepNodes."""

    def test_steps_created(self, parsed):
        assert len(parsed.test_steps) >= 3

    def test_setup_block(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        step0 = next(
            s for s in parsed.test_steps
            if s.qualified_name == f"{insert.qualified_name}::step_0"
        )
        assert step0.description == "Setup block"
        assert step0.body_start == 5   # first body line
        assert step0.body_end == 7     # last setup line (before assert)

    def test_action_block_after_assert(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        qnames = [
            s.qualified_name for s in parsed.test_steps
            if s.qualified_name.startswith(insert.qualified_name)
        ]
        assert f"{insert.qualified_name}::step_1" in qnames

    def test_steps_have_implementation_source(self, parsed):
        """Each step carries its source via HAS_IMPLEMENTATION refs."""
        impl_by_member = {
            r.member_refid: r.implementation
            for r in parsed.implementation_refs
        }
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        step0 = next(
            s for s in parsed.test_steps
            if s.qualified_name == f"{insert.qualified_name}::step_0"
        )
        impl = impl_by_member.get(step0.refid)
        assert impl is not None
        assert "getDAO" in impl.implementation
        assert "addToBuffer" in impl.implementation


class TestRelationships:
    """COMPOSES / CALLEE / VERIFIES edges are recorded."""

    def test_tests_compose_children(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        children = [
            tc.child_refid for tc in parsed.test_compositions
            if tc.parent_refid == insert.refid
        ]
        # 2 assertions + 2 steps
        assert len(children) == 4
        child_types = {
            tc.child_type for tc in parsed.test_compositions
            if tc.parent_refid == insert.refid
        }
        assert child_types == {"AssertionNode", "TestStepNode"}

    def test_verifies_resolve_to_methods(self, parsed):
        """VERIFIES edges hit the methods the test exercises.

        Both step-level calls (``db.getDAO<Product>()``) and calls
        inside assertions (``dao.insert()``, ``dao.isInitialized()``)
        resolve to the parsed DAO methods.
        """
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        targets = {
            (v.to_refid, v.to_type) for v in parsed.verifies
            if v.from_refid == insert.refid
        }
        method_refids = {
            m.refid for m in parsed.methods
            if m.name in ("getDAO", "insert", "isInitialized")
        }
        hit = targets & {(r, "MethodNode") for r in method_refids}
        assert len(hit) >= 3, (
            f"expected VERIFIES to hit getDAO/insert/isInitialized, got "
            f"{sorted(targets)}"
        )

    def test_callees_from_steps(self, parsed):
        insert = next(t for t in parsed.tests if t.name == "InsertProduct")
        step0 = next(
            s for s in parsed.test_steps
            if s.qualified_name == f"{insert.qualified_name}::step_0"
        )
        callees = [c for c in parsed.callees if c.from_refid == step0.refid]
        # getDAO / addToBuffer calls in the setup block
        assert len(callees) >= 1


def test_uid_determinism_across_parses(tmp_path):
    """Parsing twice yields identical uids (identity = qualified_name)."""
    src = _write_source(tmp_path / "a")
    xml_dir = _write_xml(tmp_path / "a", src)
    r1 = parse_xml_dir(xml_dir, source="probe", layer="codebase")
    src2 = _write_source(tmp_path / "b")
    xml_dir2 = _write_xml(tmp_path / "b", src2)
    r2 = parse_xml_dir(xml_dir2, source="probe", layer="codebase")

    qns1 = {t.qualified_name: t._compute_uid() for t in r1.tests}
    qns2 = {t.qualified_name: t._compute_uid() for t in r2.tests}
    assert qns1 == qns2
