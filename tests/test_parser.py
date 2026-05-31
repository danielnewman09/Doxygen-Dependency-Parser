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

        # Typed compound lists
        assert len(result.classes) == 1
        assert len(result.enums) == 0
        assert len(result.unions) == 0
        assert len(result.interfaces) == 0

        cls = result.classes[0]
        assert cls.name == "MyClass"
        assert cls.qualified_name == "myns::MyClass"
        assert cls.kind == "class"
        assert cls.source == "test"
        assert "test class" in cls.brief_description

        # Backward-compat properties
        assert len(result.compounds) == 1
        assert result.compounds[0] is cls

        # Typed member lists
        assert len(result.methods) == 1
        assert len(result.attributes) == 0
        assert len(result.enum_values) == 0
        assert len(result.defines) == 0
        assert len(result.functions) == 0

        fn = result.methods[0]
        assert fn.name == "doSomething"
        assert fn.compound_refid == "classMyClass"
        assert fn.source == "test"
        # qualified_name includes normalized argsstring for overload safety
        assert fn.qualified_name == "myns::MyClass::doSomething(double, int)"

        # Backward-compat members property
        assert len(result.members) == 1
        assert result.members[0] is fn

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


class TestNormalizeArgsstring:
    def test_empty(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("") == "()"
        assert _normalize_argsstring("()") == "()"
        assert _normalize_argsstring("(void)") == "()"

    def test_simple_types(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(int)") == "(int)"
        assert _normalize_argsstring("(int, float)") == "(int, float)"

    def test_strips_param_names(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(int x, const char* str)") == "(int, const char*)"
        assert _normalize_argsstring("(double val, int count)") == "(double, int)"

    def test_preserves_qualifiers(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(const Foo& foo)") == "(const Foo&)"
        assert _normalize_argsstring("(volatile int* ptr)") == "(volatile int*)"

    def test_function_pointer(self):
        from doxygen_index.parser import _normalize_argsstring
        assert _normalize_argsstring("(int (*callback)(int))") == "(int (*callback)(int))"


class TestDeriveModule:
    def test_namespaced(self):
        from doxygen_index.parser import _derive_module
        assert _derive_module("myns::MyClass") == "myns"
        assert _derive_module("ns1::ns2::ClassName") == "ns1::ns2"

    def test_top_level(self):
        from doxygen_index.parser import _derive_module
        assert _derive_module("MyClass") == ""
        assert _derive_module("") == ""


class TestDeriveSourceType:
    def test_header(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("src/Foo.h") == "header"
        assert _derive_source_type("include/bar.hpp") == "header"

    def test_source(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("src/Foo.cpp") == "source"
        assert _derive_source_type("tests/test.c") == "source"

    def test_unknown(self):
        from doxygen_index.parser import _derive_source_type
        assert _derive_source_type("") == ""
        assert _derive_source_type("README.md") == ""
