---
stepsCompleted: ['step-01-document-discovery']
documentsIncluded:
  spec: '_bmad-output/specs/spec-openclawgotchi-v2/SPEC.md'
  architecture: '_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md'
  epics: '_bmad-output/planning-artifacts/epics.md'
  prd: 'N/A — spec-path project (SPEC.md is the requirements contract)'
  ux: 'N/A — chat-bot-first, embodiment optional; no UI surface in scope'
---

# Implementation Readiness Assessment Report

**Date:** 2026-06-16
**Project:** shelldon

## Document Inventory

| Type | Status | File |
|---|---|---|
| Requirements (PRD) | ✅ via SPEC | `_bmad-output/specs/spec-openclawgotchi-v2/SPEC.md` |
| Architecture | ✅ | `.../architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md` |
| Epics & Stories | ✅ | `_bmad-output/planning-artifacts/epics.md` |
| UX | ➖ N/A | chat-bot-first, no UI surface in scope |

**Superseded reference (not a duplicate):** `_bmad-output/specs/spec-openclawgotchi-v2/architecture-spine.md` is explicitly marked superseded and defers to the adopted spine. Retained for v1-conceptual-lift reference only.

## Requirements Analysis (SPEC.md as PRD-equivalent)

### Functional Requirements (Capabilities)

- **CAP-1** — Owner sends a text message over the pluggable chat transport → per-turn brain (remote LLM) replies in-conversation while the E-Ink display reflects the pet's face/state. *Success: message → LLM reply within tolerable latency, display shows state, end-to-end.*
- **CAP-2** — Resident reflexes: rule-based micro-behaviors (blink, idle, time-of-day mood) reading a persistent mood/energy/last-interaction struct, independent of the ephemeral brain; runs even with network down. *Success: pet visibly changes state on a schedule with no LLM turn active.*
- **CAP-3** — OPTIONAL physical sensing (PiSugar2 button, BLE presence of known devices) via the plugin model (CAP-7), not core. *Success: with plugin enabled a physical event produces an observable reaction; absent, chat-bot pet still fully functions.*
- **CAP-4** — Proactive behavior: pet initiates action with no preceding user input, driven by personality state + environment. *Success: pet initiates (greeting on presence, mood-driven idle) with no prior prompt.*
- **CAP-5** — Single capability broker for ALL privileged ops: credential access, the LLM call itself, tool execution, safety policy; also abstracts provider choice. *Success: accessing creds / calling model / running a tool from outside the broker is impossible by construction; swapping provider touches only the broker.*
- **CAP-6** — Hybrid memory: (a) sqlite conversation-history (ordered timestamped messages + FTS5 recall; single-owner schema future-proofed for chat_id/user_id) + `learnings` table (dedup by pattern_key, recurrence_count, status pending); (b) markdown curated layer (rewritable about.md, facts/, people/, broker-gated vault/). Owner-only `DIRECTIVE.md` injected into every prompt, never bot-written. Core is sole writer; workers read + propose via Result. *Success: prior-turn message recallable by order + FTS5; a curated fact influences a later turn.*
- **CAP-7** — ONE generalized plugin model (hardware AND behavioral): emit events, subscribe to broadcast event kinds, own private state, claim a display region. Host = plugin-host. Plugins speak only the bus contract, never import core. XP/leveling is an example behavioral plugin. *Success: new plugin added + exercised without changing `core/`; import-linter still passes.*
- **CAP-8** — LLM call failure (error/timeout/rate-limit) → broker retries and/or falls back to next provider so the turn completes; all-providers-fail degrades to reflex-only (CAP-2). *Success: injected provider failure → turn completes via fallback.*
- **CAP-9** — Chat conversation over a pluggable transport behind a transport-agnostic message contract; Telegram not hardcoded. One initial adapter ships; transport adapter holds its own connection credential. *Success: initial adapter carries messages end-to-end; a stub second adapter swaps in with no `core/` change, import-linter passing.*
- **CAP-10** — Autonomous background life: core-resident scheduler runs named jobs at independent cadences (interval/cron/idle-triggered), cost-tiered (cheap in-core reflex jobs vs cooldown-gated LLM turn jobs within a daily credit/turn budget), battery-aware (reads PiSugar2; stretches cadences / skips non-essential LLM turns on battery). Incoming messages bypass scheduler. *Success: distinct cadences observably fire; background spend stays bounded; on simulated battery, cadences stretch and non-essential turns skip.*
- **CAP-11** — Light self-improving learning: worker proposes `capture_learning(observation, pattern_key?)` on hot path (no extra LLM) → core writes to sqlite `learnings`; dream cycle (scheduled turn, CAP-10) classifies pending learnings, promotes durable/high-value ones into curated markdown (sensitive → vault), prunes rest. *Success: a recurring captured observation promoted to durable memory after a dream cycle influences a later turn.*

**Total functional capabilities: 11 (CAP-1 … CAP-11)**

### Non-Functional Requirements & Constraints

- **NFR-1 (Memory ceiling)** — 512MB RAM ceiling (Pi Zero 2W) bounds every choice; per-turn worker memory reclaimed after each turn, nothing accumulates (designs out v1 OOM).
- **NFR-2 (Display latency)** — E-Ink refresh is seconds, not frames; behaviors/animations must tolerate it.
- **NFR-3 (Turn latency)** — Python cold-start (0.3–1s) sets latency floor; warm-start mechanism required (fork-server).
- **NFR-4 (LLM-free core)** — `core/` must remain LLM-free, mechanically enforced; build fails if `core/` imports forbidden LLM modules.
- **NFR-5 (Offline degradation)** — Remote-LLM network dependency; no brain offline (incl. LAN Ollama); must degrade gracefully to resident reflexes.
- **NFR-6 (SD-card wear)** — High-freq state in RAM, periodically checkpointed to one file; sqlite (history only) uses WAL + batched commits; curated markdown written atomically (temp+rename); no vector DB.
- **NFR-7 (Provider set)** — Pluggable brain behind broker; default GLM; supported: Ollama (LAN), Gemini, OpenAI/ChatGPT, OpenRouter.
- **NFR-8 (Transport pluggability)** — Transport pluggable behind transport-agnostic contract; never hardcoded into core; single-owner now, schema shaped for non-breaking chat_id/user_id later.
- **NFR-9 (Credential split)** — Transport adapter holds its own connection credential; broker is sole holder of model + tool credentials; no broker bypass.
- **NFR-10 (Sole writer)** — Core is sole writer of all state/memory incl. sqlite store; workers only read history + non-vault markdown, propose writes via Result.
- **NFR-11 (Test harness from M0)** — Typed, versioned contracts (Envelope/Job/Result) with test harness present from M0.
- **NFR-12 (Default hardware)** — Waveshare V4 E-Ink (output) + PiSugar2 HAT (power+button); everything beyond is a plugin (CAP-7).
- **NFR-13 (BLE privacy)** — BLE presence is pair-first (only previously-paired devices "present", keyed on stable BLE address); arbitrary devices never scanned/logged.
- **NFR-14 (Budgeted autonomy)** — No unbounded background LLM spend; background turn jobs cooldown-gated + daily credit/turn budget; scheduler backs off on battery.
- **NFR-15 (Provenance)** — Built ground-up; v1 reference only; MIT attribution to Dmitry Turmyshev retained (README/NOTICE + MIT notice).

**Total NFRs/constraints: 15**

### Non-Goals (explicit scope boundaries)

On-device LLM inference · always-on audio/mic · sound output · vector DB · Docker/Node · on-device camera vision · group chat/multi-user/web (initial build) · copying v1 code · XP/gamification in core.

### Requirements Completeness Assessment

SPEC is **unusually rigorous** for a spec-path project: every capability carries an explicit, demonstrable success criterion, and constraints are concrete and testable. Capabilities and constraints cross-reference cleanly (CAP-3↔CAP-7, CAP-8↔CAP-2, CAP-11↔CAP-10↔CAP-6). No vague "should be fast/secure" language. **Watch-item for traceability:** CAP-4 (proactive) overlaps heavily with CAP-10 (scheduler) and CAP-11 (dream) — epic coverage must show CAP-4 is realized, not silently absorbed into the scheduler.

## Epic Coverage Validation

### Coverage Matrix

| FR (CAP) | Requirement | Epic / Story | Status |
|---|---|---|---|
| FR1 (CAP-1) | Chat turn end-to-end (msg → LLM → reply + face) | Epic 1 (1.1–1.8) | ✅ |
| FR2 (CAP-2) | Resident reflexes between turns / offline | Epic 3 (3.1, 3.2, 3.3) | ✅ |
| FR3 (CAP-3) | Optional physical sensing (button/BLE) | Epic 7 (7.4) | ✅ |
| FR4 (CAP-4) | Proactive action, no prompt | Epic 5 (**5.4 — own story**) | ✅ |
| FR5 (CAP-5) | Single capability broker | Epic 1 (1.4) | ✅ |
| FR6 (CAP-6) | Hybrid memory (sqlite + curated markdown + vault) | Epic 4 (4.1–4.4) | ✅ |
| FR7 (CAP-7) | Generalized plugin model | Epic 7 (7.1, 7.2) | ✅ |
| FR8 (CAP-8) | Retry / provider fallback / degrade-to-reflex | Epic 1 (1.4 basic retry) + Epic 2 (2.1–2.3) | ✅ |
| FR9 (CAP-9) | Pluggable chat transport | Epic 1 (1.6) | ✅ |
| FR10 (CAP-10) | Autonomous scheduler (cadence/cost/battery) | Epic 5 (5.1, 5.2, 5.3) | ✅ |
| FR11 (CAP-11) | Self-improving learning (capture + dream) | Epic 6 (6.1, 6.2) | ✅ |

### Missing Requirements

**None.** All 11 functional capabilities have a traceable implementation path. CAP-4's overlap risk is explicitly handled by giving proactive action its own Story 5.4 (distinct from scheduler plumbing in 5.1–5.3). No epic stories exist that are *not* traceable back to a SPEC capability or architecture AD.

### Coverage Statistics

- Total functional requirements (CAPs): **11**
- Covered in epics: **11**
- **Coverage: 100%**

## UX Alignment Assessment

### UX Document Status

**Not Found — and correctly so.** This is a chat-first product with no GUI/web/mobile surface. The only visual surface is the E-Ink pet face + display-region compositor.

### Implied-UX Check

The E-Ink face *is* a user-facing surface, so I verified it isn't an unspecified gap. It is fully captured WITHOUT a separate UX doc:
- **Display compositor** — closed/registered `region-id` type in `contracts/`, core owns the `face` region, latest-wins by monotonic `seq` (AD-5, Story 3.3).
- **Concrete expression vocabulary** — Story 3.3 pins a starter emotion set: **content, sleepy, curious, grumpy, excited, low-battery**, each "visibly distinct, recognizable" with mood→face mapping in `contracts/`. This is the de-facto UX contract and it is testable.
- **Latency-aware rendering** — partial-refresh / layered-sprite techniques explicitly required to tolerate E-Ink's seconds-scale refresh (NFR-2/NFR-3).

### Alignment Issues

None. Architecture (AD-5 compositor) supports the visual requirements (CAP-1/CAP-2 faces). No UI component is left architecturally unsupported.

### Warnings

**None blocking.** Minor note: the mood→face *visual* design (actual sprite art per emotion) is an implementation-time asset task inside Epic 3, not a planning gap — the vocabulary and acceptance criteria are specified, the pixels are not (correct level of detail for this phase).

## Epic Quality Review

**Method:** every epic checked for user value vs. technical-milestone framing, independence (Epic N never requires Epic N+1), and forward dependencies; every story checked for sizing, BDD acceptance-criteria quality, and table-creation timing.

### What's strong (verified, not assumed)

- **Walking-skeleton Epic 1 is correctly framed** — titled by user outcome ("Talking Pet"), and its purely-technical stories (1.1 scaffold, 1.2 contracts, 1.3 bus) are justified by the Story 1.8 end-to-end payoff. The cross-cutting note (each 1.1–1.7 ships isolation tests so 1.8 *confirms* wiring) is exactly right.
- **No forward dependencies.** Epic 5↔3 coupling is handled cleanly: Story 3.2 explicitly states the reflex tick "works standalone now… no forward dependency," and Story 5.1 *subsumes* it later (a legal backward dependency). Epic 6 builds on 4+5 (backward). Verified across all 7 epics.
- **Table-creation timing is textbook.** `history.db` created in 4.1 when first needed; `learnings` table "created here" in 6.1 — not provisioned upfront. This is the single most common epic-quality violation and it's absent here.
- **Acceptance criteria are uniformly high quality** — Given/When/Then throughout, with error/edge paths (client disconnect 1.3, power-loss restore 3.1, exhausted retry 1.8, conflicting region claims 7.1). The smaller model did genuinely well here.

### ✅ RESOLVED (2026-06-16) — CAP-5 tool/safety gap deferred by decision

Elliot chose to **defer** tool execution and v1-style safety lists. SPEC updated: CAP-5 intent + success narrowed to credentials + model call; constraint and assumption reworded; two Non-goals added (tool execution; v1 safety-list policy) — both naming the broker as their designated home if added later. `epics.md` FR5 line updated to match. The `tool-used` broadcast event (Stories 7.2/7.3) is now explicitly a no-op until tools land; the XP plugin relies on `message-answered`/`day-alive` triggers. Original finding retained below for the record.

### 🟠 Major Finding (RESOLVED above) — CAP-5 was only half-built across all epics

**CAP-5** defines the broker boundary over **four** privileged operations: credential access, the LLM call, **tool execution**, and **safety policy**. Its success criterion explicitly requires that *"running a tool from outside the broker is impossible by construction."*

The epics cover credentials + model call (Stories 1.4, 2.1–2.2) but **no story implements tool execution or safety-policy enforcement.** Consequences:
- CAP-5's tool clause is **untestable** — there are no tools, so "running a tool from outside the broker is impossible" can't be demonstrated.
- v1's "40+ tool patterns" and "safety lists" (called out in the spine to lift conceptually, and in SPEC assumptions: *"Safety policy content is ported… the broker enforces it"*) have **no implementing story anywhere**.
- This is **not** tracked in `deferred-work.md` (that file is Story-1.1 code-review scope only).

**Recommendation — decide one of:**
1. **In scope** → add a story (likely a new Epic, e.g. "Epic 8: Tools & Safety," or fold into Epic 2's broker hardening): worker requests a tool → broker executes under safety policy → returns Result; plus a safety-policy-enforcement story for the model call. This also lights up the `tool-used` event that Story 7.2/7.3 already assume exists.
2. **Deferred** → explicitly add "tool execution" + "v1-style safety lists" to SPEC Non-goals (or a planning-level deferred-work entry), and soften CAP-5's success criterion so readiness isn't blocked on an untestable clause.

Either is fine — but right now it's an *implicit* gap, which is the dangerous kind.

### 🟡 Minor Concerns

1. ~~**Gemini provider has no adapter path.**~~ **RESOLVED (2026-06-16).** Adapter strategy clarified to group by wire format: **Anthropic-format adapter built first** (GLM-5.2 via Z.ai's Anthropic-compatible endpoint + native Claude), then a single OpenAI-compatible adapter (Ollama/OpenAI/OpenRouter), with **Gemini getting its own adapter**. Stories 1.4 and 2.1 updated accordingly; the spine already permitted this ("Z.ai OpenAI/Anthropic-compatible endpoint").
2. **Proactive-on-presence (5.4) vs BLE (7.4) sequencing.** SPEC's CAP-4 example is "greeting on presence," but presence is Epic 7 (optional, later). Story 5.4 must demonstrate proactive via the **mood-driven** path (no presence) to avoid an implicit dependency on Epic 7. Recommend making that explicit in 5.4's AC ("demonstrable proactive trigger is mood/time-driven; presence-triggered greeting emerges once 7.4 emits presence events").
3. **M0 "atomic-write crash-safety" test lands in Epic 3, not M0.** The architecture lists atomic-write crash-safety as an M0-required test, but the first atomic write (state checkpoint) appears in Story 3.1 — Epics past M0. Reconcile: either pull a minimal atomic-write into Epic 1, or relabel that test as "first-atomic-write story" rather than M0.
4. **`day-alive` event emitter unspecified.** Stories 7.2/7.3 consume a `day-alive` broadcast, but no story says who emits it (presumably a scheduler job in Epic 5). Trivial — name the emitter when Epic 5 or 7 is detailed.

### Best-Practices Compliance

| Check | Result |
|---|---|
| Epics deliver user value | ✅ all 7 |
| Epic independence (no N→N+1) | ✅ verified |
| Story sizing | ✅ appropriate |
| No forward dependencies | ✅ (5.4 caveat → minor #2) |
| Tables created when needed | ✅ exemplary |
| Clear acceptance criteria | ✅ strong |
| Traceability to FRs | ✅ 100% (but CAP-5 partial → major finding) |

## Summary and Recommendations

### Overall Readiness Status

**READY (3 trivial polish items remain).** The Major finding (CAP-5 scope) is resolved by deferral, and Minor #1 (adapters) is fixed. The planning set is strong — 100% FR coverage, clean epic independence, exemplary table-creation timing, high-quality acceptance criteria. The 3 remaining items are one-line clarifications that block nothing before Epics 5/7.

### Issue Count (final)

Started: **1 Major + 4 Minor = 5**. Now: **Major resolved (deferred), Minor #1 fixed → 3 trivial open.**

### Remaining Items (all non-blocking, one-line each)

- **#2** — Story 5.4: state that the demonstrable proactive trigger is mood/time-driven (presence-triggered greeting emerges once Epic 7 BLE lands). Affects Epic 5.
- **#3** — Reconcile the M0 "atomic-write crash-safety" test label: first atomic write is Story 3.1, past M0. Affects Epic 3.
- **#4** — Name the emitter of the `day-alive` broadcast event (likely an Epic 5 scheduler job). Affects Epics 5/7.

### Recommended Next Steps

1. **Continue Epic 1** (Story 1.2 contracts) — nothing blocks it. Story 1.4 now correctly targets the Anthropic-format adapter.
2. **Fix #2–#4 when you reach Epics 3/5/7** — they're due-date-distant; no need to touch them now.
3. **Optional:** run `bmad-spec` validate mode to re-confirm SPEC preservation after today's edits.

### Final Note

Assessment found **5 issues**; the load-bearing one (CAP-5 tool/safety) is resolved by an explicit deferral now recorded in SPEC Non-goals. Remaining work is cosmetic and time-distant. **The plan is ready for continued implementation.**

---

*Assessor: Implementation Readiness workflow (run under Opus 4.8) · Date: 2026-06-16 · Project: shelldon*
