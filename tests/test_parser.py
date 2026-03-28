"""Tests for the Doxygen XML parser."""

import tempfile
import textwrap
from pathlib import Path

import pytest

from doxygen_index.parser import (
    ParseResult, get_text, parse_description, parse_xml_dir, parse_index,
)
import xml.etree.ElementTree as ET


class TestGetText:
    def test_none_returns_default(self):
        assert get_text(None) == ""
        assert get_text(None, "fallback") == "fallback"

    def test_simple_text(self):
        elem = ET.fromstring("<p>hello world</p>")
        assert get_text(elem) == "hello world"

    def test_nested_elements(self):
        elem = ET.fromstring("<p>hello <b>bold</b> world</p>")
        assert get_text(elem) == "hello bold world"

    def test_whitespace_normalization(self):
        elem = ET.fromstring("<p>hello   \n  world</p>")
        assert get_text(elem) == "hello world"


class TestParseDescription:
    def test_none(self):
        assert parse_description(None) == ""

    def test_simple(self):
        elem = ET.fromstring("<briefdescription><para>A brief description.</para></briefdescription>")
        assert "brief description" in parse_description(elem)


class TestParseXmlDir:
    """Integration tests using a minimal Doxygen XML fixture."""

    @pytest.fixture
    def xml_dir(self, tmp_path):
        """Create a minimal valid Doxygen XML directory."""
        # index.xml
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classMyClass" kind="class">
                <name>MyClass</name>
              </compound>
              <compound refid="math_8h" kind="file">
                <name>math.h</name>
              </compound>
            </doxygenindex>
        """))

        # classMyClass.xml
        (tmp_path / "classMyClass.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classMyClass" kind="class" language="C++">
                <compoundname>myns::MyClass</compoundname>
                <briefdescription><para>A test class.</para></briefdescription>
                <detaileddescription><para>Detailed info.</para></detaileddescription>
                <location file="src/MyClass.h" line="10"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classMyClass_1aDoSomething"
                             prot="public" static="no" const="no" virt="non-virtual">
                    <name>doSomething</name>
                    <qualifiedname>myns::MyClass::doSomething</qualifiedname>
                    <type>int</type>
                    <definition>int myns::MyClass::doSomething</definition>
                    <argsstring>(double x, int y)</argsstring>
                    <briefdescription><para>Does something.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/MyClass.cpp" line="25"/>
                    <param><type>double</type><declname>x</declname></param>
                    <param><type>int</type><declname>y</declname><defval>0</defval></param>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))

        # math_8h.xml (file compound)
        (tmp_path / "math_8h.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="math_8h" kind="file" language="C++">
                <compoundname>math.h</compoundname>
                <location file="src/math.h"/>
                <includes refid="classMyClass" local="yes">MyClass.h</includes>
              </compounddef>
            </doxygen>
        """))

        return tmp_path

    def test_parse_index(self, xml_dir):
        compounds = parse_index(xml_dir / "index.xml")
        assert len(compounds) == 2
        refids = {c[0] for c in compounds}
        assert "classMyClass" in refids
        assert "math_8h" in refids

    def test_parse_xml_dir(self, xml_dir):
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)

        assert isinstance(result, ParseResult)

        # Files
        assert len(result.files) == 1
        assert result.files[0].name == "math.h"
        assert result.files[0].source == "test"

        # Compounds
        assert len(result.compounds) == 1
        cls = result.compounds[0]
        assert cls.name == "MyClass"
        assert cls.qualified_name == "myns::MyClass"
        assert cls.kind == "class"
        assert cls.source == "test"
        assert "test class" in cls.brief_description

        # Members
        assert len(result.members) == 1
        fn = result.members[0]
        assert fn.name == "doSomething"
        assert fn.compound_refid == "classMyClass"
        assert fn.source == "test"

        # Parameters
        assert len(result.parameters) == 2
        assert result.parameters[0].name == "x"
        assert result.parameters[0].type == "double"
        assert result.parameters[1].name == "y"
        assert result.parameters[1].default_value == "0"

        # Includes
        assert len(result.includes) == 1
        assert result.includes[0].included_file == "MyClass.h"
        assert result.includes[0].is_local is True


class TestSqliteRoundTrip:
    """Test SQLite ingestion with a parsed result."""

    def test_round_trip(self, tmp_path):
        from doxygen_index.parser import (
            ParseResult, FileEntry, NamespaceEntry, CompoundEntry, MemberEntry,
        )
        from doxygen_index.sqlite_backend import create_schema, write_result

        import sqlite3

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        create_schema(conn)

        result = ParseResult(
            files=[FileEntry("f1", "test.h", "src/test.h", "C++", "mylib")],
            namespaces=[NamespaceEntry("ns1", "myns", "myns", "mylib")],
            compounds=[CompoundEntry(
                "c1", "class", "Foo", "myns::Foo", "", None,
                "A class.", "", [], False, False, "mylib",
            )],
            members=[MemberEntry(
                "m1", "c1", "function", "bar", "myns::Foo::bar",
                "void", "void myns::Foo::bar", "()", "", None,
                "Does bar.", "", "public",
                False, False, False, False, False, False, "mylib",
            )],
        )

        counts = write_result(conn, result)
        assert counts["files"] == 1
        assert counts["compounds"] == 1
        assert counts["members"] == 1

        # Verify source column
        row = conn.execute("SELECT source FROM compounds WHERE name = 'Foo'").fetchone()
        assert row[0] == "mylib"

        row = conn.execute("SELECT source FROM members WHERE name = 'bar'").fetchone()
        assert row[0] == "mylib"

        conn.close()


class TestDepsConfig:
    def test_builtin_configs_exist(self):
        from doxygen_index.deps_config import BUILTIN_CONFIGS, get_config
        assert "eigen" in BUILTIN_CONFIGS
        assert "sdl" in BUILTIN_CONFIGS

        config = get_config("eigen")
        assert config is not None
        assert config.subdir == "eigen3/Eigen"

    def test_override(self):
        from doxygen_index.deps_config import DepConfig, get_config
        overrides = {"eigen": DepConfig(subdir="custom/path")}
        config = get_config("eigen", overrides)
        assert config.subdir == "custom/path"

    def test_unknown_returns_none(self):
        from doxygen_index.deps_config import get_config
        assert get_config("nonexistent") is None
