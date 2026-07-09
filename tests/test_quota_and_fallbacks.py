"""cmd_play failure/fallback coverage, driven through the real dispatch path:

- both trivia sources down → ephemeral "sources are unavailable", no round
- ctx.discord.send_message raising SdkError → ephemeral "Couldn't post"
- KvQuotaError on the inflight save (v1.0.13) → the just-posted round message
  is edited down to a "couldn't start" notice and the starter is told the
  server's trivia storage is full — with NO "Round started" success ack.

Events are shaped like the real v0.5.2 runtime delivers them
(`command_options`, not `options`) — same convention as test_dispatch.py.
"""
from __future__ import annotations

import json

from yourbot_sdk import KvQuotaError, SdkError
from yourbot_sdk.testing import MockContext, make_event

from plugin_main import trivia_root


def _play_event(*, user_id="u1", channel_id="chan1"):
    return make_event(
        "interaction_create",
        interaction_type=2,
        command_name="trivia",
        user_id=user_id,
        channel_id=channel_id,
        custom_id="",
        modal_values={},
        command_options=[{"name": "play", "type": 1, "options": [
            {"name": "category", "type": 3, "value": "General Knowledge"},
            {"name": "difficulty", "type": 3, "value": "easy"},
        ]}],
    )


def _stub_healthy_otdb(ctx):
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=json.dumps({
        "response_code": 0,
        "results": [{
            "category": "General Knowledge", "type": "multiple",
            "difficulty": "easy", "question": "Q?", "correct_answer": "C",
            "incorrect_answers": ["w1", "w2", "w3"],
        }],
    }))


def _inflight_writes(ctx):
    return [w for w in ctx.kv_writes
            if w["op"] == "set" and w["key"].startswith("inflight:")]


# ── Both sources down ───────────────────────────────────────────────────────

def test_play_all_sources_down_reports_ephemerally_and_starts_nothing():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=503, body="down")
    ctx.http.mock_response("the-trivia-api.com", status=503, body="down")

    trivia_root(ctx, _play_event())

    assert ctx.interaction.defers, "cmd_play defers before fetching"
    fails = [f for f in ctx.interaction.followups
             if "sources are unavailable" in f["content"]]
    assert fails
    assert fails[0]["ephemeral"] is True
    assert _inflight_writes(ctx) == []
    assert ctx.messages_sent == []


# ── Discord send failure ────────────────────────────────────────────────────

def test_play_send_message_failure_reports_ephemerally():
    """A Discord outage on the round post must surface as the ephemeral
    "Couldn't post" followup from cmd_play's own SdkError handler — not
    bubble up into trivia_root's generic last-resort catch."""
    ctx = MockContext()
    _stub_healthy_otdb(ctx)

    def _send_fails(**kwargs):
        raise SdkError("discord unavailable")

    ctx.discord.send_message = _send_fails
    trivia_root(ctx, _play_event())

    fails = [f for f in ctx.interaction.followups
             if "Couldn't post" in f["content"]]
    assert fails
    assert fails[0]["ephemeral"] is True
    assert _inflight_writes(ctx) == []
    # cmd_play handled it itself — the generic crash apology never fired.
    assert not any("Something went wrong" in (r.get("content") or "")
                   for r in ctx.interaction.responses)
    assert not any("Round started" in f["content"]
                   for f in ctx.interaction.followups)


# ── Inflight-save KvQuotaError (v1.0.13) ────────────────────────────────────

def test_play_inflight_quota_takes_round_down_and_tells_the_truth():
    """If the kv_inflight save hits KvQuotaError, no click could ever score
    the round — cmd_play must edit the just-posted message down to a
    "couldn't start" notice (no embeds/components), tell the starter the
    storage is full, and NOT claim "Round started"."""
    ctx = MockContext()
    _stub_healthy_otdb(ctx)

    # Only the inflight: write fails — the mock KV has no quota simulation,
    # so wrap set() selectively (batch cache / seen-ring writes still land).
    healthy_set = ctx.kv.set

    def _quota_on_inflight(key, value, **kwargs):
        if key.startswith("inflight:"):
            raise KvQuotaError("kv quota exhausted")
        return healthy_set(key, value, **kwargs)

    ctx.kv.set = _quota_on_inflight
    trivia_root(ctx, _play_event())

    # The round message went out, then was edited down to the failure notice.
    assert len(ctx.messages_sent) == 1
    assert ctx.messages_edited, "the dead round message must be edited"
    edit = ctx.messages_edited[-1]
    assert edit["message_id"] == ctx.messages_sent[0]["message_id"]
    assert "couldn't start" in (edit["content"] or "")
    assert edit["embeds"] == []
    assert edit["components"] == []

    followups = ctx.interaction.followups
    assert any("storage is full" in f["content"] and f["ephemeral"]
               for f in followups)
    assert not any("Round started" in f["content"] for f in followups)
    # No success metric for a round that never started.
    assert not any(m["metric"] == "trivium_round_started"
                   for m in ctx.metrics.recorded)


# ── Last-resort net after a defer (v1.0.13) ─────────────────────────────────

def test_crash_after_defer_still_reaches_user_via_followup():
    """trivia_root's apology must survive respond() being rejected. After
    cmd_play/cmd_config defer, a crashing sub-handler leaves the interaction
    ack'd — the real transport raises plain RuntimeError on the double-ack
    (MockContext doesn't enforce that, so this test does) and the net must
    fall back to followup() instead of leaving an eternal "thinking…"."""
    ctx = MockContext()
    _stub_healthy_otdb(ctx)

    # Crash cmd_play after its defer: the inflight read isn't reached, but
    # the cooldown ephemeral call is — easiest deterministic post-defer
    # crash is send_message raising something cmd_play does NOT catch.
    def _send_crashes(**kwargs):
        raise RuntimeError("RPC error (discord.send_message): unclassified")

    ctx.discord.send_message = _send_crashes

    # Enforce the platform's single-ack rule that MockContext skips.
    def _respond_rejected(**kwargs):
        raise RuntimeError("RPC error (interaction.respond): already acknowledged")

    ctx.interaction.respond = _respond_rejected

    trivia_root(ctx, _play_event())  # must not raise

    assert ctx.interaction.defers, "cmd_play defers first"
    apology = [f for f in ctx.interaction.followups
               if "Something went wrong" in f["content"]]
    assert apology and apology[0]["ephemeral"] is True
