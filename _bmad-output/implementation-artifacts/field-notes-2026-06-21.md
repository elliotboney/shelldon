# Field Notes — Real Usage (2026-06-21)

Findings from actually using the deployed shelldon (phone → Telegram → Pi). Raw backlog, not stories yet. Pick up via `bmad-quick-dev` (one-offs) or `bmad-sprint-planning` (batch). Several reference **v1** behavior worth porting.

**v1 repo:** `/Users/eboney/Code/04 Mine/openclawgotchi` (`elliotboney/openclawgotchi`).
**Key difference:** v1 uses the `python-telegram-bot` library; v2 transport is **raw Bot API over httpx (0-dep)**. So v1's mechanisms don't copy-paste — but the API calls map 1:1 (`send_chat_action`→`sendChatAction`, `set_my_commands`→`setMyCommands`).

Severity: 🐛 bug · 😤 annoyance · ✨ nice-to-have · ❓ needs investigation

---

## 1. 🐛 Robot voice/noise on by default
Shelldon uses a "robot noise" speech style by default when talking. Should be **off by default**.
- **Action:** find where the robot-noise/voice style is set as default; flip default to plain. Likely a config/personality default.

## 2. ✅ DONE (2026-06-21) — Telegram "typing…" indicator
v1 showed the bot typing; v2 showed nothing → looked dead during the slow LLM turn.
- **Fix (`transport/telegram.py`):** `_start_typing()` on each permitted inbound launches a `_typing_loop` that re-POSTs `sendChatAction: typing` every 4s (Telegram clears it after ~5s); `outbound()` calls `_stop_typing()` before sending the reply. Transport-local — no core/import-linter impact. TDD, 2 tests.

## 3. 🐛 Bot claims it can't code, but the coding tool should be available
Shelldon tells the user it doesn't have the ability to code. It's **supposed to have a code/tool capability** wired in.
- **Action:** confirm whether the tool is actually registered/exposed to the brain, and whether the system prompt advertises it. Either the tool isn't wired, or the prompt doesn't tell the model it exists.
- **Related:** [[shelldon-self-coding-tools]] (deferred self-coding feature) — confirm this isn't conflating "run a tool" with "write its own tools."
- **RESOLVED 2026-06-21:** Not a bug — investigation confirmed v2 has **no tool capability at all** (the bot was telling the truth); this is the deferred self-coding feature. Promoted to **Epic 9 (Self-Coding)** — full live self-coding, tiered, native function-calling. Items **4a (parse_mode) + 5 (slash commands)** are absorbed into Story 9.3's Telegram work. Design: `_bmad-output/planning-artifacts/epic-9-self-coding-design-2026-06-21.md`.

## ✅ DONE (2026-06-21) — root cause of the visible code block

The "code block at the bottom" was a **leaked ops block**, not a formatting issue. GLM filed memories under `collection:"preferences"` / `"capabilities"` (seen twice in the Pi journal), which weren't in the `Literal["facts","people"]` enum → whole block failed decode → left visible by design (`worker.py:88`).

**Fix (option B — broadened the memory model), TDD, 554 tests + 3 import-linters green:**
- `contracts/__init__.py` — `collection` Literal now `facts|people|preferences|capabilities`
- `core/memory.py` — `_COLLECTIONS` mirrors it; new `read_all_collections()` iterates the set (future collections = edit 2 places, not 4)
- `worker/prompt.py` — surfacing uses `read_all_collections()`; SYSTEM_INSTRUCTION now names the valid set so GLM stops inventing collections
- **Deployed + verified live on the Pi (2026-06-21, commit `6ffc0d4`):** real Telegram turn → GLM filed `preferences/ui-mode.md` ("Elliot prefers dark mode"), **0 malformed-ops warnings**, no leaked code block. On-arch tests: 77 passed.

**Still open:** see 4a below, and the defensive "strip any malformed ops block from the reply" (deferred — B reduces its urgency; the `worker.py:88` "never silently swallow" intent is now served by the journal warning).

## 4a. ✅ DONE (2026-06-21) — general parse_mode rendering (shipped early, ahead of 9.3)
v2 sent replies with **no `parse_mode`**, so any markdown rendered as raw text.
- **Fix (`transport/telegram.py`):** `outbound()` sends with `parse_mode="Markdown"`; on a parse rejection (`ok:false` — unbalanced `*`/`_`/backticks in free-form model text) it resends **plain**, so a reply is never dropped over formatting. v1 used the same markdown→plain fallback (`src/bot/telegram.py:86`). TDD, 2 tests.
- **Still owned by Story 9.3:** the richer **HTML+`<pre>` rendering for tool-output blocks** + inline keyboards + `callback_query`. This early fix only covers general reply markdown — it does NOT replace 9.3's tool-output formatting.

## 5. ✨ No Telegram slash commands (never built in v2)
Verdict: **never built** — v1 registers a full command menu via `set_my_commands` in `post_init` (`src/main.py:217-234`) with handlers in `src/bot/handlers.py`. v2's raw-Bot-API transport has no command set or routing yet.
- **Action:** call `setMyCommands` once on startup + route `/cmd` messages in the transport. Decide which v1 commands still apply to v2's architecture before porting.
- **v1 command set:** `status`, `syncvault`, `vault`, `context`, `mode`, `xp`, `remember`, `recall`, `jobs`, `clear`, `health`, `battery`, `update`. (Several are v1-specific — Obsidian vault, UPS HAT, Lite/Pro mode — and may not map.)

