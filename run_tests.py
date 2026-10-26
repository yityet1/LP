#!/usr/bin/env python3
"""Cross-platform zero-config test runner for Windows (cmd/PowerShell), macOS, and Linux.

Usage on Windows (cmd.exe or PowerShell):
    python run_tests.py

Automatically injects `src/` into `sys.path` so you never need to set `PYTHONPATH=src`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def main() -> int:
    print("=" * 72)
    print("ARB-PLATFORM Cross-Platform Automated Test Suite (Windows / macOS / Linux)")
    print(f"Python: {sys.version.split()[0]} | Platform: {sys.platform}")
    print("=" * 72)
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(REPO_ROOT / "tests" / "unit"),
        pattern="test_*.py"
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
