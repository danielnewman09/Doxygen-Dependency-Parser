"""
cppreference parser — parse the offline HTML book into :class:`ParseResult`.

Requires the ``cppreference`` extra::

    pip install doxygen-index[cppreference]

Quick start::

    from doxygen_index.cppreference import download, parse

    archive_root = download(Path("~/.cache/doxygen-index/cppreference"))
    result = parse(archive_root)
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

from codegraph import NamespaceNode
from doxygen_index.parser import ParseResult

from .classifier import PageType, classify_pages


def _check_deps() -> None:
    try:
        import bs4  # noqa: F401
    except ImportError:
        raise ImportError(
            "cppreference parsing requires extra dependencies.\n"
            "Install them with:  pip install doxygen-index[cppreference]"
        ) from None


SOURCE = "cppreference"


def download(
    dest_dir: Path | str,
    url: str | None = None,
    force: bool = False,
) -> Path:
    """Download and extract the cppreference HTML book archive.

    Args:
        dest_dir: Where to cache the archive and extracted files.
        url: Override the default archive URL.
        force: Re-download even if cached.

    Returns:
        Path to the ``reference/`` directory inside the extracted archive.
    """
    _check_deps()
    from .downloader import download_archive
    return download_archive(Path(dest_dir), url=url, force=force)


def parse(
    archive_root: Path | str,
    progress_interval: int = 100,
) -> ParseResult:
    """Parse the extracted cppreference HTML archive into a ParseResult.

    Args:
        archive_root: Path to the ``reference/`` directory (as returned
            by :func:`download`).
        progress_interval: Print progress every N pages (0 to disable).

    Returns:
        A :class:`ParseResult` containing all parsed C++ standard library
        documentation.
    """
    _check_deps()
    from bs4 import BeautifulSoup

    from .page_parser import (
        parse_class_page,
        parse_free_function_page,
        parse_header_page,
        parse_member_page,
    )

    archive_root = Path(archive_root)
    print(f"Classifying pages in {archive_root} ...")
    pages = classify_pages(archive_root)

    by_type: dict[PageType, int] = {}
    for p in pages:
        by_type[p.page_type] = by_type.get(p.page_type, 0) + 1
    print(f"Found {len(pages)} pages: " + ", ".join(
        f"{t.value}={n}" for t, n in sorted(by_type.items(), key=lambda x: x[0].value)
    ))

    result = ParseResult()

    # --- Pass 1: Class pages (compounds + member stubs) ---
    class_pages = [p for p in pages if p.page_type == PageType.CLASS]
    print(f"\nPass 1: Parsing {len(class_pages)} class pages ...")
    stub_refids: set[str] = set()

    for i, info in enumerate(class_pages):
        try:
            soup = _parse_html(info.path)
            compound, stubs = parse_class_page(info, soup)
            result.compounds.append(compound)
            for stub in stubs:
                stub_refids.add(stub.refid)
                result.members.append(stub)
        except Exception as e:
            print(f"  Warning: Failed to parse {info.relative}: {e}", file=sys.stderr)

        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Parsed {i + 1}/{len(class_pages)} class pages ...")

    # --- Pass 2: Member pages (full declarations + parameters) ---
    member_pages = [p for p in pages if p.page_type == PageType.MEMBER]
    print(f"\nPass 2: Parsing {len(member_pages)} member pages ...")

    for i, info in enumerate(member_pages):
        try:
            soup = _parse_html(info.path)
            entries = parse_member_page(info, soup)
            for member, params in entries:
                # If this member was already added as a stub, replace it
                _replace_or_add_member(result, member, stub_refids)
                result.parameters.extend(params)
        except Exception as e:
            print(f"  Warning: Failed to parse {info.relative}: {e}", file=sys.stderr)

        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Parsed {i + 1}/{len(member_pages)} member pages ...")

    # --- Pass 3: Free function pages ---
    free_pages = [p for p in pages if p.page_type == PageType.FREE_FUNCTION]
    print(f"\nPass 3: Parsing {len(free_pages)} free function pages ...")

    for i, info in enumerate(free_pages):
        try:
            soup = _parse_html(info.path)
            entries = parse_free_function_page(info, soup)
            for member, params in entries:
                result.members.append(member)
                result.parameters.extend(params)
        except Exception as e:
            print(f"  Warning: Failed to parse {info.relative}: {e}", file=sys.stderr)

        if progress_interval and (i + 1) % progress_interval == 0:
            print(f"  Parsed {i + 1}/{len(free_pages)} free function pages ...")

    # --- Pass 4: Header pages + DEFINED_IN linkage ---
    header_pages = [p for p in pages if p.page_type == PageType.HEADER]
    print(f"\nPass 4: Parsing {len(header_pages)} header pages ...")

    # Build lookup maps: refid → compound/member for DEFINED_IN linking
    compound_by_refid = {c.refid: c for c in result.compounds}
    member_by_refid = {m.refid: m for m in result.members}
    # Also map base refid (without #N overload suffix) → list of members
    member_by_base_refid: dict[str, list] = {}
    for m in result.members:
        base = m.refid.split("#")[0]
        member_by_base_refid.setdefault(base, []).append(m)

    defined_in_count = 0
    for info in header_pages:
        try:
            soup = _parse_html(info.path)
            file_entry, symbol_refids = parse_header_page(info, soup)
            result.files.append(file_entry)

            # Link symbols to this header via file_path (used by
            # _write_file_relationships to create DEFINED_IN edges)
            for sym_refid in symbol_refids:
                if sym_refid in compound_by_refid:
                    compound_by_refid[sym_refid].file_path = info.relative
                    defined_in_count += 1
                # Also check members (free functions have page-level refids)
                if sym_refid in member_by_base_refid:
                    for m in member_by_base_refid[sym_refid]:
                        m.file_path = info.relative
                        defined_in_count += 1
        except Exception as e:
            print(f"  Warning: Failed to parse {info.relative}: {e}", file=sys.stderr)

    print(f"  Linked {defined_in_count} symbols to headers (DEFINED_IN)")

    # --- Synthesize namespaces ---
    result.namespaces = _synthesize_namespaces(result.compounds, result.members)

    # --- Summary ---
    print(f"\nParsed cppreference:")
    print(f"  Files:      {len(result.files)}")
    print(f"  Namespaces: {len(result.namespaces)}")
    print(f"  Compounds:  {len(result.compounds)}")
    print(f"  Members:    {len(result.members)}")
    print(f"  Parameters: {len(result.parameters)}")

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_html(path: Path):
    """Parse an HTML file with BeautifulSoup using the best available parser."""
    from bs4 import BeautifulSoup

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return BeautifulSoup(f, "lxml")


def _replace_or_add_member(
    result: ParseResult,
    member,
    stub_refids: set[str],
) -> None:
    """Replace a stub member with the full version, or add as new."""
    # Check if any existing member is a stub for this member's base refid
    base_refid = member.refid.split("#")[0]
    for i, existing in enumerate(result.members):
        if existing.refid.split("#")[0] == base_refid and existing.refid in stub_refids:
            # Replace stub with full version, preserving brief from stub if
            # the full version doesn't have one
            if not member.brief_description and existing.brief_description:
                member.brief_description = existing.brief_description
            result.members[i] = member
            stub_refids.discard(existing.refid)
            return
    result.members.append(member)


def _synthesize_namespaces(compounds, members) -> list[NamespaceNode]:
    """Create NamespaceNode records from qualified names."""
    ns_set: set[str] = set()
    for entry in itertools.chain(compounds, members):
        qn = entry.qualified_name
        parts = qn.split("::")
        # Build all namespace prefixes (e.g. std, std::ranges)
        for i in range(1, len(parts)):
            prefix = "::".join(parts[:i])
            if prefix and prefix != qn:
                ns_set.add(prefix)

    return [
        NamespaceNode(
            refid=f"cppreference:ns/{qn.replace('::', '/')}",
            name=qn.split("::")[-1],
            qualified_name=qn,
            source=SOURCE,
            layer="dependency",
        )
        for qn in sorted(ns_set)
    ]
