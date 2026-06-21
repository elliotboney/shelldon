# Field Notes — Real Usage (2026-06-21)

Findings from actually using the deployed shelldon (phone → Telegram → Pi). Raw backlog, not stories yet. Pick up via `bmad-quick-dev` (one-offs) or `bmad-sprint-planning` (batch). Several reference **v1** behavior worth porting.

**v1 repo:** `/Users/eboney/Code/04 Mine/openclawgotchi` (`elliotboney/openclawgotchi`).
**Key difference:** v1 uses the `python-telegram-bot` library; v2 transport is **raw Bot API over httpx (0-dep)**. So v1's mechanisms don't copy-paste — but the API calls map 1:1 (`send_chat_action`→`sendChatAction`, `set_my_commands`→`setMyCommands`).

Severity: 🐛 bug · 😤 annoyance · ✨ nice-to-have · ❓ needs investigation

---

## 1. 🐛 Robot voice/noise on by default
Shelldon uses a "robot noise" speech style by default when talking. Should be **off by default**.
- **Action:** find where the robot-noise/voice style is set as default; flip default to plain. Likely a config/personality default.

## 2. ✨ No Telegram "typing…" indicator (v1 regression)
When a Telegram message arrives, v1 showed the bot **typing** so you knew it was working. v2 shows nothing → looks dead during the (slow, LLM) turn.
- **Action:** send Telegram `sendChatAction: typing` when a turn starts, refresh until reply sent. (Telegram clears typing after ~5s, so re-send on a timer for slow turns.)
- **v1 ref:** `src/bot/handlers.py:63` (`send_chat_action(chat_id, action=ChatAction.TYPING)`), also lines 868, 1339.
- **Note:** turns go through the fork worker + live GLM, so this needs to fire from the transport layer on receive, not after the reply is ready.

## 3. 🐛 Bot claims it can't code, but the coding tool should be available
Shelldon tells the user it doesn't have the ability to code. It's **supposed to have a code/tool capability** wired in.
- **Action:** confirm whether the tool is actually registered/exposed to the brain, and whether the system prompt advertises it. Either the tool isn't wired, or the prompt doesn't tell the model it exists.
- **Related:** [[shelldon-self-coding-tools]] (deferred self-coding feature) — confirm this isn't conflating "run a tool" with "write its own tools."

## ✅ DONE (2026-06-21) — root cause of the visible code block

The "code block at the bottom" was a **leaked ops block**, not a formatting issue. GLM filed memories under `collection:"preferences"` / `"capabilities"` (seen twice in the Pi journal), which weren't in the `Literal["facts","people"]` enum → whole block failed decode → left visible by design (`worker.py:88`).

**Fix (option B — broadened the memory model), TDD, 554 tests + 3 import-linters green:**
- `contracts/__init__.py` — `collection` Literal now `facts|people|preferences|capabilities`
- `core/memory.py` — `_COLLECTIONS` mirrors it; new `read_all_collections()` iterates the set (future collections = edit 2 places, not 4)
- `worker/prompt.py` — surfacing uses `read_all_collections()`; SYSTEM_INSTRUCTION now names the valid set so GLM stops inventing collections
- **Not yet deployed to the Pi** — verify live after deploy.

**Still open:** see 4a below, and the defensive "strip any malformed ops block from the reply" (deferred — B reduces its urgency; the `worker.py:88` "never silently swallow" intent is now served by the journal warning).

## 4a. 🐛 Tool usage renders as a raw markdown code block in Telegram
Bottom of the Telegram message shows literal ` ``` ` backticks instead of a formatted code block.
- **Root cause (likely):** v1 builds the *same* ` ``` ` block (`src/llm/litellm_connector.py:1245` — `lines = ["```", f"🔧 Tool usage…"]`) but **sends it with `parse_mode`** so Telegram renders it as monospace. v2 is probably sending with **no/incorrect `parse_mode`**, so the backticks show literally.
- **Action:** set `parse_mode` on the v2 `sendMessage` call. **MarkdownV2 requires escaping** special chars (will break on tool output / URLs) — **HTML mode + `<pre>` is safer**. Confirm what v1 passed (`src/bot/telegram.py:74,86,110` thread `parse_mode` through).

## 5. ✨ No Telegram slash commands (never built in v2)
Verdict: **never built** — v1 registers a full command menu via `set_my_commands` in `post_init` (`src/main.py:217-234`) with handlers in `src/bot/handlers.py`. v2's raw-Bot-API transport has no command set or routing yet.
- **Action:** call `setMyCommands` once on startup + route `/cmd` messages in the transport. Decide which v1 commands still apply to v2's architecture before porting.
- **v1 command set:** `status`, `syncvault`, `vault`, `context`, `mode`, `xp`, `remember`, `recall`, `jobs`, `clear`, `health`, `battery`, `update`. (Several are v1-specific — Obsidian vault, UPS HAT, Lite/Pro mode — and may not map.)

