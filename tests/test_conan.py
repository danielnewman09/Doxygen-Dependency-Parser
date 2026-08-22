"""Unit tests for optional Conan dependency discovery."""

from __future__ import annotations

import pytest

import doxygen_index.conan as conan


def test_discover_packages_without_manifest_is_project_only(monkeypatch, tmp_path):
    def unexpected_subprocess(*_args, **_kwargs):
        pytest.fail("Conan should not run without a conanfile")

    monkeypatch.setattr(conan.subprocess, "run", unexpected_subprocess)

    assert conan.discover_packages(tmp_path) == {}
