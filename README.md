# Trivium

> Multiple-choice trivia rounds played through Discord buttons, with per-server leaderboards, per-user streaks, and optional admin-scheduled daily trivia.

A plugin for [MMO Maid](https://mmomaid.com) — runs sandboxed in the platform and reacts to Discord interactions on installed servers.

## What it does

Members run `/trivia play` to start a multiple-choice question with four answer buttons. The default mode is **single-player** (only the user who started the round can answer); an admin can switch a server to **open mode** where anyone can answer and the first-correct click wins. Points are awarded by difficulty (easy +10, medium +20, hard +30) and tracked per user in a server leaderboard. Streaks bump on every correct answer and break on wrong ones.

Admins can configure a **daily trivia channel and UTC time** with `/trivia config`. At the configured time the plugin posts a one-hour open round in the chosen channel, and the first-correct answerer gets a +50 daily bonus on top of the difficulty award. `/trivia daily` shows the current day's result (or "not posted yet").

Questions come from [Open Trivia DB](https://opentdb.com) (primary) with [The Trivia API](https://the-trivia-api.com) as fallback when OTDB is rate-limited, returns no results for a (category, difficulty) combo, or exhausts its session-token suppression window. The OTDB session token is per-server and reused across all categories — its 6-hour idle timeout naturally rolls suppression state on a quiet server.

## Capabilities

This plugin lands in the **Safe** tier. Each capability is requested for a specific behavior:

| Capability | Tier | Why |
|---|---|---|
| `discord:edit_message` | Safe | Reveal the answer in the round embed after a correct (or wrong) click. |
| `discord:send_message` | Safe | Post the round embed for `/trivia play` and the daily question. |
| `interaction:respond` | Safe | Slash-command replies + ephemeral feedback on button clicks. (Auto-added by the runtime; listed for transparency.) |
| `proxy:http` | Safe | Fetch trivia questions from `opentdb.com` and `the-trivia-api.com`. |
| `storage:kv` | Safe | Per-server config, per-user scores, question-batch cache, daily history, in-flight round state. |

## Slash commands

| Command | Description | Permission |
|---|---|---|
| `/trivia play [category] [difficulty]` | Start a round. Defaults: category=General Knowledge, difficulty=any. | Anyone |
| `/trivia leaderboard` | Top 10 by score for this server. | Anyone |
| `/trivia stats [user]` | Lifetime stats. Defaults to your own. Viewing others' stats is open by default. | Anyone |
| `/trivia daily` | Show today's daily trivia status (or how to configure if it isn't set). | Anyone |
| `/trivia config <action> [value]` | Configure daily channel, time, default difficulty, timer (10–60s), mode (single/open), daily category. | Admin (Manage Server) |

`/trivia config` is gated by Discord's `default_member_permissions` field (MANAGE_GUILD bit). The plugin also re-checks the requesting member's permission bit inside the handler as a belt-and-suspenders defense.

## Known limitations (v1)

These are deliberate trade-offs documented up-front so server admins know what to expect:

1. **The embed doesn't auto-reveal the answer on timeout.** If no one clicks an answer button before the round's inflight TTL expires (default 20s for `/trivia play`, 1h for daily), the public embed stays as the original question. Subsequent clicks land on a "this round has ended" ephemeral. We don't have a reliable background-task mechanism that works across pool-mode workers, so we trade silent timeout for guaranteed multi-tenant reliability. Daily rounds with their 1-hour window almost always have a winner.
2. **The buttons aren't visually disabled after the answer is revealed.** The SDK's `edit_message` in v0.5.2 doesn't accept a `components` arg, so the round embed gets the answer revealed but the four buttons remain clickable. Late clicks are caught by the "inflight not found" guard and respond with "this round has ended."
3. **Some categories have no fallback.** Open Trivia DB has 24 categories; The Trivia API covers ~10 of those cleanly. Categories like Video Games, Mythology, Anime & Manga don't have a Trivia API mapping — if OTDB is unavailable for them, `/trivia play` returns "Trivia sources are unavailable, try again in a few minutes."

## Quick start (development)

```bash
# 1. Clone & install
git clone https://github.com/CubCadetXT1/mmo-maid-plugin-trivium.git
cd mmo-maid-plugin-trivium
python -m venv ../mmo-maid-plugin-trivium-venv          # keep .venv outside the repo
source ../mmo-maid-plugin-trivium-venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 2. Local dev loop (hot-reload + mock host)
mmo dev --watch

# 3. Tests
python -m pytest -q

# 4. Pre-flight validation (also runs in CI)
python scripts/validate_plugin.py .
```

Keep the virtualenv outside the repo (or use a name not on the validator's disallowed list) — `validate_plugin.py` flags a `.venv/` at the repo root since it isn't supposed to ship in the upload zip.

## Release process

Releases are tagged on `main` with semver tags (`v1.2.3`), which triggers `.github/workflows/release.yml` to validate, test, build the upload zip, and attach it to the GitHub release.

```bash
# 1. Bump manifest.json "version" and update CHANGELOG.md
# 2. Verify locally
make release          # validates, tests, builds dist/trivium-<version>.zip

# 3. Commit, tag, push
git commit -am "Release v1.2.3"
git tag v1.2.3
git push && git push --tags
```

The tag's version (`v1.2.3` → `1.2.3`) must match `manifest.json`'s `version` field; CI rejects the release otherwise.

## Submitting for review

The MMO Maid dev portal links this repo and pulls the latest tag for review. Review turnaround is typically 1–3 business days. The reviewer checks the manifest, scans for disallowed imports, validates SQL safety (Trivium uses no SQL), and re-prompts installed users on any tier shift.

## Project structure

Runtime files (in the upload zip):

```
manifest.json          slash command schema + capabilities
__main__.py            entire plugin runtime
requirements.txt       pinned to mmo-maid-sdk 0.5.x
```

Repo-only (stripped at release time by `scripts/build_release.py`):

```
README.md              this file
LICENSE                MIT
CHANGELOG.md           version history
tests/                 pytest suite (~120 cases using MockContext)
.github/workflows/     CI + release automation
.gitignore .gitattributes Makefile requirements-dev.txt
scripts/               validate_plugin.py + build_release.py
```

## License

MIT — see [`LICENSE`](LICENSE).
