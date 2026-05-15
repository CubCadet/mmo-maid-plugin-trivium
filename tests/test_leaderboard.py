"""Tests for /trivia leaderboard and /trivia stats."""
from __future__ import annotations

from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import (
    KV_SCORE_INDEX,
    add_to_score_index,
    cmd_leaderboard,
    cmd_stats,
    kv_score,
)


def _seed_score(ctx, user_id, *, score=0, correct=0, total=0,
                streak_current=0, streak_best=0):
    """Write a score record AND update the score index.

    1.0.4: cmd_leaderboard reads from KV_SCORE_INDEX (a manually maintained
    list of user_ids), not via kv.list. Tests must keep both in sync.
    """
    ctx.kv.set(kv_score(user_id), {
        "score": score, "correct": correct, "total": total,
        "streak_current": streak_current, "streak_best": streak_best,
        "last_played_ts": 0,
    })
    add_to_score_index(ctx, user_id)


# ── /trivia leaderboard ────────────────────────────────────────────────────

def test_empty_leaderboard_says_no_scores_yet():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    assert ctx.interaction.responses
    assert "No trivia scores yet" in ctx.interaction.responses[-1]["content"]


def test_zero_score_only_treated_as_empty():
    """A user record with score=0 (e.g., from break_streak before any correct
    answer) shouldn't populate the leaderboard."""
    ctx = MockContext()
    _seed_score(ctx, "u1", score=0, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    assert "No trivia scores yet" in ctx.interaction.responses[-1]["content"]


def test_leaderboard_ranks_by_score_descending():
    ctx = MockContext()
    _seed_score(ctx, "low", score=10, correct=1, total=2)
    _seed_score(ctx, "mid", score=50, correct=5, total=10)
    _seed_score(ctx, "high", score=200, correct=10, total=10)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    embed = ctx.interaction.responses[-1]["embeds"][0]
    desc = embed["description"]
    # Order: high, mid, low — first occurrence wins
    pos_high = desc.index("<@high>")
    pos_mid = desc.index("<@mid>")
    pos_low = desc.index("<@low>")
    assert pos_high < pos_mid < pos_low


def test_leaderboard_truncates_to_top_10():
    ctx = MockContext()
    for i in range(15):
        _seed_score(ctx, f"u{i:02d}", score=100 - i, correct=1, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    # u00..u09 should be present (top 10)
    for i in range(10):
        assert f"<@u{i:02d}>" in desc
    # u10..u14 should NOT be present
    for i in range(10, 15):
        assert f"<@u{i:02d}>" not in desc


def test_leaderboard_uses_allowed_mentions_none():
    """User-id mentions in the leaderboard must not actually ping anyone."""
    ctx = MockContext()
    _seed_score(ctx, "u1", score=100, correct=1, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    assert ctx.interaction.responses[-1].get("allowed_mentions") == {"parse": []}


def test_leaderboard_shows_accuracy_percentage():
    ctx = MockContext()
    _seed_score(ctx, "sharp", score=100, correct=10, total=10)    # 100%
    _seed_score(ctx, "mid", score=50, correct=5, total=10)        # 50%
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "100%" in desc
    assert "50%" in desc


# ── v1.0.4 regression: score-index path ────────────────────────────────────

def test_leaderboard_reads_from_score_index_not_kv_list():
    """v1.0.4: cmd_leaderboard reads from the manual scoreindex:users
    KV value (a list of user_ids) and fetches per-user records via
    kv.get_many. v1.0.2 and v1.0.3 both used kv.list*/kv.list_values
    primitives that came back empty in production."""
    ctx = MockContext()
    _seed_score(ctx, "alice", score=100, correct=5, total=5)
    _seed_score(ctx, "bob", score=80, correct=4, total=5)
    event = make_event("interaction_create", interaction_type=2, user_id="someone")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "<@alice>" in desc
    assert "<@bob>" in desc


def test_leaderboard_ignores_users_not_in_index_even_if_score_key_exists():
    """If a score:<uid> KV key exists but the user_id isn't in the index,
    they don't appear on the leaderboard. This is by design — the index is
    the source of truth in 1.0.4 (since kv.list is unreliable)."""
    ctx = MockContext()
    # Write a score record directly, bypassing add_to_score_index
    ctx.kv.set(kv_score("orphan"), {
        "score": 999, "correct": 99, "total": 99,
        "streak_current": 0, "streak_best": 0, "last_played_ts": 0,
    })
    # Index doesn't include "orphan"
    _seed_score(ctx, "alice", score=10, correct=1, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="someone")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "<@orphan>" not in desc
    assert "<@alice>" in desc


def test_leaderboard_index_dedupes_repeat_additions():
    """add_to_score_index should be idempotent on repeat user_ids."""
    ctx = MockContext()
    _seed_score(ctx, "u1", score=10, correct=1, total=1)
    _seed_score(ctx, "u1", score=20, correct=2, total=2)        # re-seed
    idx = ctx.kv.get(KV_SCORE_INDEX) or []
    assert idx == ["u1"]


def test_leaderboard_parses_json_string_values_defensively():
    """If the v0.5.2 runtime returns score values as JSON strings (rather
    than deserialized dicts), cmd_leaderboard should still recover."""
    import json as _json
    ctx = MockContext()
    # Write a string-encoded value to simulate a quirky runtime response.
    ctx.kv.set(kv_score("u1"), _json.dumps({
        "score": 42, "correct": 3, "total": 5,
        "streak_current": 1, "streak_best": 2, "last_played_ts": 0,
    }))
    # Add to the index manually (since we bypassed _seed_score's index update)
    add_to_score_index(ctx, "u1")
    event = make_event("interaction_create", interaction_type=2, user_id="someone")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    assert "<@u1>" in desc
    assert "42" in desc


def test_leaderboard_batches_get_many_over_50_key_chunks():
    """get_many caps at 50 keys per call. Verify cmd_leaderboard batches."""
    ctx = MockContext()
    for i in range(120):
        _seed_score(ctx, f"u{i:03d}", score=1000 - i, correct=1, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="someone")
    cmd_leaderboard(ctx, event)
    desc = ctx.interaction.responses[-1]["embeds"][0]["description"]
    # u000 has the highest score → appears
    assert "<@u000>" in desc
    # The top 10 cap still applies — u099 should not appear
    assert "<@u099>" not in desc


def test_leaderboard_emits_diagnostic_log_with_counts():
    """The "leaderboard fetched" info log captures index_size and
    value_count so ops can see what the index + get_many returned. Don't
    accidentally remove it."""
    ctx = MockContext()
    _seed_score(ctx, "u1", score=50, correct=1, total=1)
    event = make_event("interaction_create", interaction_type=2, user_id="someone")
    cmd_leaderboard(ctx, event)
    assert any("leaderboard fetched" in e.get("message", "")
               for e in ctx.log_entries)


# ── /trivia stats ──────────────────────────────────────────────────────────

def test_stats_self_with_no_record_returns_zeros():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2, user_id="newbie")
    cmd_stats(ctx, event, {})
    embed = ctx.interaction.responses[-1]["embeds"][0]
    assert "<@newbie>" in embed["description"]


def test_stats_self_returns_own_record():
    ctx = MockContext()
    _seed_score(ctx, "u1", score=75, correct=5, total=10,
                streak_current=2, streak_best=4)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_stats(ctx, event, {})
    embed = ctx.interaction.responses[-1]["embeds"][0]
    desc = embed["description"]
    assert "<@u1>" in desc
    # Score / correct / streak should appear in the fields
    fields_str = " ".join(f"{f['name']}={f['value']}" for f in embed["fields"])
    assert "75" in fields_str
    assert "5" in fields_str   # correct
    assert "10" in fields_str  # total
    assert "current **2**" in fields_str
    assert "best **4**" in fields_str


def test_stats_lookup_other_user_when_user_option_provided():
    """Per the plan: /trivia stats user:<other> is open by default."""
    ctx = MockContext()
    _seed_score(ctx, "u2", score=200, correct=10, total=10)
    event = make_event("interaction_create", interaction_type=2, user_id="u1")
    cmd_stats(ctx, event, {"user": "u2"})
    embed = ctx.interaction.responses[-1]["embeds"][0]
    assert "<@u2>" in embed["description"]
    fields_str = " ".join(f"{f['name']}={f['value']}" for f in embed["fields"])
    assert "200" in fields_str


def test_stats_handles_division_by_zero_for_no_attempts():
    """A user with total=0 must not crash the / divide for accuracy."""
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2, user_id="fresh")
    cmd_stats(ctx, event, {})
    embed = ctx.interaction.responses[-1]["embeds"][0]
    fields_str = " ".join(f"{f['name']}={f['value']}" for f in embed["fields"])
    # Display "—" rather than "0%" or NaN
    assert "—" in fields_str
