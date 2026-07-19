"""
Conan package discovery — finds installed dependency include paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional


def discover_packages(
    project_dir: Path | str = ".",
    build_type: str = "Debug",
    only: Optional[set[str]] = None,
) -> dict[str, Path]:
    """Discover Conan dependency include paths.

    Uses ``conan graph info`` to enumerate dependencies, then resolves
    each package's include directory via ``conan cache path``.

    Args:
        project_dir: Project root containing conanfile.py/txt.
        build_type: Conan build type to match installed packages.
        only: If provided, only discover these dependency names.

    Returns:
        Dict mapping dependency name to its include directory Path.
    """
    project_dir = Path(project_dir).resolve()

    print(f"Discovering Conan dependency paths (build_type={build_type})...")

    try:
        result = subprocess.run(
            ["conan", "graph", "info", ".", "--format=json",
             "-s", f"build_type={build_type}"],
            capture_output=True, text=True, cwd=project_dir, check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Error running 'conan graph info': {e.stderr}", file=sys.stderr)
        return {}
    except FileNotFoundError:
        print("Error: 'conan' not found on PATH.", file=sys.stderr)
        return {}

    raw = json.loads(result.stdout)
    graph_nodes = raw.get("graph", raw).get("nodes", {})
    paths: dict[str, Path] = {}

    for node in graph_nodes.values():
        name = node.get("name", "")
        # Skip the project itself (node id 0, or no ref)
        if not name or node.get("id") == "0" or not node.get("ref"):
            continue

        # Filter by --only if specified
        if only and name not in only:
            continue

        # Try package_folder first (set in local build contexts)
        pkg_folder = node.get("package_folder") or node.get("immutable_package_folder")

        # Resolve via 'conan cache path' if not directly available
        if not pkg_folder:
            ref = node.get("ref", "")
            package_id = node.get("package_id", "")
            if ref and package_id:
                try:
                    cache_result = subprocess.run(
                        ["conan", "cache", "path", f"{ref}:{package_id}"],
                        capture_output=True, text=True, check=True,
                    )
                    pkg_folder = cache_result.stdout.strip()
                except subprocess.CalledProcessError:
                    pass

        if pkg_folder:
            include_dir = Path(pkg_folder) / "include"
            if include_dir.exists():
                paths[name] = include_dir
                print(f"  Found {name}: {include_dir}")
            else:
                print(f"  Warning: {name} include dir not found: {include_dir}")
        else:
            binary_status = node.get("binary", "unknown")
            print(f"  Warning: {name} not installed (binary: {binary_status})")

    return paths
