"""Tests for the three-layer has_manage_guild check.

Layered defense:
    A. event["member"]["permissions"] (forward-compat if SDK exposes it)
    B. Cached guild_owner_id + role permission union
    C. Cold-cache refresh: get_guild() + list_roles()

Fail-closed on any Discord error — the v1.0.1 vulnerability was that the
"unknown" branch returned True. These tests pin the new deny-by-default
contract in place.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import (
    ADMIN_CACHE_TTL,
    KV_ADMIN_CACHE,
    PERM_ADMINISTRATOR,
    PERM_MANAGE_GUILD,
    has_manage_guild,
)


# ── Discord mock helpers ───────────────────────────────────────────────────

class _ScriptedDiscord:
    """Drop-in replacement for ctx.discord that scripts get_guild,
    list_roles, and get_member responses. MockContext's discord doesn't
    let us return arbitrary dicts from these methods, so we monkey-patch
    them onto ctx.discord for the duration of a test."""

    def __init__(self, *, guild=None, roles=None, members=None,
                 guild_error=None, roles_error=None, member_error=None):
        self.guild = guild or {}
        self.roles = roles or []
        self.members = members or {}
        self.guild_error = guild_error
        self.roles_error = roles_error
        self.member_error = member_error
        self.get_guild_calls = 0
        self.list_roles_calls = 0
        self.get_member_calls: list[str] = []

    def get_guild(self):
        self.get_guild_calls += 1
        if self.guild_error:
            raise self.guild_error
        return dict(self.guild)

    def list_roles(self):
        self.list_roles_calls += 1
        if self.roles_error:
            raise self.roles_error
        return list(self.roles)

    def get_member(self, *, user_id):
        self.get_member_calls.append(user_id)
        if self.member_error:
            raise self.member_error
        return dict(self.members.get(user_id, {}))


def _ctx_with_discord(scripted: _ScriptedDiscord) -> MockContext:
    ctx = MockContext()
    # Mokey-patch the three methods has_manage_guild calls
    ctx.discord.get_guild = scripted.get_guild
    ctx.discord.list_roles = scripted.list_roles
    ctx.discord.get_member = scripted.get_member
    return ctx


def _event(user_id="u1", **overrides: Any) -> dict:
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="trivia",
        user_id=user_id,
        **overrides,
    )


# ── Layer A: event["member"]["permissions"] (forward-compat) ──────────────

def test_layer_a_perms_int_with_manage_guild_bit_allows():
    ctx = _ctx_with_discord(_ScriptedDiscord())
    event = _event()
    event["member"] = {"permissions": PERM_MANAGE_GUILD}
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is True
    assert src == "member_perms"


def test_layer_a_perms_string_with_administrator_bit_allows():
    """Discord can deliver permissions as a stringified int (Discord-spec)."""
    ctx = _ctx_with_discord(_ScriptedDiscord())
    event = _event()
    event["member"] = {"permissions": str(PERM_ADMINISTRATOR)}
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is True
    assert src == "member_perms"


def test_layer_a_perms_present_without_required_bits_denies():
    ctx = _ctx_with_discord(_ScriptedDiscord())
    event = _event()
    event["member"] = {"permissions": 0x4}    # SEND_MESSAGES only
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is False
    assert src == "no_perms_member"


# ── Layer B (cached): guild-owner shortcut ─────────────────────────────────

def test_layer_b_cache_hit_owner_match_allows_without_discord_calls():
    """When the user is the guild owner, no get_member call is needed."""
    scripted = _ScriptedDiscord()
    ctx = _ctx_with_discord(scripted)
    ctx.kv.set(KV_ADMIN_CACHE, {
        "owner_id": "owner",
        "roles_by_id": {},
        "fetched_at": int(time.time()),
    })
    allowed, src = has_manage_guild(ctx, _event(user_id="owner"))
    assert allowed is True
    assert src == "guild_owner"
    assert scripted.get_guild_calls == 0
    assert scripted.list_roles_calls == 0
    assert scripted.get_member_calls == []


def test_layer_b_cache_hit_role_with_manage_guild_allows():
    scripted = _ScriptedDiscord(members={
        "u1": {"user_id": "u1", "roles": ["role_admin"]},
    })
    ctx = _ctx_with_discord(scripted)
    ctx.kv.set(KV_ADMIN_CACHE, {
        "owner_id": "someone_else",
        "roles_by_id": {"role_admin": PERM_MANAGE_GUILD, "role_basic": 0x4},
        "fetched_at": int(time.time()),
    })
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is True
    assert src == "role_perms"
    # One get_member call, no fresh get_guild/list_roles
    assert scripted.get_guild_calls == 0
    assert scripted.list_roles_calls == 0
    assert scripted.get_member_calls == ["u1"]


def test_layer_b_cache_hit_role_with_administrator_allows():
    scripted = _ScriptedDiscord(members={
        "u1": {"user_id": "u1", "roles": ["role_admin"]},
    })
    ctx = _ctx_with_discord(scripted)
    ctx.kv.set(KV_ADMIN_CACHE, {
        "owner_id": "someone_else",
        "roles_by_id": {"role_admin": PERM_ADMINISTRATOR},
        "fetched_at": int(time.time()),
    })
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is True
    assert src == "role_perms"


def test_layer_b_cache_hit_no_qualifying_role_denies():
    scripted = _ScriptedDiscord(members={
        "u1": {"user_id": "u1", "roles": ["role_basic"]},
    })
    ctx = _ctx_with_discord(scripted)
    ctx.kv.set(KV_ADMIN_CACHE, {
        "owner_id": "someone_else",
        "roles_by_id": {"role_basic": 0x4, "role_admin": PERM_MANAGE_GUILD},
        "fetched_at": int(time.time()),
    })
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is False
    assert src == "no_perms_roles"


# ── Layer C (cold): cache refresh ──────────────────────────────────────────

def test_layer_c_cold_cache_calls_get_guild_and_list_roles_once():
    scripted = _ScriptedDiscord(
        guild={"id": "g1", "owner_id": "owner"},
        roles=[{"id": "role_admin", "permissions": str(PERM_MANAGE_GUILD)}],
        members={"u1": {"user_id": "u1", "roles": ["role_admin"]}},
    )
    ctx = _ctx_with_discord(scripted)
    # No cache exists
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is True
    assert src == "role_perms"
    assert scripted.get_guild_calls == 1
    assert scripted.list_roles_calls == 1
    # Cache was populated for future calls
    cache = ctx.kv.get(KV_ADMIN_CACHE)
    assert cache is not None
    assert cache["owner_id"] == "owner"
    assert cache["roles_by_id"]["role_admin"] == PERM_MANAGE_GUILD


def test_layer_c_second_call_hits_cache_no_new_discord_lookups():
    scripted = _ScriptedDiscord(
        guild={"id": "g1", "owner_id": "owner"},
        roles=[{"id": "role_admin", "permissions": str(PERM_MANAGE_GUILD)}],
        members={"u1": {"user_id": "u1", "roles": ["role_admin"]}},
    )
    ctx = _ctx_with_discord(scripted)
    has_manage_guild(ctx, _event(user_id="u1"))            # cold
    scripted.get_guild_calls = 0
    scripted.list_roles_calls = 0
    has_manage_guild(ctx, _event(user_id="u1"))            # warm
    assert scripted.get_guild_calls == 0
    assert scripted.list_roles_calls == 0


def test_layer_c_stale_cache_triggers_refetch():
    scripted = _ScriptedDiscord(
        guild={"id": "g1", "owner_id": "owner"},
        roles=[{"id": "role_admin", "permissions": str(PERM_MANAGE_GUILD)}],
        members={"u1": {"user_id": "u1", "roles": ["role_admin"]}},
    )
    ctx = _ctx_with_discord(scripted)
    # Stale cache: fetched ADMIN_CACHE_TTL + 60s ago
    ctx.kv.set(KV_ADMIN_CACHE, {
        "owner_id": "stale_owner",
        "roles_by_id": {"role_old": PERM_MANAGE_GUILD},
        "fetched_at": int(time.time()) - ADMIN_CACHE_TTL - 60,
    })
    has_manage_guild(ctx, _event(user_id="u1"))
    # Refetched
    assert scripted.get_guild_calls == 1
    assert scripted.list_roles_calls == 1
    new_cache = ctx.kv.get(KV_ADMIN_CACHE)
    assert new_cache["owner_id"] == "owner"


# ── Fail-closed paths ──────────────────────────────────────────────────────

def test_sdk_error_on_get_guild_denies_closed():
    from mmo_maid_sdk import SdkError
    scripted = _ScriptedDiscord(guild_error=SdkError("upstream down"))
    ctx = _ctx_with_discord(scripted)
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is False
    assert src == "denied_error_no_cache"
    # The warning log is recorded
    assert any("get_guild failed" in e.get("message", "") for e in ctx.log_entries)


def test_sdk_error_on_list_roles_denies_closed():
    from mmo_maid_sdk import SdkError
    scripted = _ScriptedDiscord(
        guild={"owner_id": "owner"},
        roles_error=SdkError("upstream down"),
    )
    ctx = _ctx_with_discord(scripted)
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is False
    assert src == "denied_error_no_cache"


def test_sdk_error_on_get_member_denies_closed():
    from mmo_maid_sdk import SdkError
    scripted = _ScriptedDiscord(
        guild={"owner_id": "owner"},
        roles=[{"id": "role_admin", "permissions": str(PERM_MANAGE_GUILD)}],
        member_error=SdkError("404"),
    )
    ctx = _ctx_with_discord(scripted)
    allowed, src = has_manage_guild(ctx, _event(user_id="u1"))
    assert allowed is False
    assert src.startswith("denied_error_")


# ── Critical regression: the 1.0.1 vulnerability is dead ──────────────────

def test_missing_member_no_longer_allows_by_default():
    """The 1.0.1 vulnerability: no member.permissions in the payload → True.
    In 1.0.2 the fallback path requires Discord lookups, which deny when
    they fail. This pins the new contract in place forever."""
    from mmo_maid_sdk import SdkError
    scripted = _ScriptedDiscord(guild_error=SdkError("simulating 1.0.1 environment"))
    ctx = _ctx_with_discord(scripted)
    event = _event(user_id="any_user")
    # No "member" key — same as the 1.0.1 production payload shape
    assert event.get("member") is None
    allowed, src = has_manage_guild(ctx, event)
    assert allowed is False
    assert src != "manifest_default"


# ── Backward-compat: layer A short-circuits without Discord calls ─────────

def test_layer_a_present_skips_layers_b_and_c_entirely():
    """If member.permissions is present, no Discord API calls happen.
    Cheap path stays cheap."""
    scripted = _ScriptedDiscord()
    ctx = _ctx_with_discord(scripted)
    event = _event()
    event["member"] = {"permissions": PERM_MANAGE_GUILD}
    has_manage_guild(ctx, event)
    assert scripted.get_guild_calls == 0
    assert scripted.list_roles_calls == 0
    assert scripted.get_member_calls == []
