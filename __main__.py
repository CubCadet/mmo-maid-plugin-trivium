"""Trivium — multiple-choice trivia for MMO Maid (v1.0.0).

Multiple-choice trivia rounds played through Discord buttons. Questions are
sourced from Open Trivia DB (primary) with The Trivia API as fallback.
Per-server leaderboards and per-user streaks are stored in KV. An optional
admin-scheduled daily trivia question lands in a configured channel.

Architectural notes worth knowing if you're maintaining this file:

  * Single-file by necessity. The upload-zip allowlist permits only
    manifest.json, __main__.py, requirements.txt, dashboard_manifest.json,
    and dashboard/. Helper .py modules at the repo root would be stripped
    at release time. Section banners below substitute for module split.

  * OTDB strings are HTML-entity-encoded ("&quot;", "&#039;", "&amp;").
    The Trivia API returns plain unicode. html.unescape() runs at the OTDB
    source-adapter layer only; applying it to Trivia API data would corrupt
    legitimate "&"-containing strings.

  * Custom_ids are dynamic ("triv:1:{game_id}:{choice_idx}"), so the answer
    buttons are dispatched via @plugin.on_event("interaction_create") with
    manual filtering — NOT via @plugin.on_component (which is an exact-
    string match).

  * @plugin.schedule(60) drives the daily-trivia post, but the SDK docs warn
    schedules may not fire in pool mode. /trivia play carries an opportunistic
    backstop check on every invocation so quiet pool-mode servers still
    benefit when at least one user plays. The ephemeral dedup gate on
    "dedup:daily:{YYYY-MM-DD}" prevents double-posts regardless of path.

  * Inflight round state lives in ctx.kv (with a short TTL = timer + 5s),
    NOT ctx.ephemeral. The ephemeral API only supports counter/cooldown/
    dedup/flag primitives — no arbitrary dict storage.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import time
from datetime import datetime, timezone

from mmo_maid_sdk import (
    ActionRow,
    Button,
    CapabilityError,
    Context,
    KvQuotaError,
    Plugin,
    RateLimitError,
    RpcTimeoutError,
    SdkError,
)

# Module-level version constant. Kept in sync with manifest.json by a regression
# test in tests/test_meta.py. Used in the on_ready log because ctx.version is
# empty under v0.5.2 pool-mode workers.
__version__ = "1.0.3"

plugin = Plugin()


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

CACHE_SCHEMA_V = 1                  # bump if qbatch/inflight value shape changes
CUSTOM_ID_VERSION = 1               # bump if parse_custom_id semantics change
CUSTOM_ID_PREFIX = "triv:"

POINT_VALUES = {"easy": 10, "medium": 20, "hard": 30}
DAILY_BONUS = 50
COOLDOWN_SECONDS = 3
SEEN_RING_CAP = 200

OTDB_TOKEN_TTL = 6 * 60 * 60           # 6h — matches OTDB's idle timeout
BATCH_CACHE_TTL = 24 * 60 * 60         # 24h
DAILY_HISTORY_TTL = 30 * 24 * 60 * 60  # 30d
INFLIGHT_GRACE_SECONDS = 5
FETCHER_BUDGET = 12.0                  # seconds — hung fetcher fails closed
DEFAULT_TIMER_SECONDS = 20
DAILY_TIMER_SECONDS = 60 * 60          # 1h answer window for daily

# Negative-cache reasons + per-reason TTLs.
RATE_LIMITED = "rate_limited"
FETCH_ERROR = "fetch_error"
PARSE_ERROR = "parse_error"
HTTP_ERROR = "http_error"
NO_QUESTIONS = "no_questions"
TIMEOUT = "timeout"
TOKEN_EXHAUSTED = "token_exhausted"    # OTDB response_code=4 under current token

NEGATIVE_TTL = {
    RATE_LIMITED: 600,
    FETCH_ERROR: 300,
    PARSE_ERROR: 300,
    HTTP_ERROR: 300,
    NO_QUESTIONS: 1800,
    TIMEOUT: 300,
    TOKEN_EXHAUSTED: 7200,             # 2h — avoid burning HTTP cap before
                                       # OTDB token's 6h idle has a chance to roll
}

VALID_DIFFICULTIES = ("easy", "medium", "hard", "any")
VALID_MODES = ("single", "open")
DIFFICULTY_COLOR = {"easy": 0x57F287, "medium": 0xF1C40F, "hard": 0xED4245}
DIFFICULTY_LABEL = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}
ANSWER_LABELS = ["A", "B", "C", "D"]

# Discord permission bits (Discord-side, not plugin capabilities)
PERM_ADMINISTRATOR = 0x8
PERM_MANAGE_GUILD = 0x20

# The 24 canonical OTDB categories. Names match the slash-command choices
# in manifest.json exactly.
OTDB_CATEGORY_IDS = {
    "General Knowledge": 9,
    "Books": 10,
    "Film": 11,
    "Music": 12,
    "Musicals & Theatres": 13,
    "Television": 14,
    "Video Games": 15,
    "Board Games": 16,
    "Science & Nature": 17,
    "Computers": 18,
    "Mathematics": 19,
    "Mythology": 20,
    "Sports": 21,
    "Geography": 22,
    "History": 23,
    "Politics": 24,
    "Art": 25,
    "Celebrities": 26,
    "Animals": 27,
    "Vehicles": 28,
    "Comics": 29,
    "Gadgets": 30,
    "Anime & Manga": 31,
    "Cartoons & Animations": 32,
}

# Mapping OTDB category → Trivia API category. Many OTDB categories have
# no clean fallback in Trivia API; the value is None for those and the
# fallback fetcher returns NO_QUESTIONS for them.
TRIVIA_API_MAPPING = {
    "General Knowledge": "general_knowledge",
    "Books": "arts_and_literature",
    "Film": "film_and_tv",
    "Music": "music",
    "Musicals & Theatres": "arts_and_literature",
    "Television": "film_and_tv",
    "Video Games": None,
    "Board Games": None,
    "Science & Nature": "science",
    "Computers": "science",
    "Mathematics": "science",
    "Mythology": None,
    "Sports": "sport_and_leisure",
    "Geography": "geography",
    "History": "history",
    "Politics": None,
    "Art": "arts_and_literature",
    "Celebrities": None,
    "Animals": None,
    "Vehicles": None,
    "Comics": None,
    "Gadgets": None,
    "Anime & Manga": None,
    "Cartoons & Animations": None,
}

ALL_CATEGORIES = list(OTDB_CATEGORY_IDS.keys())

DEFAULT_CONFIG = {
    "daily_channel_id": None,
    "daily_time_utc": None,            # "HH:MM" once configured
    "default_difficulty": "any",
    "timer_seconds": DEFAULT_TIMER_SECONDS,
    "mode": "single",                  # "single" | "open"
    "daily_category": "General Knowledge",
    "version": 1,                      # config schema version
}


# ──────────────────────────────────────────────────────────────────────────
# Safety layer — html unescape (OTDB only), markdown/mention scrub,
# request_id for log threading.
# ──────────────────────────────────────────────────────────────────────────

# Bidi-control codepoints (U+202A–U+202E and U+2066–U+2069). Strip from
# user-visible question text to prevent right-to-left injection tricks
# that can flip the visible answer-letter ordering.
_BIDI_TRANSLATE = {
    0x202A: None, 0x202B: None, 0x202C: None, 0x202D: None, 0x202E: None,
    0x2066: None, 0x2067: None, 0x2068: None, 0x2069: None,
}


def otdb_unescape(s: str) -> str:
    """Decode HTML entities in Open Trivia DB strings.

    Apply at the OTDB source-adapter layer ONLY. The Trivia API returns
    plain unicode and must NOT be unescaped — doing so would corrupt
    legitimate "&"-containing strings.
    """
    if not s:
        return ""
    return html.unescape(s)


def scrub_for_display(text: str) -> str:
    """Neutralize markdown markers, masked links, bidi controls, and
    @everyone/@here in user-visible question and answer text.

    Belt-and-suspenders alongside `allowed_mentions={"parse": []}` on every
    interaction response. Even with mentions suppressed, Discord still
    renders @everyone-styled tokens visually (bold red); scrubbing prevents
    that pseudo-ping appearance.
    """
    if not text:
        return ""
    text = text.translate(_BIDI_TRANSLATE)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    # Escape backticks to keep them from opening code blocks inside embeds.
    text = text.replace("`", "\u200b`")
    # Neutralize masked-link markdown: [text](url) -> [text](​url)
    text = text.replace("](http", "](\u200bhttp").replace("](www", "](\u200bwww")
    return text


def make_request_id() -> str:
    """Six hex chars threaded into every log call within a handler invocation."""
    return secrets.token_hex(3)


# ──────────────────────────────────────────────────────────────────────────
# KV / ephemeral key helpers + storage primitives
# ──────────────────────────────────────────────────────────────────────────

# Untimed KV keys
KV_CONFIG = "cfg:server"
KV_OTDB_TOKEN = "otdb:session_token"
KV_ADMIN_CACHE = "cache:admin"          # {owner_id, roles_by_id, fetched_at}

# Admin gate cache lifetime. Short enough that newly-granted MANAGE_GUILD
# propagates within ~10 min; long enough to absorb burst use without hitting
# the Discord 60-actions/min cap. Invalidating on guild_role_* events is a
# v1.0.3 follow-up.
ADMIN_CACHE_TTL = 600


def kv_score(user_id: str) -> str:
    return f"score:{user_id}"


def kv_seen(category: str) -> str:
    return f"seen:{category}"


def kv_qbatch(source: str, category: str, difficulty: str) -> str:
    return f"qbatch:{source}:{category}:{difficulty}"


def kv_qbatch_neg(source: str, category: str, difficulty: str) -> str:
    return f"qbatch_neg:{source}:{category}:{difficulty}"


def kv_daily(date_str: str) -> str:
    return f"daily:{date_str}"


def kv_inflight(game_id: str) -> str:
    return f"inflight:game:{game_id}"


# Ephemeral keys (consumed by ctx.ephemeral.* primitives)
def eph_cooldown(user_id: str) -> str:
    return f"cooldown:trivia:{user_id}"


def eph_dedup_answer(game_id: str) -> str:
    return f"dedup:answer:{game_id}"


def eph_dedup_daily(date_str: str) -> str:
    return f"dedup:daily:{date_str}"


def eph_expired(game_id: str) -> str:
    return f"dedup:expired:{game_id}"


# ── Config

def get_config(ctx: Context) -> dict:
    """Read per-server config and merge over DEFAULT_CONFIG so missing keys
    don't surface as None downstream."""
    stored = ctx.kv.get(KV_CONFIG)
    if not isinstance(stored, dict):
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(stored)
    return cfg


def save_config(ctx: Context, cfg: dict) -> None:
    ctx.kv.set(KV_CONFIG, cfg)         # untimed


# ── Score

def get_score(ctx: Context, user_id: str) -> dict:
    rec = ctx.kv.get(kv_score(user_id))
    if not isinstance(rec, dict):
        return {
            "score": 0, "correct": 0, "total": 0,
            "streak_current": 0, "streak_best": 0, "last_played_ts": 0,
        }
    return rec


def award_points(ctx: Context, user_id: str, difficulty: str, *, is_daily: bool = False) -> int:
    base = POINT_VALUES.get(difficulty, 10)
    pts = base + (DAILY_BONUS if is_daily else 0)
    rec = get_score(ctx, user_id)
    rec["score"] = int(rec.get("score") or 0) + pts
    rec["correct"] = int(rec.get("correct") or 0) + 1
    rec["total"] = int(rec.get("total") or 0) + 1
    streak = int(rec.get("streak_current") or 0) + 1
    rec["streak_current"] = streak
    rec["streak_best"] = max(int(rec.get("streak_best") or 0), streak)
    rec["last_played_ts"] = int(time.time())
    ctx.kv.set(kv_score(user_id), rec)
    return pts


def break_streak(ctx: Context, user_id: str) -> None:
    """Bump total, reset current streak. Used on wrong answers in single mode."""
    rec = get_score(ctx, user_id)
    rec["total"] = int(rec.get("total") or 0) + 1
    rec["streak_current"] = 0
    rec["last_played_ts"] = int(time.time())
    ctx.kv.set(kv_score(user_id), rec)


# ── Seen ring (per-category, capped at 200)

def get_seen(ctx: Context, category: str) -> list:
    raw = ctx.kv.get(kv_seen(category))
    if isinstance(raw, list):
        return list(raw)
    return []


def push_seen(ctx: Context, category: str, qhash: str) -> None:
    ring = get_seen(ctx, category)
    if qhash in ring:
        return
    ring.append(qhash)
    if len(ring) > SEEN_RING_CAP:
        ring = ring[-SEEN_RING_CAP:]
    try:
        ctx.kv.set(kv_seen(category), ring)
    except KvQuotaError:
        pass


# ── Batch cache (versioned; reader rejects mismatched schema)

def read_batch(ctx: Context, source: str, category: str, difficulty: str) -> list | None:
    raw = ctx.kv.get(kv_qbatch(source, category, difficulty))
    if not isinstance(raw, dict):
        return None
    if raw.get("v") != CACHE_SCHEMA_V:
        return None
    questions = raw.get("questions")
    if not isinstance(questions, list):
        return None
    return list(questions)


def write_batch(ctx: Context, source: str, category: str, difficulty: str, questions: list) -> None:
    if not questions:
        ctx.kv.delete(kv_qbatch(source, category, difficulty))
        return
    blob = {
        "v": CACHE_SCHEMA_V,
        "questions": questions,
        "fetched_at": int(time.time()),
    }
    try:
        ctx.kv.set(kv_qbatch(source, category, difficulty), blob, ttl_seconds=BATCH_CACHE_TTL)
    except KvQuotaError:
        # Halve and retry. Worst case the batch lands empty and the next
        # /trivia play hits the upstream again — degraded but correct.
        if len(questions) > 1:
            write_batch(ctx, source, category, difficulty, questions[: len(questions) // 2])


def clear_batch(ctx: Context, source: str, category: str, difficulty: str) -> None:
    ctx.kv.delete(kv_qbatch(source, category, difficulty))


# ── Negative cache

def read_negative(ctx: Context, source: str, category: str, difficulty: str) -> str | None:
    raw = ctx.kv.get(kv_qbatch_neg(source, category, difficulty))
    if not isinstance(raw, dict):
        return None
    reason = raw.get("reason")
    return reason if isinstance(reason, str) else None


def write_negative(ctx: Context, source: str, category: str, difficulty: str, reason: str) -> None:
    ttl = NEGATIVE_TTL.get(reason, 300)
    try:
        ctx.kv.set(
            kv_qbatch_neg(source, category, difficulty),
            {"reason": reason, "until": int(time.time()) + ttl},
            ttl_seconds=ttl,
        )
    except KvQuotaError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Source adapters — OTDB + The Trivia API
# ──────────────────────────────────────────────────────────────────────────

class FetchResult:
    """Tagged-union return type. Avoids None sentinels for control flow."""
    __slots__ = ("kind", "questions", "reason")

    def __init__(self, kind: str, *, questions: list | None = None, reason: str = ""):
        self.kind = kind                       # "ok" | "neg"
        self.questions = questions if questions is not None else []
        self.reason = reason

    @classmethod
    def ok(cls, questions: list) -> "FetchResult":
        return cls("ok", questions=questions)

    @classmethod
    def neg(cls, reason: str) -> "FetchResult":
        return cls("neg", reason=reason)


def _qhash(question_text: str) -> str:
    return hashlib.sha256(question_text.encode("utf-8")).hexdigest()[:16]


def _parse_json_body(resp: dict) -> tuple[object | None, str | None]:
    """Parse the body_bytes field of an http response.

    Returns (parsed_object, error_reason). On success the second element is
    None; on failure the first is None and the second holds a negative-cache
    reason code.
    """
    if not isinstance(resp, dict):
        return None, PARSE_ERROR
    if resp.get("truncated"):
        return None, PARSE_ERROR
    try:
        body = resp.get("body_bytes") or ""
        return json.loads(body), None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, PARSE_ERROR


def _classify_http_status(resp: dict) -> str | None:
    """Return a negative-cache reason if status is bad, or None on success."""
    if not isinstance(resp, dict):
        return HTTP_ERROR
    status = resp.get("status", 0)
    if status == 429:
        return RATE_LIMITED
    if 500 <= status < 600:
        return HTTP_ERROR
    if status != 200:
        return HTTP_ERROR
    return None


def _ensure_otdb_token(ctx: Context, *, force_refresh: bool = False) -> str | None:
    """Get-or-create an OTDB session token. Returns None on failure — caller
    proceeds without a token (OTDB still serves results, just without
    per-server question suppression)."""
    if not force_refresh:
        existing = ctx.kv.get(KV_OTDB_TOKEN)
        if isinstance(existing, str) and existing:
            return existing
    try:
        resp = ctx.http.get("https://opentdb.com/api_token.php?command=request")
    except SdkError:
        return None
    if _classify_http_status(resp):
        return None
    body, parse_err = _parse_json_body(resp)
    if parse_err or not isinstance(body, dict):
        return None
    if body.get("response_code") != 0:
        return None
    token = body.get("token")
    if not isinstance(token, str) or not token:
        return None
    try:
        ctx.kv.set(KV_OTDB_TOKEN, token, ttl_seconds=OTDB_TOKEN_TTL)
    except KvQuotaError:
        pass
    return token


def fetch_otdb(ctx: Context, category: str, difficulty: str) -> FetchResult:
    """Fetch a 50-question batch from Open Trivia DB. Returns ok(list) or neg(reason).

    On response_code=3 (token expired) the call refreshes the token and
    retries once in-band. On response_code=4 (token exhausted for this
    category × difficulty under the current token) it returns
    TOKEN_EXHAUSTED — the dispatcher then falls through to The Trivia API
    while leaving the token alive to keep suppressing other categories.
    """
    otdb_id = OTDB_CATEGORY_IDS.get(category)
    if otdb_id is None:
        return FetchResult.neg(NO_QUESTIONS)

    started = time.monotonic()
    token = _ensure_otdb_token(ctx)
    base = f"https://opentdb.com/api.php?amount=50&category={otdb_id}&type=multiple"
    if difficulty in ("easy", "medium", "hard"):
        base += f"&difficulty={difficulty}"

    def _budget_remaining() -> bool:
        return (time.monotonic() - started) < FETCHER_BUDGET

    def _do_call(tok: str | None) -> tuple[str, object]:
        if not _budget_remaining():
            return ("neg", TIMEOUT)
        url = base + (f"&token={tok}" if tok else "")
        try:
            resp = ctx.http.get(url)
        except RateLimitError:
            return ("neg", RATE_LIMITED)
        except RpcTimeoutError:
            return ("neg", TIMEOUT)
        except SdkError:
            return ("neg", FETCH_ERROR)
        bad = _classify_http_status(resp)
        if bad:
            return ("neg", bad)
        body, parse_err = _parse_json_body(resp)
        if parse_err or not isinstance(body, dict):
            return ("neg", PARSE_ERROR)
        rc = body.get("response_code")
        if rc == 0:
            results = body.get("results") or []
            return ("ok", results if isinstance(results, list) else [])
        if rc == 1:
            return ("neg", NO_QUESTIONS)
        if rc == 2:
            return ("neg", FETCH_ERROR)        # invalid parameter — bug-level
        if rc == 3:
            return ("token_expired", None)
        if rc == 4:
            return ("neg", TOKEN_EXHAUSTED)
        if rc == 5:
            return ("neg", RATE_LIMITED)
        return ("neg", FETCH_ERROR)

    outcome, payload = _do_call(token)
    if outcome == "token_expired":
        new_tok = _ensure_otdb_token(ctx, force_refresh=True)
        outcome, payload = _do_call(new_tok)

    if outcome == "neg":
        return FetchResult.neg(payload if isinstance(payload, str) else FETCH_ERROR)

    raw_items = payload if isinstance(payload, list) else []
    questions: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            q_text = otdb_unescape(item.get("question") or "")
            correct = otdb_unescape(item.get("correct_answer") or "")
            incorrect_raw = item.get("incorrect_answers") or []
            incorrect = [otdb_unescape(s) for s in incorrect_raw if isinstance(s, str)]
        except Exception:
            continue
        if not q_text or not correct or len(incorrect) != 3:
            continue
        item_diff = item.get("difficulty") if isinstance(item.get("difficulty"), str) else difficulty
        questions.append({
            "qhash": _qhash(q_text),
            "question": q_text,
            "correct": correct,
            "incorrect": incorrect,
            "category": category,
            "difficulty": item_diff,
            "source": "otdb",
        })
    if not questions:
        return FetchResult.neg(NO_QUESTIONS)
    return FetchResult.ok(questions)


def fetch_trivia_api(ctx: Context, category: str, difficulty: str) -> FetchResult:
    """Fetch a 50-question batch from The Trivia API. Returns ok(list) or neg(reason).

    Trivia API returns plain unicode — no html.unescape() applied.
    Categories without a mapping in TRIVIA_API_MAPPING return NO_QUESTIONS
    immediately rather than guessing a near-equivalent category.
    """
    api_cat = TRIVIA_API_MAPPING.get(category)
    if api_cat is None:
        return FetchResult.neg(NO_QUESTIONS)

    started = time.monotonic()
    url = f"https://the-trivia-api.com/v2/questions?limit=50&categories={api_cat}"
    if difficulty in ("easy", "medium", "hard"):
        url += f"&difficulties={difficulty}"

    if (time.monotonic() - started) >= FETCHER_BUDGET:
        return FetchResult.neg(TIMEOUT)
    try:
        resp = ctx.http.get(url)
    except RateLimitError:
        return FetchResult.neg(RATE_LIMITED)
    except RpcTimeoutError:
        return FetchResult.neg(TIMEOUT)
    except SdkError:
        return FetchResult.neg(FETCH_ERROR)
    bad = _classify_http_status(resp)
    if bad:
        return FetchResult.neg(bad)
    body, parse_err = _parse_json_body(resp)
    if parse_err:
        return FetchResult.neg(PARSE_ERROR)
    if not isinstance(body, list) or not body:
        return FetchResult.neg(NO_QUESTIONS)

    questions: list[dict] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        q_obj = item.get("question")
        q_text = q_obj.get("text") if isinstance(q_obj, dict) else None
        correct = item.get("correctAnswer")
        incorrect_raw = item.get("incorrectAnswers") or []
        incorrect = [s for s in incorrect_raw if isinstance(s, str)]
        if not isinstance(q_text, str) or not isinstance(correct, str) or len(incorrect) != 3:
            continue
        item_diff = item.get("difficulty") if isinstance(item.get("difficulty"), str) else difficulty
        questions.append({
            "qhash": _qhash(q_text),
            "question": q_text,
            "correct": correct,
            "incorrect": list(incorrect),
            "category": category,
            "difficulty": item_diff,
            "source": "trivia_api",
        })
    if not questions:
        return FetchResult.neg(NO_QUESTIONS)
    return FetchResult.ok(questions)


# Source dispatch order. Adding a third source is a one-line change here.
SOURCES = [
    ("otdb", fetch_otdb),
    ("trivia_api", fetch_trivia_api),
]


# ──────────────────────────────────────────────────────────────────────────
# Question selection — dispatcher with fallback + seen-ring filter
# ──────────────────────────────────────────────────────────────────────────

def fetch_one_question(ctx: Context, category: str, difficulty: str,
                       *, request_id: str = "") -> dict | None:
    """Return one question dict, or None when every source is exhausted.

    Iterates SOURCES in order, honoring per-source negative-cache windows.
    Pops the first unseen question from each source's batch, refetching on
    exhaustion. Writes the surviving batch back to KV and pushes the picked
    qhash to the seen ring.
    """
    last_reason = NO_QUESTIONS
    seen = set(get_seen(ctx, category))
    for source_name, fetcher in SOURCES:
        neg = read_negative(ctx, source_name, category, difficulty)
        if neg is not None:
            last_reason = neg
            ctx.metrics.record("trivium_fetch", tags={
                "source": source_name, "result": "neg_cache", "reason": neg,
            })
            continue

        picked = None
        for _ in range(2):                  # original cache, then one refetch
            cached = read_batch(ctx, source_name, category, difficulty)
            if cached is None:
                result = fetcher(ctx, category, difficulty)
                if result.kind == "neg":
                    write_negative(ctx, source_name, category, difficulty, result.reason)
                    last_reason = result.reason
                    ctx.metrics.record("trivium_fetch", tags={
                        "source": source_name, "result": "neg", "reason": result.reason,
                    })
                    break
                cached = list(result.questions)
                write_batch(ctx, source_name, category, difficulty, cached)

            # Pop the first unseen question; keep everything else in the batch
            picked = None
            remaining: list[dict] = []
            for q in cached:
                if picked is None and isinstance(q, dict) and q.get("qhash") not in seen:
                    picked = q
                else:
                    remaining.append(q)
            if picked is not None:
                write_batch(ctx, source_name, category, difficulty, remaining)
                break

            # Every cached question is in the seen ring — clear and refetch.
            clear_batch(ctx, source_name, category, difficulty)

        if picked is not None:
            push_seen(ctx, category, picked["qhash"])
            ctx.metrics.record("trivium_fetch", tags={
                "source": source_name, "result": "ok",
                "difficulty": picked.get("difficulty") or difficulty,
            })
            return picked

    ctx.log("trivium: no question available", level="warning",
            tags=["trivium", "fetch"],
            request_id=request_id, category=category, difficulty=difficulty,
            last_reason=last_reason)
    return None


# ──────────────────────────────────────────────────────────────────────────
# Round building — custom_id schema, shuffle, embed, action row
# ──────────────────────────────────────────────────────────────────────────

def make_game_id() -> str:
    return secrets.token_hex(3)         # 6 hex chars


def format_custom_id(game_id: str, choice_idx: int) -> str:
    return f"{CUSTOM_ID_PREFIX}{CUSTOM_ID_VERSION}:{game_id}:{choice_idx}"


# Match exactly "triv:<v>:<6-hex-id>:<0-3>" — anchored to reject suffixes
# like ":done" used on the post-round disabled buttons.
CUSTOM_ID_RE = re.compile(r"^triv:(\d+):([0-9a-f]{1,16}):([0-3])$")


def parse_custom_id(custom_id: str) -> tuple[int, str, int] | None:
    """Return (version, game_id, choice_idx) or None on parse/version mismatch."""
    if not isinstance(custom_id, str) or not custom_id:
        return None
    m = CUSTOM_ID_RE.match(custom_id)
    if not m:
        return None
    try:
        version = int(m.group(1))
        if version != CUSTOM_ID_VERSION:
            return None
        return (version, m.group(2), int(m.group(3)))
    except (ValueError, IndexError):
        return None


def shuffle_answers(correct: str, incorrect: list[str]) -> tuple[list[str], int]:
    """Securely shuffle answers and return (shuffled_list, correct_index)."""
    rng = secrets.SystemRandom()
    answers = [correct] + list(incorrect)
    rng.shuffle(answers)
    return answers, answers.index(correct)


def build_question_embed(*, question: str, answers: list[str], category: str,
                         difficulty: str, timer_seconds: int, game_id: str,
                         mode: str, is_daily: bool = False) -> dict:
    label = DIFFICULTY_LABEL.get(difficulty, (difficulty or "").title() or "Unknown")
    mode_label = "open" if mode == "open" else "single-player"
    daily_tag = "Daily • " if is_daily else ""
    if timer_seconds >= 60:
        minutes = timer_seconds // 60
        timer_label = f"{minutes} minute{'s' if minutes != 1 else ''}"
    else:
        timer_label = f"{timer_seconds}s"

    description_lines = [scrub_for_display(question), ""]
    for i, ans in enumerate(answers):
        description_lines.append(f"**{ANSWER_LABELS[i]}.** {scrub_for_display(ans)}")

    return {
        "title": f"{daily_tag}Trivia — {scrub_for_display(category)}",
        "description": "\n".join(description_lines),
        "color": DIFFICULTY_COLOR.get(difficulty, 0x5865F2),
        "footer": {
            "text": f"{label} • {mode_label} • {timer_label} to answer • Round {game_id}",
        },
    }


def build_answer_row(game_id: str) -> ActionRow:
    return ActionRow(*[
        Button(label=ANSWER_LABELS[i],
               custom_id=format_custom_id(game_id, i),
               style="primary")
        for i in range(4)
    ])


def build_finalized_embed(inflight: dict, *, winner_uid: str | None,
                          outcome: str) -> dict:
    """outcome ∈ {"correct", "wrong", "timeout"}."""
    category = inflight.get("category", "Trivia")
    difficulty = inflight.get("difficulty", "")
    label = DIFFICULTY_LABEL.get(difficulty, (difficulty or "").title() or "Unknown")
    is_daily = bool(inflight.get("is_daily"))
    daily_tag = "Daily • " if is_daily else ""
    answers = inflight.get("shuffled_answers") or []
    correct_idx = int(inflight.get("correct_idx") or 0)
    correct = answers[correct_idx] if 0 <= correct_idx < len(answers) else "?"
    question = inflight.get("question", "")

    if winner_uid:
        footer_text = f"✅ Answered correctly • {label}"
        result_line = (f"**Answer: {ANSWER_LABELS[correct_idx]}. {scrub_for_display(correct)}** "
                       f"— won by <@{winner_uid}>")
        color = 0x57F287
    elif outcome == "wrong":
        footer_text = f"❌ Wrong answer • {label}"
        result_line = f"**Answer: {ANSWER_LABELS[correct_idx]}. {scrub_for_display(correct)}**"
        color = 0xED4245
    else:
        footer_text = f"⏱ Round ended • {label}"
        result_line = f"**Answer: {ANSWER_LABELS[correct_idx]}. {scrub_for_display(correct)}**"
        color = 0x99AAB5

    description_lines = [scrub_for_display(question), ""]
    for i, ans in enumerate(answers):
        marker = " ✓" if i == correct_idx else ""
        description_lines.append(f"**{ANSWER_LABELS[i]}.** {scrub_for_display(ans)}{marker}")
    description_lines.append("")
    description_lines.append(result_line)

    return {
        "title": f"{daily_tag}Trivia — {scrub_for_display(category)}",
        "description": "\n".join(description_lines),
        "color": color,
        "footer": {"text": footer_text},
    }


# ──────────────────────────────────────────────────────────────────────────
# Round finalization — edit message, scoring already happened in caller
# ──────────────────────────────────────────────────────────────────────────

def finalize_round(ctx: Context, *, game_id: str, inflight: dict,
                   winner_uid: str | None, outcome: str,
                   request_id: str = "") -> None:
    """Reveal the answer by editing the round message.

    SDK note: ctx.discord.edit_message in v0.5.2 accepts only content and
    embeds — no `components` arg, so the original action row stays clickable
    after the round ends. The "round has ended" guard in the click handler
    (inflight-not-found check) catches any late clicks gracefully.
    """
    embed = build_finalized_embed(inflight, winner_uid=winner_uid, outcome=outcome)
    channel_id = str(inflight.get("channel_id") or "")
    message_id = str(inflight.get("message_id") or "")

    if channel_id and message_id:
        try:
            ctx.discord.edit_message(
                channel_id=channel_id,
                message_id=message_id,
                embeds=[embed],
            )
        except SdkError as exc:
            ctx.log(f"finalize edit failed: {exc}",
                    level="error", tags=["trivium", "discord"],
                    request_id=request_id, game_id=game_id)

    ctx.ephemeral.flag_set(eph_expired(game_id), ttl_seconds=60)
    ctx.kv.delete(kv_inflight(game_id))


# ──────────────────────────────────────────────────────────────────────────
# Permission check — for /trivia config
# ──────────────────────────────────────────────────────────────────────────

def _check_member_perms_int(perms) -> bool | None:
    """Return True/False if perms is parseable; None if unparseable."""
    try:
        if isinstance(perms, str):
            p = int(perms)
        elif isinstance(perms, int):
            p = perms
        else:
            return None
    except (ValueError, TypeError):
        return None
    return bool(p & PERM_ADMINISTRATOR or p & PERM_MANAGE_GUILD)


def _load_admin_cache(ctx: Context) -> dict | None:
    """Read the admin cache. Returns None if missing, stale, or malformed."""
    raw = ctx.kv.get(KV_ADMIN_CACHE)
    if not isinstance(raw, dict):
        return None
    fetched_at = int(raw.get("fetched_at") or 0)
    if int(time.time()) - fetched_at >= ADMIN_CACHE_TTL:
        return None
    if not isinstance(raw.get("owner_id"), str):
        return None
    if not isinstance(raw.get("roles_by_id"), dict):
        return None
    return raw


def _refresh_admin_cache(ctx: Context, *, request_id: str = "") -> dict | None:
    """Call ctx.discord.get_guild() + list_roles() and write the cache.

    Returns the freshly-built cache dict, or None if list_roles fails (the
    role-permissions union is load-bearing; without it we have nothing
    to check against).

    get_guild failure is non-fatal — we lose the guild-owner shortcut but
    still gate on role permissions. A 1.0.2 production install hit a 404 on
    get_guild that crashed the handler; making it optional fixes that.

    The exception classes the runner raises aren't always typed SdkError —
    in 1.0.2 production logs we saw a plain RuntimeError wrapping the 404.
    Catch broadly here so the gate fails closed gracefully rather than
    crashing.
    """
    owner_id = ""
    try:
        guild = ctx.discord.get_guild()
        if isinstance(guild, dict):
            owner_id = str(guild.get("owner_id") or "")
    except Exception as exc:
        ctx.log(f"admin cache refresh: get_guild failed: {exc}",
                level="warning", tags=["trivium", "admin"],
                request_id=request_id, exc_type=type(exc).__name__)
        # Continue without owner_id — role lookup is the load-bearing check.
    try:
        roles = ctx.discord.list_roles()
    except Exception as exc:
        ctx.log(f"admin cache refresh: list_roles failed: {exc}",
                level="warning", tags=["trivium", "admin"],
                request_id=request_id, exc_type=type(exc).__name__)
        return None
    roles_by_id: dict[str, int] = {}
    if isinstance(roles, list):
        for r in roles:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            perms = r.get("permissions")
            if not isinstance(rid, (str, int)):
                continue
            try:
                if isinstance(perms, str):
                    pi = int(perms)
                elif isinstance(perms, int):
                    pi = perms
                else:
                    pi = 0
            except (ValueError, TypeError):
                pi = 0
            roles_by_id[str(rid)] = pi
    cache = {
        "owner_id": owner_id,
        "roles_by_id": roles_by_id,
        "fetched_at": int(time.time()),
    }
    try:
        ctx.kv.set(KV_ADMIN_CACHE, cache, ttl_seconds=ADMIN_CACHE_TTL + 60)
    except KvQuotaError:
        pass
    return cache


def has_manage_guild(ctx: Context, event: dict) -> tuple[bool, str]:
    """Return (allowed, source) for /trivia config. Three layers, fail-closed.

    Layer A (no API call):
        If event["member"]["permissions"] is present, decide from that.
        v0.5.2 doesn't actually expose this field on interaction_create —
        this branch is forward-compat. Kept first so tests that supply
        member.permissions don't pay for Discord calls they don't need.

    Layer B (cached):
        Read KV_ADMIN_CACHE. If user is the guild owner → allow. Otherwise
        fetch member roles via ctx.discord.get_member, union their
        permissions from the cached roles_by_id map, check the bit.

    Layer C (cold):
        If the cache is missing or stale, call ctx.discord.get_guild() +
        list_roles() once each, populate, then fall through to Layer B
        logic.

    Fail-closed: any unhandled SDK error → (False, "denied_error_<type>").
    """
    request_id = make_request_id()
    user_id = str(event.get("user_id") or "")

    # Layer A — free, no Discord call
    member = event.get("member")
    if isinstance(member, dict):
        verdict = _check_member_perms_int(member.get("permissions"))
        if verdict is True:
            return True, "member_perms"
        if verdict is False:
            return False, "no_perms_member"

    # Layer C — refresh cache if missing/stale
    cache = _load_admin_cache(ctx)
    if cache is None:
        try:
            cache = _refresh_admin_cache(ctx, request_id=request_id)
        except CapabilityError:
            ctx.log("admin check requires discord:read but it's not granted",
                    level="error", tags=["trivium", "admin", "capability"],
                    request_id=request_id)
            return False, "denied_error_CapabilityError"
        if cache is None:
            return False, "denied_error_no_cache"

    # Layer B — owner shortcut
    owner_id = str(cache.get("owner_id") or "")
    if owner_id and user_id == owner_id:
        return True, "guild_owner"

    # Layer B — role permissions union
    try:
        m = ctx.discord.get_member(user_id=user_id)
    except CapabilityError:
        ctx.log("admin check requires discord:read but it's not granted",
                level="error", tags=["trivium", "admin", "capability"],
                request_id=request_id)
        return False, "denied_error_CapabilityError"
    except Exception as exc:
        # The runner wraps some Discord errors as RuntimeError (seen in
        # 1.0.2 production). Catch broadly so we fail closed instead of
        # crashing the handler.
        ctx.log(f"admin check: get_member failed: {exc}",
                level="warning", tags=["trivium", "admin"],
                request_id=request_id, exc_type=type(exc).__name__)
        return False, f"denied_error_{type(exc).__name__}"
    if not isinstance(m, dict):
        return False, "denied_error_no_member"
    member_roles = m.get("roles")
    if not isinstance(member_roles, list):
        return False, "no_perms_no_roles"
    roles_by_id = cache.get("roles_by_id") or {}
    union_perms = 0
    for rid in member_roles:
        if isinstance(rid, (str, int)):
            union_perms |= int(roles_by_id.get(str(rid), 0) or 0)
    if union_perms & PERM_ADMINISTRATOR or union_perms & PERM_MANAGE_GUILD:
        return True, "role_perms"
    return False, "no_perms_roles"


# ──────────────────────────────────────────────────────────────────────────
# /trivia play
# ──────────────────────────────────────────────────────────────────────────

def cmd_play(ctx: Context, event: dict, opts: dict) -> None:
    request_id = make_request_id()
    cfg = get_config(ctx)
    uid = str(event.get("user_id") or "")
    if not uid:
        ctx.interaction.respond(content="Couldn't determine your user ID.", ephemeral=True)
        return

    raw_cat = opts.get("category") or "General Knowledge"
    if not isinstance(raw_cat, str) or raw_cat not in OTDB_CATEGORY_IDS:
        ctx.interaction.respond(
            content=f"Unknown category: `{scrub_for_display(str(raw_cat))[:60]}`. Try `/trivia play` and pick from the dropdown.",
            ephemeral=True, allowed_mentions={"parse": []},
        )
        return

    raw_diff = opts.get("difficulty") or cfg.get("default_difficulty") or "any"
    raw_diff = raw_diff.lower() if isinstance(raw_diff, str) else "any"
    if raw_diff not in VALID_DIFFICULTIES:
        ctx.interaction.respond(
            content=f"Unknown difficulty: `{scrub_for_display(str(raw_diff))[:40]}`.",
            ephemeral=True, allowed_mentions={"parse": []},
        )
        return

    # Cooldown — checked before defer so an error is instant, not 3s of "thinking…"
    state = ctx.ephemeral.cooldown_check(eph_cooldown(uid))
    if isinstance(state, dict) and state.get("active"):
        remaining = int(state.get("remaining_seconds") or 0)
        ctx.interaction.respond(
            content=f"Slow down — try again in {remaining}s.",
            ephemeral=True,
        )
        return

    ctx.interaction.defer()
    ctx.ephemeral.cooldown_set(eph_cooldown(uid), ttl_seconds=COOLDOWN_SECONDS)

    # Pool-mode backstop for daily — cheap on the no-op path.
    try:
        _maybe_post_daily(ctx, request_id=request_id)
    except Exception as exc:
        ctx.log(f"daily backstop error (suppressed): {exc}",
                level="warning", tags=["trivium", "daily"],
                request_id=request_id, exc_type=type(exc).__name__)

    question = fetch_one_question(ctx, raw_cat, raw_diff, request_id=request_id)
    if question is None:
        ctx.interaction.followup(
            content="Trivia sources are unavailable, try again in a few minutes.",
            ephemeral=True, allowed_mentions={"parse": []},
        )
        return

    answers, correct_idx = shuffle_answers(question["correct"], question["incorrect"])
    game_id = make_game_id()
    timer_seconds = int(cfg.get("timer_seconds") or DEFAULT_TIMER_SECONDS)
    mode = cfg.get("mode") if cfg.get("mode") in VALID_MODES else "single"
    channel_id = str(event.get("channel_id") or "")
    qdiff = question.get("difficulty") if question.get("difficulty") in {"easy", "medium", "hard"} else "medium"

    # Defense-in-depth — custom_ids should always fit comfortably.
    if len(format_custom_id(game_id, 0)) > 90:
        ctx.log("custom_id too long (impossible?)", level="error",
                tags=["trivium", "internal"], request_id=request_id)
        ctx.interaction.followup(
            content="Internal error building the round.", ephemeral=True,
        )
        return

    embed = build_question_embed(
        question=question["question"], answers=answers,
        category=raw_cat, difficulty=qdiff, timer_seconds=timer_seconds,
        game_id=game_id, mode=mode,
    )
    row = build_answer_row(game_id)

    # Post the round via ctx.discord.send_message — its return carries the
    # message_id we need later for edit_message. ctx.interaction.followup
    # returns None in v0.5.2, so using it for the round message itself would
    # lose the edit handle. We ack the deferred interaction with a separate
    # ephemeral followup right after.
    if not channel_id:
        ctx.interaction.followup(
            content="Couldn't determine the channel for this round.",
            ephemeral=True,
        )
        return
    try:
        sent = ctx.discord.send_message(
            channel_id=channel_id,
            embeds=[embed],
            components=[row],
        )
    except SdkError as exc:
        ctx.log(f"send_message failed: {exc}",
                level="error", tags=["trivium", "discord"],
                request_id=request_id, exc_type=type(exc).__name__)
        ctx.interaction.followup(
            content="Couldn't post the trivia round. Try again.",
            ephemeral=True,
        )
        return

    message_id = ""
    channel_resolved = channel_id
    if isinstance(sent, dict):
        message_id = str(sent.get("message_id") or sent.get("id") or "")
        channel_resolved = str(sent.get("channel_id") or channel_id)

    inflight = {
        "question": question["question"],
        "shuffled_answers": answers,
        "correct_idx": correct_idx,
        "started_by_uid": uid,
        "started_at": int(time.time()),
        "message_id": message_id,
        "channel_id": channel_resolved,
        "mode": mode,
        "difficulty": qdiff,
        "category": raw_cat,
        "source": question.get("source") or "",
        "is_daily": False,
        "timer_seconds": timer_seconds,
    }
    try:
        ctx.kv.set(kv_inflight(game_id), inflight,
                   ttl_seconds=timer_seconds + INFLIGHT_GRACE_SECONDS)
    except KvQuotaError:
        ctx.log("KV quota; could not save inflight", level="error",
                tags=["trivium", "kv"], request_id=request_id, game_id=game_id)

    ctx.metrics.record("trivium_round_started", tags={
        "mode": mode, "difficulty": qdiff, "source": question.get("source") or "",
    })

    # Ack the deferred interaction. Without a followup the "Bot is thinking..."
    # indicator sits there for 15 minutes.
    ctx.interaction.followup(
        content=f"Round started — click an answer above! (Round `{game_id}`)",
        ephemeral=True,
    )


# ──────────────────────────────────────────────────────────────────────────
# Daily backstop on message_create — pool-mode safety net
# ──────────────────────────────────────────────────────────────────────────
#
# In pool mode @plugin.schedule may never fire. The cmd_play backstop covers
# servers where someone plays trivia after the configured daily time; this
# message_create handler covers servers where the daily channel sees normal
# chat traffic but nobody plays. _maybe_post_daily short-circuits cheaply
# when daily isn't configured or has already posted today, so firing it on
# every message is bounded by a single ctx.kv.exists + ephemeral.dedup check.

@plugin.on_event("message_create")
def daily_backstop_on_message(ctx: Context, event: dict) -> None:
    if event.get("author_bot"):
        return
    try:
        _maybe_post_daily(ctx, request_id=make_request_id())
    except Exception as exc:
        ctx.log(f"daily message-backstop suppressed: {exc}",
                level="warning", tags=["trivium", "daily"],
                exc_type=type(exc).__name__)


# ──────────────────────────────────────────────────────────────────────────
# Component dispatch — button clicks via @plugin.on_event("interaction_create")
# (NOT @plugin.on_component because custom_ids are dynamic)
# ──────────────────────────────────────────────────────────────────────────

@plugin.on_event("interaction_create")
def on_button_click(ctx: Context, event: dict) -> None:
    if event.get("interaction_type") != 3:
        return
    custom_id = event.get("custom_id") or ""
    if not isinstance(custom_id, str) or not custom_id.startswith(CUSTOM_ID_PREFIX):
        return

    request_id = make_request_id()
    parsed = parse_custom_id(custom_id)
    if parsed is None:
        ctx.interaction.respond(
            content="This round has expired. Run `/trivia play` again.",
            ephemeral=True,
        )
        return

    _, game_id, choice_idx = parsed
    inflight = ctx.kv.get(kv_inflight(game_id))
    if not isinstance(inflight, dict):
        ctx.interaction.respond(
            content="This round has ended.",
            ephemeral=True,
        )
        return

    clicker_uid = str(event.get("user_id") or "")
    started_by_uid = str(inflight.get("started_by_uid") or "")
    mode = inflight.get("mode") if inflight.get("mode") in VALID_MODES else "single"
    correct_idx = int(inflight.get("correct_idx") or -1)
    is_correct = (choice_idx == correct_idx)
    difficulty = inflight.get("difficulty") or "medium"
    is_daily = bool(inflight.get("is_daily"))
    source = inflight.get("source") or ""
    timer = int(inflight.get("timer_seconds") or DEFAULT_TIMER_SECONDS)

    common_tags = {
        "mode": mode, "difficulty": difficulty, "source": source,
        "is_daily": "1" if is_daily else "0",
    }

    if mode == "single":
        # Single-player: only the starter can answer.
        if started_by_uid and clicker_uid != started_by_uid:
            ctx.interaction.respond(
                content=f"This round was started by <@{started_by_uid}>.",
                ephemeral=True, allowed_mentions={"parse": []},
            )
            return
        if is_correct:
            pts = award_points(ctx, clicker_uid, difficulty, is_daily=is_daily)
            ctx.interaction.respond(content=f"✅ Correct! +{pts} points.", ephemeral=True)
            ctx.metrics.record("trivium_answer", tags={**common_tags, "result": "correct"})
            finalize_round(ctx, game_id=game_id, inflight=inflight,
                           winner_uid=clicker_uid, outcome="correct",
                           request_id=request_id)
            if is_daily:
                _record_daily_winner(ctx, clicker_uid)
        else:
            break_streak(ctx, clicker_uid)
            ctx.interaction.respond(content="❌ Not quite. Streak broken.", ephemeral=True)
            ctx.metrics.record("trivium_answer", tags={**common_tags, "result": "wrong"})
            finalize_round(ctx, game_id=game_id, inflight=inflight,
                           winner_uid=None, outcome="wrong",
                           request_id=request_id)
        return

    # Open mode: first correct click wins. Wrong clicks are private nudges.
    if not is_correct:
        ctx.interaction.respond(
            content="❌ Not quite — wait for someone else, or for the timer.",
            ephemeral=True,
        )
        ctx.metrics.record("trivium_answer", tags={**common_tags, "result": "wrong"})
        return

    won = ctx.ephemeral.dedup(eph_dedup_answer(game_id),
                              ttl_seconds=timer + INFLIGHT_GRACE_SECONDS)
    if not won:
        ctx.interaction.respond(content="Someone beat you to it!", ephemeral=True)
        return

    pts = award_points(ctx, clicker_uid, difficulty, is_daily=is_daily)
    ctx.interaction.respond(content=f"✅ Correct! +{pts} points.", ephemeral=True)
    ctx.metrics.record("trivium_answer", tags={**common_tags, "result": "correct"})
    finalize_round(ctx, game_id=game_id, inflight=inflight,
                   winner_uid=clicker_uid, outcome="correct",
                   request_id=request_id)
    if is_daily:
        _record_daily_winner(ctx, clicker_uid)


# ──────────────────────────────────────────────────────────────────────────
# Slash command router — /trivia
# ──────────────────────────────────────────────────────────────────────────

@plugin.on_slash_command("trivia")
def trivia_root(ctx: Context, event: dict) -> None:
    # v0.5.2 runtime delivers slash-command sub-command + arg list under
    # `command_options`. The SDK reference docs list it as `options`; the
    # actual runtime disagrees. Try the runtime key first, fall back to the
    # documented key in case a future SDK version aligns.
    opts = event.get("command_options") or event.get("options") or []
    if not isinstance(opts, list) or not opts or not isinstance(opts[0], dict):
        ctx.interaction.respond(
            content="Use `/trivia play`, `/trivia leaderboard`, `/trivia stats`, `/trivia daily`, or `/trivia config`.",
            ephemeral=True,
        )
        return

    sub = opts[0]
    sub_name = sub.get("name", "") if isinstance(sub, dict) else ""
    sub_options: dict = {}
    for o in (sub.get("options") or []):
        if isinstance(o, dict) and "name" in o:
            sub_options[o["name"]] = o.get("value")

    try:
        if sub_name == "play":
            cmd_play(ctx, event, sub_options)
        elif sub_name == "leaderboard":
            cmd_leaderboard(ctx, event)
        elif sub_name == "stats":
            cmd_stats(ctx, event, sub_options)
        elif sub_name == "daily":
            cmd_daily(ctx, event)
        elif sub_name == "config":
            cmd_config(ctx, event, sub_options)
        else:
            ctx.interaction.respond(
                content=f"Unknown sub-command: `{scrub_for_display(str(sub_name))[:40]}`.",
                ephemeral=True,
            )
    except Exception as exc:
        # Last-resort safety net so the round handler can't leave Discord
        # holding an unanswered interaction. The runner ACKs either way; we
        # try to surface a polite message but swallow the response failure.
        ctx.log(f"unhandled exception in /trivia {sub_name}: {exc}",
                level="error", tags=["trivium", "internal"],
                exc_type=type(exc).__name__)
        try:
            ctx.interaction.respond(
                content="Something went wrong handling that command. Try again.",
                ephemeral=True,
            )
        except SdkError:
            pass


# ──────────────────────────────────────────────────────────────────────────
# /trivia leaderboard
# ──────────────────────────────────────────────────────────────────────────

def cmd_leaderboard(ctx: Context, event: dict) -> None:
    # 1.0.3: switched from ctx.kv.list_values to ctx.kv.list + ctx.kv.get_many.
    # In v1.0.2 production list_values consistently returned empty for the
    # "score:" prefix even when the keys clearly existed. list + get_many is
    # more primitive and well-exercised by other plugins. get_many takes up
    # to 50 keys per call so we batch.
    try:
        keys = ctx.kv.list(prefix="score:", limit=1000) or []
    except Exception as exc:
        ctx.log(f"leaderboard: kv.list failed: {exc}",
                level="warning", tags=["trivium", "leaderboard"],
                exc_type=type(exc).__name__)
        keys = []

    all_scores: dict = {}
    if keys:
        # get_many caps at 50 — batch through the key list
        for i in range(0, len(keys), 50):
            chunk = keys[i:i + 50]
            try:
                got = ctx.kv.get_many(chunk) or {}
            except Exception as exc:
                ctx.log(f"leaderboard: kv.get_many failed: {exc}",
                        level="warning", tags=["trivium", "leaderboard"],
                        exc_type=type(exc).__name__, batch=i // 50)
                continue
            if isinstance(got, dict):
                all_scores.update(got)

    ctx.log("leaderboard fetched",
            level="info", tags=["trivium", "leaderboard"],
            key_count=len(keys), value_count=len(all_scores))

    rows: list[tuple[str, int, dict]] = []
    for key, val in all_scores.items():
        if not isinstance(key, str):
            continue
        # In case the runtime returns JSON-encoded values rather than dicts,
        # try to parse them. Belt-and-suspenders.
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(val, dict):
            continue
        if ":" not in key:
            continue
        user_id = key.split(":", 1)[1]
        rows.append((user_id, int(val.get("score") or 0), val))
    rows.sort(key=lambda r: r[1], reverse=True)

    if not rows or all(r[1] == 0 for r in rows):
        ctx.interaction.respond(
            content="No trivia scores yet. Be the first — run `/trivia play`!",
            ephemeral=True,
        )
        return

    lines = []
    for i, (uid, score, rec) in enumerate(rows[:10], start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`#{i:>2}`")
        correct = int(rec.get("correct") or 0)
        total = int(rec.get("total") or 0)
        rate = f"{(correct * 100 // total)}%" if total else "—"
        lines.append(f"{medal} <@{uid}> — **{score}** pts • {correct}/{total} ({rate})")

    ctx.interaction.respond(
        embeds=[{
            "title": "🏆 Trivia Leaderboard",
            "description": "\n".join(lines),
            "color": 0xC9A24F,
        }],
        allowed_mentions={"parse": []},
    )


# ──────────────────────────────────────────────────────────────────────────
# /trivia stats [user]
# ──────────────────────────────────────────────────────────────────────────

def cmd_stats(ctx: Context, event: dict, opts: dict) -> None:
    target_uid = opts.get("user") or event.get("user_id") or ""
    target_uid = str(target_uid)
    if not target_uid:
        ctx.interaction.respond(content="Couldn't determine whose stats to show.", ephemeral=True)
        return
    rec = get_score(ctx, target_uid)
    score = int(rec.get("score") or 0)
    correct = int(rec.get("correct") or 0)
    total = int(rec.get("total") or 0)
    streak_cur = int(rec.get("streak_current") or 0)
    streak_best = int(rec.get("streak_best") or 0)
    rate = f"{(correct * 100 // total)}%" if total else "—"

    ctx.interaction.respond(
        embeds=[{
            "title": "Trivia Stats",
            "description": f"Stats for <@{target_uid}>",
            "color": 0x5865F2,
            "fields": [
                {"name": "Score",         "value": f"**{score}**", "inline": True},
                {"name": "Correct/Total", "value": f"{correct} / {total} ({rate})", "inline": True},
                {"name": "Streak",        "value": f"current **{streak_cur}** • best **{streak_best}**", "inline": True},
            ],
        }],
        allowed_mentions={"parse": []},
    )


# ──────────────────────────────────────────────────────────────────────────
# /trivia daily
# ──────────────────────────────────────────────────────────────────────────

def cmd_daily(ctx: Context, event: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = ctx.kv.get(kv_daily(today))
    if not isinstance(rec, dict):
        # Either no daily scheduled, or scheduled but not yet posted.
        cfg = get_config(ctx)
        if not cfg.get("daily_channel_id") or not cfg.get("daily_time_utc"):
            ctx.interaction.respond(
                content="Daily trivia isn't configured on this server. An admin can run `/trivia config action:channel`.",
                ephemeral=True,
            )
        else:
            ctx.interaction.respond(
                content=f"Today's daily hasn't been posted yet (scheduled for {cfg['daily_time_utc']} UTC).",
                ephemeral=True,
            )
        return

    channel_id = rec.get("channel_id")
    winners = rec.get("winners") if isinstance(rec.get("winners"), list) else []
    answered = int(rec.get("answered_count") or 0)
    posted_at = int(rec.get("posted_at") or 0)
    winner_line = ""
    if winners:
        extras = len(winners) - 1
        winner_line = f"\nFirst-correct: <@{winners[0]}>"
        if extras > 0:
            winner_line += f" (+{extras} more)"
    location = f"\nPosted in <#{channel_id}>" if channel_id else ""
    ts_part = f" at <t:{posted_at}:t>" if posted_at else ""
    ctx.interaction.respond(
        content=f"Today's daily trivia was posted{ts_part}.{location}{winner_line}\nAnswered: **{answered}**",
        ephemeral=True, allowed_mentions={"parse": []},
    )


# ──────────────────────────────────────────────────────────────────────────
# /trivia config (admin-only via has_manage_guild + discord:read)
# ──────────────────────────────────────────────────────────────────────────

CONFIG_HELP = (
    "**Config actions** (admin only):\n"
    "• `action:show` — display current config\n"
    "• `action:channel value:#channel` — set daily channel (omit value to use current channel)\n"
    "• `action:time value:HH:MM` — set daily UTC time (e.g. `09:00`)\n"
    "• `action:difficulty value:easy|medium|hard|any` — default `/trivia play` difficulty\n"
    "• `action:timer value:10..60` — answer timer in seconds\n"
    "• `action:mode value:single|open` — game mode\n"
    "• `action:category value:<one of the 24 categories>` — daily question category\n"
)

CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
RAW_ID_RE = re.compile(r"^(\d{15,21})$")
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def cmd_config(ctx: Context, event: dict, opts: dict) -> None:
    allowed, src = has_manage_guild(ctx, event)
    if not allowed:
        # Log the denial source so ops can see why a "legitimate" admin
        # got rejected (cache miss, Discord error, role lookup failure, etc.)
        ctx.log("trivia config denied",
                level="info", tags=["trivium", "admin"],
                user_id=str(event.get("user_id") or ""), source=src)
        ctx.interaction.respond(
            content="You need the **Manage Server** permission to run this.",
            ephemeral=True,
        )
        return

    action_raw = opts.get("action") or "show"
    action = action_raw.lower().strip() if isinstance(action_raw, str) else "show"
    raw_value = opts.get("value")
    cfg = get_config(ctx)

    if action == "show":
        _config_show(ctx, cfg)
    elif action == "channel":
        _config_set_channel(ctx, event, cfg, raw_value)
    elif action == "time":
        _config_set_time(ctx, cfg, raw_value)
    elif action == "difficulty":
        _config_set_difficulty(ctx, cfg, raw_value)
    elif action == "timer":
        _config_set_timer(ctx, cfg, raw_value)
    elif action == "mode":
        _config_set_mode(ctx, cfg, raw_value)
    elif action == "category":
        _config_set_category(ctx, cfg, raw_value)
    else:
        ctx.interaction.respond(content=CONFIG_HELP, ephemeral=True)


def _config_show(ctx: Context, cfg: dict) -> None:
    chan = cfg.get("daily_channel_id")
    chan_line = f"<#{chan}>" if chan else "(unset)"
    fields = [
        {"name": "Daily channel",      "value": chan_line, "inline": True},
        {"name": "Daily time (UTC)",   "value": cfg.get("daily_time_utc") or "(unset)", "inline": True},
        {"name": "Daily category",     "value": cfg.get("daily_category") or "General Knowledge", "inline": True},
        {"name": "Default difficulty", "value": str(cfg.get("default_difficulty") or "any"), "inline": True},
        {"name": "Answer timer (s)",   "value": str(int(cfg.get("timer_seconds") or DEFAULT_TIMER_SECONDS)), "inline": True},
        {"name": "Mode",               "value": str(cfg.get("mode") or "single"), "inline": True},
    ]
    ctx.interaction.respond(
        embeds=[{
            "title": "Trivium config",
            "color": 0x5865F2,
            "fields": fields,
            "footer": {"text": "Change with /trivia config action:<thing> value:<value>"},
        }],
        ephemeral=True, allowed_mentions={"parse": []},
    )


def _config_set_channel(ctx: Context, event: dict, cfg: dict, value) -> None:
    new_id = ""
    if isinstance(value, str) and value.strip():
        m = CHANNEL_MENTION_RE.search(value)
        if m:
            new_id = m.group(1)
        else:
            m2 = RAW_ID_RE.match(value.strip())
            if m2:
                new_id = m2.group(1)
    if not new_id:
        new_id = str(event.get("channel_id") or "")
    if not new_id:
        ctx.interaction.respond(
            content="Couldn't resolve a channel. Run this in the target channel, or pass `value:#channel`.",
            ephemeral=True,
        )
        return
    cfg["daily_channel_id"] = new_id
    save_config(ctx, cfg)
    ctx.interaction.respond(
        content=f"Daily channel set to <#{new_id}>.",
        ephemeral=True, allowed_mentions={"parse": []},
    )


def _config_set_time(ctx: Context, cfg: dict, value) -> None:
    if not isinstance(value, str) or not HHMM_RE.match(value.strip()):
        ctx.interaction.respond(
            content="Time must be in `HH:MM` UTC (00:00–23:59). Example: `value:09:00`.",
            ephemeral=True,
        )
        return
    cfg["daily_time_utc"] = value.strip()
    save_config(ctx, cfg)
    ctx.interaction.respond(
        content=f"Daily time set to {cfg['daily_time_utc']} UTC.",
        ephemeral=True,
    )


def _config_set_difficulty(ctx: Context, cfg: dict, value) -> None:
    v = value.lower().strip() if isinstance(value, str) else ""
    if v not in VALID_DIFFICULTIES:
        ctx.interaction.respond(
            content="Difficulty must be `easy`, `medium`, `hard`, or `any`.",
            ephemeral=True,
        )
        return
    cfg["default_difficulty"] = v
    save_config(ctx, cfg)
    ctx.interaction.respond(content=f"Default difficulty set to `{v}`.", ephemeral=True)


def _config_set_timer(ctx: Context, cfg: dict, value) -> None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        ctx.interaction.respond(content="Timer must be an integer between 10 and 60 seconds.", ephemeral=True)
        return
    if n < 10 or n > 60:
        ctx.interaction.respond(content="Timer must be between 10 and 60 seconds.", ephemeral=True)
        return
    cfg["timer_seconds"] = n
    save_config(ctx, cfg)
    ctx.interaction.respond(content=f"Answer timer set to {n}s.", ephemeral=True)


def _config_set_mode(ctx: Context, cfg: dict, value) -> None:
    v = value.lower().strip() if isinstance(value, str) else ""
    if v not in VALID_MODES:
        ctx.interaction.respond(
            content="Mode must be `single` (one user plays the round they started) or `open` (any member can answer, first correct wins).",
            ephemeral=True,
        )
        return
    cfg["mode"] = v
    save_config(ctx, cfg)
    ctx.interaction.respond(content=f"Mode set to `{v}`.", ephemeral=True)


def _config_set_category(ctx: Context, cfg: dict, value) -> None:
    if not isinstance(value, str) or value not in OTDB_CATEGORY_IDS:
        ctx.interaction.respond(
            content="Category must match one of the 24 supported categories — try `/trivia play` to see them in the dropdown.",
            ephemeral=True,
        )
        return
    cfg["daily_category"] = value
    save_config(ctx, cfg)
    ctx.interaction.respond(
        content=f"Daily category set to **{value}**.",
        ephemeral=True, allowed_mentions={"parse": []},
    )


# ──────────────────────────────────────────────────────────────────────────
# Daily scheduler
# ──────────────────────────────────────────────────────────────────────────

@plugin.schedule(60)
def daily_tick(ctx: Context) -> None:
    """Every 60s, check whether it's time to post today's daily.

    Pool-mode caveat: @plugin.schedule may not fire. The opportunistic
    backstop _maybe_post_daily() call in cmd_play and the message_create
    handler below both cover servers where the schedule is silent. The
    ephemeral dedup gate on "dedup:daily:{date}" prevents double-posts
    whichever path fires.

    The "daily_tick fired" diagnostic log is intentional — grep production
    logs for this line over 24h to confirm whether pool-mode schedules
    actually run on this install."""
    ctx.log("daily_tick fired",
            level="info", tags=["trivium", "daily", "diagnostic"])
    try:
        _maybe_post_daily(ctx, request_id=make_request_id())
    except Exception as exc:
        ctx.log(f"daily_tick error (suppressed): {exc}",
                level="error", tags=["trivium", "daily"],
                exc_type=type(exc).__name__)


def _maybe_post_daily(ctx: Context, *, request_id: str = "") -> None:
    cfg = get_config(ctx)
    if not cfg.get("daily_channel_id") or not cfg.get("daily_time_utc"):
        return
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    try:
        hh_str, mm_str = cfg["daily_time_utc"].split(":")
        target = now.replace(hour=int(hh_str), minute=int(mm_str), second=0, microsecond=0)
    except (ValueError, AttributeError):
        return
    if now < target:
        return
    if ctx.kv.exists(kv_daily(today_str)):
        return
    if not ctx.ephemeral.dedup(eph_dedup_daily(today_str), ttl_seconds=86400):
        return
    _post_daily_question(ctx, cfg, today_str, request_id=request_id)


def _post_daily_question(ctx: Context, cfg: dict, today_str: str,
                         *, request_id: str = "") -> None:
    category = cfg.get("daily_category") or "General Knowledge"
    if category not in OTDB_CATEGORY_IDS:
        category = "General Knowledge"
    difficulty = cfg.get("default_difficulty") or "any"
    if difficulty not in VALID_DIFFICULTIES:
        difficulty = "any"
    question = fetch_one_question(ctx, category, difficulty, request_id=request_id)
    if question is None:
        ctx.log("daily: no question available; skipping today",
                level="warning", tags=["trivium", "daily"],
                request_id=request_id, date=today_str)
        return

    answers, correct_idx = shuffle_answers(question["correct"], question["incorrect"])
    game_id = make_game_id()
    qdiff = question.get("difficulty") if question.get("difficulty") in {"easy", "medium", "hard"} else "medium"
    channel_id = str(cfg["daily_channel_id"])

    embed = build_question_embed(
        question=question["question"], answers=answers,
        category=category, difficulty=qdiff,
        timer_seconds=DAILY_TIMER_SECONDS, game_id=game_id,
        mode="open", is_daily=True,
    )
    row = build_answer_row(game_id)
    try:
        sent = ctx.discord.send_message(
            channel_id=channel_id, embeds=[embed], components=[row],
        )
    except SdkError as exc:
        ctx.log(f"daily post failed: {exc}",
                level="error", tags=["trivium", "daily", "discord"],
                request_id=request_id)
        return

    message_id = ""
    if isinstance(sent, dict):
        message_id = str(sent.get("message_id") or sent.get("id") or "")

    inflight = {
        "question": question["question"],
        "shuffled_answers": answers,
        "correct_idx": correct_idx,
        "started_by_uid": "",
        "started_at": int(time.time()),
        "message_id": message_id,
        "channel_id": channel_id,
        "mode": "open",
        "difficulty": qdiff,
        "category": category,
        "source": question.get("source") or "",
        "is_daily": True,
        "timer_seconds": DAILY_TIMER_SECONDS,
    }
    try:
        ctx.kv.set(kv_inflight(game_id), inflight,
                   ttl_seconds=DAILY_TIMER_SECONDS + INFLIGHT_GRACE_SECONDS)
    except KvQuotaError:
        ctx.log("KV quota; could not save daily inflight",
                level="error", tags=["trivium", "kv", "daily"],
                request_id=request_id, game_id=game_id)

    history = {
        "question": question["question"],
        "answers": answers,
        "correct_idx": correct_idx,
        "category": category,
        "difficulty": qdiff,
        "posted_at": int(time.time()),
        "message_id": message_id,
        "channel_id": channel_id,
        "winners": [],
        "answered_count": 0,
        "game_id": game_id,
    }
    try:
        ctx.kv.set(kv_daily(today_str), history, ttl_seconds=DAILY_HISTORY_TTL)
    except KvQuotaError:
        pass

    ctx.log("daily trivia posted",
            level="info", tags=["trivium", "daily"],
            request_id=request_id, date=today_str, category=category,
            difficulty=qdiff, game_id=game_id, source=question.get("source") or "")
    ctx.metrics.record("trivium_daily_posted",
                       tags={"category": category, "difficulty": qdiff})


def _record_daily_winner(ctx: Context, user_id: str) -> None:
    """Append a winner to today's daily history. Idempotent — repeat user_ids
    aren't duplicated in the winners list, but answered_count keeps incrementing."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = ctx.kv.get(kv_daily(today))
    if not isinstance(rec, dict):
        return
    winners = rec.get("winners") if isinstance(rec.get("winners"), list) else []
    if user_id and user_id not in winners:
        winners.append(user_id)
    rec["winners"] = winners
    rec["answered_count"] = int(rec.get("answered_count") or 0) + 1
    try:
        ctx.kv.set(kv_daily(today), rec, ttl_seconds=DAILY_HISTORY_TTL)
    except KvQuotaError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────────

@plugin.on_install
def on_install(ctx: Context) -> None:
    # Idempotent — never overwrite existing config.
    stored = ctx.kv.get(KV_CONFIG)
    if not isinstance(stored, dict):
        ctx.kv.set(KV_CONFIG, dict(DEFAULT_CONFIG))
        ctx.log("trivium installed; seeded default config",
                level="info", tags=["trivium", "lifecycle"])


@plugin.on_ready
def on_ready(ctx: Context) -> None:
    # Prefer the module-level __version__ since ctx.version is empty under
    # v0.5.2 pool-mode workers. Log both so future drift is visible.
    ctx.log(
        f"trivium v{__version__} ready on server {ctx.server_id} "
        f"(ctx.version={ctx.version or 'unset'})",
        level="info", tags=["trivium", "lifecycle"],
    )


# ──────────────────────────────────────────────────────────────────────────
# Entry point — must be the last executable line of this file
# ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    plugin.run()
