"""
Python source parser subpackage.

Re-exports the public API:

* :class:`PythonParser` — concrete parser for Python source files.
* :data:`DEFAULT_EXCLUDE_DIRS` — default directory exclusion set.
"""

from __future__ import annotations

from doxygen_index.parser.python._parser import PythonParser
from doxygen_index.parser.python._ast_utils import DEFAULT_EXCLUDE_DIRS

__all__ = ["PythonParser", "DEFAULT_EXCLUDE_DIRS"]