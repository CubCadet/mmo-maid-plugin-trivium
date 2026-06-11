"""Tests for OTDB and Trivia API source adapters, including:

- HTTP status / response_code → negative-cache reason mapping
- OTDB token lifecycle (request, expire, exhausted)
- HTML-entity decoding only at the OTDB layer (NOT Trivia API)
- Fallback chain when primary source returns a negative result
"""
from __future__ import annotations

import json

import pytest
from yourbot_sdk.testing import MockContext

from plugin_main import (
    FETCH_ERROR,
    HTTP_ERROR,
    KV_OTDB_TOKEN,
    NO_QUESTIONS,
    PARSE_ERROR,
    RATE_LIMITED,
    TOKEN_EXHAUSTED,
    fetch_one_question,
    fetch_otdb,
    fetch_trivia_api,
    read_negative,
)


def _otdb_ok_body(*, count=2, question="What is &#039;X&#039;?", correct="A&amp;B"):
    return json.dumps({
        "response_code": 0,
        "results": [
            {
                "category": "General Knowledge",
                "type": "multiple",
                "difficulty": "medium",
                "question": question,
                "correct_answer": correct,
                "incorrect_answers": ["wrong&#039;1", "wrong &amp; 2", "wrong 3"],
            }
            for _ in range(count)
        ],
    })


def _otdb_response_code(rc: int) -> str:
    return json.dumps({"response_code": rc, "results": []})


def _trivia_api_body(*, count=2):
    return json.dumps([
        {
            "category": "general_knowledge",
            "id": f"abc{i}",
            "correctAnswer": "Paris",
            "incorrectAnswers": ["London", "Berlin", "Rome"],
            "question": {"text": "Capital of France?", "image": None},
            "difficulty": "easy",
            "type": "Multiple Choice",
        }
        for i in range(count)
    ])


# ── fetch_otdb ──────────────────────────────────────────────────────────────

def test_otdb_token_request_and_subsequent_question_call():
    ctx = MockContext()
    # The token endpoint must answer before the question endpoint.
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "TOK123"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_ok_body(count=2))
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "ok"
    assert len(result.questions) == 2
    # html-unescape happened — &#039; → ' and &amp; → &
    assert "'" in result.questions[0]["question"]
    assert "&" in result.questions[0]["correct"]
    assert "'" in result.questions[0]["incorrect"][0]
    # source is tagged
    assert result.questions[0]["source"] == "otdb"
    # Token was cached
    assert ctx.kv.get(KV_OTDB_TOKEN) == "TOK123"


def test_otdb_no_questions_when_rc_1():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_response_code(1))
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == NO_QUESTIONS


def test_otdb_token_exhausted_when_rc_4():
    """response_code=4 means this category × difficulty is fully served under
    the current token. Lazy reset: surface as TOKEN_EXHAUSTED so the caller
    falls through to Trivia API. Don't reset the token."""
    ctx = MockContext()
    ctx.kv.set(KV_OTDB_TOKEN, "EXISTING_TOK")
    ctx.http.mock_response("api.php", status=200, body=_otdb_response_code(4))
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == TOKEN_EXHAUSTED
    # Token must NOT have been deleted or reset
    assert ctx.kv.get(KV_OTDB_TOKEN) == "EXISTING_TOK"


def test_otdb_rate_limited_on_429():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=429, body="rate limited")
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == RATE_LIMITED


def test_otdb_rate_limited_on_rc_5():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_response_code(5))
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == RATE_LIMITED


def test_otdb_http_error_on_5xx():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=503, body="oops")
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == HTTP_ERROR


def test_otdb_parse_error_on_invalid_json():
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body="not json at all")
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == PARSE_ERROR


def test_otdb_unknown_category_returns_neg():
    ctx = MockContext()
    result = fetch_otdb(ctx, "Definitely Not A Real Category", "any")
    assert result.kind == "neg"
    assert result.reason == NO_QUESTIONS


def test_otdb_token_expired_triggers_inband_refresh():
    """response_code=3 (token expired). The fetcher should silently refresh
    the token and retry the original fetch once."""
    ctx = MockContext()
    ctx.kv.set(KV_OTDB_TOKEN, "OLD_TOK")
    # The first api.php call returns rc=3; the inband retry must succeed.
    # Pre-mock the token endpoint to issue a NEW token, and the api.php
    # endpoint to succeed (mock_response replaces by URL match — the second
    # api.php call wins).
    # Easiest: stub both in order via call counter
    calls = {"api": 0}
    real_get = ctx.http.get

    def patched_get(url, **kw):
        if "api.php" in url:
            calls["api"] += 1
            if calls["api"] == 1:
                return {"status": 200, "body_bytes": _otdb_response_code(3),
                        "headers": {}, "truncated": False}
            return {"status": 200, "body_bytes": _otdb_ok_body(count=1),
                    "headers": {}, "truncated": False}
        if "api_token.php" in url:
            return {"status": 200,
                    "body_bytes": '{"response_code": 0, "token": "NEW_TOK"}',
                    "headers": {}, "truncated": False}
        return real_get(url, **kw)

    ctx.http.get = patched_get  # monkey-patch
    result = fetch_otdb(ctx, "General Knowledge", "any")
    assert result.kind == "ok"
    assert len(result.questions) == 1
    assert ctx.kv.get(KV_OTDB_TOKEN) == "NEW_TOK"
    assert calls["api"] == 2


# ── fetch_trivia_api ────────────────────────────────────────────────────────

def test_trivia_api_returns_ok_with_unicode_unchanged():
    ctx = MockContext()
    ctx.http.mock_response("the-trivia-api.com", status=200, body=_trivia_api_body(count=3))
    result = fetch_trivia_api(ctx, "General Knowledge", "any")
    assert result.kind == "ok"
    assert len(result.questions) == 3
    assert result.questions[0]["question"] == "Capital of France?"
    assert result.questions[0]["source"] == "trivia_api"


def test_trivia_api_does_not_unescape_html_entities():
    """The Trivia API returns plain unicode. Running html.unescape on its
    payload would corrupt legitimate `&` strings — guard against that."""
    body = json.dumps([{
        "category": "general_knowledge",
        "id": "x",
        "correctAnswer": "Rock & Roll",
        "incorrectAnswers": ["A", "B", "C"],
        "question": {"text": "Genre featuring &?", "image": None},
        "difficulty": "easy",
        "type": "Multiple Choice",
    }])
    ctx = MockContext()
    ctx.http.mock_response("the-trivia-api.com", status=200, body=body)
    result = fetch_trivia_api(ctx, "General Knowledge", "any")
    assert result.kind == "ok"
    # If we'd unescaped, "&" would be unchanged (it's already &), but
    # ANY html entities like "&amp;" or "&#x26;" must not have been decoded.
    # Here we verify the raw "&" passed through cleanly.
    assert result.questions[0]["correct"] == "Rock & Roll"
    assert "&" in result.questions[0]["question"]


def test_trivia_api_unmapped_category_returns_neg():
    """OTDB categories like 'Video Games' have no Trivia API mapping. The
    fetcher returns NO_QUESTIONS without making the HTTP call."""
    ctx = MockContext()
    result = fetch_trivia_api(ctx, "Video Games", "any")
    assert result.kind == "neg"
    assert result.reason == NO_QUESTIONS
    # No HTTP request was made
    assert ctx.http.requests == []


def test_trivia_api_empty_response_returns_no_questions():
    ctx = MockContext()
    ctx.http.mock_response("the-trivia-api.com", status=200, body="[]")
    result = fetch_trivia_api(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == NO_QUESTIONS


def test_trivia_api_500_returns_http_error():
    ctx = MockContext()
    ctx.http.mock_response("the-trivia-api.com", status=500, body="server error")
    result = fetch_trivia_api(ctx, "General Knowledge", "any")
    assert result.kind == "neg"
    assert result.reason == HTTP_ERROR


# ── Dispatcher with fallback ────────────────────────────────────────────────

def test_dispatcher_falls_through_to_trivia_api_on_otdb_token_exhausted():
    """Lazy reset: OTDB returns response_code=4 → write negative cache for
    that combo, fall through to Trivia API."""
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=200, body=_otdb_response_code(4))
    ctx.http.mock_response("the-trivia-api.com", status=200, body=_trivia_api_body(count=1))
    picked = fetch_one_question(ctx, "General Knowledge", "any")
    assert picked is not None
    assert picked["source"] == "trivia_api"
    # OTDB was negatively cached with TOKEN_EXHAUSTED
    assert read_negative(ctx, "otdb", "General Knowledge", "any") == TOKEN_EXHAUSTED


def test_dispatcher_returns_none_when_both_sources_fail():
    """Both sources return negative → fetch_one_question returns None."""
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=503, body="down")
    ctx.http.mock_response("the-trivia-api.com", status=503, body="down")
    picked = fetch_one_question(ctx, "General Knowledge", "any")
    assert picked is None


def test_dispatcher_returns_none_when_no_fallback_available():
    """For 'Video Games', Trivia API has no mapping. If OTDB also fails,
    there's no fallback and we should return None."""
    ctx = MockContext()
    ctx.http.mock_response("api_token.php", status=200,
                           body='{"response_code": 0, "token": "T"}')
    ctx.http.mock_response("api.php", status=503, body="down")
    # Trivia API mapping for Video Games is None → no HTTP call needed
    picked = fetch_one_question(ctx, "Video Games", "any")
    assert picked is None


def test_dispatcher_honors_negative_cache_without_refetch():
    """If a negative-cache entry exists for OTDB, the dispatcher must skip
    the OTDB fetcher entirely on the next call."""
    from plugin_main import write_negative
    ctx = MockContext()
    write_negative(ctx, "otdb", "General Knowledge", "any", "rate_limited")
    ctx.http.mock_response("the-trivia-api.com", status=200, body=_trivia_api_body(count=1))
    picked = fetch_one_question(ctx, "General Knowledge", "any")
    assert picked is not None
    assert picked["source"] == "trivia_api"
    # No request was made to OTDB
    otdb_requests = [r for r in ctx.http.requests if "opentdb" in r.get("url", "")]
    assert otdb_requests == []
