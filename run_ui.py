#!/usr/bin/env python3
"""Cross-platform zero-config Web UI Server launcher for Windows (cmd/PowerShell), macOS, and Linux.

Usage on Windows (cmd.exe or PowerShell):
    python run_ui.py
    python run_ui.py --port 8080

Automatically injects `src/` into `sys.path` so you never need to set `PYTHONPATH=src`.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from arb.ui.server import main  # noqa: E402

if __name__ == "__main__":
    main()
