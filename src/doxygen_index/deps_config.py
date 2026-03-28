"""
Default dependency configurations for common Conan packages.

Each entry configures how to run Doxygen on a specific dependency:

- ``file_patterns``: which header extensions to index
- ``recursive``: whether to recurse into subdirectories
- ``subdir``: optional subdirectory under ``include/`` to index
- ``exclude_patterns``: Doxygen EXCLUDE_PATTERNS value
- ``predefined``: preprocessor definitions for Doxygen

Users can override or extend these via the Python API or CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DepConfig:
    """Configuration for indexing a single dependency."""
    file_patterns: str = "*.h *.hpp"
    recursive: bool = True
    subdir: Optional[str] = None
    exclude_patterns: str = ""
    predefined: str = ""


# Built-in configurations for popular C++ libraries
BUILTIN_CONFIGS: dict[str, DepConfig] = {
    "eigen": DepConfig(
        subdir="eigen3/Eigen",
        predefined="EIGEN_PARSED_BY_DOXYGEN",
    ),
    "boost": DepConfig(
        file_patterns="*.hpp",
        subdir="boost",
        exclude_patterns="*/detail/* */impl/* */aux_/*",
    ),
    "sdl": DepConfig(
        file_patterns="*.h",
        recursive=False,
        subdir="SDL3",
    ),
    "sdl_image": DepConfig(
        file_patterns="*.h",
        recursive=False,
        subdir="SDL3_image",
    ),
    "sdl_mixer": DepConfig(
        file_patterns="*.h",
        recursive=False,
        subdir="SDL3_mixer",
    ),
    "sdl_ttf": DepConfig(
        file_patterns="*.h",
        recursive=False,
        subdir="SDL3_ttf",
    ),
    "spdlog": DepConfig(
        file_patterns="*.h",
        subdir="spdlog",
    ),
    "nlopt": DepConfig(),
    "qhull": DepConfig(
        file_patterns="*.h",
        subdir="libqhull_r",
    ),
    "fmt": DepConfig(
        subdir="fmt",
    ),
    "gtest": DepConfig(
        subdir="gtest",
    ),
    "catch2": DepConfig(
        file_patterns="*.hpp",
        subdir="catch2",
    ),
    "nlohmann_json": DepConfig(
        file_patterns="*.hpp",
        subdir="nlohmann",
    ),
    "glm": DepConfig(
        file_patterns="*.hpp *.h",
        subdir="glm",
    ),
    "imgui": DepConfig(
        file_patterns="*.h",
        recursive=False,
    ),
}


def get_config(name: str, overrides: Optional[dict[str, DepConfig]] = None) -> Optional[DepConfig]:
    """Get the configuration for a dependency, with optional overrides.

    Args:
        name: Dependency name (e.g., "eigen", "boost").
        overrides: User-provided configs that take precedence over builtins.

    Returns:
        DepConfig if found, None otherwise.
    """
    if overrides and name in overrides:
        return overrides[name]
    return BUILTIN_CONFIGS.get(name)


def list_known_deps(overrides: Optional[dict[str, DepConfig]] = None) -> dict[str, DepConfig]:
    """Return all known dependency configurations (builtins + overrides)."""
    configs = dict(BUILTIN_CONFIGS)
    if overrides:
        configs.update(overrides)
    return configs
