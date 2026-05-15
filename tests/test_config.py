"""Tests for /trivia config — admin permission spot-checks, config
sub-action routing, value validation. The full admin-gate matrix lives in
tests/test_admin_gate.py."""
from __future__ import annotations

from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import (
    DEFAULT_CONFIG,
    KV_CONFIG,
    PERM_ADMINISTRATOR,
    PERM_MANAGE_GUILD,
    cmd_config,
    get_config,
    has_manage_guild,
)


# ── has_manage_guild — Layer A spot-checks ────────────────────────────────
# Comprehensive coverage of Layers A/B/C is in tests/test_admin_gate.py.
# This file checks Layer A only (the cheap, no-Discord-call path) since
# every existing /trivia config test below supplies member.permissions.

def test_perms_present_as_int_with_manage_guild_bit():
    ctx = MockContext()
    event = {"user_id": "u1", "member": {"permissions": PERM_MANAGE_GUILD}}
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is True
    assert src == "member_perms"


def test_perms_present_as_string_with_administrator_bit():
    """Discord can send permissions as a stringified int. Coerce defensively."""
    ctx = MockContext()
    event = {"user_id": "u1", "member": {"permissions": str(PERM_ADMINISTRATOR)}}
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is True
    assert src == "member_perms"


def test_perms_present_without_required_bit_denied():
    ctx = MockContext()
    event = {"user_id": "u1", "member": {"permissions": 0x4}}   # SEND_MESSAGES only
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is False
    assert src == "no_perms_member"


# ── /trivia config show ────────────────────────────────────────────────────

def test_show_renders_current_config_as_embed():
    ctx = MockContext()
    ctx.kv.set(KV_CONFIG, {**DEFAULT_CONFIG, "daily_channel_id": "123"})
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "show"})
    assert ctx.interaction.responses
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert "embeds" in last
    assert any("Trivium" in (e.get("title") or "") for e in last["embeds"])


# ── /trivia config channel ─────────────────────────────────────────────────

def test_set_channel_with_mention():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin", channel_id="xxx",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "channel", "value": "<#999888>"})
    cfg = get_config(ctx)
    assert cfg["daily_channel_id"] == "999888"


def test_set_channel_with_raw_id():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin", channel_id="xxx",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "channel", "value": "987654321098765432"})
    cfg = get_config(ctx)
    assert cfg["daily_channel_id"] == "987654321098765432"


def test_set_channel_with_no_value_uses_current_channel():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin", channel_id="555",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "channel"})
    cfg = get_config(ctx)
    assert cfg["daily_channel_id"] == "555"


# ── /trivia config time ────────────────────────────────────────────────────

def test_set_time_valid_hh_mm():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "time", "value": "09:30"})
    cfg = get_config(ctx)
    assert cfg["daily_time_utc"] == "09:30"


def test_set_time_rejects_malformed():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "time", "value": "9am"})
    cfg = get_config(ctx)
    assert cfg["daily_time_utc"] is None      # unchanged
    assert "HH:MM" in ctx.interaction.responses[-1]["content"]


def test_set_time_rejects_out_of_range_hour():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "time", "value": "25:00"})
    cfg = get_config(ctx)
    assert cfg["daily_time_utc"] is None


# ── /trivia config difficulty ──────────────────────────────────────────────

def test_set_difficulty_valid():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "difficulty", "value": "hard"})
    cfg = get_config(ctx)
    assert cfg["default_difficulty"] == "hard"


def test_set_difficulty_rejects_garbage():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "difficulty", "value": "extreme"})
    cfg = get_config(ctx)
    assert cfg["default_difficulty"] == "any"     # unchanged from default


# ── /trivia config timer ───────────────────────────────────────────────────

def test_set_timer_in_range():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "timer", "value": "30"})
    cfg = get_config(ctx)
    assert cfg["timer_seconds"] == 30


def test_set_timer_below_range_rejected():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "timer", "value": "5"})
    cfg = get_config(ctx)
    assert cfg["timer_seconds"] == 20         # unchanged


def test_set_timer_non_integer_rejected():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "timer", "value": "fast"})
    cfg = get_config(ctx)
    assert cfg["timer_seconds"] == 20


# ── /trivia config mode ────────────────────────────────────────────────────

def test_set_mode_valid():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "mode", "value": "open"})
    cfg = get_config(ctx)
    assert cfg["mode"] == "open"


def test_set_mode_invalid_rejected():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "mode", "value": "team"})
    cfg = get_config(ctx)
    assert cfg["mode"] == "single"


# ── /trivia config category ────────────────────────────────────────────────

def test_set_category_valid():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "category", "value": "Sports"})
    cfg = get_config(ctx)
    assert cfg["daily_category"] == "Sports"


def test_set_category_invalid_rejected():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="admin",
                       member={"permissions": PERM_MANAGE_GUILD})
    cmd_config(ctx, event, {"action": "category", "value": "Cooking"})
    cfg = get_config(ctx)
    assert cfg["daily_category"] == "General Knowledge"


# ── Non-admin rejection ────────────────────────────────────────────────────

def test_non_admin_with_explicit_perms_denied():
    """A user whose perms are explicitly present but lack MANAGE_GUILD must
    be rejected even if the manifest default somehow let them through."""
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       user_id="rando",
                       member={"permissions": 0x4})    # SEND_MESSAGES only
    cmd_config(ctx, event, {"action": "channel", "value": "<#999>"})
    # Channel must NOT have been updated
    cfg = get_config(ctx)
    assert cfg["daily_channel_id"] is None
    # And the response should be the denial
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert "Manage Server" in last["content"]
