# Changelog

All notable changes to this plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version-bump policy, tied to `manifest.json`:

- **MAJOR** (`1.x.y → 2.0.0`) — added a Dangerous capability, removed a slash
  command, breaking KV/SQL schema change.
- **MINOR** (`1.0.x → 1.1.0`) — new slash command, new event handler, additive
  KV/SQL columns.
- **PATCH** (`1.0.0 → 1.0.1`) — bug fix, internal refactor, docs/CI changes.

The tag in GitHub (`v1.2.3`) must match the `version` field in `manifest.json`.
CI enforces this during release builds.

---

## [Unreleased]

## [1.0.4] - 2026-05-15

### Changed
- **`/trivia config` admin gate is now KV-allowlist-based.** v1.0.3
  production confirmed that the v0.5.2 runtime's `ctx.discord.get_guild`
  returns HTTP 404 and `ctx.discord.list_roles` returns all roles with
  `permissions=0` — so neither the owner-shortcut nor the role-bit-union
  path could detect a legitimate admin. 1.0.4 adds a primary
  `cfg:server.admin_user_ids` allowlist (Layer 0) that decides without
  any Discord API call. The Discord-based check is retained as a safety
  net for future runtimes.
- New first-time setup: run `/trivia config action:admin-bootstrap`
  immediately after install. The user who runs it claims admin while
  the allowlist is empty. Subsequent additions use `action:admin-add
  value:@user` (admin-only).
- New admin sub-commands: `admin-bootstrap`, `admin-list`, `admin-add`,
  `admin-remove`. `admin-remove` refuses to remove the last admin to
  prevent lockout.
- `/trivia config action:show` now displays the admins list.

### Fixed
- **`/trivia leaderboard` works again.** v1.0.3 used `ctx.kv.list` after
  v1.0.2's `ctx.kv.list_values` failed; production confirmed both come
  back empty in pool mode (`key_count=0` even with valid score keys
  present). 1.0.4 maintains a manual `scoreindex:users` KV value (list
  of user_ids) that `award_points` and `break_streak` keep updated.
  `cmd_leaderboard` reads this index and uses `kv.get_many` to fetch the
  score records — no `kv.list*` calls anywhere.

### Added
- Diagnostic log `list_roles diagnostic` on cache refresh with the first
  role's keys, the `permissions` field type, and a short repr. If a
  future runtime starts returning real permissions data, the log will
  show it.

### Migration note
- Existing v1.0.3 users who already have score records won't appear on
  the leaderboard until they play once more (their next `award_points`
  or `break_streak` call adds them to the manual index). Since
  `kv.list*` is broken, there's no way to rebuild the index from
  existing keys at install time — lazy rebuild is the only option.

## [1.0.3] - 2026-05-15

### Fixed
- **`/trivia config` crashed on every invocation in v1.0.2 production.** Logs
  showed `RuntimeError: RPC error (discord.get_guild): HTTP Error 404: Not
  Found`. Two compounding causes:
  1. The runner wraps Discord REST errors as `RuntimeError`, not the typed
     `SdkError` / `DiscordApiError` we expected. Our `except (SdkError,
     RpcTimeoutError)` clauses missed it, and the exception escaped to the
     outer "Something went wrong" safety net. Now we catch `Exception` in
     the admin-cache refresh helper and the `get_member` block. The gate
     fails closed gracefully; ops can read `exc_type` in the log.
  2. `get_guild` itself returning 404 is mysterious — the bot is plainly in
     the guild (events are delivered). Until we understand why, we make
     get_guild's failure **non-fatal** in the admin gate. The guild-owner
     shortcut is nice-to-have; the load-bearing check is the role-permissions
     union via `list_roles` + `get_member`. If `get_guild` 404s, we lose
     the owner shortcut but the gate still works for any user with a role
     that has MANAGE_GUILD or ADMINISTRATOR.
- **`/trivia leaderboard` returned "No trivia scores yet" even with valid
  KV state.** v1.0.2 production logs showed `score:<uid>` keys existed
  with score=30 etc., but `ctx.kv.list_values(prefix="score:")` consistently
  came back empty. Replaced with `ctx.kv.list(prefix="score:")` followed by
  batched `ctx.kv.get_many(...)` calls (50-key chunks). list_values may be
  broken or unimplemented in v0.5.2 pool-mode workers; this is the safer
  primitive. Also added defensive JSON-string parsing in case values come
  back stringified, and a diagnostic log line (`leaderboard fetched
  key_count=N value_count=M`) so we can see in one place whether the
  fix landed.

### Changed
- README "Known limitations" — corrected the claim that "the embed doesn't
  auto-reveal the answer." It *does* reveal on click; v1.0.2 production
  confirmed `discord.edit_message` updates the embed correctly even without
  the `components` arg. The remaining limitation is timeout (the silent-
  expiry case when no one clicks at all).

## [1.0.2] - 2026-05-15

### Security
- **`/trivia config` was accessible to any user in 1.0.0 and 1.0.1.** The
  runtime `interaction_create` payload doesn't include
  `event["member"]["permissions"]` in v0.5.2, and the manifest's
  `default_member_permissions: "32"` was misplaced on the `config`
  sub-command (Discord only honors it at root-command level). Net effect:
  the in-handler `has_manage_guild` check always hit the "trust manifest
  gate" fallback and returned True. Severity: low (config-only, no
  destructive operations exposed) but real.
- 1.0.2 enforces MANAGE_GUILD strictly via a layered check:
  `event["member"]["permissions"]` (when present) → guild-owner-id match
  → role-permissions union via `ctx.discord.get_member` +
  `ctx.discord.list_roles`. Fails closed on any Discord error.
- **Servers that relied on the open gate need to grant Manage Server** to
  whichever users were previously running `/trivia config`. Non-admins
  attempting it will now see "You need the Manage Server permission to
  run this." with the denial source logged at info level for ops triage.

### Added
- New capability: `discord:read`. Re-prompts users at upgrade. Used solely
  to look up the guild owner ID and role permissions for the admin gate.
- Per-server admin cache (`KV_ADMIN_CACHE`) keeps the guild owner + roles
  map for 10 minutes to avoid burning the 60-actions/min Discord cap.
- Diagnostic `daily_tick fired` log line. Grep it in production logs over
  24 hours to verify whether `@plugin.schedule` actually runs in pool
  mode for this install. If absent, the message_create backstop below is
  the only daily-post mechanism.
- New `@plugin.on_event("message_create")` daily backstop. Fires
  `_maybe_post_daily` on every non-bot message, but short-circuits cheaply
  when daily isn't configured or has already posted today. Catches the
  case where pool-mode schedules don't fire but the daily channel has
  ordinary chat traffic.
- Module-level `__version__` constant kept in sync with `manifest.version`
  by a regression test (`tests/test_meta.py`). Used in the lifecycle
  log because `ctx.version` is empty under v0.5.2 pool-mode workers.

### Fixed
- `on_ready` log now reads `trivium v1.0.2 ready on server <id>
  (ctx.version=<value-or-unset>)` instead of the blank version under
  pool-mode workers.
- Removed misplaced `default_member_permissions` from the `config`
  sub-command in the manifest (Discord ignored it; replaced by the
  layered in-handler gate).

### Deferred to v1.0.3
- Invalidate `KV_ADMIN_CACHE` on `guild_role_create` / `guild_role_update` /
  `guild_role_delete` / `guild_member_update` events so newly-granted
  MANAGE_GUILD propagates immediately instead of waiting up to 10 minutes.
- Investigate manifest-declared server-side cron schema if the v1.0.2
  diagnostic confirms `@plugin.schedule` doesn't fire in pool mode.

## [1.0.1] - 2026-05-15

### Fixed
- **Every slash sub-command fell through to the help message.** The v0.5.2
  runtime delivers slash-command sub-command + args under
  `event["command_options"]`, not `event["options"]` as the SDK reference
  documents. `trivia_root` now reads the runtime key first and falls back
  to the documented key for forward compatibility. Caught only after
  first-install testing (no logs from a real runtime existed before then).
- Added `tests/test_dispatch.py` with 9 regression cases using real-shape
  interaction payloads taken from production logs, so the next SDK
  contract surprise gets caught locally rather than in production.

## [1.0.0] - 2026-05-15

### Added
- Initial release of Trivium — multiple-choice trivia for MMO Maid.
- `/trivia play [category] [difficulty]` — start a round in one of 24
  categories at easy/medium/hard/any difficulty. Per-user 3-second cooldown.
- `/trivia leaderboard` — top 10 scores for the server.
- `/trivia stats [user]` — lifetime stats; viewing other users' stats is
  open by default.
- `/trivia daily` — show today's daily trivia status.
- `/trivia config <action> [value]` — admin-only configuration of daily
  channel, daily UTC time, default difficulty, answer timer (10–60s), mode
  (single/open), and daily category.
- Single-player mode: only the user who started the round can answer.
- Open mode: any server member can answer; the first-correct click wins
  via a Redis-backed dedup gate.
- Per-server leaderboard backed by KV (`score:{user_id}` records score,
  correct, total, streak_current, streak_best, last_played_ts).
- Daily trivia: posts a 1-hour open round at a configured UTC time, with
  a +50 bonus for the first correct answerer. Idempotency-guarded by an
  ephemeral dedup key on `dedup:daily:{YYYY-MM-DD}`.
- Two-source fetcher chain: Open Trivia DB (primary, with per-server
  session-token suppression) and The Trivia API (fallback, plain-unicode).
- Lazy-reset OTDB token strategy: on `response_code=4` (combo exhausted
  under the current token), Trivium falls through to The Trivia API
  without resetting the token, preserving suppression for the other 23
  categories. Token's 6-hour idle timeout rolls naturally.
- Versioned question-batch cache (`v=1`, 24-hour TTL) keyed by
  source + category + difficulty.
- 200-entry per-category seen-ring suppresses recent repeats across
  sources.
- Typed negative-cache reasons with per-reason TTLs
  (RATE_LIMITED 600s, NO_QUESTIONS 1800s, TOKEN_EXHAUSTED 7200s, etc.).
- HTML-entity decoding applied at the OTDB adapter only — preserves
  legitimate "&" in The Trivia API responses.
- Display scrubber neutralizes bidi controls, `@everyone`/`@here`,
  backticks, and masked-link markdown in user-visible question text.
- Custom_id schema with explicit version prefix (`triv:1:{game_id}:{idx}`)
  so stale buttons across a deploy decode to "this round has expired"
  rather than misbehaving.
- 119-test pytest suite covering safety, sources, cache, game flow,
  scheduler, config, and leaderboard.

### Known limitations
- Round embed doesn't auto-reveal the answer on timeout (no winner click).
  Pool-mode workers may not run `@plugin.schedule`, so we don't rely on
  background ticks for round timeout. Documented in the README.
- The SDK's `edit_message` in v0.5.2 doesn't accept a `components` arg,
  so the round buttons stay clickable after the answer is revealed. Late
  clicks are caught gracefully by the "round has ended" guard.
