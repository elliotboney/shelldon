# Brainstorm Intent — shelldon (OpenClawGotchi v2)

> **Updated 2026-06-15 during spec'ing** (`_bmad-output/specs/spec-openclawgotchi-v2/SPEC.md`). Sections below reflect decisions refined after this brainstorm: provider default + fallback, sensor scope pullback to plugins, deferred audio. The SPEC is the authoritative contract.
>
> **Corrected 2026-06-16** — product framing fix: shelldon v2 is fundamentally a **chat bot** (text conversation with the LLM brain) whose **chat transport is pluggable** (Telegram becomes one adapter, not the whole interface). Embodiment (sensors/buttons/BLE/GPIO) is an **optional** peripheral layer, not the core direction. Single owner now; group/multi-user/web are architected-for, not initial. The SPEC remains authoritative.

## Framing
`shelldon` is a ground-up v2 rebuild of openclawgotchi: an E-Ink AI pet running on a Raspberry Pi Zero 2W (~512MB RAM) with remote LLMs as the brain. The real driver is autonomy and craft — Elliot wants something that is genuinely *his* and enjoys building from scratch, not legal or architectural necessity. openclawgotchi is MIT-licensed (created by Dmitry Turmyshev), so lifting/rebuilding is lawful; Dmitry is to be credited.

## Locked Decisions
- **Build ground-up** — reason is enjoyment of building from scratch, not necessity.
- **v1 as reference, not source** — own the spine, study the guts.
- **Attribute Dmitry** — README/NOTICE plus MIT notice retained.

## Architecture Spine (one line each, each maps to a real v1 pain)
- **Ephemeral workers** — spawn per turn, die after → fixes v1's documented OOM crashes; this is the load-bearing, non-retrofittable bet that justifies a rewrite.
- **One capability broker** — sole holder of credentials + safety policy → replaces safety scattered across v1's 1513-line litellm_connector.
- **Envelope bus** — async in-process message seam (Envelope/Job/Result) → replaces v1's bolted-on Discord-thread plumbing.
- **Long-lived display service** — persistent renderer → replaces v1's per-face-change subprocess spawn.
- **msgspec contracts + tests** — versioned typed contracts with a test harness from M0 → replaces v1's zero tests.

## Top Design Upgrades to Fold In
- **Fork-server** (vs the doc's recycle worker) — parent pre-imports LLM libs once, `os.fork()` per turn; COW shares pages, child death frees RAM → warm-start *and* bounded RAM, dissolving the cold-start-vs-RAM tradeoff.
- **Resident reflexes + personality-state struct in core** — rule-based micro-behaviors (blink, idle, time-of-day mood) reading a mood/energy/last-interaction struct → the soul persists though the brain is ephemeral; the missing "feels alive" piece.
- **Broker the LLM call itself** — broker proxies the model call, not just tools → extends the single security boundary to credentials, cost, and audit.
- **CI import-linter on core/** — build fails if `core/` imports forbidden LLM modules → turns "LLM-free core" from aspiration into a mechanically enforced invariant.
- **Pluggable provider chain + fallback (broker)** — brain is pluggable behind the broker; default **GLM** (Anthropic-like), alternates: Ollama (self-hosted over LAN), Gemini, OpenAI/ChatGPT, OpenRouter. The broker treats providers as an ordered chain — on error/timeout/rate-limit it retries and falls through to the next, so a failing GLM call doesn't kill the turn (a recurring v1-era pain). All-providers-fail degrades to reflex-only.

## Core Direction — CHAT BOT with PLUGGABLE TRANSPORT
shelldon is fundamentally a **chat bot**: the primary interaction is text messages with the LLM brain. v1 hardcoded that conversation to Telegram. v2's real change is making the **chat transport pluggable** — Telegram becomes one adapter behind a transport seam, not the whole interface. The E-Ink display stays the pet's face/expression surface; the conversation happens over the chat transport.
- **Single owner now.** Group chat / multi-user / web interface are architected-for (future transport adapters) but not implemented initially.
- Proactive reflexes (act without being prompted) still hold — they surface through the same brain.

## Optional Embodiment Layer
Physical inputs are an **optional** add-on via the peripheral plugin model — not the fundamental direction. The Pi Zero's spare GPIO headroom enables them, but they are never required for the chat-bot core:
- **Default hardware: Waveshare V4 E-Ink screen + PiSugar2 battery HAT** (power + button). Everything else is a plugin.
- **Sensors/peripherals are pluggable**, not core — deliberately pulled back from a broad sensor surface.
- **BLE presence detection — pair-first** (optional): a device counts as present only if previously paired (keyed on BLE address, friendly-name labelled); no scanning/logging of strangers.

## Constraints (real)
512MB RAM · E-Ink refresh latency (seconds) · Python cold-start (0.3–1s) · remote-LLM network dependency · SD-card write wear from sqlite.

## Explicitly NOT Doing
On-Pi LLM inference (Ollama allowed only as a remote LAN endpoint) · always-on audio / mic listening (deferred — too much for a battery Pi Zero) · sound-out (deferred) · vector DB · Docker/Node · on-device camera vision.

## Lift Conceptually from v1 (reference, rewrite — don't copy)
Faces/display tricks (partial refresh, layered sprites), the 40+ tool patterns, the **hybrid memory model** — sqlite for conversation history (messages + FTS5 recall, as v1 did) plus a markdown curated layer (about.md / facts / vault), where "people" means people the owner mentions, not BLE-detected — and the safety lists — study the approach, re-implement clean on the new spine.
