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


class TestParseDescriptionLineFidelity:
    """Descriptions must preserve line endings so they can be re-emitted."""

    def test_paragraphs_separated_by_blank_line(self):
        elem = ET.fromstring(
            "<detaileddescription>"
            "<para>First paragraph.</para>"
            "<para>Second paragraph.</para>"
            "</detaileddescription>"
        )
        assert parse_description(elem) == "First paragraph.\n\nSecond paragraph."

    def test_param_return_throws_directives(self):
        elem = ET.fromstring(
            "<detaileddescription>"
            "<para><parameterlist kind=\"param\">"
            "<parameteritem><parameternamelist><parametername>db</parametername>"
            "</parameternamelist><parameterdescription><para>Reference to the database</para>"
            "</parameterdescription></parameteritem>"
            "</parameterlist>"
            "<simplesect kind=\"return\"><para>Optional containing the object, or empty if not found</para>"
            "</simplesect>"
            "<parameterlist kind=\"exception\">"
            "<parameteritem><parameternamelist><parametername>TransactionError</parametername>"
            "</parameternamelist><parameterdescription><para>if BEGIN fails</para>"
            "</parameterdescription></parameteritem>"
            "</parameterlist>"
            "</para>"
            "</detaileddescription>"
        )
        assert parse_description(elem) == (
            "\\param db Reference to the database\n"
            "\\return Optional containing the object, or empty if not found\n"
            "\\throws TransactionError if BEGIN fails"
        )

    def test_note_and_warning_simplesect(self):
        elem = ET.fromstring(
            "<detaileddescription>"
            "<para>Prose line.</para>"
            "<para><simplesect kind=\"note\"><para>Use this carefully.</para></simplesect></para>"
            "<para><simplesect kind=\"warning\"><para>Danger.</para></simplesect></para>"
            "</detaileddescription>"
        )
        assert parse_description(elem) == (
            "Prose line.\n\n"
            "\\note Use this carefully.\n\n"
            "\\warning Danger."
        )

    def test_returns_mapped_to_return(self):
        elem = ET.fromstring(
            "<detaileddescription><para><simplesect kind=\"returns\">"
            "<para>true if active</para></simplesect></para></detaileddescription>"
        )
        assert parse_description(elem) == "\\return true if active"

    def test_programlisting_lines_preserved(self):
        elem = ET.fromstring(
            "<detaileddescription><para><programlisting><codeline>"
            "<highlight class=\"keywordtype\">int</highlight><highlight class=\"normal\"> x = 1;</highlight>"
            "</codeline><codeline><highlight class=\"normal\">int y = 2;</highlight></codeline>"
            "</programlisting></para></detaileddescription>"
        )
        assert parse_description(elem) == "int x = 1;\nint y = 2;"

    def test_verbatim_preserved(self):
        elem = ET.fromstring(
            "<detaileddescription><para><verbatim>line one\nline two</verbatim></para></detaileddescription>"
        )
        assert parse_description(elem) == "line one\nline two"

    def test_linebreak_element(self):
        elem = ET.fromstring(
            "<detaileddescription><para>first<linebreak/>second</para></detaileddescription>"
        )
        assert parse_description(elem) == "first\nsecond"

    def test_inline_refs_flattened(self):
        elem = ET.fromstring(
            "<detaileddescription><para>See <ref refid=\"x\">Foo</ref> for details.</para></detaileddescription>"
        )
        assert parse_description(elem) == "See Foo for details."

    def test_itemized_and_ordered_lists(self):
        elem = ET.fromstring(
            "<detaileddescription><para><itemizedlist>"
            "<listitem><para>First feature</para></listitem>"
            "<listitem><para>Second feature</para></listitem>"
            "</itemizedlist></para>"
            "<para><orderedlist>"
            "<listitem><para>ordered one</para></listitem>"
            "<listitem><para>ordered two</para></listitem>"
            "</orderedlist></para></detaileddescription>"
        )
        assert parse_description(elem) == (
            "- First feature\n- Second feature\n\n"
            "1. ordered one\n2. ordered two"
        )

    def test_section_title_and_content(self):
        elem = ET.fromstring(
            "<detaileddescription><sect1><title>A heading</title>"
            "<para>Content paragraph.</para></sect1></detaileddescription>"
        )
        assert parse_description(elem) == "A heading\n\nContent paragraph."

    def test_brief_multiline_newline_preserved(self):
        elem = ET.fromstring(
            "<briefdescription><para>Multi-line brief:\ncontinues here.</para></briefdescription>"
        )
        assert parse_description(elem) == "Multi-line brief:\ncontinues here."


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
        # No bodystart/bodyend in this fixture (method declaration only, not definition)
        assert fn.body_start == 0
        assert fn.body_end == 0

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


class TestNormalizeArgsstring:
    def test_empty(self):
        from doxygen_index.parser import normalize_argsstring
        assert normalize_argsstring("") == "()"
        assert normalize_argsstring("()") == "()"
        assert normalize_argsstring("(void)") == "()"

    def test_simple_types(self):
        from doxygen_index.parser import normalize_argsstring
        assert normalize_argsstring("(int)") == "(int)"
        assert normalize_argsstring("(int, float)") == "(int, float)"

    def test_strips_param_names(self):
        from doxygen_index.parser import normalize_argsstring
        assert normalize_argsstring("(int x, const char* str)") == "(int, const char*)"
        assert normalize_argsstring("(double val, int count)") == "(double, int)"

    def test_preserves_qualifiers(self):
        from doxygen_index.parser import normalize_argsstring
        assert normalize_argsstring("(const Foo& foo)") == "(const Foo&)"
        assert normalize_argsstring("(volatile int* ptr)") == "(volatile int*)"

    def test_function_pointer(self):
        from doxygen_index.parser import normalize_argsstring
        assert normalize_argsstring("(int (*callback)(int))") == "(int (*callback)(int))"


class TestDeriveModule:
    def test_namespaced(self):
        from doxygen_index.parser import derive_module
        assert derive_module("myns::MyClass") == "myns"
        assert derive_module("ns1::ns2::ClassName") == "ns1::ns2"

    def test_top_level(self):
        from doxygen_index.parser import derive_module
        assert derive_module("MyClass") == ""
        assert derive_module("") == ""


class TestDeriveSourceType:
    def test_header(self):
        from doxygen_index.parser import derive_source_type
        assert derive_source_type("src/Foo.h") == "header"
        assert derive_source_type("include/bar.hpp") == "header"

    def test_source(self):
        from doxygen_index.parser import derive_source_type
        assert derive_source_type("src/Foo.cpp") == "source"
        assert derive_source_type("tests/test.c") == "source"

    def test_unknown(self):
        from doxygen_index.parser import derive_source_type
        assert derive_source_type("") == ""
        assert derive_source_type("README.md") == ""


class TestDetectTemplateSpecialization:
    def test_simple_specialization(self):
        from doxygen_index.parser import detect_template_specialization
        assert detect_template_specialization("std::vector<int>") == (True, "std::vector")
        assert detect_template_specialization("ns::Foo<Bar>") == (True, "ns::Foo")

    def test_no_specialization(self):
        from doxygen_index.parser import detect_template_specialization
        assert detect_template_specialization("MyClass") == (False, "")
        assert detect_template_specialization("ns::Foo") == (False, "")
        assert detect_template_specialization("Foo") == (False, "")

    def test_nested_angle_brackets(self):
        from doxygen_index.parser import detect_template_specialization
        assert detect_template_specialization(
            "IsVector< std::vector< T, Allocator > >") == (True, "IsVector")
        assert detect_template_specialization(
            "cpp_sqlite::ForeignKeyTypeT< ForeignKey< T > >") == (True, "cpp_sqlite::ForeignKeyTypeT")
        assert detect_template_specialization(
            "cpp_sqlite::GetRepeatedFieldParams< RepeatedFieldTransferObject< T > >") == (
            True, "cpp_sqlite::GetRepeatedFieldParams")

    def test_spaced_brackets(self):
        from doxygen_index.parser import detect_template_specialization
        assert detect_template_specialization("std::vector< int >") == (True, "std::vector")


class TestParseTemplateParams:
    def test_none_input(self):
        from doxygen_index.parser import parse_template_params
        import xml.etree.ElementTree as ET
        assert parse_template_params(None) == []

    def test_single_param_with_constraint(self):
        from doxygen_index.parser import parse_template_params
        import xml.etree.ElementTree as ET
        xml_str = '''<templateparamlist>
            <param>
                <type>ValidTransferObject</type>
                <declname>T</declname>
                <defname>T</defname>
            </param>
        </templateparamlist>'''
        elem = ET.fromstring(xml_str)
        params = parse_template_params(elem)
        assert len(params) == 1
        assert params[0].type_constraint == "ValidTransferObject"
        assert params[0].declname == "T"
        assert params[0].defname == "T"

    def test_typename_param(self):
        from doxygen_index.parser import parse_template_params
        import xml.etree.ElementTree as ET
        xml_str = '''<templateparamlist>
            <param>
                <type>typename T</type>
            </param>
        </templateparamlist>'''
        elem = ET.fromstring(xml_str)
        params = parse_template_params(elem)
        assert len(params) == 1
        assert params[0].type_constraint == "typename T"
        assert params[0].declname == ""

    def test_multiple_params(self):
        from doxygen_index.parser import parse_template_params
        import xml.etree.ElementTree as ET
        xml_str = '''<templateparamlist>
            <param>
                <type>typename T</type>
                <declname>T</declname>
                <defname>T</defname>
            </param>
            <param>
                <type>typename Allocator</type>
            </param>
        </templateparamlist>'''
        elem = ET.fromstring(xml_str)
        params = parse_template_params(elem)
        assert len(params) == 2
        assert params[0].type_constraint == "typename T"
        assert params[1].type_constraint == "typename Allocator"


class TestParseConceptXml:
    """Test parsing of C++20 concept compounds."""

    @pytest.fixture
    def concept_xml_dir(self, tmp_path):
        """Create a minimal Doxygen XML with a concept compound."""
        (tmp_path / "index.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<doxygenindex>\n'
            '  <compound refid="conceptns_1_1MyConcept" kind="concept">\n'
            '    <name>ns::MyConcept</name>\n'
            '  </compound>\n'
            '</doxygenindex>\n'
        )

        (tmp_path / "conceptns_1_1MyConcept.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<doxygen>\n'
            '  <compounddef id="conceptns_1_1MyConcept" kind="concept" language="C++">\n'
            '    <compoundname>ns::MyConcept</compoundname>\n'
            '    <templateparamlist>\n'
            '      <param>\n'
            '        <type>typename T</type>\n'
            '      </param>\n'
            '    </templateparamlist>\n'
            '    <initializer>template&lt;typename T&gt;\nconcept ns::MyConcept = std::integral&lt;T&gt;</initializer>\n'
            '    <briefdescription><para>A test concept.</para></briefdescription>\n'
            '    <detaileddescription/>\n'
            '    <location file="src/concept.hpp" line="10"/>\n'
            '  </compounddef>\n'
            '</doxygen>\n'
        )
        return tmp_path

    def test_concept_parsed(self, concept_xml_dir):
        from doxygen_index.parser import parse_xml_dir
        result = parse_xml_dir(concept_xml_dir, source="test", progress_interval=0)
        assert len(result.concepts) == 1
        concept = result.concepts[0]
        assert concept.qualified_name == "ns::MyConcept"
        assert concept.kind == "concept"
        assert len(result.template_param_refs) == 1
        tp = result.template_param_refs[0]
        assert tp.type_constraint == "typename T"
        assert tp.declname == ""


class TestParseTemplateClassXml:
    """Test parsing of class template parameters."""

    @pytest.fixture
    def template_xml_dir(self, tmp_path):
        """Create a minimal Doxygen XML with a template class."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classMyTemplate" kind="class">
                <name>MyTemplate</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "classMyTemplate.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classMyTemplate" kind="class" language="C++">
                <compoundname>MyTemplate</compoundname>
                <templateparamlist>
                  <param>
                    <type>typename T</type>
                    <declname>T</declname>
                    <defname>T</defname>
                  </param>
                  <param>
                    <type>int</type>
                    <declname>N</declname>
                    <defname>N</defname>
                    <defval>10</defval>
                  </param>
                </templateparamlist>
                <briefdescription><para>A template class.</para></briefdescription>
                <detaileddescription/>
                <location file="src/template.hpp" line="5"/>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def test_template_class_parsed(self, template_xml_dir):
        from doxygen_index.parser import parse_xml_dir
        result = parse_xml_dir(template_xml_dir, source="test", progress_interval=0)
        assert len(result.classes) == 1
        cls = result.classes[0]
        assert cls.qualified_name == "MyTemplate"
        # Template params are stored as relationship entries
        assert len(result.template_param_refs) == 2
        tp0 = result.template_param_refs[0]
        assert tp0.type_constraint == "typename T"
        assert tp0.declname == "T"
        assert tp0.from_refid == "classMyTemplate"
        assert tp0.position == 0
        tp1 = result.template_param_refs[1]
        assert tp1.type_constraint == "int"
        assert tp1.declname == "N"
        assert tp1.defval == "10"
        assert tp1.position == 1


class TestConceptConstraintResolution:
    """Test that type_constraint text resolves to concept qualified names."""

    @pytest.fixture
    def constraint_xml_dir(self, tmp_path):
        """Create Doxygen XML with a template class constrained by a concept."""
        (tmp_path / "index.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<doxygenindex>\n'
            '  <compound refid="classMyClass" kind="class">\n'
            '    <name>ns::MyClass</name>\n'
            '  </compound>\n'
            '  <compound refid="conceptns_1_1Valid" kind="concept">\n'
            '    <name>ns::Valid</name>\n'
            '  </compound>\n'
            '</doxygenindex>\n'
        )

        (tmp_path / "classMyClass.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<doxygen>\n'
            '  <compounddef id="classMyClass" kind="class" language="C++">\n'
            '    <compoundname>ns::MyClass</compoundname>\n'
            '    <templateparamlist>\n'
            '      <param>\n'
            '        <type>Valid</type>\n'
            '        <declname>T</declname>\n'
            '        <defname>T</defname>\n'
            '      </param>\n'
            '    </templateparamlist>\n'
            '    <location file="src/MyClass.hpp" line="5"/>\n'
            '  </compounddef>\n'
            '</doxygen>\n'
        )

        (tmp_path / "conceptns_1_1Valid.xml").write_text(
            '<?xml version="1.0"?>\n'
            '<doxygen>\n'
            '  <compounddef id="conceptns_1_1Valid" kind="concept" language="C++">\n'
            '    <compoundname>ns::Valid</compoundname>\n'
            '    <templateparamlist>\n'
            '      <param>\n'
            '        <type>typename T</type>\n'
            '      </param>\n'
            '    </templateparamlist>\n'
            '    <initializer>concept ns::Valid = true</initializer>\n'
            '    <location file="src/concept.hpp" line="5"/>\n'
            '  </compounddef>\n'
            '</doxygen>\n'
        )
        return tmp_path

    def test_concept_resolution(self, constraint_xml_dir):
        from doxygen_index.parser import parse_xml_dir
        result = parse_xml_dir(constraint_xml_dir, source="test", progress_interval=0)
        # Two template param refs: one from the class, one from the concept itself
        assert len(result.template_param_refs) == 2
        # Find the class's template param ref
        class_tp = [tp for tp in result.template_param_refs
                     if tp.from_refid == "classMyClass"][0]
        assert class_tp.type_constraint == "Valid"
        assert class_tp.concept_qualified_name == "ns::Valid"
        # The concept's own template param (typename T) should not resolve to a concept
        concept_tp = [tp for tp in result.template_param_refs
                      if tp.from_refid == "conceptns_1_1Valid"][0]
        assert concept_tp.type_constraint == "typename T"
        assert concept_tp.concept_qualified_name == ""


class TestParseTemplateSpecialization:
    """Test parsing of template specialization compounds."""

    @pytest.fixture
    def spec_xml_dir(self, tmp_path):
        """Create Doxygen XML with a primary template and its specialization."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="structFoo" kind="struct">
                <name>Foo</name>
              </compound>
              <compound refid="structFoo_3_01int_01_4" kind="struct">
                <name>Foo&lt; int &gt;</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "structFoo.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="structFoo" kind="struct" language="C++">
                <compoundname>Foo</compoundname>
                <templateparamlist>
                  <param><type>typename T</type></param>
                </templateparamlist>
                <location file="src/foo.hpp" line="1"/>
              </compounddef>
            </doxygen>
        """))

        (tmp_path / "structFoo_3_01int_01_4.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="structFoo_3_01int_01_4" kind="struct" language="C++">
                <compoundname>Foo&lt; int &gt;</compoundname>
                <location file="src/foo.hpp" line="10"/>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def test_specialization_detected(self, spec_xml_dir):
        from doxygen_index.parser import parse_xml_dir
        result = parse_xml_dir(spec_xml_dir, source="test", progress_interval=0)
        assert len(result.classes) == 2
        assert len(result.specializes_refs) == 1
        spec = result.specializes_refs[0]
        assert spec.from_qualified_name == "Foo< int >"
        assert spec.primary_template_qualified_name == "Foo"


class TestBodyLocationExtraction:
    """Test that bodystart/bodyend are parsed from <location> elements."""

    @pytest.fixture
    def method_with_body_xml(self, tmp_path):
        """Create Doxygen XML with a method that has a body location."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classWidget" kind="class">
                <name>Widget</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "classWidget.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classWidget" kind="class" language="C++">
                <compoundname>Widget</compoundname>
                <briefdescription><para>A widget.</para></briefdescription>
                <detaileddescription/>
                <location file="src/widget.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classWidget_1adraw"
                             prot="public" static="no" const="no">
                    <name>draw</name>
                    <qualifiedname>Widget::draw</qualifiedname>
                    <type>void</type>
                    <definition>void Widget::draw</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>Draw the widget.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/widget.cpp" line="10"
                             bodystart="10" bodyend="15"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def test_method_body_start_end_parsed(self, method_with_body_xml):
        result = parse_xml_dir(method_with_body_xml, source="test", progress_interval=0)
        assert len(result.methods) == 1
        method = result.methods[0]
        assert method.body_start == 10
        assert method.body_end == 15

    def test_method_no_body_defaults_zero(self, tmp_path):
        """Members without body locations should default to 0."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classFoo" kind="class">
                <name>Foo</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "classFoo.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classFoo" kind="class" language="C++">
                <compoundname>Foo</compoundname>
                <location file="src/foo.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classFoo_1abar"
                             prot="public" static="no" const="no">
                    <name>bar</name>
                    <qualifiedname>Foo::bar</qualifiedname>
                    <type>void</type>
                    <definition>void Foo::bar</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>A function.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/foo.h" line="10"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        result = parse_xml_dir(tmp_path, source="test", progress_interval=0)
        assert len(result.methods) == 1
        method = result.methods[0]
        assert method.body_start == 0
        assert method.body_end == 0

    def test_method_bodystart_negative_one(self, tmp_path):
        """bodystart=-1 means no implementation body."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classBar" kind="class">
                <name>Bar</name>
              </compound>
            </doxygenindex>
        """))

        (tmp_path / "classBar.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classBar" kind="class" language="C++">
                <compoundname>Bar</compoundname>
                <location file="src/bar.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classBar_1afunc"
                             prot="public" static="no" const="no">
                    <name>func</name>
                    <qualifiedname>Bar::func</qualifiedname>
                    <type>void</type>
                    <definition>void Bar::func</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>A pure virtual.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/bar.h" line="10" bodystart="-1" bodyend="-1"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        result = parse_xml_dir(tmp_path, source="test", progress_interval=0)
        assert len(result.methods) == 1
        method = result.methods[0]
        assert method.body_start is None or method.body_start == 0


class TestExtractImplementations:
    """Test implementation source extraction from body location data."""

    @pytest.fixture
    def xml_with_source(self, tmp_path):
        """Create Doxygen XML with methods that have body locations, plus source files."""
        # Create source file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "widget.cpp").write_text(
            "#include \"widget.h\"\n"
            "\n"
            "void Widget::draw() {\n"      # line 3
            "    canvas.begin();\n"       # line 4
            "    render();\n"              # line 5
            "    canvas.end();\n"         # line 6
            "}\n"                        # line 7
        )

        # Create Doxygen XML pointing to the source file
        # Note: bodystart/bodyend use 1-based line numbers
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classWidget" kind="class">
                <name>Widget</name>
              </compound>
            </doxygenindex>
        """))

        source_file = str(src_dir / "widget.cpp")
        (tmp_path / "classWidget.xml").write_text(textwrap.dedent(f"""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classWidget" kind="class" language="C++">
                <compoundname>Widget</compoundname>
                <briefdescription><para>A widget.</para></briefdescription>
                <detaileddescription/>
                <location file="src/widget.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classWidget_1adraw"
                             prot="public" static="no" const="no">
                    <name>draw</name>
                    <qualifiedname>Widget::draw</qualifiedname>
                    <type>void</type>
                    <definition>void Widget::draw</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>Draw the widget.</para></briefdescription>
                    <detaileddescription/>
                    <location file="{source_file}" line="3"
                             bodystart="3" bodyend="7"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def test_extract_creates_implementation_node(self, xml_with_source):
        """Members with body locations should produce ImplementationNodes."""
        result = parse_xml_dir(xml_with_source, source="test", progress_interval=0)
        assert len(result.implementations) == 1
        impl = result.implementations[0]
        assert impl.kind == "implementation"
        assert "void Widget::draw()" in impl.implementation
        assert impl.impl_embedding == []  # Embeddings deferred

    def test_extract_implementation_ref_links_member(self, xml_with_source):
        """ImplementationRef should link member refid to ImplementationNode."""
        result = parse_xml_dir(xml_with_source, source="test", progress_interval=0)
        assert len(result.implementation_refs) == 1
        ref = result.implementation_refs[0]
        method = result.methods[0]
        assert ref.member_refid == method.refid

    def test_extract_implementation_source_text(self, xml_with_source):
        """ImplementationNode.implementation should contain the correct source lines."""
        result = parse_xml_dir(xml_with_source, source="test", progress_interval=0)
        impl = result.implementations[0]
        # Lines 3-7 from the source file (1-based, inclusive)
        lines = impl.implementation.split("\n")
        assert len(lines) >= 5  # 5 lines of code
        assert "void Widget::draw()" in lines[0]

    def test_extract_skips_members_without_body(self, tmp_path):
        """Members without body locations should not produce implementations."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classFoo" kind="class">
                <name>Foo</name>
              </compound>
            </doxygenindex>
        """))

        # Method with no bodystart/bodyend
        (tmp_path / "classFoo.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classFoo" kind="class" language="C++">
                <compoundname>Foo</compoundname>
                <location file="src/foo.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classFoo_1anoBody"
                             prot="public" static="no" const="no">
                    <name>noBody</name>
                    <qualifiedname>Foo::noBody</qualifiedname>
                    <type>void</type>
                    <definition>void Foo::noBody</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>No body.</para></briefdescription>
                    <detaileddescription/>
                    <location file="src/foo.h" line="10"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        result = parse_xml_dir(tmp_path, source="test", progress_interval=0)
        assert len(result.implementations) == 0

    def test_extract_skips_missing_source_file(self, tmp_path):
        """Source files that don't exist should be skipped gracefully."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="classMissing" kind="class">
                <name>Missing</name>
              </compound>
            </doxygenindex>
        """))

        source_file = "/nonexistent/path/missing.cpp"
        (tmp_path / "classMissing.xml").write_text(textwrap.dedent(f"""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classMissing" kind="class" language="C++">
                <compoundname>Missing</compoundname>
                <location file="src/missing.h" line="5"/>
                <sectiondef kind="public-func">
                  <memberdef kind="function" id="classMissing_1amissing"
                             prot="public" static="no" const="no">
                    <name>missing</name>
                    <qualifiedname>Missing::missing</qualifiedname>
                    <type>void</type>
                    <definition>void Missing::missing</definition>
                    <argsstring>()</argsstring>
                    <briefdescription><para>Missing.</para></briefdescription>
                    <detaileddescription/>
                    <location file="{source_file}" line="10"
                             bodystart="10" bodyend="15"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))
        result = parse_xml_dir(tmp_path, source="test", progress_interval=0)
        assert len(result.implementations) == 0  # File not found, skipped


class TestProjectConfig:
    """Tests for .doxygen-index.toml loading (project.py)."""

    def test_load_cpp_config(self, tmp_path):
        """Loading a C++ project config."""
        from doxygen_index.project import load_config

        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            name = "mylib"
            input_paths = ["include", "src"]
        """))
        (tmp_path / "include").mkdir()
        (tmp_path / "src").mkdir()

        config, config_dir = load_config(tmp_path)
        assert config.name == "mylib"
        assert config.language == "cpp"  # default
        assert len(config.input_paths) == 2
        assert config.input_paths[0] == (tmp_path / "include").resolve()
        assert config_dir == tmp_path.resolve()

    def test_load_python_config(self, tmp_path):
        """Loading a Python project config."""
        from doxygen_index.project import load_config

        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            name = "myapp"
            language = "python"
            input_paths = ["src"]
            exclude_patterns = "build dist"
        """))
        (tmp_path / "src").mkdir()

        config, _ = load_config(tmp_path)
        assert config.name == "myapp"
        assert config.language == "python"
        assert config.exclude_patterns == "build dist"

    def test_missing_config_exits(self, tmp_path):
        """Missing config file should exit with an error."""
        from doxygen_index.project import load_config

        with pytest.raises(SystemExit):
            load_config(tmp_path)

    def test_missing_name_exits(self, tmp_path):
        """Config without 'name' should exit."""
        from doxygen_index.project import load_config

        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            input_paths = ["src"]
        """))
        with pytest.raises(SystemExit):
            load_config(tmp_path)

    def test_requirements_dir_resolved(self, tmp_path):
        """requirements_dir should be resolved relative to the config file."""
        from doxygen_index.project import load_config

        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            name = "myapp"
            language = "python"
            input_paths = ["src"]
            requirements_dir = "codegraph/requirements/"
        """))
        (tmp_path / "src").mkdir()
        (tmp_path / "codegraph" / "requirements").mkdir(parents=True)

        config, _ = load_config(tmp_path)
        assert config.requirements_dir == (tmp_path / "codegraph" / "requirements").resolve()

    def test_requirements_dir_none_by_default(self, tmp_path):
        """requirements_dir should be None when not specified."""
        from doxygen_index.project import load_config

        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            name = "myapp"
            language = "python"
            input_paths = ["src"]
        """))
        (tmp_path / "src").mkdir()

        config, _ = load_config(tmp_path)
        assert config.requirements_dir is None


class TestPythonParsing:
    """Tests for Python source parsing via the CLI project command."""

    @pytest.fixture
    def py_project(self, tmp_path):
        """Create a small Python project with a .doxygen-index.toml."""
        (tmp_path / ".doxygen-index.toml").write_text(textwrap.dedent("""\
            [project]
            name = "test_py"
            language = "python"
            input_paths = ["src"]
        """))
        src = tmp_path / "src" / "mypkg"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text('"""My package."""\n')
        (src / "core.py").write_text(textwrap.dedent("""\
            \"\"\"Core module.\"\"\"

            class Widget:
                \"\"\"A widget.\"\"\"

                def __init__(self, name: str):
                    self.name = name

                def render(self) -> str:
                    return self.name

            def factory() -> Widget:
                \"\"\"Create a widget.\"\"\"
                return Widget("default")
        """))
        return tmp_path

    def test_parse_python_dir_basic(self, py_project):
        """parse_python_dir should extract classes, methods, and functions."""
        from doxygen_index.parser import parse_python_dir

        result = parse_python_dir(
            py_project / "src",
            source="test_py",
            progress_interval=0,
        )
        assert len(result.classes) == 1
        assert result.classes[0].name == "Widget"
        assert len(result.methods) == 2  # __init__ + render
        assert len(result.functions) == 1  # factory

    def test_parse_python_dir_multiple_paths(self, py_project):
        """parse_python_dir should accept a list of paths."""
        from doxygen_index.parser import parse_python_dir

        # Add a second source dir
        second = py_project / "extra"
        second.mkdir()
        (second / "helper.py").write_text(textwrap.dedent("""\
            def helper():
                pass
        """))

        result = parse_python_dir(
            [py_project / "src", second],
            source="test_py",
            progress_interval=0,
        )
        assert len(result.functions) >= 2  # factory + helper

    def test_exclude_dirs(self, py_project):
        """Files in excluded directories should be skipped."""
        from doxygen_index.parser import parse_python_dir

        # Create a .venv with a .py file
        venv = py_project / "src" / ".venv"
        venv.mkdir()
        (venv / "junk.py").write_text("class ShouldNotAppear:\n    pass\n")

        result = parse_python_dir(
            py_project / "src",
            source="test_py",
            progress_interval=0,
        )
        class_names = [c.name for c in result.classes]
        assert "Widget" in class_names
        assert "ShouldNotAppear" not in class_names

    def test_custom_exclude_dirs(self, py_project):
        """User-specified exclude_dirs should be respected."""
        from doxygen_index.parser import parse_python_dir

        # Create a 'custom_excl' dir with a .py file
        excl = py_project / "src" / "custom_excl"
        excl.mkdir()
        (excl / "excluded.py").write_text("class Excluded:\n    pass\n")

        result = parse_python_dir(
            py_project / "src",
            source="test_py",
            progress_interval=0,
            exclude_dirs=["custom_excl"],
        )
        class_names = [c.name for c in result.classes]
        assert "Excluded" not in class_names


class TestGraphJson:
    """Tests for ParseResult → LayerGraph JSON conversion."""

    def test_merged_dependency_namespace_does_not_collide_with_project_namespace(self):
        """Dependency ``std`` must retain its namespace composition edges."""
        from codegraph import ClassNode, NamespaceNode
        from doxygen_index.graph_json import result_to_graph_json
        from doxygen_index.parser.model import CompositionEntry

        project_std = NamespaceNode(
            refid="project:std", name="std", qualified_name="std",
            source="cpp-sqlite",
        )
        dependency_std = NamespaceNode(
            refid="cppreference:ns/std", name="std", qualified_name="std",
            source="cppreference", tags=["dependency"],
        )
        vector = ClassNode(
            refid="cppreference:cpp/container/vector", name="vector",
            qualified_name="std::vector", source="cppreference",
            tags=["dependency"],
        )
        result = ParseResult(
            namespaces=[project_std, dependency_std],
            classes=[vector],
            compositions=[CompositionEntry(
                parent_refid=dependency_std.refid,
                child_refid=vector.refid,
                child_type="ClassNode",
            )],
        )

        graph = result_to_graph_json(result, source="cpp-sqlite", text_scan=False)
        std_nodes = [n for n in graph if n.get("qualified_name") == "std"]
        assert len(std_nodes) == 2
        dependency_entry = next(n for n in std_nodes if n["source"] == "cppreference")
        assert dependency_entry["canonical_key"] != next(
            n["canonical_key"] for n in std_nodes if n["source"] == "cpp-sqlite"
        )
        assert any(
            edge["target_key"] == next(
                n["canonical_key"] for n in graph
                if n.get("qualified_name") == "std::vector"
            )
            for edge in dependency_entry.get("edges", [])
            if edge["relation_type"] == "COMPOSES"
        )

    def test_result_to_graph_json(self, tmp_path):
        """result_to_graph_json should produce valid LayerGraph-compatible JSON."""
        from doxygen_index.parser import parse_python_dir
        from doxygen_index.graph_json import result_to_graph_json

        # Create a small Python project
        src = tmp_path / "src" / "mypkg"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text('"""My package."""\n')
        (src / "core.py").write_text(textwrap.dedent("""\
            class Widget:
                def __init__(self):
                    pass
                def render(self):
                    pass

            def factory():
                return Widget()
        """))

        result = parse_python_dir(tmp_path / "src", source="test", progress_interval=0)
        graph_data = result_to_graph_json(result, source="test")

        # Should be a list of node dicts
        assert isinstance(graph_data, list)
        assert len(graph_data) > 0

        # Each entry should have type and tags
        for entry in graph_data:
            assert "type" in entry
            assert "tags" in entry
            assert entry["tags"] == ["as-built"]

        # Should contain a ClassNode for Widget
        types = [e["type"] for e in graph_data]
        assert "ClassNode" in types

        # Should have COMPOSES edges from class to methods
        has_composes = any(
            any(e.get("relation_type") == "COMPOSES" for e in entry.get("edges", []))
            for entry in graph_data
        )
        assert has_composes

    def test_graph_json_deserializable(self, tmp_path):
        """The JSON should be consumable by LayerGraph.deserialize."""
        from codegraph.graph import LayerGraph
        from doxygen_index.parser import parse_python_dir
        from doxygen_index.graph_json import result_to_graph_json

        src = tmp_path / "src"
        src.mkdir()
        (src / "mod.py").write_text(textwrap.dedent("""\
            class Foo:
                def bar(self):
                    pass
        """))

        result = parse_python_dir(tmp_path / "src", source="test", progress_interval=0)
        graph_data = result_to_graph_json(result, source="test")

        # This should not raise
        graph = LayerGraph.deserialize(graph_data)
        assert len(graph.entries) > 0


class TestEnumValueInitializerParsing:
    """Enum values capture the Doxygen <initializer> (Phase 0 codegen blocker)."""

    @pytest.fixture
    def enum_xml_dir(self, tmp_path):
        """A minimal Doxygen XML directory with an enum carrying explicit values."""
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="enumColor" kind="enum">
                <name>Color</name>
              </compound>
              <compound refid="color_8h" kind="file">
                <name>color.h</name>
              </compound>
            </doxygenindex>
        """))

        # enumColor.xml
        (tmp_path / "enumColor.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="enumColor" kind="enum" language="C++">
                <compoundname>palette::Color</compoundname>
                <location file="src/color.h" line="5"/>
                <sectiondef kind="public-type">
                  <memberdef kind="enumvalue" id="enumColor_1aRed"
                             prot="public">
                    <name>Red</name>
                    <qualifiedname>palette::Color::Red</qualifiedname>
                    <initializer>= 1</initializer>
                    <briefdescription><para>Red.</para></briefdescription>
                    <location file="src/color.h" line="6"/>
                  </memberdef>
                  <memberdef kind="enumvalue" id="enumColor_1aGreen"
                             prot="public">
                    <name>Green</name>
                    <qualifiedname>palette::Color::Green</qualifiedname>
                    <initializer>= 1 &lt;&lt; 2</initializer>
                    <location file="src/color.h" line="7"/>
                  </memberdef>
                  <memberdef kind="enumvalue" id="enumColor_1aBlue"
                             prot="public">
                    <name>Blue</name>
                    <qualifiedname>palette::Color::Blue</qualifiedname>
                    <location file="src/color.h" line="8"/>
                  </memberdef>
                </sectiondef>
              </compounddef>
            </doxygen>
        """))

        # color_8h.xml (file compound — files are created from file
        # compounddefs, not from <location> elements)
        (tmp_path / "color_8h.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="color_8h" kind="file" language="C++">
                <compoundname>color.h</compoundname>
                <location file="src/color.h"/>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def test_enum_value_initializers_captured(self, enum_xml_dir):
        result = parse_xml_dir(enum_xml_dir, source="test", progress_interval=0)

        assert len(result.enums) == 1
        enum = result.enums[0]
        assert enum.name == "Color"
        assert enum.qualified_name == "palette::Color"

        assert len(result.enum_values) == 3
        by_name = {v.name: v for v in result.enum_values}
        assert by_name["Red"].initializer == "= 1"
        assert by_name["Green"].initializer == "= 1 << 2"
        # No <initializer> element → implicit sequential value
        assert by_name["Blue"].initializer == ""

    def test_enum_values_composed_by_enum(self, enum_xml_dir):
        """Enum values nest under their EnumNode in the LayerGraph, and the
        initializer survives the JSON → LayerGraph round-trip (codegen's
        input path)."""
        result = parse_xml_dir(enum_xml_dir, source="test", progress_interval=0)
        from codegraph.graph import LayerGraph
        from doxygen_index.graph_json import result_to_graph_json

        graph_data = result_to_graph_json(result, source="test")
        graph = LayerGraph.deserialize(graph_data)

        flat = graph._flat_index()
        enum_entries = [
            e for e in flat.values()
            if type(e.node).__name__ == "EnumNode"
        ]
        assert len(enum_entries) == 1
        enum_entry = enum_entries[0]
        values = list(enum_entry.children.get("EnumValueNode", {}).values())
        assert len(values) == 3
        assert all(
            type(e.node).__name__ == "EnumValueNode" for e in values
        )
        by_name = {e.node.name: e.node for e in values}
        assert by_name["Red"].initializer == "= 1"
        assert by_name["Green"].initializer == "= 1 << 2"
        assert by_name["Blue"].initializer == ""

    def test_file_language_normalized_to_cpp(self, enum_xml_dir):
        """Doxygen's language="C++" normalizes to the canonical "cpp"."""
        result = parse_xml_dir(enum_xml_dir, source="test", progress_interval=0)
        assert len(result.files) == 1
        assert result.files[0].path == "src/color.h"
        assert result.files[0].language == "cpp"




class TestCatchClauseDeps:
    """Catch-clause exception types become DEPENDS_ON edges."""

    #: <codeline> lines shared across fixture variants
    TRY_OPEN = '                  <codeline lineno="12"><highlight class="normal"><sp />try</highlight></codeline>'
    TRY_BODY = '                  <codeline lineno="13"><highlight class="normal"><sp />{</highlight></codeline>\n'                '                  <codeline lineno="14"><highlight class="normal"><sp /><sp />work();</highlight></codeline>\n'                '                  <codeline lineno="15"><highlight class="normal"><sp />}</highlight></codeline>'
    FUNC_HEAD = '                  <codeline lineno="10"><highlight class="keywordtype">int</highlight><highlight class="normal"><sp /><ref refid="app_8h_1aRun" kindref="member">app::run</ref>()</highlight></codeline>\n'                 '                  <codeline lineno="11"><highlight class="normal">{</highlight></codeline>'

    @pytest.fixture
    def xml_dir(self, tmp_path):
        (tmp_path / "index.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygenindex>
              <compound refid="app_8h" kind="file"><name>app.h</name></compound>
              <compound refid="classapp_1_1AppError" kind="class"><name>AppError</name></compound>
            </doxygenindex>
        """))
        (tmp_path / "classapp_1_1AppError.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="classapp_1_1AppError" kind="class" language="C++">
                <compoundname>app::AppError</compoundname>
                <location file="src/app.h" line="5"/>
              </compounddef>
            </doxygen>
        """))
        return tmp_path

    def _write_file(self, xml_dir, body):
        (Path(xml_dir) / "app_8h.xml").write_text(textwrap.dedent("""\
            <?xml version="1.0"?>
            <doxygen>
              <compounddef id="app_8h" kind="file" language="C++">
                <compoundname>app.h</compoundname>
                <location file="src/app.h"/>
                <sectiondef kind="func">
                  <memberdef kind="function" id="app_8h_1aRun"
                             prot="public" static="no" const="no"
                             explicit="no" inline="no" virt="non-virtual">
                    <name>run</name>
                    <qualifiedname>app::run</qualifiedname>
                    <type>int</type>
                    <definition>int app::run</definition>
                    <argsstring>()</argsstring>
                    <location file="src/app.h" line="10"
                              bodystart="10" bodyend="30"/>
                  </memberdef>
                </sectiondef>
                <programlisting>
        """) + body + "\n                </programlisting>\n              </compounddef>\n            </doxygen>\n        ")

    def _body(self, catch_lines, extra=None):
        lines = [self.FUNC_HEAD, self.TRY_OPEN, self.TRY_BODY]
        if extra:
            lines.extend(extra)
        lines.extend(catch_lines)
        lines.append('                  <codeline lineno="90"><highlight class="normal">}</highlight></codeline>')
        return "\n".join(lines)

    def test_catch_with_ref_produces_depends_on(self, xml_dir):
        self._write_file(xml_dir, self._body([
            '                  <codeline lineno="16"><highlight class="normal"><sp />catch(const<ref refid="classapp_1_1AppError" kindref="compound">app::AppError</ref>&amp;e)</highlight></codeline>',
            '                  <codeline lineno="17"><highlight class="normal"><sp />{</highlight></codeline>',
            '                  <codeline lineno="18"><highlight class="normal"><sp /><sp />handle(e);</highlight></codeline>',
            '                  <codeline lineno="19"><highlight class="normal"><sp />}</highlight></codeline>',
        ]))
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)
        deps = [d for d in result.depends_on if d.to_refid == "classapp_1_1AppError"]
        assert len(deps) == 1, deps
        assert deps[0].from_refid == "app_8h_1aRun"

    def test_macro_catch_ignored(self, xml_dir):
        self._write_file(xml_dir, self._body([
            '                  <codeline lineno="16"><highlight class="normal"><sp />catch(const<ref refid="classapp_1_1AppError" kindref="compound">app::AppError</ref>&amp;e)</highlight></codeline>',
            '                  <codeline lineno="17"><highlight class="normal"><sp />{</highlight></codeline>',
            '                  <codeline lineno="18"><highlight class="normal"><sp /><sp />handle(e);</highlight></codeline>',
            '                  <codeline lineno="19"><highlight class="normal"><sp />}</highlight></codeline>',
        ], extra=[
            '                  <codeline lineno="7"><highlight class="normal">#define TRY_MACRO catch(app::AppError&amp;)</highlight></codeline>',
        ]))
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)
        clauses = [c for c in result.catch_clauses]
        # The macro catch (line 7) is skipped; only the real one remains.
        assert len(clauses) == 1, clauses
        assert clauses[0].from_refid == "app_8h_1aRun"
        assert clauses[0].to_refid == "classapp_1_1AppError"

    def test_ref_less_catch_of_parsed_class_resolves_by_name(self, xml_dir):
        self._write_file(xml_dir, self._body([
            '                  <codeline lineno="16"><highlight class="normal"><sp />catch(constapp::AppError&amp;e)</highlight></codeline>',
            '                  <codeline lineno="17"><highlight class="normal"><sp />{</highlight></codeline>',
            '                  <codeline lineno="18"><highlight class="normal"><sp /><sp />handle(e);</highlight></codeline>',
            '                  <codeline lineno="19"><highlight class="normal"><sp />}</highlight></codeline>',
        ]))
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)
        deps = [d for d in result.depends_on if d.to_refid == "classapp_1_1AppError"]
        assert len(deps) == 1, deps
        assert deps[0].from_refid == "app_8h_1aRun"
        assert deps[0].to_type == "ClassNode"

    def test_unresolvable_and_catch_all_produce_no_edges(self, xml_dir):
        self._write_file(xml_dir, self._body([
            '                  <codeline lineno="16"><highlight class="normal"><sp />catch(conststd::exception&amp;e)</highlight></codeline>',
            '                  <codeline lineno="17"><highlight class="normal"><sp />{</highlight></codeline>',
            '                  <codeline lineno="18"><highlight class="normal"><sp /><sp />report(e);</highlight></codeline>',
            '                  <codeline lineno="19"><highlight class="normal"><sp />}</highlight></codeline>',
            '                  <codeline lineno="20"><highlight class="normal"><sp />catch(...)</highlight></codeline>',
            '                  <codeline lineno="21"><highlight class="normal"><sp />{</highlight></codeline>',
            '                  <codeline lineno="22"><highlight class="normal"><sp /><sp />abort();</highlight></codeline>',
            '                  <codeline lineno="23"><highlight class="normal"><sp />}</highlight></codeline>',
        ]))
        result = parse_xml_dir(xml_dir, source="test", progress_interval=0)
        assert len(result.catch_clauses) == 2
        assert all(d.to_refid != "classapp_1_1AppError" for d in result.depends_on)
