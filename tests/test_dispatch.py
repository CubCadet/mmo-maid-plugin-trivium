"""End-to-end slash-command dispatch tests using real-shape events.

These tests caught the v1.0.0 → v1.0.1 production bug where the runtime
delivers slash-command args under `command_options` while the SDK
reference docs documented `options`. Trivia routing pulled the wrong key
and fell through to the help message on every invocation.

Each test below uses an interaction event payload shaped exactly like
the real v0.5.2 runtime sends (verified against trivium_logs).
"""
from __future__ import annotations

import json

from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import (
    KV_CONFIG,
    DEFAULT_CONFIG,
    PERM_MANAGE_GUILD,
    trivia_root,
)


# Real-shape interaction event from the v0.5.2 runtime. See trivium_logs
# entry 10615309 for the canonical structure.
def _runtime_event(*, command_options, user_id="1185783088802955287",
                   channel_id="1504829648951971950",
                   guild_id="1504263538015994016"):
    """Build an event dict whose shape matches what the v0.5.2 runtime
    actually delivers — `command_options` rather than `options`."""
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="trivia",
        user_id=user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        custom_id="",
        modal_values={},
        # The real runtime key — what the production bug hinged on.
        command_options=command_options,
    )


# ── /trivia daily ───────────────────────────────────────────────────────────

def test_dispatch_daily_routes_to_cmd_daily():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "daily", "type": 1, "options": []},
    ])
    trivia_root(ctx, event)
    # cmd_daily was called — its no-config path gives this ephemeral.
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert "isn't configured" in last["content"]
    # NOT the help fallback
    assert "Use `/trivia play`" not in last["content"]


# ── /trivia play ────────────────────────────────────────────────────────────

def test_dispatch_play_routes_to_cmd_play():
    ctx = MockContext()
    # Stub OTDB so cmd_play doesn't hang trying to fetch
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=json.dumps({
        "response_code": 0,
        "results": [{
            "category": "Music", "type": "multiple", "difficulty": "easy",
            "question": "Q?", "correct_answer": "C",
            "incorrect_answers": ["w1", "w2", "w3"],
        }],
    }))
    event = _runtime_event(command_options=[
        {"name": "play", "type": 1, "options": [
            {"name": "category", "type": 3, "value": "Music"},
            {"name": "difficulty", "type": 3, "value": "easy"},
        ]},
    ])
    trivia_root(ctx, event)
    # cmd_play deferred the interaction and posted a public message
    assert ctx.interaction.defers, "cmd_play must defer"
    assert ctx.messages_sent, "cmd_play must post the round embed"
    # And gave an ephemeral "Round started" followup
    assert any("Round started" in f["content"] for f in ctx.interaction.followups)


# ── /trivia leaderboard ────────────────────────────────────────────────────

def test_dispatch_leaderboard_routes_to_cmd_leaderboard():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "leaderboard", "type": 1, "options": []},
    ])
    trivia_root(ctx, event)
    last = ctx.interaction.responses[-1]
    assert "No trivia scores yet" in last["content"]


# ── /trivia stats ──────────────────────────────────────────────────────────

def test_dispatch_stats_self_no_user_arg():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "stats", "type": 1, "options": []},
    ])
    trivia_root(ctx, event)
    embed = ctx.interaction.responses[-1]["embeds"][0]
    assert "<@1185783088802955287>" in embed["description"]


def test_dispatch_stats_with_user_arg():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "stats", "type": 1, "options": [
            {"name": "user", "type": 6, "value": "999000111222"},
        ]},
    ])
    trivia_root(ctx, event)
    embed = ctx.interaction.responses[-1]["embeds"][0]
    assert "<@999000111222>" in embed["description"]


# ── /trivia config ─────────────────────────────────────────────────────────

def test_dispatch_config_show_routes_to_cmd_config_with_admin():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "config", "type": 1, "options": [
            {"name": "action", "type": 3, "value": "show"},
        ]},
    ])
    # Inject member.permissions for admin gate
    event["member"] = {"permissions": PERM_MANAGE_GUILD}
    trivia_root(ctx, event)
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert last["embeds"]
    assert any("Trivium" in (e.get("title") or "") for e in last["embeds"])


def test_dispatch_config_set_difficulty():
    ctx = MockContext()
    event = _runtime_event(command_options=[
        {"name": "config", "type": 1, "options": [
            {"name": "action", "type": 3, "value": "difficulty"},
            {"name": "value", "type": 3, "value": "hard"},
        ]},
    ])
    event["member"] = {"permissions": PERM_MANAGE_GUILD}
    trivia_root(ctx, event)
    cfg = ctx.kv.get(KV_CONFIG) or {}
    assert cfg.get("default_difficulty") == "hard"


# ── Fallback path — empty command_options ──────────────────────────────────

def test_dispatch_no_options_shows_help_fallback():
    """If somehow neither command_options nor options is present, we should
    still respond with the help message (rather than crash)."""
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       command_name="trivia", user_id="u1")
    trivia_root(ctx, event)
    last = ctx.interaction.responses[-1]
    assert "Use `/trivia play`" in last["content"]


# ── Backward-compat: pre-fix code path still works if SDK ever realigns ────

def test_dispatch_falls_back_to_options_key_if_command_options_missing():
    """If a future SDK version delivers args under the documented `options`
    key instead of `command_options`, we still route correctly."""
    ctx = MockContext()
    event = make_event(
        "interaction_create",
        interaction_type=2,
        command_name="trivia",
        user_id="u1",
        # Note: no command_options — this is the "options" path
        options=[{"name": "leaderboard", "type": 1, "options": []}],
    )
    trivia_root(ctx, event)
    last = ctx.interaction.responses[-1]
    assert "No trivia scores yet" in last["content"]
