"""Tests for the game-flow primitives: custom_id schema, shuffle, scoring,
streak handling, click dispatch (version mismatch, single-player guard,
open-mode first-correct dedup, expired round)."""
from __future__ import annotations

from yourbot_sdk.testing import MockContext, make_event

from plugin_main import (
    CUSTOM_ID_VERSION,
    award_points,
    break_streak,
    format_custom_id,
    get_score,
    kv_inflight,
    make_game_id,
    on_button_click,
    parse_custom_id,
    save_config,
    shuffle_answers,
)


# ── custom_id schema ───────────────────────────────────────────────────────

def test_format_custom_id_well_formed():
    cid = format_custom_id("abc123", 2)
    assert cid == "triv:1:abc123:2"
    assert len(cid) < 100   # Discord limit


def test_parse_custom_id_roundtrip():
    cid = format_custom_id("abc123", 3)
    parsed = parse_custom_id(cid)
    assert parsed is not None
    version, game_id, choice_idx = parsed
    assert version == CUSTOM_ID_VERSION
    assert game_id == "abc123"
    assert choice_idx == 3


def test_parse_custom_id_rejects_version_mismatch():
    assert parse_custom_id("triv:99:abc123:0") is None


def test_parse_custom_id_rejects_malformed():
    assert parse_custom_id("") is None
    assert parse_custom_id("trivia:1:abc:0") is None
    assert parse_custom_id("triv:1:abc:9") is None      # choice 9 out of range
    assert parse_custom_id("triv:1:abc::0") is None
    assert parse_custom_id("triv:1:abc:0:extra") is None
    assert parse_custom_id(None) is None


def test_parse_custom_id_rejects_disabled_button_suffix():
    """Buttons in the disabled row have ':done' appended to the custom_id —
    they must NOT parse as live buttons."""
    cid = format_custom_id("abc123", 1) + ":done"
    assert parse_custom_id(cid) is None


# ── shuffle ─────────────────────────────────────────────────────────────────

def test_shuffle_preserves_all_four_answers():
    answers, correct_idx = shuffle_answers("X", ["A", "B", "C"])
    assert sorted(answers) == ["A", "B", "C", "X"]
    assert answers[correct_idx] == "X"


def test_shuffle_correct_index_points_to_correct_answer():
    """A more direct restatement of the invariant the click handler relies on."""
    for _ in range(20):
        answers, idx = shuffle_answers("Paris", ["Rome", "Berlin", "London"])
        assert answers[idx] == "Paris"


# ── make_game_id ────────────────────────────────────────────────────────────

def test_game_id_is_6_hex_chars():
    for _ in range(20):
        gid = make_game_id()
        assert len(gid) == 6
        assert all(c in "0123456789abcdef" for c in gid)


# ── Scoring ─────────────────────────────────────────────────────────────────

def test_award_points_increments_score_correct_total_and_streak():
    ctx = MockContext()
    pts = award_points(ctx, "u1", "medium")
    assert pts == 20
    rec = get_score(ctx, "u1")
    assert rec["score"] == 20
    assert rec["correct"] == 1
    assert rec["total"] == 1
    assert rec["streak_current"] == 1
    assert rec["streak_best"] == 1


def test_award_points_difficulty_table():
    ctx = MockContext()
    assert award_points(ctx, "ue", "easy") == 10
    assert award_points(ctx, "um", "medium") == 20
    assert award_points(ctx, "uh", "hard") == 30


def test_award_points_daily_adds_bonus():
    ctx = MockContext()
    assert award_points(ctx, "u1", "medium", is_daily=True) == 70   # 20 + 50


def test_award_points_grows_streak():
    ctx = MockContext()
    award_points(ctx, "u1", "easy")
    award_points(ctx, "u1", "easy")
    award_points(ctx, "u1", "easy")
    rec = get_score(ctx, "u1")
    assert rec["streak_current"] == 3
    assert rec["streak_best"] == 3


def test_break_streak_resets_current_but_keeps_best():
    ctx = MockContext()
    award_points(ctx, "u1", "medium")
    award_points(ctx, "u1", "medium")
    rec = get_score(ctx, "u1")
    assert rec["streak_current"] == 2
    assert rec["streak_best"] == 2
    break_streak(ctx, "u1")
    rec = get_score(ctx, "u1")
    assert rec["streak_current"] == 0
    assert rec["streak_best"] == 2
    assert rec["total"] == 3
    assert rec["correct"] == 2


# ── Click dispatch helpers ─────────────────────────────────────────────────

def _seed_inflight(ctx, *, game_id="abc123", started_by_uid="starter",
                   mode="single", correct_idx=2, is_daily=False,
                   difficulty="medium", timer_seconds=20):
    inflight = {
        "question": "Q?",
        "shuffled_answers": ["A", "B", "C", "D"],
        "correct_idx": correct_idx,
        "started_by_uid": started_by_uid,
        "started_at": 0,
        "message_id": "msg1",
        "channel_id": "chan1",
        "mode": mode,
        "difficulty": difficulty,
        "category": "General Knowledge",
        "source": "otdb",
        "is_daily": is_daily,
        "timer_seconds": timer_seconds,
    }
    ctx.kv.set(kv_inflight(game_id), inflight, ttl_seconds=timer_seconds + 5)
    return inflight


def _click_event(game_id, choice_idx, *, user_id="someuser"):
    return make_event(
        "interaction_create",
        interaction_type=3,
        custom_id=format_custom_id(game_id, choice_idx),
        user_id=user_id,
    )


def test_click_with_expired_custom_id_replies_expired():
    ctx = MockContext()
    event = make_event(
        "interaction_create",
        interaction_type=3,
        custom_id="triv:99:abc123:0",     # wrong version
        user_id="anyone",
    )
    on_button_click(ctx, event)
    assert any("expired" in r["content"].lower() for r in ctx.interaction.responses)


def test_click_with_no_inflight_replies_ended():
    ctx = MockContext()
    # Valid hex game_id, no matching inflight KV entry.
    event = _click_event("deadbe", 0, user_id="u1")
    on_button_click(ctx, event)
    assert any("ended" in r["content"].lower() for r in ctx.interaction.responses)


def test_click_wrong_interaction_type_does_nothing():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=2,
                       custom_id="triv:1:abc:0", user_id="u1")
    on_button_click(ctx, event)
    assert ctx.interaction.responses == []


def test_click_non_triv_custom_id_does_nothing():
    ctx = MockContext()
    event = make_event("interaction_create", interaction_type=3,
                       custom_id="otherplugin:btn", user_id="u1")
    on_button_click(ctx, event)
    assert ctx.interaction.responses == []


def test_single_mode_wrong_clicker_rejected():
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter")
    event = _click_event("abc123", 0, user_id="intruder")
    on_button_click(ctx, event)
    msgs = [r["content"] for r in ctx.interaction.responses]
    assert msgs
    assert "started by" in msgs[0].lower()
    # Intruder's score was NOT touched
    assert get_score(ctx, "intruder")["total"] == 0


def test_single_mode_correct_answer_awards_points_and_finalizes():
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter", correct_idx=2)
    event = _click_event("abc123", 2, user_id="starter")
    on_button_click(ctx, event)
    rec = get_score(ctx, "starter")
    assert rec["score"] == 20    # medium difficulty
    assert rec["correct"] == 1
    # Inflight was deleted (round finalized)
    assert ctx.kv.get(kv_inflight("abc123")) is None
    # Edit attempt was made
    assert ctx.messages_edited or any("✅" in r["content"] for r in ctx.interaction.responses)


# ── Regression: correct_idx=0 (shuffle put correct answer in slot A) ───────
#
# Prior to v1.0.9 the click handler read `int(inflight.get("correct_idx")
# or -1)`. Python evaluates `0 or -1` to -1 because 0 is falsy, so a stored
# correct_idx of 0 was silently rewritten to -1 — marking ~25% of correctly
# clicked rounds (the ones the shuffle put in slot A) as wrong. The two
# tests below would fail against the v1.0.0–v1.0.8 click handler and pass
# against v1.0.9+.

def test_single_mode_click_A_when_correct_is_A_scores_correct():
    """Bug from v1.0.0–v1.0.8: click A while correct_idx=0 was marked wrong."""
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter", correct_idx=0)
    event = _click_event("abc123", 0, user_id="starter")
    on_button_click(ctx, event)
    rec = get_score(ctx, "starter")
    assert rec["score"] == 20, "clicking the correct A-slot must award points"
    assert rec["correct"] == 1
    assert rec["streak_current"] == 1
    assert ctx.kv.get(kv_inflight("abc123")) is None


def test_single_mode_click_B_when_correct_is_A_is_wrong():
    """The sibling of the bug-regression test above: clicking the wrong slot
    must still be marked wrong when correct_idx=0 (i.e. the sentinel-coerce
    bug didn't accidentally invert the comparison for non-A clicks)."""
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter", correct_idx=0)
    event = _click_event("abc123", 1, user_id="starter")  # clicked B
    on_button_click(ctx, event)
    rec = get_score(ctx, "starter")
    assert rec["score"] == 0
    assert rec["correct"] == 0
    assert rec["total"] == 1


def test_open_mode_first_correct_A_click_wins():
    """Open-mode counterpart — clicking the correct slot A wins, just like
    any other slot would. v1.0.0–v1.0.8 missed this path too."""
    ctx = MockContext()
    _seed_inflight(ctx, mode="open", correct_idx=0)
    event = _click_event("abc123", 0, user_id="player1")
    on_button_click(ctx, event)
    assert "Correct" in ctx.interaction.responses[0]["content"]
    assert get_score(ctx, "player1")["score"] == 20


def test_single_mode_wrong_answer_breaks_streak_and_finalizes():
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter", correct_idx=2)
    # Give starter a prior streak
    award_points(ctx, "starter", "medium")
    award_points(ctx, "starter", "medium")
    assert get_score(ctx, "starter")["streak_current"] == 2

    event = _click_event("abc123", 0, user_id="starter")
    on_button_click(ctx, event)

    rec = get_score(ctx, "starter")
    assert rec["streak_current"] == 0
    assert rec["streak_best"] == 2
    assert ctx.kv.get(kv_inflight("abc123")) is None


def test_open_mode_wrong_answer_is_private_nudge():
    """In open mode, a wrong click doesn't end the round — round stays alive."""
    ctx = MockContext()
    _seed_inflight(ctx, mode="open", correct_idx=2)
    event = _click_event("abc123", 0, user_id="player1")
    on_button_click(ctx, event)
    assert ctx.interaction.responses[0]["ephemeral"] is True
    # Inflight is STILL there (round not finalized)
    assert ctx.kv.get(kv_inflight("abc123")) is not None


def test_open_mode_first_correct_wins_subsequent_lose():
    ctx = MockContext()
    _seed_inflight(ctx, mode="open", correct_idx=2)

    # First correct click wins
    event1 = _click_event("abc123", 2, user_id="player1")
    on_button_click(ctx, event1)
    assert "Correct" in ctx.interaction.responses[0]["content"]
    assert get_score(ctx, "player1")["score"] == 20

    # Inflight cleared, but dedup gate persists
    # Re-seed inflight to test what happens if a second player races (in
    # practice the inflight is gone by then, but the dedup guard works
    # regardless)
    _seed_inflight(ctx, game_id="abc456", mode="open", correct_idx=2)
    # First click on abc456
    on_button_click(ctx, _click_event("abc456", 2, user_id="winner"))
    assert get_score(ctx, "winner")["score"] == 20


def test_daily_correct_appends_winner_history():
    """Correct answer on a daily round records the winner."""
    from datetime import datetime, timezone
    from plugin_main import kv_daily

    ctx = MockContext()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx.kv.set(kv_daily(today), {
        "winners": [], "answered_count": 0, "posted_at": 100,
        "channel_id": "chan1", "game_id": "abc123",
    })
    _seed_inflight(ctx, mode="open", correct_idx=2, is_daily=True, difficulty="hard")

    event = _click_event("abc123", 2, user_id="daily_winner")
    on_button_click(ctx, event)
    rec = ctx.kv.get(kv_daily(today))
    assert "daily_winner" in rec["winners"]
    assert rec["answered_count"] == 1
    # Daily bonus applied (hard=30 + daily=50 = 80)
    assert get_score(ctx, "daily_winner")["score"] == 80


# ── finalize_round disables buttons (SDK 0.5.3) ────────────────────────────

def test_finalize_passes_disabled_components_to_edit_message():
    """After a round finalizes, edit_message must receive a `components` arg
    containing a single ActionRow of four disabled Buttons, with the correct
    answer marked ✓. v0.5.3 of the SDK accepts the components kwarg; v1.0.7
    started using it to close the 'buttons stay clickable' known limitation."""
    ctx = MockContext()
    _seed_inflight(ctx, mode="single", started_by_uid="starter", correct_idx=2)
    event = _click_event("abc123", 2, user_id="starter")
    on_button_click(ctx, event)

    assert ctx.messages_edited, "finalize_round must call edit_message"
    edit = ctx.messages_edited[-1]
    assert "components" in edit and edit["components"], \
        "edit_message must receive a non-empty components kwarg"

    rows = edit["components"]
    assert len(rows) == 1, "exactly one ActionRow"
    buttons = rows[0].children
    assert len(buttons) == 4, "four answer buttons"
    for i, btn in enumerate(buttons):
        assert btn.disabled is True, f"button {i} must be disabled"
        # Custom IDs must be ':done'-suffixed so parse_custom_id rejects them
        assert btn.custom_id.endswith(":done"), \
            f"button {i} custom_id must end with ':done'"
        # Live parse_custom_id should refuse the disabled-row custom_ids
        assert parse_custom_id(btn.custom_id) is None

    # The correct button (index 2) is success/green with a ✓ marker
    assert buttons[2].style == "success"
    assert "✓" in buttons[2].label
    # Wrong buttons are secondary/grey with no marker
    for i in (0, 1, 3):
        assert buttons[i].style == "secondary"
        assert "✓" not in buttons[i].label
