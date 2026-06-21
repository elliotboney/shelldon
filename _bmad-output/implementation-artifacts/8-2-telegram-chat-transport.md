---
baseline_commit: 78ecf67
---
# Story 8.2: Telegram chat transport — talk to shelldon from your phone

Status: done

<!-- Epic 8, the transport follow-on flagged by 8.1's finding #1 (only the stdin-CLI transport existed). Built TDD locally, then verified on the Pi with a DEDICATED v2 bot (separate from v1). Closes the 8.1 multiprocess-transport-stdin gap (Telegram needs no stdin → `python -m shelldon` multiprocess drives real turns). -->

## Story

As the owner,
I want to talk to shelldon over Telegram from my phone — not just stdin on the box,
so that it's an actual desk pet I can message from anywhere, and so the full multiprocess app has a real transport (the headless 8.1 run could only be driven by the in-process launcher).

## Acceptance Criteria (proven on hardware)

### AC1 — A Telegram adapter on the existing transport seam, 0 new deps ✅

**Given** the transport seam (Story 1.6: an `inbound` async string source + an `outbound` async sink; the bus loops are generic)
**When** the adapter is built
**Then** the generic bus loops are extracted to `transport/runner.py` (`run_transport`); `cli.py` is a thin stdin/stdout wrapper; `transport/telegram.py` provides a raw **Bot-API long-poll** `inbound` (httpx `getUpdates`) + a `sendMessage` `outbound`. `httpx` is already transitive (openai/anthropic) → **0 new deps, no Telegram framework**; it's lazy-imported, and the bot token is the adapter's OWN connection credential, so the **"transport holds no model/tool creds" import contract still holds** (3 KEPT).

### AC2 — The allowlist is the security gate ✅

**Given** the brain is behind the transport — a stranger must not be able to drive it
**Then** only `ALLOWED_USERS` (Telegram user ids) reach core; an unauthorized message is dropped + logged, never forwarded. Single-owner: replies route to the chat of the last permitted message. `ALLOW_ALL_USERS` bypasses for open setups. (TDD: a fake httpx client drives allowlist-gate / reply-routing / offset-advance / safe-no-op tests — 7 total.)

### AC3 — A dedicated v2 bot, end-to-end on the Pi ✅

**Given** v1 (openclawgotchi) uses `TELEGRAM_BOT_TOKEN`
**Then** shelldon prefers `SHELLDON_TELEGRAM_BOT_TOKEN` (falls back to the plain name) so **v2 runs a separate bot — no message overlap**. Verified: the owner created a v2 bot via @BotFather, set the token, and messaged it from a phone → the panel showed `thinking → happy`, and shelldon replied over Telegram with a live GLM response ("Hi there! I'm Shelldon, your little screen-dwelling buddy…"). The full multiprocess app (`python -m shelldon`, 5 processes + telegram + E-Ink + GLM) ran on the 416MB Pi at **295MB used / 120MB free**, and a chat turn applied a memory-op (`facts/owner-status.md` written).

### AC4 — Closes the 8.1 multiprocess-transport-stdin gap ✅

Because Telegram needs no stdin, `python -m shelldon` (the production `launch_multiprocess`) now drives real turns end-to-end — the full 5-process separation is proven with a real transport, not just the in-process launcher.

### Out of scope / follow-ons

- **The recurring sqlite history-read degrade** (8.1 finding #2) is now confirmed EVERY turn on the Pi — fail-soft (replies fine) but the worker can't read recent history, so there's **no short-term conversational memory on the Pi**. The top quality bug; tracked in deferred-work, NOT fixed here.
- Telegram niceties (typing indicator, markdown, message chunking for long replies, group support) — later polish.

## Dev Agent Record

### Completion Notes List

- **You can text shelldon now.** A second transport on the same seam, built TDD with a fake httpx client (allowlist gate, reply routing, offset advance), then proven on a real v2 bot end-to-end on the Pi — message in from a phone → GLM reply out + the face on the panel. The transport seam (1.6's injectable inbound/outbound) paid off exactly as designed: a whole new chat surface with `core/` byte-unchanged.
- **0 new deps, contract held.** Raw Bot-API over the already-present httpx (no python-telegram-bot/aiogram). The bot token is the transport's own credential; httpx is a plain HTTP client → the "transport holds no model/tool creds" import-linter contract still passes.
- **Dedicated v2 bot.** `SHELLDON_TELEGRAM_BOT_TOKEN` wins over `TELEGRAM_BOT_TOKEN`, so shelldon and a still-installed v1 run separate bots with no overlap.
- **Closed the 8.1 stdin gap** as a bonus — the full `launch_multiprocess` path now drives real turns (Telegram doesn't need the parent's stdin the way the CLI child did).

### File List

- `shelldon/transport/runner.py` — NEW. Generic bus loops + `run_transport` (extracted from cli.py).
- `shelldon/transport/telegram.py` — NEW. `TelegramChat` (httpx long-poll inbound + sendMessage outbound), `run_telegram_transport`, `run_telegram_from_env`, `resolve_token`, `parse_allowed_users`.
- `shelldon/transport/cli.py` — MODIFIED. Thin stdin/stdout wrapper over `run_transport`.
- `shelldon/app.py` — MODIFIED. `_transport_actor(env)` gate (SHELLDON_TRANSPORT=telegram → telegram, else CLI; both launchers).
- `tests/test_telegram_transport.py` — NEW. 7 tests (allowlist/routing/offset/no-op/token-precedence) via a fake httpx client.

### Change Log

- 2026-06-20 — Story 8.2 done (TDD local, verified on the Pi with a dedicated v2 bot): a Telegram chat transport on the existing seam — raw Bot-API long-poll over httpx (0 new deps), ALLOWED_USERS security gate, SHELLDON_TELEGRAM_BOT_TOKEN for a separate v2 bot. Owner messaged the v2 bot from a phone → thinking→happy on the panel + a live GLM reply; full multiprocess app on the 416MB Pi at 295MB, a chat turn wrote a fact. Closes the 8.1 transport-stdin finding. Suite 548→549 green, 3 contracts KEPT, 0 new deps. Status → done.
