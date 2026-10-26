"""Multi-Strategy Arbitrage Platform Core Package."""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

__version__ = "0.1.0"

_ARB_ROOT = Path(__file__).resolve().parent


class _DirectFsPathFinder(importlib.abc.MetaPathFinder):
    """Fallback finder that resolves `arb.*` submodules directly by path when 9p readdir cache lags."""

    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith("arb."):
            return None
        rel_parts = fullname.split(".")[1:]
        candidate_dir = _ARB_ROOT.joinpath(*rel_parts)
        candidate_init = candidate_dir / "__init__.py"
        try:
            if candidate_init.is_file():
                return importlib.machinery.ModuleSpec(
                    fullname,
                    importlib.machinery.SourceFileLoader(fullname, str(candidate_init)),
                    origin=str(candidate_init),
                    is_package=True,
                )
        except OSError:
            pass
        candidate_py = _ARB_ROOT.joinpath(*rel_parts[:-1], f"{rel_parts[-1]}.py")
        try:
            if candidate_py.is_file():
                return importlib.machinery.ModuleSpec(
                    fullname,
                    importlib.machinery.SourceFileLoader(fullname, str(candidate_py)),
                    origin=str(candidate_py),
                    is_package=False,
                )
        except OSError:
            pass
        return None


if not any(isinstance(f, _DirectFsPathFinder) for f in sys.meta_path):
    sys.meta_path.append(_DirectFsPathFinder())
