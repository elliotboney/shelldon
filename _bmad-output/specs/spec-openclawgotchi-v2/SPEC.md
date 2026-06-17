---
id: SPEC-openclawgotchi-v2
companions:
  - ../../planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md
  - architecture-spine.md
sources:
  - ../../brainstorming/brainstorm-openclawgotchi-v2-2026-06-15/brainstorm-intent.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability only — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# OpenClawGotchi v2 (shelldon)

## Why

A **vision to realize**, driven by **autonomy and craft**: Elliot wants an E-Ink AI pet that is genuinely his — built ground-up for the enjoyment of building it, not out of legal or architectural necessity. At its core, `shelldon` is a **chat-bot pet**: the owner converses with the LLM brain by text message, and v2's defining move is that this conversation runs over a **pluggable chat transport** rather than v1's hardcoded Telegram. `shelldon` is a v2 rebuild of openclawgotchi (MIT, by Dmitry Turmyshev), running on a Raspberry Pi Zero 2W with remote LLMs as the brain. v2 exists to do two things v1 could not: design out v1's documented failures (OOM crashes, zero tests, safety scattered across a 1513-line connector, per-face-change subprocess spawn, transport hardwired to Telegram) on a clean spine, and give the pet a transport-agnostic conversational core whose face and presence live on the E-Ink screen. **Physical embodiment** through the Pi's unused GPIO (button, BLE presence, sensors) remains a genuinely interesting capability the hardware affords, but it is an *optional, secondary* layer over the chat — not the headline.

## Capabilities

- id: CAP-1
  intent: The owner sends a text message over the pluggable chat transport; this triggers a per-turn brain (remote LLM) that replies in the conversation, while the E-Ink display reflects the pet's face/state.
  success: A message sent by the owner over the initial chat adapter produces an LLM reply in the same conversation within tolerable latency, with the display showing the pet's state, demonstrable end-to-end.

- id: CAP-2
  intent: The pet feels alive between LLM turns via resident reflexes — rule-based micro-behaviors (blink, idle, time-of-day mood) reading a persistent mood/energy/last-interaction struct, independent of the ephemeral brain.
  success: With no LLM turn active (and even with the network down), the pet visibly changes state on a demonstrable schedule.

- id: CAP-3
  intent: OPTIONAL physical sensing — the PiSugar2 button and BLE presence of known devices — is available via the peripheral plugin model (CAP-7), not as core interaction. When enabled, a physical event can feed the pet's state and reactions; the conversation itself stays on the chat transport.
  success: With the optional physical-sensing plugin enabled, a physical event (button press, or BLE presence of a paired device) produces an observable pet reaction; with it absent, the chat-bot pet still functions fully.

- id: CAP-4
  intent: The pet acts proactively, initiating behavior with no preceding user input, driven by personality state and environment.
  success: The pet initiates an action (e.g. greeting on presence, a mood-driven idle behavior) with no prior prompt, demonstrable.

- id: CAP-5
  intent: All privileged operations — credential access, the LLM call itself, tool execution, and safety policy — pass through a single capability broker, which also abstracts the choice of LLM provider.
  success: A test demonstrates that accessing credentials, calling the model, or running a tool from outside the broker is impossible by construction; swapping the provider requires no change outside the broker.

- id: CAP-6
  intent: Context persists across ephemeral turns via a HYBRID memory — (a) a sqlite store holding the conversation-history (ordered, timestamped messages with FTS5 keyword recall; single-owner now, schema shaped so chat_id/user_id can be added non-breaking later) and a `learnings` table of captured observations (dedup by pattern_key, recurrence_count, status pending; see CAP-11's capture/promote pipeline), and (b) a filesystem markdown curated layer: a rewritable about.md doc, discrete facts/, a people/ directory (people the owner MENTIONS in conversation — not humans detected via BLE), and a broker-gated vault/, curated by the LLM (no vector DB) — though workers are spawned and die per turn. Separately, a human-only `DIRECTIVE.md` is owner-authored and read by the bot as authoritative (injected into every prompt) but NEVER written by the bot — the pet's owner-controlled "constitution." The bot may fully rewrite its own about.md; the directive file is off-limits to it. sqlite is raw+queryable; markdown is curated+durable; the dream cycle (CAP-11) bridges them. Writer sets are disjoint: core owns about.md + curated tree + sqlite; the owner solely owns DIRECTIVE.md.
  success: A message stored in a prior turn is recallable (by order and by FTS5 keyword search) and a fact curated into markdown demonstrably influences a later turn's behavior.

- id: CAP-7
  intent: Extensions beyond core are added as plugins under ONE generalized plugin model covering hardware AND behavioral plugins — a single plugin kind that can emit events, SUBSCRIBE to broadcast event kinds (message-answered, tool-used, day-alive...), own PRIVATE plugin state (its own, not core's soul/memory), and CLAIM a display region. The host is the PLUGIN-HOST (renamed from peripheral-host). Plugins still speak only the bus contract and never import core. XP/leveling is an example optional behavioral plugin (CAP-F-style: subscribes to events, owns XP/level state, draws a status-bar widget in a claimed region).
  success: A new plugin (hardware or behavioral) can be added and exercised without changing `core/`, and the build still passes the import-linter.

- id: CAP-8
  intent: When an LLM call fails (error, timeout, rate-limit), the broker retries and/or falls back to the next configured provider so the turn still completes; if every provider fails, the pet degrades to reflex-only (CAP-2).
  success: Injecting a provider failure (e.g. a GLM 500 or timeout) results in the turn completing via a fallback provider, demonstrable.

- id: CAP-9
  intent: The chat conversation runs over a pluggable chat transport behind a transport-agnostic message contract — Telegram is not hardcoded (v1's flaw). One initial adapter ships; further adapters are added later without touching core. The transport adapter holds its own connection credential (e.g. bot token); the broker still holds model + tool credentials.
  success: The initial chat adapter carries owner messages and pet replies end-to-end; a second (stub) adapter can be swapped in by adding an adapter only, with no change to `core/` and the import-linter still passing.

- id: CAP-10
  intent: The pet has an autonomous background life — a core-resident scheduler runs named jobs at independent cadences (interval / cron-style / idle-triggered), replacing v1's single heartbeat loop. Jobs are cost-tiered: cheap in-core reflex jobs (mood drift, blink) need no LLM; few cooldown-gated turn jobs (reflection, dreaming, proactive messages) cost a fork + LLM and run within a daily credit/turn budget. The scheduler is battery-aware (reads PiSugar2 power state): it stretches cadences and skips non-essential LLM turns on battery or low charge, livelier when plugged in. Incoming messages/events bypass the scheduler (immediate); heartbeat is just one job, not the engine.
  success: Distinct scheduled behaviors fire at differing cadences (a reflex job and an LLM turn job observably run on independent schedules); background LLM/credit spend stays bounded by the budget; and on simulated battery / low charge the scheduler demonstrably stretches cadences and skips non-essential LLM turns.

- id: CAP-11
  intent: The pet improves over time via light self-improving learning. During normal turns the worker proposes a `capture_learning(observation, pattern_key?)` memory-op (hot path, no extra LLM); core writes a row to the sqlite `learnings` table (dedup by pattern_key, increment recurrence_count, status pending). In the dream cycle (a scheduled worker turn, CAP-10) the LLM classifies pending learnings and promotes durable/high-value ones — judged by impact + recurrence, not a rigid count — into curated markdown (about.md/facts); sensitive ones route to the broker-gated vault; the rest are pruned. Light scope: no ERRORS/FEATURE_REQUESTS taxonomy, no promotion-to-CLAUDE.md or skill-extraction.
  success: A recurring captured observation is promoted to durable curated memory after a dream cycle and demonstrably influences a later turn's behavior.

## Constraints

- 512MB RAM ceiling (Pi Zero 2W) bounds every design choice.
- Per-turn worker memory must be reclaimed after each turn; nothing accumulates across turns. v1's documented OOM is the defining failure being designed out.
- E-Ink refresh latency is in seconds, not frames; behaviors and animations must tolerate it.
- Python cold-start (0.3–1s) sets the turn-latency floor; a warm-start mechanism is required to keep turns tolerable.
- The brain is pluggable behind the broker; default provider is **GLM**. The supported set includes Ollama (self-hosted over LAN), Gemini, OpenAI/ChatGPT, and OpenRouter.
- Remote-LLM network dependency: there is no brain when offline (this includes self-hosted Ollama over the LAN). The pet must degrade gracefully — resident reflexes (CAP-2) keep running without the LLM.
- Default hardware is the **Waveshare V4 E-Ink screen** (output) and the **PiSugar2 battery HAT** (power + button). Everything beyond this is a plugin (CAP-7).
- BLE presence is **pair-first**: a device counts as "present" only if previously paired (keyed on its stable BLE address, labelled with a friendly name). Arbitrary nearby devices are never scanned or logged — this is the privacy boundary for an always-on desk device.
- SD-card write wear: high-frequency state stays in RAM (periodically checkpointed to one file). Memory is hybrid: sqlite is scoped to the conversation-history store (messages + FTS5) and must use WAL with batched commits to bound writes; the curated markdown layer (about.md/facts/vault) is written atomically (temp file + rename). sqlite is used ONLY for conversation history — not for the curated layer, and no vector DB.
- `core/` must remain LLM-free, mechanically enforced — the build fails if `core/` imports forbidden LLM modules.
- The chat transport is pluggable behind a transport-agnostic message contract; Telegram (or any one transport) must never be hardcoded into core. Single owner now — an owner identity exists, and the conversation schema is shaped so chat_id/user_id keys can be added later without a breaking change.
- Credential split: the chat transport adapter holds its OWN connection credential (e.g. bot token); the capability broker remains the sole holder of MODEL and TOOL credentials.
- Core is the sole WRITER of all state and memory, including the sqlite conversation store; workers may only READ history and non-vault markdown and propose writes via Result.
- No bypass of the capability broker: nothing outside it may hold model or tool credentials, call the model, or run tools.
- Typed, versioned contracts (Envelope/Job/Result) with a test harness present from the first milestone (M0). v1 shipped with zero tests.
- Built ground-up; v1 is reference only (study the guts, own the spine), never a code source.
- MIT attribution to Dmitry Turmyshev is retained (README/NOTICE plus the MIT notice).
- Battery + credit-aware autonomy: no unbounded background LLM spend. Background turn jobs (reflection, dreaming, proactive messages) are cooldown-gated and bounded by a daily credit/turn budget, and the scheduler backs off on battery — stretching cadences and skipping non-essential LLM turns on battery or low charge (CAP-10).

## Non-goals

- Running an LLM on the Pi itself (on-device inference). Self-hosted models such as Ollama are allowed only as remote endpoints over the LAN.
- Always-on audio / microphone listening (deferred — too much for a battery-powered Pi Zero for now).
- Sound output (deferred — none in the default build for now).
- Vector database.
- Docker or Node.
- On-device camera vision.
- Group chat / multi-user / web interface in the initial build (architected-for via the pluggable-transport adapter model and a non-breaking conversation schema, but not implemented now — build single-owner).
- Copying v1 code (v1 is conceptual reference only — see `architecture-spine.md`).
- XP / gamification in core: XP/leveling is an OPTIONAL behavioral plugin (CAP-7), not core, and not necessarily in the default build.

## Success signal

`shelldon` runs continuously on the Pi Zero 2W, holding a multi-turn text conversation with the owner over the pluggable chat transport without OOM (the defining v1 failure), feeling alive between LLM turns through resident reflexes shown on the E-Ink face. Optionally, with physical-sensing plugins enabled, it reacts to presence and button without being prompted. It is a chat-bot pet Elliot built from scratch — a transport-agnostic conversational core with a face on the desk, freed from v1's hardcoded Telegram.

## Assumptions

- Primary IO = the pluggable chat transport (the owner's text conversation in and the pet's replies out), with the Waveshare V4 display as the pet's face/state surface (out). Physical input — the PiSugar2 button and BLE presence — is OPTIONAL, arriving via the peripheral plugin model (CAP-3/CAP-7), not core. No sound in or out in the default build; additional sensors/peripherals arrive via plugins (CAP-7).
- "M0" denotes the first build milestone, with the test harness present from the start.
- Safety policy content is ported conceptually from v1's safety lists rather than newly authored here; the broker enforces it.
- "Dreaming" and the scheduler are autonomous background behavior, bounded by the battery + credit budget — the pet runs scheduled cycles on its own (multi-cadence, cost-tiered) and backs off on battery, never spending unbounded LLM credit in the background (CAP-10/CAP-11).
