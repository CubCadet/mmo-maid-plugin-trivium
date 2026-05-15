"""Test bootstrap.

1. Side-load __main__.py as `plugin_main` so tests can `from plugin_main import ...`.
   pytest reserves the `__main__` module name; a direct import collides with
   pytest's own runtime.

2. Patch v0.5.2 MockContext.interaction.respond / followup to accept the
   `allowed_mentions` kwarg the real Context already accepts. The SDK
   testing harness in v0.5.2 lags the real Context signature by one kwarg;
   forward-port the contract in the test layer so production code can pass
   `allowed_mentions={"parse": []}` directly (and tests can assert it).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict


# ── Patch MockContext _MockInteraction to accept allowed_mentions ───────────

from mmo_maid_sdk import testing as _sdk_testing  # noqa: E402


_orig_respond = _sdk_testing._MockInteraction.respond
_orig_followup = _sdk_testing._MockInteraction.followup


def _patched_respond(self, content: str = "", embeds=None, components=None,
                     ephemeral: bool = False,
                     allowed_mentions: Dict[str, Any] | None = None) -> None:
    self.responses.append({
        "content": content,
        "embeds": embeds,
        "components": components,
        "ephemeral": ephemeral,
        "allowed_mentions": allowed_mentions,
    })


def _patched_followup(self, content: str = "", embeds=None, components=None,
                      ephemeral: bool = False,
                      allowed_mentions: Dict[str, Any] | None = None) -> None:
    self.followups.append({
        "content": content,
        "embeds": embeds,
        "components": components,
        "ephemeral": ephemeral,
        "allowed_mentions": allowed_mentions,
    })


_sdk_testing._MockInteraction.respond = _patched_respond
_sdk_testing._MockInteraction.followup = _patched_followup


# ── Side-load __main__.py ───────────────────────────────────────────────────

_main_py = Path(__file__).resolve().parent.parent / "__main__.py"
_spec = importlib.util.spec_from_file_location("plugin_main", _main_py)
_module = importlib.util.module_from_spec(_spec)
sys.modules["plugin_main"] = _module
_spec.loader.exec_module(_module)
