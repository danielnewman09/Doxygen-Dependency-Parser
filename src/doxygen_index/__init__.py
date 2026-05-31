"""
doxygen-index: Index Doxygen XML and Conan C++ dependencies into Neo4j graph databases.

Discovers Conan dependency headers, generates Doxygen XML documentation,
and ingests the results into Neo4j for searchable code navigation.

Basic usage::

    from doxygen_index import discover_packages, generate_xml
    from doxygen_index.neo4j_backend import ingest as ingest_neo4j

    packages = discover_packages(build_type="Debug")
    xml_dirs = generate_xml(packages, output_dir="build/docs/deps")
    for name, xml_dir in xml_dirs.items():
        ingest_neo4j(xml_dir, source=name, uri="bolt://localhost:7687")
"""

__version__ = "0.1.0"

from doxygen_index.conan import discover_packages
from doxygen_index.doxygen import generate_xml, generate_doxyfile
from doxygen_index.parser import parse_xml_dir
from doxygen_index.tools import create_toolset, DependencyGraphTools

__all__ = [
    "discover_packages",
    "generate_xml",
    "generate_doxyfile",
    "parse_xml_dir",
    "create_toolset",
    "DependencyGraphTools",
]


def parse_cppreference(archive_root, **kwargs):
    """Parse cppreference HTML archive. Requires ``[cppreference]`` extra."""
    from doxygen_index.cppreference import parse
    return parse(archive_root, **kwargs)
