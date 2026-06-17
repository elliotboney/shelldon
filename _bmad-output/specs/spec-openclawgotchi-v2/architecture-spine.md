# Architecture Spine — OpenClawGotchi v2 (shelldon)

> **⚠️ Superseded for forward architecture.** The authoritative architecture now lives in the adopted companion `ARCHITECTURE-SPINE.md` (`../../planning-artifacts/architecture/architecture-shelldon-2026-06-15/`), with stable `AD-1…AD-12`. This file is retained for the **v1-conceptual-lift reference** (bottom section) and the original rebuild rationale. Where the two differ, the adopted spine wins.

Companion to `SPEC.md`. Holds the load-bearing, non-retrofittable architecture bets that justify a ground-up rewrite, the design upgrades folded in, and the v1 material to lift conceptually. Downstream architecture must honor the spine; these are the HOW behind the kernel's WHAT.

## Spine (each maps to a real v1 pain)

- **Ephemeral workers** — spawn per turn, die after. Fixes v1's documented OOM crashes; this is the load-bearing, non-retrofittable bet that justifies the rewrite.
- **One capability broker** — sole holder of credentials + safety policy; replaces safety scattered across v1's 1513-line `litellm_connector`. (See CAP-5.)
- **Pluggable chat transport** — the owner's conversation with the LLM brain runs over a transport-agnostic message contract behind a chat-transport adapter; v1's hardcoded Telegram becomes one swappable adapter (one ships initially, more later). Single owner now, schema shaped for non-breaking chat_id/user_id later. The transport adapter holds its own connection credential (e.g. bot token); the broker still holds model + tool credentials.
- **Envelope bus** — typed message seam (Envelope / Job / Result); replaces v1's bolted-on Discord-thread plumbing. *(Architecture: cross-process over Unix domain sockets, hub-routed through core — see adopted spine AD-4/AD-11.)*
- **Hybrid memory** — sqlite conversation-history store (ordered/timestamped messages + FTS5 recall, WAL + batched commits) plus a markdown curated layer (about.md/facts/broker-gated vault, atomic temp+rename writes); personality-state + working window in RAM, checkpointed to one file. Core is the sole writer; workers read history + non-vault markdown and propose writes via Result. (See CAP-6.)
- **Long-lived display service** — a persistent renderer holding the display; replaces v1's per-face-change subprocess spawn.
- **msgspec contracts + tests** — versioned typed contracts with a test harness from M0; replaces v1's zero tests.

## Design upgrades folded in

- **Fork-server (vs recycle worker)** — parent pre-imports LLM libs once, `os.fork()` per turn; copy-on-write shares pages, child death frees RAM. Delivers warm-start *and* bounded RAM, dissolving the cold-start-vs-RAM tradeoff. (Addresses the cold-start and RAM constraints.)
- **Resident reflexes + personality-state struct in core** — rule-based micro-behaviors (blink, idle, time-of-day mood) reading a mood/energy/last-interaction struct. The soul persists though the brain is ephemeral; the missing "feels alive" piece. (See CAP-2.)
- **Broker the LLM call itself** — the broker proxies the model call, not just tools; extends the single security boundary to credentials, cost, and audit. (See CAP-5.)
- **CI import-linter on `core/`** — build fails if `core/` imports forbidden LLM modules; turns "LLM-free core" from aspiration into a mechanically enforced invariant.
- **Provider abstraction in the broker** — the broker hides the LLM provider behind one interface; default is GLM (Anthropic-like), with Ollama (self-hosted over LAN), Gemini, OpenAI/ChatGPT, and OpenRouter as alternates. Swapping providers touches only the broker. (Realizes CAP-5.)
- **Ordered provider chain with retry/fallback** — the broker treats providers as an ordered chain: on error/timeout/rate-limit it retries and falls through to the next provider, so a failing GLM call doesn't kill the turn. All-providers-fail degrades to reflex-only. Lives in the broker, never in callers. (Realizes CAP-8.)

## Hardware & peripheral plugin model

- **Default hardware:** Waveshare V4 E-Ink screen (output) and PiSugar2 battery HAT (power + button). The spec deliberately pulls back from a broad sensor surface.
- **Peripheral/sensor plugins:** anything beyond the default is a plugin loaded at the edge, never wired into `core/`. The import-linter that keeps `core/` LLM-free also keeps peripheral drivers out of it. (Realizes CAP-7.)
- **BLE presence (pair-first):** a device is "present" only if previously paired; keyed on stable BLE address, labelled with a friendly name. No scanning/logging of arbitrary nearby devices — the privacy boundary for an always-on desk device.

## Lift conceptually from v1 (reference — rewrite clean, do not copy)

- Faces / display tricks: partial refresh, layered sprites.
- The 40+ tool patterns.
- The 3-layer memory + vault *intent*. (Superseded — CAP-6 now realizes this as a HYBRID: sqlite conversation history (messages + FTS5) plus a markdown curated layer (rewritable about-doc + facts + broker-gated vault), no vector DB. The earlier "no sqlite at all" stance is amended: sqlite returns, scoped to conversation history only. See adopted spine AD-5/AD-6/AD-7.)
- The safety lists.

Study the approach; re-implement clean on the new spine.
