"""
Download and extract the cppreference offline HTML book archive.
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Optional


# The cppreference HTML book is maintained by PeterFeicht/cppreference-doc.
# This URL points to the latest release on GitHub.
DEFAULT_ARCHIVE_URL = (
    "https://github.com/PeterFeicht/cppreference-doc/releases/download/"
    "v20250209/html-book-20250209.tar.xz"
)


def download_archive(
    dest_dir: Path,
    url: str | None = None,
    force: bool = False,
) -> Path:
    """Download and extract the cppreference HTML book.

    Args:
        dest_dir: Directory to store the downloaded/extracted archive.
        url: URL to the archive file.  Defaults to the latest known URL.
        force: If True, re-download even if a cached copy exists.

    Returns:
        Path to the extracted ``reference/`` directory.
    """
    import requests

    url = url or DEFAULT_ARCHIVE_URL
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Check for already-extracted archive
    reference_dir = _find_reference_dir(dest_dir)
    if reference_dir and not force:
        print(f"Using cached archive at {reference_dir}")
        return reference_dir

    # Download
    archive_name = url.rsplit("/", 1)[-1]
    archive_path = dest_dir / archive_name

    if not archive_path.exists() or force:
        print(f"Downloading {url} ...")
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(archive_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded * 100 // total
                    print(f"\r  {pct}% ({downloaded // 1024 // 1024}MB)", end="", flush=True)
        print()
        print(f"Downloaded {archive_path} ({archive_path.stat().st_size // 1024 // 1024}MB)")
    else:
        print(f"Using cached download: {archive_path}")

    # Extract
    print(f"Extracting {archive_path} ...")
    _extract(archive_path, dest_dir)

    reference_dir = _find_reference_dir(dest_dir)
    if reference_dir is None:
        raise RuntimeError(
            f"Extraction succeeded but could not find reference/ directory in {dest_dir}. "
            "The archive structure may have changed."
        )
    print(f"Extracted to {reference_dir}")
    return reference_dir


def _find_reference_dir(base: Path) -> Optional[Path]:
    """Locate the reference/ directory within the extracted archive."""
    # Direct child
    if (base / "reference").is_dir():
        return base / "reference"
    # One level nested (e.g. html_book_20230815/reference/)
    for child in base.iterdir():
        if child.is_dir() and (child / "reference").is_dir():
            return child / "reference"
    # The base itself might be the reference dir
    if (base / "en" / "cpp").is_dir():
        return base
    return None


def _extract(archive_path: Path, dest_dir: Path) -> None:
    """Extract a tar.xz, tar.gz, or zip archive."""
    name = archive_path.name.lower()

    if name.endswith((".tar.xz", ".tar.gz", ".tar.bz2", ".tgz")):
        with tarfile.open(archive_path) as tf:
            tf.extractall(path=dest_dir)
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(path=dest_dir)
    elif name.endswith(".7z"):
        raise RuntimeError(
            "7z archives require the 'py7zr' package.  "
            "Please use the .tar.xz or .zip version instead."
        )
    else:
        raise RuntimeError(f"Unsupported archive format: {archive_path.name}")
