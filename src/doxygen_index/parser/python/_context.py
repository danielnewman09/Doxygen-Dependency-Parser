"""
Shared parsing context for Python AST handler modules.

:class:`ParseContext` carries the mutable state that was previously held as
instance attributes on ``_PythonVisitor``.  Each handler module receives a
``ParseContext`` instead of a ``self`` reference, making the handlers
testable and independent of the visitor class.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from doxygen_index.parser.model import ParseResult


@dataclass
class ParseContext:
    """State shared across all Python AST handler modules for one file.

    Created fresh per source file in
    :meth:`~doxygen_index.parser.python._parser.PythonParser._parse_python_file`.
    """

    module_name: str
    file_path: str
    source: str
    layer: str
    result: ParseResult
    #: Stack of ``(refid, qualified_name)`` for the containing class.
    class_stack: list[tuple[str, str]] = field(default_factory=list)
    #: Fixture variable names checked by the current assertion.
    checked_fixtures: set[str] = field(default_factory=set)

    #: Tagged ``# codegraph:test-desc <qn>`` comments parsed from the
    #: current file (``qualified_name → description``).  Populated in
    #: :meth:`PythonParser._parse_python_file` via
    #: :func:`~doxygen_index.parser.python.test_comments.read_test_comments`.
    #: The test handlers apply this map to each node's ``description``
    #: field so that descriptions enriched and written back to the
    #: source files survive a re-parse — the bidirectional sync.
    test_comments: dict[str, str] = field(default_factory=dict)

    @property
    def current_class(self) -> tuple[str, str] | None:
        """Return ``(refid, qualified_name)`` of the innermost class, or ``None``."""
        return self.class_stack[-1] if self.class_stack else None