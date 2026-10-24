# Copyright (c) 2026 Taras Bulubas. All rights reserved.
# Use requires prior written permission; see LICENSE.

"""Check that the running interpreter and an existing .venv use the expected Python.

Usage: python scripts/check_venv.py 3.12
"""

from __future__ import annotations

import sys
from pathlib import Path

VENV_CFG = Path(".venv") / "pyvenv.cfg"


def venv_version() -> str | None:
    """Python version recorded in .venv/pyvenv.cfg, or None without a .venv."""
    if not VENV_CFG.exists():
        return None
    for line in VENV_CFG.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            return value.strip()
    return None


def main() -> None:
    expected = sys.argv[1]
    running = ".".join(map(str, sys.version_info[:2]))
    if running != expected:
        sys.exit(f"base interpreter is Python {running}, expected {expected}")

    existing = venv_version()
    if existing is not None and not existing.startswith(expected + "."):
        sys.exit(f".venv uses Python {existing}, expected {expected}: run 'make recreate'")


if __name__ == "__main__":
    main()
