"""Make the fixture-generation subprocess's symbol resolution reproducible.

The production C++ parser intentionally parses XML compounds in parallel.
Its test-call resolver historically selected the first same-name method, so
completion order could change a CALLEE/VERIFIES endpoint.  Fixture generation
uses this test-only startup hook to provide a stable candidate order without
changing the production parser.
"""

from __future__ import annotations


def _stable_key(node) -> tuple[str, str, str, str]:
    return (
        getattr(node, "qualified_name", "") or "",
        getattr(node, "argsstring", "") or "",
        getattr(node, "refid", "") or "",
        getattr(node, "name", "") or "",
    )


def _stable_pending_key(call) -> tuple[str, str, str, bool]:
    return (
        getattr(call, "from_refid", "") or "",
        getattr(call, "test_refid", "") or "",
        getattr(call, "callee_text", "") or "",
        bool(getattr(call, "is_assert", False)),
    )


try:
    from doxygen_index.parser.cpp_parser import CppParser

    _original_post_process = CppParser.post_process

    def _deterministic_post_process(self, result):
        for attr in ("methods", "functions", "classes"):
            values = getattr(result, attr, None)
            if values is not None:
                values.sort(key=_stable_key)
        pending = getattr(result, "pending_test_calls", None)
        if pending is not None:
            pending.sort(key=_stable_pending_key)
        return _original_post_process(self, result)

    CppParser.post_process = _deterministic_post_process
except Exception:
    # The hook is only relevant to the C++ owner pipeline.  Keep unrelated
    # Python subprocesses usable if the optional parser dependencies are absent.
    pass
