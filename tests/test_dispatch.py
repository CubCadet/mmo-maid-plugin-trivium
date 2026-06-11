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

from yourbot_sdk.testing import MockClock, MockContext, make_event

from plugin_main import (
    COOLDOWN_SECONDS,
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


# ── Cooldown gate (MockClock-driven, SDK 0.5.4+) ──────────────────────────

def _play_event(user_id="u1"):
    return _runtime_event(
        user_id=user_id,
        command_options=[{"name": "play", "type": 1, "options": [
            {"name": "category", "type": 3, "value": "Music"},
            {"name": "difficulty", "type": 3, "value": "easy"},
        ]}],
    )


def _stub_otdb(ctx):
    """Wire MockContext.http so cmd_play has a working OTDB to fetch from."""
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


def test_cooldown_gate_blocks_second_play_within_window():
    """Two /trivia plays inside COOLDOWN_SECONDS: the second is rejected
    ephemerally with a 'Slow down' message before the defer fires.

    Locks in the comment at __main__.py:1213 — the cooldown check must
    happen BEFORE ctx.interaction.defer(), so a tripped cooldown shows
    instantly rather than after a 3-second 'thinking…' indicator."""
    clock = MockClock(start=1000.0)
    ctx = MockContext(clock=clock)
    _stub_otdb(ctx)

    # First call: succeeds. Defer + public round embed + ephemeral followup.
    trivia_root(ctx, _play_event())
    assert ctx.interaction.defers, "first /trivia play must defer"
    assert ctx.messages_sent, "first /trivia play must post the round embed"
    first_defer_count = len(ctx.interaction.defers)

    # Second call within the cooldown window: blocked.
    trivia_root(ctx, _play_event())
    last = ctx.interaction.responses[-1]
    assert last["ephemeral"] is True
    assert "Slow down" in last["content"]
    # And critically: NO additional defer fired (rejection happened first).
    assert len(ctx.interaction.defers) == first_defer_count


def test_cooldown_gate_releases_after_window_elapses():
    """Once COOLDOWN_SECONDS have passed, /trivia play succeeds again."""
    clock = MockClock(start=1000.0)
    ctx = MockContext(clock=clock)
    _stub_otdb(ctx)

    trivia_root(ctx, _play_event())
    defers_after_first = len(ctx.interaction.defers)

    clock.advance(COOLDOWN_SECONDS + 1)
    trivia_root(ctx, _play_event())

    # Second call deferred too — cooldown was no longer active.
    assert len(ctx.interaction.defers) == defers_after_first + 1
    # And no "Slow down" message was emitted on this round.
    assert not any("Slow down" in (r.get("content") or "")
                   for r in ctx.interaction.responses)


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
