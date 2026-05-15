"""Tests for the v1.0.4 KV-based admin allowlist + bootstrap.

The Discord-based admin gate (test_admin_gate.py) is the safety net; this
file pins the new primary gate (Layer 0) and the admin-bootstrap /
admin-add / admin-remove / admin-list sub-commands.
"""
from __future__ import annotations

from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import (
    DEFAULT_CONFIG,
    KV_CONFIG,
    cmd_config,
    get_config,
    has_manage_guild,
)


def _event(user_id="u1", **kwargs):
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="trivia",
        user_id=user_id,
        **kwargs,
    )


# ── Layer 0: allowlist short-circuits everything ──────────────────────────

def test_layer_0_allowlist_match_allows_without_discord_calls():
    """If the user_id is in cfg.admin_user_ids, return immediately without
    consulting Discord. Cheapest path."""
    ctx = MockContext()
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin_user_ids"] = ["admin_uid"]
    ctx.kv.set(KV_CONFIG, cfg)
    allowed, src = has_manage_guild(ctx, _event(user_id="admin_uid"))
    assert allowed is True
    assert src == "admin_allowlist"


def test_layer_0_empty_allowlist_falls_through_to_other_layers():
    """When the allowlist is empty, the gate behaves like 1.0.3 — falls
    through to Layer A/B/C and (without a Discord mock) denies."""
    ctx = MockContext()
    # Default config has admin_user_ids=[]
    allowed, src = has_manage_guild(ctx, _event(user_id="anyone"))
    assert allowed is False
    # Source isn't admin_allowlist — we fell through to the Discord-based
    # check, which denies because of the default MockContext (no cache,
    # default get_guild/list_roles return empty).
    assert src != "admin_allowlist"


def test_layer_0_non_matching_user_falls_through_too():
    """A user NOT in the allowlist must continue through later layers,
    not get auto-denied at Layer 0."""
    ctx = MockContext()
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin_user_ids"] = ["admin_uid"]
    ctx.kv.set(KV_CONFIG, cfg)
    # Add a member.permissions to give Layer A a path to allow
    event = _event(user_id="other_uid")
    event["member"] = {"permissions": 0x8}     # ADMINISTRATOR
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is True
    # Layer A took the decision, not Layer 0
    assert src == "member_perms"


# ── /trivia config action:admin-bootstrap ─────────────────────────────────

def test_bootstrap_claims_admin_when_allowlist_is_empty():
    ctx = MockContext()
    event = _event(user_id="installer")
    cmd_config(ctx, event, {"action": "admin-bootstrap"})
    cfg = get_config(ctx)
    assert cfg["admin_user_ids"] == ["installer"]
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert "first Trivium admin" in last["content"]


def test_bootstrap_refuses_when_admins_already_set():
    """Second call to admin-bootstrap must refuse without modifying state."""
    ctx = MockContext()
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin_user_ids"] = ["already_admin"]
    ctx.kv.set(KV_CONFIG, cfg)
    event = _event(user_id="opportunist")
    cmd_config(ctx, event, {"action": "admin-bootstrap"})
    cfg_after = get_config(ctx)
    # State must be unchanged — opportunist did NOT get added
    assert cfg_after["admin_user_ids"] == ["already_admin"]
    last = ctx.interaction.responses[-1]
    assert "already configured" in last["content"]


def test_bootstrap_bypasses_admin_gate():
    """admin-bootstrap must work even when the caller isn't an admin yet —
    that's its whole purpose."""
    ctx = MockContext()
    event = _event(user_id="newcomer")
    # No admins exist, no member perms set, no Discord data mocked
    cmd_config(ctx, event, {"action": "admin-bootstrap"})
    cfg = get_config(ctx)
    assert cfg["admin_user_ids"] == ["newcomer"]


# ── /trivia config action:admin-add ────────────────────────────────────────

def _seed_admin(ctx, *uids):
    """Seed the allowlist with the given user IDs."""
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin_user_ids"] = list(uids)
    ctx.kv.set(KV_CONFIG, cfg)


def test_admin_add_with_mention():
    ctx = MockContext()
    _seed_admin(ctx, "existing")
    event = _event(user_id="existing")
    cmd_config(ctx, event, {"action": "admin-add", "value": "<@123456789012345678>"})
    cfg = get_config(ctx)
    assert "123456789012345678" in cfg["admin_user_ids"]


def test_admin_add_with_raw_id():
    ctx = MockContext()
    _seed_admin(ctx, "existing")
    event = _event(user_id="existing")
    cmd_config(ctx, event, {"action": "admin-add", "value": "987654321098765432"})
    cfg = get_config(ctx)
    assert "987654321098765432" in cfg["admin_user_ids"]


def test_admin_add_rejects_bad_value():
    ctx = MockContext()
    _seed_admin(ctx, "existing")
    event = _event(user_id="existing")
    cmd_config(ctx, event, {"action": "admin-add", "value": "not-a-user"})
    cfg = get_config(ctx)
    assert cfg["admin_user_ids"] == ["existing"]      # unchanged
    last = ctx.interaction.responses[-1]
    assert "Couldn't parse" in last["content"]


def test_admin_add_dedupe_on_repeat():
    """Adding an existing admin again should be a no-op with friendly message."""
    ctx = MockContext()
    _seed_admin(ctx, "alice")
    event = _event(user_id="alice")
    cmd_config(ctx, event, {"action": "admin-add", "value": "<@alice>"})
    cfg = get_config(ctx)
    # The mention "<@alice>" doesn't match the digit regex, so this branch
    # tests the "couldn't parse" path. Test the actual dedup with digits:
    cmd_config(ctx, event, {"action": "admin-add", "value": "123456789012345678"})
    cmd_config(ctx, event, {"action": "admin-add", "value": "<@123456789012345678>"})
    cfg = get_config(ctx)
    # Should appear exactly once
    assert cfg["admin_user_ids"].count("123456789012345678") == 1


def test_admin_add_requires_admin_caller():
    """Only existing admins can add more admins (gated by has_manage_guild)."""
    ctx = MockContext()
    # No admins yet — the bootstrap branch is for setup; admin-add requires admin
    event = _event(user_id="rando")
    cmd_config(ctx, event, {"action": "admin-add", "value": "<@123456789012345678>"})
    cfg = get_config(ctx)
    assert cfg["admin_user_ids"] == []        # unchanged
    last = ctx.interaction.responses[-1]
    assert "admin-bootstrap" in last["content"]


# ── /trivia config action:admin-remove ─────────────────────────────────────

def test_admin_remove_removes_user():
    ctx = MockContext()
    _seed_admin(ctx, "alice", "bob")
    event = _event(user_id="alice")
    cmd_config(ctx, event, {"action": "admin-remove", "value": "<@bob>"})
    # Note: <@bob> doesn't parse (not digits). Use a real-shape ID:
    cmd_config(ctx, event, {"action": "admin-remove", "value": "234567890123456789"})
    # Re-seed with that ID present
    _seed_admin(ctx, "alice", "234567890123456789")
    cmd_config(ctx, event, {"action": "admin-remove", "value": "234567890123456789"})
    cfg = get_config(ctx)
    assert "234567890123456789" not in cfg["admin_user_ids"]
    assert "alice" in cfg["admin_user_ids"]


def test_admin_remove_refuses_to_remove_last_admin():
    """Last admin removal locks everyone out — refuse the operation."""
    ctx = MockContext()
    _seed_admin(ctx, "123456789012345678")
    event = _event(user_id="123456789012345678")
    cmd_config(ctx, event, {"action": "admin-remove", "value": "123456789012345678"})
    cfg = get_config(ctx)
    # Still there
    assert cfg["admin_user_ids"] == ["123456789012345678"]
    last = ctx.interaction.responses[-1]
    assert "last admin" in last["content"]


def test_admin_remove_unknown_user_is_informative():
    ctx = MockContext()
    _seed_admin(ctx, "111111111111111111", "222222222222222222")
    event = _event(user_id="111111111111111111")
    cmd_config(ctx, event, {"action": "admin-remove", "value": "999999999999999999"})
    cfg = get_config(ctx)
    # No change
    assert sorted(cfg["admin_user_ids"]) == ["111111111111111111", "222222222222222222"]
    last = ctx.interaction.responses[-1]
    assert "isn't currently a Trivium admin" in last["content"]


# ── /trivia config action:admin-list ───────────────────────────────────────

def test_admin_list_empty_says_run_bootstrap():
    ctx = MockContext()
    # Have to be an admin to run admin-list, but allowlist is empty.
    # In real life, this happens via Layer A or some other path. Here we
    # cheat by populating then clearing.
    _seed_admin(ctx, "anchor")
    event = _event(user_id="anchor")
    # Clear the list (we can do this via admin-remove only if more than one).
    # Easier: directly set empty after setting up an admin via member.perms
    cfg = dict(DEFAULT_CONFIG)
    cfg["admin_user_ids"] = []
    ctx.kv.set(KV_CONFIG, cfg)
    event["member"] = {"permissions": 0x8}    # ADMINISTRATOR — gets us past gate
    cmd_config(ctx, event, {"action": "admin-list"})
    last = ctx.interaction.responses[-1]
    assert "No Trivium admins configured" in last["content"]


def test_admin_list_shows_admins():
    ctx = MockContext()
    _seed_admin(ctx, "111111111111111111", "222222222222222222")
    event = _event(user_id="111111111111111111")
    cmd_config(ctx, event, {"action": "admin-list"})
    last = ctx.interaction.responses[-1]
    assert "<@111111111111111111>" in last["content"]
    assert "<@222222222222222222>" in last["content"]


# ── /trivia config action:show includes admins ─────────────────────────────

def test_config_show_lists_admins():
    ctx = MockContext()
    _seed_admin(ctx, "111111111111111111")
    event = _event(user_id="111111111111111111")
    cmd_config(ctx, event, {"action": "show"})
    embed = ctx.interaction.responses[-1]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "Admins" in fields
    assert "<@111111111111111111>" in fields["Admins"]
