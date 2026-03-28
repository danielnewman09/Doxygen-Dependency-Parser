"""
doxygen-index: Index Doxygen XML and Conan C++ dependencies into graph databases.

Discovers Conan dependency headers, generates Doxygen XML documentation,
and ingests the results into SQLite and/or Neo4j for searchable code navigation.

Basic usage::

    from doxygen_index import discover_packages, generate_xml, ingest_sqlite

    packages = discover_packages(build_type="Debug")
    xml_dirs = generate_xml(packages, output_dir="build/docs/deps")
    ingest_sqlite(xml_dirs, db_path="codebase.db")
"""

__version__ = "0.1.0"

from doxygen_index.conan import discover_packages
from doxygen_index.doxygen import generate_xml, generate_doxyfile
from doxygen_index.sqlite_backend import ingest as ingest_sqlite
from doxygen_index.parser import parse_xml_dir
from doxygen_index.tools import create_toolset, DependencyGraphTools

__all__ = [
    "discover_packages",
    "generate_xml",
    "generate_doxyfile",
    "ingest_sqlite",
    "parse_xml_dir",
    "create_toolset",
    "DependencyGraphTools",
]


def parse_cppreference(archive_root, **kwargs):
    """Parse cppreference HTML archive. Requires ``[cppreference]`` extra."""
    from doxygen_index.cppreference import parse
    return parse(archive_root, **kwargs)
