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

## [1.0.10] - 2026-06-10

### Changed
- **Migrated to the rebranded SDK: `yourbot-sdk>=0.6.1,<0.7.0`** in both
  `requirements.txt` and `requirements-dev.txt`. The platform renamed
  MMO Maid → YourBot.gg; SDK 0.6.0 is the package rename
  (`mmo-maid-sdk` on PyPI is now a deprecated alias that installs
  `yourbot-sdk`), 0.6.1 adds PEP 561 typing, typed responses,
  `ctx.discord.iter_messages`, machine-readable error codes, and
  MockContext capability enforcement. All imports updated
  `mmo_maid_sdk` → `yourbot_sdk` (incl. `yourbot_sdk.testing` in
  `tests/`), which also silences the 0.6.x `DeprecationWarning`. The
  full 190-test suite passed against 0.6.1 *before* the import rename
  (via the wheel's compat shim) and after it.
- **`make dev` now invokes `yourbot dev --watch`** — the `mmo`
  console script no longer exists in SDK 0.6.x.
- **Answer-button dispatch now uses `@plugin.on_component(prefix=CUSTOM_ID_PREFIX)`**
  instead of hand-filtering `@plugin.on_event("interaction_create")`.
  Prefix routing was already present in SDK 0.5.4 (verified in its
  `on_component` signature); the handler keeps its own
  interaction-type/prefix guards so direct calls (tests) behave
  identically. Runtime dispatch is unchanged — the SDK registers
  component handlers on the same `interaction_create` list, in the same
  registration order, with no first-match short-circuit.
- `scripts/validate_plugin.py` accepts `Plugin` imported from either
  `yourbot_sdk` (canonical) or `mmo_maid_sdk` (legacy shim), and brand
  prose in `README.md` / `scripts/build_release.py` now says YourBot.

### Fixed
- **`validate_plugin.py` could no longer see any `discord:read` usage.**
  Its `CAPABILITY_REQUIREMENTS` map listed four method names that have
  never existed in the SDK (`fetch_messages`, `fetch_member`,
  `fetch_channel`, `fetch_role`), so Trivium's real `get_guild` /
  `get_member` / `list_roles` calls went undetected and every run
  emitted a false "capability 'discord:read' declared but no matching
  ctx.* call was detected — drop if unused" WARN. Following that advice
  would have raised `CapabilityError` on every admin-permission check
  at runtime. The map now mirrors the SDK's own `_validation.py`
  read-method patterns (`get_member`, `get_channel`, `get_guild`,
  `list_roles`, `list_members`, `list_channels`, `search_members`,
  `get_messages`). `make validate` is now warning-free (on a clean
  tree — run `make clean` first; pytest caches still ERROR by design).
- **Validator map audited end-to-end against the installed SDK** (every
  entry checked against 0.6.1 `_context.py` docstrings): dropped five
  more never-existed names (`remove_reaction`, `create_role`,
  `delete_role`, `http.put/patch/delete`), added the missing real
  methods for every capability (`kv.get_many`/`exists`/`count`/
  `set_many`/`decrement`/`list_values` — Trivium calls the first two —
  `iter_messages`, `pin/unpin_message`, `bulk_delete_messages`, thread
  and channel-permission methods, bulk moderation, `execute_webhook`,
  `sql.query/query_one/scalar`, `secrets.*`, `interaction.respond/
  defer/followup/send_modal`, `http.request`). The forward
  used-but-not-declared check now models the upload pipeline's
  auto-adds (`slash_commands` ⇒ `interaction:respond`, proxy domains ⇒
  `proxy:http`). `storage:secrets` and `events:message_content` are
  recognised (provisionally Risky; the 0.6.x platform validator knows
  both — `events:message_content` was wrongly in the legacy-ERROR
  list), with a new local mirror of the platform's message-content
  source check. The SQL f-string/interpolation check now also covers
  `sql.query`/`query_one`/`scalar`, not just `execute`.

### Added
- `tests/test_meta.py::test_manifest_caps_cover_click_flow_under_strict_enforcement`
  and `::test_manifest_caps_cover_admin_cache_refresh_under_strict_enforcement` —
  every other test uses MockContext's grant-everything default, so a ctx
  call gated by a capability missing from `manifest.json` would pass the
  suite and `CapabilityError` in production. These run the answer-click
  flow and the admin-cache refresh under exactly the manifest's six
  capabilities (SDK 0.6.1 MockContext enforces an explicit
  `capabilities=` list), with positive assertions so a deny-closed
  `SdkError` path can't swallow the failure.

### Notes
- No manifest capability or slash-command changes; the upload zip
  differs from v1.0.9 only in `manifest.json` (version), the
  `requirements.txt` pin, and the import/decorator/docstring lines in
  `__main__.py`. PATCH bump per policy (refactor/docs/CI).
- The dev venv (`../mmo-maid-plugin-trivium-venv`) was recreated in
  place: its `bin/` entry-point scripts had stale shebangs pointing at
  a deleted in-repo `.venv`, which broke `make dev` and direct
  `pytest`/`pip` invocation (everything had been running via
  `python3 -m ...`).

## [1.0.9] - 2026-05-31

### Fixed
- **Clicking the correct answer in slot A was marked wrong.** The click
  handler at `on_button_click` read
  `int(inflight.get("correct_idx") or -1)`. Python evaluates `0 or -1` to
  `-1` (because `0` is falsy), so any stored `correct_idx = 0` was
  silently rewritten to the `-1` sentinel and the comparison
  `choice_idx == correct_idx` failed for the (~25% of) rounds whose
  shuffle landed the correct answer in slot A. Display paths
  (`build_finalized_embed`, `build_disabled_row`) used `or 0` and
  happened to render the right answer regardless, which is why the
  embed footer and disabled row marked A as correct even as the
  scoring path told the user "wrong" and broke their streak. Fix
  replaces the truthy-`or` with a type-narrowed default so a stored
  `0` survives. Bug has been live since v1.0.0; first reproduced in
  production on a Video Games round where "Table Tennis" landed in A.

### Added
- Three regression tests in `tests/test_game.py` exercising
  `correct_idx = 0`:
  - `test_single_mode_click_A_when_correct_is_A_scores_correct` —
    clicking the correct A-slot must award points.
  - `test_single_mode_click_B_when_correct_is_A_is_wrong` — the
    sibling check that a wrong click is still wrong (i.e. the
    sentinel coerce didn't accidentally invert the comparison).
  - `test_open_mode_first_correct_A_click_wins` — open-mode
    counterpart so the regression is covered on both code paths.

  These tests fail against v1.0.0–v1.0.8 and pass against v1.0.9.

### Notes
- Zero behavior change outside the click-correctness path. All other
  v1.0.6/1.0.7/1.0.8 workarounds (admin allowlist, score index,
  bootstrap button, daily backstop, disabled-row finalize, MockClock
  tests) remain untouched.

## [1.0.8] - 2026-05-31

### Changed
- **SDK pin floor bumped to `>=0.5.4,<0.6.0`** in both `requirements.txt`
  and `requirements-dev.txt`. SDK 0.5.4 is a test-harness-only release —
  production runtime files (`_context.py`, `_plugin.py`, `_components.py`,
  `_exceptions.py`, `_transport.py`, `cli.py`) are byte-identical to 0.5.3.
  Bumping the floor locks in the native mock fixes so fresh clones can't
  resolve a stale 0.5.2/0.5.3 and break on `allowed_mentions` /
  `components` kwargs.

### Removed
- Deleted the three `tests/conftest.py` harness shims
  (`_patched_respond`, `_patched_followup`, `_patched_edit_message`).
  SDK 0.5.4's native MockContext accepts `allowed_mentions=` on
  respond/followup and `components=` on edit_message, recording
  byte-equivalent dicts to what the shims produced. Conftest is now ~20
  lines (the canonical scaffold shape: side-load `__main__.py` only).

### Added
- `tests/test_dispatch.py::test_cooldown_gate_blocks_second_play_within_window`
  and `::test_cooldown_gate_releases_after_window_elapses` — use the new
  SDK 0.5.4 `MockClock` to lock in the 3-second per-user cooldown on
  `/trivia play`, including the user-visible "Slow down — try again in
  Ns" copy and the check-before-defer ordering invariant
  ([__main__.py:1213](__main__.py)).
- `tests/test_cache.py::test_negative_cache_expiry_reopens_source` —
  verifies `NEGATIVE_TTL` actually drives KV expiry. After a
  `RATE_LIMITED` 600s window elapses, OTDB is re-attempted as a fetch
  source rather than permanently skipped. Catches a regression where
  someone drops the `ttl_seconds=` kwarg from `write_negative` and
  silently locks OTDB off after a single 429.

### Notes
- **Zero production codepath changes.** The upload zip is byte-identical
  to v1.0.7 except for `manifest.json` (version bump) and
  `requirements.txt` (SDK pin floor). All v1.0.6/1.0.7 workarounds
  (KV admin allowlist, score index, bootstrap button, daily backstop)
  remain in place.
- The three test-harness gaps Trivium previously worked around in
  `tests/conftest.py` (`allowed_mentions` on respond/followup,
  `components` on edit_message) are resolved natively in mmo-maid-sdk
  0.5.4. The five platform-side blockers (`get_guild` 404, `list_roles`
  permissions=None, `kv.list` empty, slash-command propagation,
  pool-mode `@plugin.schedule`) remain open and are unaffected by 0.5.4.

## [1.0.7] - 2026-05-17

### Changed
- **SDK pin bumped to `>=0.5.3,<0.6.0`.** v0.5.3 dropped 2026-05-17 (four
  days after the v0.5.2 we shipped on). Existing code paths require no
  rewrites; the upgrade is opt-in feature-by-feature.

### Added
- **Round buttons now disable after the answer is revealed.** v0.5.3's
  `ctx.discord.edit_message` accepts a `components` kwarg that v0.5.2
  rejected. `finalize_round` now swaps the live answer row for a disabled
  row that greys out all four buttons and marks the correct one with a
  green ✓ — closing the v1 "Buttons remain visually clickable" known
  limitation. Disabled-row custom_ids carry a `:done` suffix so the
  existing `parse_custom_id` regex rejects any click that might still
  slip through, and the user gets the same "this round has expired"
  feedback. A `TypeError` from a downgraded runtime falls back to
  embed-only edit and logs a warning.

### Diagnostic
- **SDK 0.5.3 does NOT resolve any of the five known platform/SDK
  roadblocks** documented through v1.0.6: `get_guild` 404, `list_roles`
  returning `permissions=None`/`0`, `kv.list` returning empty, slash-command
  propagation to Discord, and `@plugin.schedule` not firing in pool-mode
  workers. All v1.0.6 workarounds (KV admin allowlist, score index,
  bootstrap-via-button, daily-backstop-on-message) remain necessary.

### Tests
- New regression in `tests/test_game.py` asserts that after a correct
  click, `edit_message` receives `components=[ActionRow(four disabled
  Buttons)]` with the correct button styled `success` and labeled with
  a ✓ marker.
- `tests/conftest.py` patches `_MockDiscord.edit_message` to accept and
  record the `components` kwarg — the SDK 0.5.3 testing harness still
  lags the real Context signature by that one kwarg.

## [1.0.6] - 2026-05-15

### Fixed
- **Bootstrap-via-button to unblock first-time admin setup.** v1.0.5
  production confirmed that the MMO Maid platform isn't pushing manifest
  slash-command choice updates to Discord — the dropdown still serves the
  v1.0.3 choice list, even after a manifest with renamed values and a
  full Discord client refresh. Until the platform fixes that, the
  admin-bootstrap sub-command remains unreachable via slash.
- 1.0.6 sidesteps the issue with a button: when `/trivia config action:`
  denies a user and the admin allowlist is empty, the denial message now
  includes a "Claim Trivium admin (one-time)" button. Clicking it runs
  the same bootstrap logic. Buttons are delivered inline on the message
  and don't require pre-registration, so this works regardless of
  slash-command propagation state.
- Once the platform fixes propagation, the slash path becomes available
  too — this button stays as a UX nicety.

### Added
- `@plugin.on_component("triv-bootstrap:claim")` handler — exact-match
  component routing, so it doesn't conflict with the existing dynamic
  `triv:` game-button dispatcher.
- 4 new tests covering denial-with-button, denial-without-button (when
  admins exist), button-click-bootstraps, and button-click-refuses-when-
  already-bootstrapped.

### Diagnostic confirmed
- v0.5.2 production `list_roles` returns role dicts **without a
  `permissions` field at all** (`first_role_keys: "color,id,managed,
  mentionable,name,position"` — no permissions). The Discord-based admin
  path (Layers B/C) is permanently dead in v0.5.2, not just degraded.
  Layer 0 (KV allowlist) is the only working gate; this release ensures
  it can actually be seeded.

## [1.0.5] - 2026-05-15

### Fixed
- **`/trivia config action:admin-bootstrap` and the other admin actions
  weren't selectable in Discord's dropdown.** v1.0.4 logs confirmed
  Discord still served the v1.0.3 choice list (only show/channel/time/
  difficulty/timer/mode/category) — the new manifest with hyphenated
  `admin-*` values hadn't propagated. Either Discord cached the old
  command tree or the platform didn't re-register on plain upgrade.
- 1.0.5 renames the four admin choice **values** to hyphenless strings
  (`adminbootstrap`, `adminlist`, `adminadd`, `adminremove`). The
  user-facing choice **names** keep the hyphenated form (Discord's
  dropdown shows `admin-bootstrap` etc., which is what users type).
  Net effect: a fresh manifest, distinct from v1.0.4's, that Discord
  has to re-register.
- The `cmd_config` dispatch accepts **both** old (hyphenated) and new
  (hyphenless) value strings for the transition window, so any stale
  Discord cache delivering the v1.0.4 strings continues to work.

### Why this works
- A manifest with new choice values forces Discord to refresh its
  cached command tree on the next interaction.
- The hyphenless values sidestep any Discord-side restriction on
  hyphens in choice values (I haven't fully ruled out as the original
  cause).
- Dual-spelling dispatch eliminates the lock-out window between
  redeploy and Discord cache refresh.

### Verification path
- After deploy, `/trivia config action:` dropdown should now include
  the four admin actions. If still missing, the issue is platform-side
  slash-command registration (not the plugin).

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
