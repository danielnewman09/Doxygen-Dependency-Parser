"""
Path and module-name helpers for Python source parsing.

These functions convert between filesystem paths and Python dotted module
names, and filter source files based on exclude rules.
"""

from __future__ import annotations

from pathlib import Path


def is_excluded(file_path: Path, base_dir: Path, exclude_dirs: set[str]) -> bool:
    """Check whether *file_path* should be skipped.

    Returns ``True`` if any component of the path (relative to *base_dir*)
    matches a name in *exclude_dirs*.
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        rel = file_path
    return any(part in exclude_dirs for part in rel.parts[:-1])


def module_path(file_path: Path, base_dir: Path) -> str:
    """Convert a file path to a dotted Python module name.

    Example: ``base_dir/mypackage/sub/mod.py`` → ``mypackage.sub.mod``
    """
    try:
        rel = file_path.relative_to(base_dir)
    except ValueError:
        rel = file_path
    parts = list(rel.with_suffix("").parts)
    # If the file is __init__.py, the module name is the package name
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)