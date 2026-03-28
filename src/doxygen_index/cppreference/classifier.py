"""
Classify cppreference HTML pages by type based on filesystem path structure.

The cppreference HTML book archive has a consistent layout::

    reference/en/cpp/
        header/vector.html          → HEADER  (<vector>)
        container/vector.html       → CLASS   (std::vector)
        container/vector/
            push_back.html          → MEMBER  (std::vector::push_back)
        algorithm/sort.html         → FREE_FUNCTION (std::sort)
        language/...                → SKIP
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PageType(Enum):
    HEADER = "header"
    CLASS = "class"
    MEMBER = "member"
    FREE_FUNCTION = "free_function"
    NAMESPACE = "namespace"
    SKIP = "skip"


@dataclass
class PageInfo:
    """Metadata about a cppreference HTML page."""
    path: Path              # absolute path to the HTML file
    relative: str           # relative to reference/ root, e.g. "en/cpp/container/vector.html"
    refid: str              # stable identifier, e.g. "cppreference:cpp/container/vector"
    page_type: PageType
    parent_refid: str = ""  # for MEMBER pages, the owning class refid
    header_name: str = ""   # for HEADER pages, e.g. "<vector>"


# Directories that contain non-API pages (language reference, etc.)
_SKIP_DIRS = {
    "language", "preprocessor", "comment", "keyword", "types",
    "experimental", "symbol_index", "links", "regex",
}

# Directories known to contain class pages with member sub-pages
# (used as a hint, but the sibling-directory heuristic is primary)
_CLASS_HINT_DIRS = {
    "container", "string", "io", "filesystem", "thread", "atomic",
    "memory", "regex", "chrono", "utility", "locale", "error",
    "numeric", "ranges", "iterator",
}


def _make_refid(relative: str) -> str:
    """Convert a relative HTML path to a stable refid.

    ``en/cpp/container/vector.html`` → ``cppreference:cpp/container/vector``
    """
    # Strip leading en/ and trailing .html
    refid = relative
    if refid.startswith("en/"):
        refid = refid[3:]
    if refid.endswith(".html"):
        refid = refid[:-5]
    return f"cppreference:{refid}"


def classify_pages(archive_root: Path) -> list[PageInfo]:
    """Walk the extracted archive and classify every C++ page.

    Args:
        archive_root: Path to the ``reference/`` directory inside the
            extracted archive.

    Returns:
        List of :class:`PageInfo` for every non-skipped page.
    """
    cpp_root = archive_root / "en" / "cpp"
    if not cpp_root.is_dir():
        # Try one level up in case the user points at the parent
        cpp_root = archive_root / "reference" / "en" / "cpp"
    if not cpp_root.is_dir():
        raise FileNotFoundError(
            f"Could not find en/cpp/ under {archive_root}. "
            "Ensure the archive is properly extracted."
        )

    # Pre-compute the set of directories that exist (for the sibling check)
    all_dirs = {p.relative_to(cpp_root) for p in cpp_root.rglob("*") if p.is_dir()}

    pages: list[PageInfo] = []

    for html_file in sorted(cpp_root.rglob("*.html")):
        rel_to_cpp = html_file.relative_to(cpp_root)
        parts = rel_to_cpp.parts  # e.g. ("container", "vector.html")

        # Skip non-API directories
        if parts[0] in _SKIP_DIRS:
            continue

        # Skip index/navigation pages
        if rel_to_cpp.stem in ("index", "cpp"):
            continue

        relative = f"en/cpp/{rel_to_cpp}"
        refid = _make_refid(relative)

        # --- HEADER pages: en/cpp/header/*.html ---
        if parts[0] == "header" and len(parts) == 2:
            header_name = f"<{rel_to_cpp.stem}>"
            pages.append(PageInfo(
                path=html_file,
                relative=relative,
                refid=refid,
                page_type=PageType.HEADER,
                header_name=header_name,
            ))
            continue

        # --- Determine if this is a CLASS page or MEMBER page ---
        # A page is a CLASS if a sibling directory with the same stem exists.
        # e.g. container/vector.html  +  container/vector/  → CLASS
        sibling_dir = rel_to_cpp.with_suffix("")  # container/vector
        has_member_dir = sibling_dir in all_dirs

        # Is this file INSIDE a class directory?
        # e.g. container/vector/push_back.html — parent is container/vector
        parent_rel = rel_to_cpp.parent  # container/vector
        # Guard against root-level files (parent is ".")
        if len(parts) < 2 or str(parent_rel) == ".":
            pages.append(PageInfo(
                path=html_file,
                relative=relative,
                refid=refid,
                page_type=PageType.FREE_FUNCTION if not has_member_dir else PageType.CLASS,
            ))
            continue
        parent_page = parent_rel.with_suffix(".html")  # container/vector.html
        parent_page_exists = (cpp_root / parent_page).is_file()

        if has_member_dir:
            # This page is a CLASS (it has member sub-pages)
            pages.append(PageInfo(
                path=html_file,
                relative=relative,
                refid=refid,
                page_type=PageType.CLASS,
            ))
        elif parent_page_exists and len(parts) > 1:
            # This page is a MEMBER of a class
            parent_relative = f"en/cpp/{parent_page}"
            parent_refid = _make_refid(parent_relative)
            pages.append(PageInfo(
                path=html_file,
                relative=relative,
                refid=refid,
                page_type=PageType.MEMBER,
                parent_refid=parent_refid,
            ))
        else:
            # Leaf page without a matching directory → free function or type
            pages.append(PageInfo(
                path=html_file,
                relative=relative,
                refid=refid,
                page_type=PageType.FREE_FUNCTION,
            ))

    return pages
