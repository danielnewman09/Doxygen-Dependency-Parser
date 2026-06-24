"""Pytest configuration shared across all test modules.

Loads environment variables from a local ``.env`` file (if present) so
that integration tests like the real-LLM enrichment test can pick up
``LLM_API_KEY``, ``LLM_MODEL``, etc. without manual ``export`` calls.
"""

import os
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file, override=False)
    except ImportError:
        # python-dotenv not installed — read the file manually
        for line in _env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().split("#", 1)[0].strip()  # strip inline comments
            if key and key not in os.environ:
                os.environ[key] = value