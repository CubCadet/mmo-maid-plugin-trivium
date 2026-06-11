"""Cross-file invariants. Catches mistakes that the rest of the suite
doesn't — like "forgot to bump both manifest.json and __version__"."""
from __future__ import annotations

import json
from pathlib import Path

from yourbot_sdk.testing import MockContext, make_event

import plugin_main
from plugin_main import (
    _refresh_admin_cache,
    format_custom_id,
    get_score,
    kv_inflight,
    on_button_click,
)


_REPO = Path(__file__).resolve().parent.parent


def _manifest_caps() -> list[str]:
    manifest = json.loads((_REPO / "manifest.json").read_text(encoding="utf-8"))
    return manifest["capabilities_required"]


def test_version_constant_matches_manifest():
    """manifest.json#version and plugin_main.__version__ must stay aligned.

    A drift here ships a plugin whose on_ready log says one version but
    whose manifest declares another — confusing for support and breaks the
    release.yml tag-vs-manifest parity check at the release-build step.
    """
    manifest = json.loads((_REPO / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == plugin_main.__version__, (
        f"version drift: manifest.json says {manifest['version']!r}, "
        f"__version__ says {plugin_main.__version__!r}. "
        "Bump both before release."
    )


def test_changelog_has_entry_for_current_version():
    """The CHANGELOG must have a section header for the current version.
    A new release without a changelog entry is a smell."""
    changelog = (_REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    expected_header = f"## [{plugin_main.__version__}]"
    assert expected_header in changelog, (
        f"CHANGELOG.md missing section {expected_header!r}. "
        "Add the section before tagging the release."
    )


def test_manifest_caps_cover_click_flow_under_strict_enforcement():
    """Answer-click flow with ONLY the manifest's declared capabilities.

    Every other test builds MockContext() with the grant-everything default,
    so a ctx call gated by a capability missing from manifest.json would
    sail through the suite and CapabilityError in production. SDK 0.6.1's
    MockContext honours an explicit capabilities= list; this exercises the
    hottest path (kv reads/writes, interaction.respond, edit_message in
    finalize_round) under exactly what the manifest grants. Assertions are
    positive — a swallowed CapabilityError can't pass as a no-op.
    """
    ctx = MockContext(capabilities=_manifest_caps())
    ctx.kv.set(kv_inflight("abc123"), {
        "question": "Q?",
        "shuffled_answers": ["A", "B", "C", "D"],
        "correct_idx": 2,
        "started_by_uid": "starter",
        "started_at": 0,
        "message_id": "msg1",
        "channel_id": "chan1",
        "mode": "single",
        "difficulty": "medium",
        "category": "General Knowledge",
        "source": "otdb",
        "is_daily": False,
        "timer_seconds": 20,
    }, ttl_seconds=25)
    event = make_event("interaction_create", interaction_type=3,
                       custom_id=format_custom_id("abc123", 2),
                       user_id="starter")
    on_button_click(ctx, event)
    assert any("Correct" in r["content"] for r in ctx.interaction.responses)
    assert get_score(ctx, "starter")["score"] == 20
    assert ctx.messages_edited, "finalize_round must edit the round embed"


def test_manifest_caps_cover_admin_cache_refresh_under_strict_enforcement():
    """discord:read coverage: _refresh_admin_cache calls get_guild and
    list_roles, and the admin gate deny-closes on SdkError — so if
    discord:read vanished from the manifest, the gate would quietly deny
    everyone rather than crash a test. Calling the refresh directly with
    only the manifest's capabilities pins it: a CapabilityError surfaces
    here as a None return (list_roles is load-bearing), so assert the cache
    was actually built.
    """
    ctx = MockContext(capabilities=_manifest_caps())
    cache = _refresh_admin_cache(ctx)
    assert cache is not None, (
        "admin-cache refresh failed under manifest capabilities — "
        "is discord:read still declared?"
    )
    assert isinstance(cache.get("roles_by_id"), dict)
