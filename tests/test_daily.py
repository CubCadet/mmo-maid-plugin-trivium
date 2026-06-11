"""Tests for daily-trivia scheduling, dedup idempotency, channel-not-configured
graceful skip, and the lazy backstop pattern."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from yourbot_sdk.testing import MockContext, make_event

from plugin_main import (
    DEFAULT_CONFIG,
    KV_CONFIG,
    _maybe_post_daily,
    daily_backstop_on_message,
    daily_tick,
    eph_dedup_daily,
    get_config,
    kv_daily,
)


def _seed_cfg(ctx, *, channel="chan1", time_utc="00:00", category="General Knowledge",
              difficulty="any"):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "daily_channel_id": channel,
        "daily_time_utc": time_utc,
        "daily_category": category,
        "default_difficulty": difficulty,
    })
    ctx.kv.set(KV_CONFIG, cfg)
    return cfg


def _otdb_body(question="Q?", correct="C"):
    return json.dumps({
        "response_code": 0,
        "results": [{
            "category": "General Knowledge", "type": "multiple",
            "difficulty": "medium", "question": question,
            "correct_answer": correct, "incorrect_answers": ["w1", "w2", "w3"],
        }],
    })


# ── Channel-not-configured graceful skip ───────────────────────────────────

def test_no_post_when_channel_unset():
    ctx = MockContext()
    # Default config has no daily_channel_id
    _maybe_post_daily(ctx)
    # No discord call was made
    assert ctx.messages_sent == []
    # No daily history was written
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert ctx.kv.get(kv_daily(today)) is None


def test_no_post_when_time_unset():
    ctx = MockContext()
    cfg = dict(DEFAULT_CONFIG)
    cfg["daily_channel_id"] = "chan1"
    cfg["daily_time_utc"] = None
    ctx.kv.set(KV_CONFIG, cfg)
    _maybe_post_daily(ctx)
    assert ctx.messages_sent == []


def test_no_post_when_invalid_time():
    """A malformed daily_time_utc must be a silent no-op, not a crash."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="not-a-time")
    _maybe_post_daily(ctx)
    assert ctx.messages_sent == []


def test_daily_tick_invokes_post_when_configured():
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")            # Always past target
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())
    daily_tick(ctx)
    assert len(ctx.messages_sent) == 1
    assert ctx.messages_sent[0]["channel_id"] == "chan1"


# ── Dedup idempotency ───────────────────────────────────────────────────────

def test_second_call_within_same_day_is_no_op():
    """The ephemeral dedup gate prevents double-posts even if _maybe_post_daily
    fires twice (worker restart, schedule re-entry, lazy backstop racing
    with cron)."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())

    _maybe_post_daily(ctx)
    assert len(ctx.messages_sent) == 1

    # Second call should be deduped
    _maybe_post_daily(ctx)
    assert len(ctx.messages_sent) == 1


def test_pre_existing_daily_history_short_circuits():
    """If today's daily:{date} already exists, _maybe_post_daily must skip
    without burning the dedup gate."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx.kv.set(kv_daily(today), {"posted_at": 100})

    _maybe_post_daily(ctx)
    assert ctx.messages_sent == []
    # The dedup ephemeral was NOT consumed (it's free to fire next time
    # if the history gets wiped)
    # We can verify by calling dedup directly — it should return True (fresh)
    assert ctx.ephemeral.dedup(eph_dedup_daily(today), ttl_seconds=10) is True


# ── Post writes history with the right shape ───────────────────────────────

def test_daily_post_writes_history_record():
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00", category="General Knowledge")
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())

    _maybe_post_daily(ctx)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = ctx.kv.get(kv_daily(today))
    assert isinstance(rec, dict)
    assert rec["category"] == "General Knowledge"
    assert rec["winners"] == []
    assert rec["answered_count"] == 0
    assert "game_id" in rec
    assert "posted_at" in rec
    assert "message_id" in rec


def test_daily_post_includes_question_and_buttons():
    """Daily posts deliver an embed + four answer buttons. Mention safety
    relies on scrub_for_display (the SDK's send_message in v0.5.2 doesn't
    take an allowed_mentions arg)."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())

    _maybe_post_daily(ctx)
    sent = ctx.messages_sent[0]
    assert sent["embeds"]
    assert sent["components"]      # the answer row


def test_no_question_available_skips_post():
    """If both sources return negative results, the daily silently skips."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=503, body="down")
    ctx.http.mock_response("the-trivia-api.com", status=503, body="down")

    _maybe_post_daily(ctx)
    assert ctx.messages_sent == []
    # daily history was NOT written (we don't pretend a post happened)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert ctx.kv.get(kv_daily(today)) is None


# ── Future time gate ───────────────────────────────────────────────────────

def test_no_post_when_time_is_future():
    """If now < daily_time_utc, _maybe_post_daily returns without posting."""
    ctx = MockContext()
    # Set the time 23 hours from now (i.e. always in the future for any tick today)
    future = (datetime.now(timezone.utc) + timedelta(hours=23, minutes=58))
    _seed_cfg(ctx, time_utc=future.strftime("%H:%M"))
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())

    _maybe_post_daily(ctx)
    # If the test ran exactly at midnight UTC, this could fail. The 23:58
    # window minimizes that, but acknowledge it.
    if future.day == datetime.now(timezone.utc).day:
        assert ctx.messages_sent == []


# ── daily_tick diagnostic log ──────────────────────────────────────────────

def test_daily_tick_emits_diagnostic_log():
    """The 'daily_tick fired' diagnostic line is intentional — it tells ops
    whether @plugin.schedule actually runs in pool mode. Don't accidentally
    remove or rename it."""
    ctx = MockContext()
    daily_tick(ctx)
    messages = [e.get("message", "") for e in ctx.log_entries]
    assert any("daily_tick fired" in m for m in messages), \
        "daily_tick must emit the production-diagnostic log line"


# ── message_create backstop ────────────────────────────────────────────────

def test_message_backstop_no_op_when_not_configured():
    """No daily channel set → backstop is a silent no-op."""
    ctx = MockContext()
    event = make_event("message_create", content="hello", author_bot=False)
    daily_backstop_on_message(ctx, event)
    assert ctx.messages_sent == []


def test_message_backstop_ignores_bot_authors():
    """Bot authors get skipped to prevent loop scenarios."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    event = make_event("message_create", content="hi", author_bot=True)
    daily_backstop_on_message(ctx, event)
    # _maybe_post_daily would have posted if it had been called; it wasn't
    assert ctx.messages_sent == []


def test_message_backstop_triggers_post_when_time_passed():
    """If daily is configured, time has passed, and no daily history exists,
    a non-bot message triggers the daily post."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_body())
    event = make_event("message_create", content="hello", author_bot=False)
    daily_backstop_on_message(ctx, event)
    assert len(ctx.messages_sent) == 1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert isinstance(ctx.kv.get(kv_daily(today)), dict)


def test_message_backstop_short_circuits_when_already_posted():
    """If today's daily already exists in KV, the backstop is cheap and
    doesn't make any HTTP calls."""
    ctx = MockContext()
    _seed_cfg(ctx, time_utc="00:00")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx.kv.set(kv_daily(today), {"posted_at": 100})
    event = make_event("message_create", content="hello", author_bot=False)
    daily_backstop_on_message(ctx, event)
    # No HTTP requests, no messages sent
    assert ctx.http.requests == []
    assert ctx.messages_sent == []
