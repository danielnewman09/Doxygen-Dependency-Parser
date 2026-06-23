"""Functions with long signatures — for testing label width constraints.

These exist purely to exercise the ``max-width`` wrapping behaviour in
the Cytoscape HTML label builder.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from pathlib import Path


def process_data(
    data: List[Dict[str, Any]],
    output_path: Optional[Path] = None,
    max_retries: int = 3,
    timeout_seconds: float = 30.0,
    callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Process data with configurable retry and timeout behaviour.

    Each parameter is intentionally verbose to produce line-wrapping
    inside the Cytoscape HTML label when ``max-width`` is enforced.
    """
    return {"status": "ok"}


class ReportingService:
    """A class whose methods have long signature lines."""

    def generate_report(
        self,
        title: str,
        sections: List[Dict[str, Any]],
        include_footer: bool = True,
        page_size: Optional[tuple[int, int]] = None,
        watermark_text: Optional[str] = "CONFIDENTIAL",
    ) -> str:
        """Generate a formatted report.

        This signature exercises the wrapping for a method inside a class,
        which is collapsed into the parent UML label — the parent's label
        should also wrap long member signatures.
        """
        return ""
