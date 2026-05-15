"""Tests for KV-backed cache layers: versioned batch cache, seen ring,
negative cache."""
from __future__ import annotations

from mmo_maid_sdk.testing import MockContext

from plugin_main import (
    CACHE_SCHEMA_V,
    NEGATIVE_TTL,
    SEEN_RING_CAP,
    TOKEN_EXHAUSTED,
    clear_batch,
    get_seen,
    kv_qbatch,
    kv_qbatch_neg,
    push_seen,
    read_batch,
    read_negative,
    write_batch,
    write_negative,
)


# ── Batch cache (versioned) ─────────────────────────────────────────────────

def test_batch_write_then_read_roundtrip():
    ctx = MockContext()
    questions = [{"qhash": "a", "question": "?", "correct": "x",
                  "incorrect": ["y", "z", "w"], "category": "General Knowledge",
                  "difficulty": "easy", "source": "otdb"}]
    write_batch(ctx, "otdb", "General Knowledge", "easy", questions)
    out = read_batch(ctx, "otdb", "General Knowledge", "easy")
    assert out == questions


def test_batch_read_rejects_wrong_schema_version():
    """A future schema bump must invalidate old cache entries (read as miss),
    not break with a KeyError."""
    ctx = MockContext()
    bad_blob = {
        "v": CACHE_SCHEMA_V + 99,
        "questions": [{"qhash": "a"}],
        "fetched_at": 0,
    }
    ctx.kv.set(kv_qbatch("otdb", "General Knowledge", "easy"), bad_blob)
    out = read_batch(ctx, "otdb", "General Knowledge", "easy")
    assert out is None


def test_batch_read_rejects_non_dict():
    ctx = MockContext()
    ctx.kv.set(kv_qbatch("otdb", "General Knowledge", "easy"), "not a dict")
    out = read_batch(ctx, "otdb", "General Knowledge", "easy")
    assert out is None


def test_batch_read_missing_returns_none():
    ctx = MockContext()
    out = read_batch(ctx, "otdb", "General Knowledge", "easy")
    assert out is None


def test_batch_write_empty_deletes_cache():
    """Writing an empty batch should delete the key, not store an empty list."""
    ctx = MockContext()
    write_batch(ctx, "otdb", "General Knowledge", "easy",
                [{"qhash": "a", "question": "?", "correct": "x",
                  "incorrect": ["y", "z", "w"], "category": "General Knowledge",
                  "difficulty": "easy", "source": "otdb"}])
    assert read_batch(ctx, "otdb", "General Knowledge", "easy") is not None
    write_batch(ctx, "otdb", "General Knowledge", "easy", [])
    assert ctx.kv.get(kv_qbatch("otdb", "General Knowledge", "easy")) is None


def test_clear_batch_removes_key():
    ctx = MockContext()
    write_batch(ctx, "otdb", "X", "easy", [{"qhash": "a", "question": "?",
                                            "correct": "c", "incorrect": ["1", "2", "3"],
                                            "category": "X", "difficulty": "easy",
                                            "source": "otdb"}])
    clear_batch(ctx, "otdb", "X", "easy")
    assert read_batch(ctx, "otdb", "X", "easy") is None


def test_batch_keys_include_source_dimension():
    """qbatch:otdb:General Knowledge:easy and qbatch:trivia_api:General Knowledge:easy
    are different keys — the two APIs return different question pools."""
    assert kv_qbatch("otdb", "X", "y") != kv_qbatch("trivia_api", "X", "y")


# ── Seen ring (per-category, capped) ───────────────────────────────────────

def test_seen_push_and_get():
    ctx = MockContext()
    push_seen(ctx, "Sports", "h1")
    push_seen(ctx, "Sports", "h2")
    assert get_seen(ctx, "Sports") == ["h1", "h2"]


def test_seen_dedupes_repeat_pushes():
    """Pushing the same hash twice doesn't grow the ring."""
    ctx = MockContext()
    push_seen(ctx, "Sports", "h1")
    push_seen(ctx, "Sports", "h1")
    push_seen(ctx, "Sports", "h1")
    assert get_seen(ctx, "Sports") == ["h1"]


def test_seen_caps_at_ring_size():
    """At cap+1 entries, the oldest gets evicted (FIFO via tail-slice)."""
    ctx = MockContext()
    for i in range(SEEN_RING_CAP + 5):
        push_seen(ctx, "History", f"h{i}")
    ring = get_seen(ctx, "History")
    assert len(ring) == SEEN_RING_CAP
    # The first 5 entries should have been evicted
    assert "h0" not in ring
    assert "h4" not in ring
    assert "h5" in ring
    assert f"h{SEEN_RING_CAP + 4}" in ring


def test_seen_separate_per_category():
    ctx = MockContext()
    push_seen(ctx, "A", "h1")
    push_seen(ctx, "B", "h2")
    assert get_seen(ctx, "A") == ["h1"]
    assert get_seen(ctx, "B") == ["h2"]


def test_seen_handles_non_list_kv_value():
    ctx = MockContext()
    from plugin_main import kv_seen
    ctx.kv.set(kv_seen("Geography"), "bogus")  # Not a list
    assert get_seen(ctx, "Geography") == []


# ── Negative cache with per-reason TTL ──────────────────────────────────────

def test_negative_cache_write_and_read():
    ctx = MockContext()
    write_negative(ctx, "otdb", "X", "easy", "rate_limited")
    assert read_negative(ctx, "otdb", "X", "easy") == "rate_limited"


def test_negative_cache_returns_none_when_missing():
    ctx = MockContext()
    assert read_negative(ctx, "otdb", "X", "easy") is None


def test_negative_cache_token_exhausted_has_longest_ttl():
    """TOKEN_EXHAUSTED is the 2h-TTL reason — prevents burning the HTTP cap
    while the OTDB token rolls naturally."""
    assert NEGATIVE_TTL[TOKEN_EXHAUSTED] == 7200
    # And it's strictly longer than the other reasons
    for reason, ttl in NEGATIVE_TTL.items():
        if reason != TOKEN_EXHAUSTED:
            assert ttl < NEGATIVE_TTL[TOKEN_EXHAUSTED]


def test_negative_keys_include_all_three_dimensions():
    """qbatch_neg keys are source + category + difficulty."""
    a = kv_qbatch_neg("otdb", "A", "easy")
    b = kv_qbatch_neg("otdb", "A", "medium")
    c = kv_qbatch_neg("otdb", "B", "easy")
    d = kv_qbatch_neg("trivia_api", "A", "easy")
    assert len({a, b, c, d}) == 4
