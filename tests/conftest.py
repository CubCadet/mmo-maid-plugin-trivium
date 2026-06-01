"""Test bootstrap.

Side-load __main__.py as `plugin_main` so tests can `from plugin_main import ...`.
pytest reserves the `__main__` module name; a direct import collides with
pytest's own runtime.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# ── Side-load __main__.py ───────────────────────────────────────────────────

_main_py = Path(__file__).resolve().parent.parent / "__main__.py"
_spec = importlib.util.spec_from_file_location("plugin_main", _main_py)
_module = importlib.util.module_from_spec(_spec)
sys.modules["plugin_main"] = _module
_spec.loader.exec_module(_module)
