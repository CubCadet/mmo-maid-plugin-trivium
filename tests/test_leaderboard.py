"""Tests for /trivia leaderboard and /trivia stats."""
from __future__ import annotations

from mmo_maid_sdk.testing import MockContext, make_event

from plugin_main import cmd_leaderboard, cmd_stats, kv_score


def _seed_score(ctx, user_id, *, score=0, correct=0, total=0,
                streak_current=0, streak_best=0):
    ctx.kv.set(kv_score(user_id), {
        "score": score, "correct": correct, "total": total,
        "streak_current": streak_current, "streak_best": streak_best,
        "last_played_ts": 0,
    })


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
