---
stepsCompleted: ['step-01-validate-prerequisites', 'step-02-design-epics', 'step-03-create-stories']
inputDocuments:
  - ../specs/spec-openclawgotchi-v2/SPEC.md
  - architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md
  - ../specs/spec-openclawgotchi-v2/architecture-spine.md
  - /Users/eboney/Code/04 Mine/openclawgotchi  # v1 reference codebase (consulted as needed, not copied)
---

# shelldon (OpenClawGotchi v2) - Epic Breakdown

## Overview

Complete epic and story breakdown for `shelldon`, decomposing the SPEC (capabilities = FRs, constraints = NFRs) and the ARCHITECTURE-SPINE (15 ADs + seed = technical requirements) into implementable stories. No PRD or UX spec — the SPEC is the requirements contract; the only visual surface (E-Ink faces, display regions) lives in the capabilities/ADs. v1 (`/Users/eboney/Code/04 Mine/openclawgotchi`) is consulted for reference only, never copied.

## Requirements Inventory

### Functional Requirements

FR1 (CAP-1): The owner sends a text message over the pluggable chat transport; a per-turn remote-LLM brain replies in the same conversation, while the E-Ink display reflects the pet's face/state.
FR2 (CAP-2): Resident reflexes (blink, idle, time-of-day mood) run continuously between LLM turns off a persistent mood/energy/last-interaction struct, independent of the ephemeral brain and even with the network down.
FR3 (CAP-3, optional): Optional physical sensing — PiSugar2 button and BLE presence of paired devices — feeds the pet's state/reactions via the plugin model; the chat-bot pet functions fully without it.
FR4 (CAP-4): The pet acts proactively, initiating behavior with no preceding user input, driven by personality state and environment.
FR5 (CAP-5): All privileged operations — credential access and the LLM call — pass through a single capability broker that also abstracts the LLM provider. (Tool execution + safety policy deferred — see SPEC Non-goals; broker is their designated home if added later.)
FR6 (CAP-6): Hybrid memory persists context across ephemeral turns — sqlite conversation history (ordered, timestamped, FTS5 recall) + a `learnings` table; markdown curated layer (`about.md`, `facts/`, `people/`, broker-gated `vault/`). Prior-turn content is recallable and curated facts influence later behavior.
FR7 (CAP-7): One generalized plugin model (hardware + behavioral) — plugins emit events, subscribe to broadcast event kinds, own private state, and claim a display region; a new plugin is added without changing `core/` and the import-linter still passes.
FR8 (CAP-8): On an LLM call failure (error/timeout/rate-limit) the broker retries and falls through the ordered provider chain; if every provider fails, the pet degrades to reflex-only.
FR9 (CAP-9): The conversation runs over a pluggable chat transport behind a transport-agnostic message contract — Telegram is not hardcoded; one adapter ships, more are added without touching core; the adapter holds its own connection credential.
FR10 (CAP-10): Autonomous background life — a core-resident scheduler runs named jobs at independent cadences (interval/cron/idle), cost-tiered (cheap reflex jobs vs. budgeted LLM turn jobs), battery-aware (PiSugar2 backoff); heartbeat is one job, not the engine.
FR11 (CAP-11): Light self-improving learning — `capture_learning` on the hot path writes to the `learnings` table; the dream cycle classifies and promotes durable/high-value learnings into curated memory (sensitive → vault), pruning the rest.

### NonFunctional Requirements

NFR1: 512MB RAM ceiling (Pi Zero 2W) bounds every design choice.
NFR2: Per-turn worker memory is reclaimed after each turn; nothing accumulates across turns (v1's OOM is designed out).
NFR3: Behaviors/animations tolerate E-Ink refresh latency measured in seconds, not frames.
NFR4: A warm-start mechanism keeps turn latency tolerable against Python's 0.3–1s cold-start.
NFR5: The brain is pluggable behind the broker; default provider GLM (GLM-5.2 via Z.ai), supported set includes Ollama (LAN), Gemini, OpenAI/ChatGPT, OpenRouter.
NFR6: Graceful offline degradation — resident reflexes keep running with no network/brain.
NFR7: SD-write-wear discipline — high-frequency state in RAM (periodic checkpoint); sqlite scoped to conversation history with WAL + batched commits; curated markdown written atomically (temp+rename); no vector DB.
NFR8: `core/` is LLM-free, mechanically enforced by a CI import-linter.
NFR9: Credential split — chat transport holds its own connection credential; the broker is the sole holder of model + tool credentials; no broker bypass.
NFR10: Typed, versioned contracts (Envelope/Job/Result) with a test harness present from M0.
NFR11: Core is the sole writer of all state and memory (incl. the sqlite store); workers read-only and propose writes via Result.
NFR12: Battery + credit-aware autonomy — bounded background LLM spend (cooldown + daily credit/turn budget; scheduler backs off on battery).
NFR13: BLE presence is pair-first — only previously-paired devices tracked, no promiscuous scanning (privacy boundary).
NFR14: Built ground-up; v1 is reference only, never a code source; MIT attribution to Dmitry Turmyshev retained (README/NOTICE + MIT notice).

### Additional Requirements

(From ARCHITECTURE-SPINE — technical requirements that shape implementation.)

- **No starter template** — greenfield scaffold: Python 3.13.x + stdlib asyncio, no web framework. Epic 1 Story 1 = project skeleton (`core/ broker/ worker/ transport/ display/ plugins/ contracts/ tests/`), the `core/` import-linter, and the M0 test harness.
- **Multi-process actor topology** — `core`, `broker`, `chat-transport`, `display`, `plugin-host` as long-lived actors + ephemeral per-turn workers (AD-1, AD-13, AD-8).
- **Envelope bus** — cross-process over Unix domain sockets, 4-byte length-prefixed msgspec frames, hub-routed through core; closed envelope header (`id/v/kind/src/dst/turn_id`); two routing modes — point-to-point `kind`→dst AND broadcast/subscription fan-out with a registry built from plugin manifests at load (AD-4, AD-11).
- **Broker** as a separate trust-boundary process — sole creds + sole model/tool egress; ordered provider chain with retry/fallback; injects creds internally (AD-2, AD-8/CAP-8).
- **Fork-server** — parent pre-imports LLM libs only, `gc.disable()`+`gc.freeze()` before `os.fork()`, readiness barrier before first turn, ≤1 worker in flight, child death frees RAM (AD-3).
- **Arbiter** in core — ≤1 turn in flight, events coalesce into a single catch-up slot, proactive cooldown, degrade-to-reflex on failure, daily credit/turn budget + battery backoff for turn jobs (AD-9).
- **Scheduler** in core — named multi-cadence jobs (interval/cron/idle), cost tiers, PiSugar2 battery backoff (AD-14).
- **Hybrid memory** — sqlite (`~/.shelldon/history.db`, WAL, FTS5) for conversation history + `learnings` table; markdown curated tree (`about.md`/`facts/`/`people/`/`vault/`), atomic writes; core sole writer; workers read-only minus vault (AD-5, AD-6).
- **Dreaming / learning consolidation** — scheduled introspective worker turn: consolidate history, classify/promote learnings via memory-ops, prune (AD-15).
- **Memory-ops contract** — `remember` / `rewrite_about` / `log_episode` / `capture_learning` with fixed arg schemas in `contracts/`; state deltas are sparse patches over fixed dotted paths (AD-5).
- **Turn identity** — every turn carries a `turn_id`; core fences on it; late/zombie Results discarded; idempotent close (AD-12).
- **Display compositor** — region-id is a closed/registered type in `contracts/`; core owns the `face` region; plugins claim widget regions; conflicts rejected at load; per-region monotonic `seq`, latest-wins (AD-5).
- **Vault isolation** — workers run under a less-privileged uid; `vault/` is OS-unreadable to that uid; vault surfacing is broker-gated (AD-6).
- **Pinned deps** — `msgspec@0.21.1`, `bleak@3.0.1`; `sqlite3` stdlib; component-local (install-time) deps: Waveshare V4 driver + `spidev`, PiSugar2 API, per-provider LLM SDKs in the broker.
- **M0 required tests** — contract round-trip (every envelope encode/decode), the ≤1-worker-in-flight bound, atomic-write crash-safety (AD-10).

### UX Design Requirements

N/A — no UX specification. shelldon is chat-first; the only visual surface (E-Ink pet faces and the display-region compositor) is captured in CAP-1/CAP-2 and AD-5, not a separate UX contract.

### FR Coverage Map

FR1: Epic 1 — chat turn end-to-end (message → LLM → reply + face)
FR2: Epic 3 — resident reflexes + expressive face between turns
FR3: Epic 7 — optional physical sensing (button/BLE) via plugin model
FR4: Epic 5 — proactive action (scheduler-driven)
FR5: Epic 1 — capability broker (single boundary, one provider)
FR6: Epic 4 — hybrid memory (sqlite history+FTS5 + curated markdown)
FR7: Epic 7 — generalized plugin model (hardware + behavioral)
FR8: Epic 1 (basic retry) + Epic 2 (full provider chain/fallback + degrade-to-reflex)
FR9: Epic 1 — pluggable chat transport (one adapter; transport-agnostic contract)
FR10: Epic 5 — autonomous scheduler (multi-cadence, cost-tiered, battery-aware)
FR11: Epic 6 — self-improving learning (capture + dream-cycle promote)

## Epic List

### Epic 1: Talking Pet (walking skeleton)
The owner sends a text message over the pluggable chat transport and the LLM brain replies in the same conversation, with the pet's face shown on the E-Ink. This thinnest end-to-end slice forces the core runtime into existence around a real turn: greenfield scaffold + `core/` import-linter + M0 test harness, the msgspec `contracts/`, the Envelope bus (UDS, hub-routed, closed header), the broker (sole creds, one provider/GLM, basic retry so a single transient error doesn't kill the turn), the fork-server + one ephemeral worker, one chat-transport adapter, and a basic display face.
**FRs covered:** FR1, FR5, FR9, FR8 (basic retry only)

### Epic 2: Resilient Brain
When the LLM errors, times out, or rate-limits, the pet retries and falls through an ordered provider chain; if every provider fails, it degrades to reflex-only instead of freezing. Hardens Epic 1's broker into the resilient brain that survives GLM failing or the network dropping.
**FRs covered:** FR8 (full provider chain, fallback, degrade-to-reflex)

### Epic 3: A Pet That Feels Alive
Between LLM turns the pet visibly lives — blink, idle, time-of-day mood — driven by resident reflexes reading a persistent mood/energy/last-interaction struct, independent of the brain and working offline. Delivers the **expressive face as a first-class deliverable** (not the skeleton's placeholder): the display-region compositor and real expressions that make a creature feel present on the desk.
**FRs covered:** FR2

### Epic 4: Memory & Continuity
The pet remembers — conversation history (sqlite, ordered, FTS5 recall) and a curated markdown layer (rewritable `about.md`, `facts/`, `people/`, broker-gated `vault/`) — so context survives across ephemeral turns and restarts, with core as sole writer and workers reading read-only (minus vault).
**FRs covered:** FR6

### Epic 5: Autonomous Life
The pet acts on its own within a battery + credit budget: a core-resident scheduler runs named jobs at independent cadences (interval/cron/idle), cost-tiered (cheap reflex jobs vs. budgeted LLM turn jobs) and battery-aware via PiSugar2, and the pet initiates proactive behavior (greeting, mood-driven idle) with no prompt.
**FRs covered:** FR10, FR4

### Epic 6: Dreaming & Learning
The pet improves over time: during normal turns it captures learnings on the hot path (`capture_learning` → sqlite `learnings` table), and in a scheduled dream cycle it classifies pending learnings and promotes the durable/high-value ones into curated memory (sensitive → vault), pruning the rest. Builds on memory (Epic 4) and the scheduler (Epic 5).
**FRs covered:** FR11

### Epic 7: Extensibility & Optional Embodiment
Anyone can extend the pet without touching `core/`: one generalized plugin model (hardware + behavioral) where plugins emit/subscribe events, own private state, and claim display regions — exercised by the optional XP/leveling plugin and optional physical sensing (PiSugar2 button, BLE presence). Explicitly optional; the core pet is complete after Epic 6.
**FRs covered:** FR7, FR3

---

## Epic 1: Talking Pet (walking skeleton)

The owner sends a text message over the pluggable chat transport and the LLM brain replies in the same conversation, with the pet's face shown on the E-Ink. The thinnest end-to-end slice — it forces the core runtime into existence around a real turn.

> **Cross-cutting (per review):** every story 1.1–1.7 ships with its own isolation tests (fakes/stubs in `tests/`) so Story 1.8 *confirms* the wiring rather than being the first time anything runs; Story 1.9 then proves it endures.

### Story 1.1: Greenfield scaffold with an enforced LLM-free core and M0 tests

As a developer building shelldon,
I want a project skeleton whose `core/` is mechanically barred from importing LLM code, with a test harness from day one,
So that the load-bearing invariant can never silently rot and every later story ships with tests.

**Acceptance Criteria:**

**Given** a fresh checkout
**When** the project is set up
**Then** the source tree exists (`core/ broker/ worker/ transport/ display/ plugins/ contracts/ tests/`) targeting Python 3.13.x with stdlib asyncio and no web framework
**And** a CI import-linter rule fails the build if any module under `core/` imports an LLM/provider library
**And** `pytest` runs green on an empty harness, wired into CI
**And** the repo carries an MIT `LICENSE` (Elliot's copyright) plus a `NOTICE` crediting Dmitry Turmyshev's openclawgotchi (per the spec's attribution constraint)

**Given** a deliberate violating import added to a `core/` module
**When** CI runs
**Then** the build fails on the import-linter rule, proving the guard works

### Story 1.2: Versioned message contracts

As a developer building shelldon,
I want the `Envelope`/`Job`/`Result` types defined once as versioned msgspec structs with a closed header,
So that every process shares one wire vocabulary and contract drift is caught by tests.

**Acceptance Criteria:**

**Given** the `contracts/` package
**When** the contracts are defined
**Then** `Envelope` carries a closed header (`id`, `v`, `kind`, `src`, `dst`, `turn_id`) and `Job`/`Result` are versioned msgspec structs
**And** a round-trip test encodes and decodes every envelope type without loss (M0 required test)

**Given** a `Job` envelope
**When** it is constructed
**Then** it contains no credential fields (creds never travel on the bus)

### Story 1.3: Envelope bus over Unix domain sockets, hub-routed through core

As a developer building shelldon,
I want core to host a UDS message bus that routes typed envelopes by kind,
So that independent processes communicate through one seam instead of ad-hoc channels.

**Acceptance Criteria:**

**Given** core is running
**When** a client process connects over the Unix domain socket
**Then** it can send and receive length-prefixed (4-byte big-endian) msgspec envelope frames
**And** core routes each envelope by a static `kind`→destination table defined in `contracts/`

**Given** a connected client that disconnects
**When** it drops
**Then** core handles the disconnect without crashing and a reconnecting client resumes cleanly

### Story 1.4: Capability broker with one provider and basic retry

As the owner,
I want a broker process that is the only holder of credentials and makes the LLM call on the pet's behalf, retrying a transient error once,
So that a single GLM hiccup doesn't kill a turn and no other process ever touches my keys.

**Acceptance Criteria:**

**Given** the broker process holding the GLM credential (GLM-5.2 via the Z.ai **Anthropic-compatible** endpoint — the Anthropic-format adapter is the first one built)
**When** core sends a model `Job` over the bus
**Then** the broker injects the credential internally, calls the model, and returns a `Result` — with the credential never appearing on the bus or in core

**Given** a transient model error (e.g. a 500 or timeout)
**When** the broker makes the call
**Then** it retries once before surfacing a failure `Result` (full multi-provider chain is Epic 2)

**Given** any process other than the broker
**When** it attempts to read the credential or call the model directly
**Then** it cannot — the credential is reachable only inside the broker

### Story 1.5: Fork-server worker that runs one turn and dies

As the owner,
I want each LLM turn handled by a warm-forked worker that exits afterward,
So that the pet stays within 512MB and never accumulates memory across turns (v1's OOM).

**Acceptance Criteria:**

**Given** a fork-server parent that has pre-imported the LLM libraries (not credentials), with `gc.disable()`+`gc.freeze()` applied before forking
**When** a turn is requested after the parent signals its readiness barrier
**Then** it `os.fork()`s exactly one worker, which assembles the prompt and proxies the authenticated call to the broker, then exits, reclaiming its RAM

**Given** a turn already in flight
**When** another turn is requested
**Then** at most one worker exists at a time (verified by an M0 concurrency test)

**Given** a worker that has exited or been superseded
**When** a late `Result` arrives carrying its closed `turn_id`
**Then** core discards it (idempotent turn close)

### Story 1.6: One chat-transport adapter over a transport-agnostic contract

As the owner,
I want to message shelldon through one real chat transport that plugs in behind a generic message contract,
So that I can talk to my pet today without Telegram being welded into its core.

**Acceptance Criteria:**

**Given** a transport-agnostic inbound/outbound message contract in `contracts/`
**When** the initial chat adapter — a **local CLI** (chosen first so the end-to-end turn is demoable on a laptop, before any hardware or bot token) — runs as a bus client
**Then** an owner message arrives at core as an inbound-message envelope and a pet reply leaves core as an outbound-message envelope over that adapter
**And** a Telegram (or other service) adapter is explicitly a *later* adapter, added without touching core

**Given** the chat adapter
**When** it connects to its service
**Then** it holds its own connection credential (e.g. a bot token) and never touches model/tool credentials

**Given** `core/`
**When** the adapter is built or swapped
**Then** nothing under `core/` changes and the import-linter still passes

### Story 1.7: Display service shows the pet's face from core state

As the owner,
I want a long-lived display service that renders the pet's face from core's pushed state,
So that there's a creature on the screen, not a terminal.

**Acceptance Criteria:**

**Given** the long-lived display process holding the Waveshare V4 (real driver behind an interface; stub renderer for tests)
**When** core pushes a state snapshot carrying a monotonic `seq`
**Then** the display renders the corresponding face and applies latest-wins, dropping any stale (lower-`seq`) snapshot

**Given** rapid successive snapshots under E-Ink's seconds-scale refresh
**When** they arrive faster than the panel can draw
**Then** the display coalesces to the latest without flicker or backlog

### Story 1.8: End-to-end turn — message in, reply out, face reacts

As the owner,
I want a message I send to produce an LLM reply and a visible face change,
So that the walking skeleton is genuinely alive end-to-end.

**Acceptance Criteria:**

**Given** all of core, broker (1.4), fork-server (1.5), transport (1.6), and display (1.7) running
**When** I send a message over the chat adapter
**Then** core's arbiter spawns a single worker turn, the broker returns an LLM reply, the reply is delivered back over the chat adapter, and the display reflects the pet's state — all within tolerable latency

**Given** a turn is already running
**When** I send another message
**Then** it is not dropped silently — it coalesces into the next turn (no second concurrent worker)

**Given** the broker's single retry is exhausted on a transient error
**When** the turn cannot complete
**Then** the pet surfaces a graceful "can't think right now" state rather than hanging (full fallback/degradation is Epic 2)

### Story 1.9: Endurance — sustained turns without memory growth

As the owner,
I want proof the pet survives a long run of turns without RAM creeping up,
So that v1's defining failure (OOM) is verifiably gone, not just designed against.

**Acceptance Criteria:**

**Given** the skeleton running (through Story 1.8)
**When** it processes a long, sustained sequence of turns (e.g. 500+ over an extended soak)
**Then** resident memory stays flat within a defined bound — no monotonic growth — because workers spawn and die and nothing accumulates across turns (NFR2)

**Given** the soak run
**When** any worker turn completes
**Then** that worker's memory is reclaimed and at no point does more than one worker live (the soak corroborates Story 1.5's bound under sustained load)

---

## Epic 2: Resilient Brain

When the LLM errors, times out, or rate-limits, the pet retries and falls through an ordered provider chain; if every provider fails, it degrades to reflex-only instead of freezing. Hardens Epic 1's broker into a brain that survives GLM failing or the network dropping.

### Story 2.1: Provider abstraction and an ordered chain

As the owner,
I want the broker to drive LLM calls through a provider-agnostic interface with a configurable ordered chain,
So that GLM is just the first choice and alternates are one config line away — never a code change.

**Acceptance Criteria:**

**Given** the broker
**When** providers are configured
**Then** each provider sits behind one common adapter interface, and the broker reads an ordered chain (GLM first, then alternates) from config
**And** adapters group by wire format: the **first adapter built is Anthropic-format** (serving GLM-5.2 via Z.ai's Anthropic-compatible endpoint, plus native Claude), a single **OpenAI-compatible** adapter serves the OpenAI-compatible endpoints (Ollama-over-LAN, OpenAI, OpenRouter), and **Gemini** — compatible with neither — gets its own adapter

**Given** a reordered or extended provider chain in config
**When** the broker restarts
**Then** the new order takes effect with no change outside the broker and the `core/` import-linter still passes

### Story 2.2: Automatic fallback through the chain

As the owner,
I want a failed model call to fall through to the next provider automatically,
So that a GLM 500 or timeout doesn't kill my turn — exactly the v1 pain we're fixing.

**Acceptance Criteria:**

**Given** an ordered provider chain
**When** the current provider returns an error, times out, or rate-limits
**Then** the broker advances to the next provider and retries the call until one succeeds or the chain is exhausted, returning the first successful `Result`

**Given** a forced failure on the primary provider (injected GLM 500/timeout)
**When** a turn runs
**Then** the turn completes via the fallback provider, demonstrable end-to-end

**Given** a fallback occurred
**When** the turn completes
**Then** which provider answered is recorded for audit (no credentials in the record)

**Given** provider faults injected under sustained load (provider killed mid-call, network flapped repeatedly)
**When** turns run continuously
**Then** fallback holds — turns keep completing via the chain or degrade cleanly per Story 2.3 — with no crash, hang, or memory leak across the run

### Story 2.3: Degrade to reflex-only when the whole chain fails

As the owner,
I want the pet to stay alive when every provider is down,
So that a full outage makes it quiet, not frozen or crashed.

**Acceptance Criteria:**

**Given** an exhausted provider chain (all providers failed)
**When** the broker returns a terminal failure `Result`
**Then** the arbiter degrades the turn to a reflex behavior (e.g. a "can't think right now" expression) and the pet keeps running its resident reflexes

**Given** the network is fully offline
**When** I send a message
**Then** the pet acknowledges via a reflex state rather than hanging, and resumes normal turns automatically once a provider is reachable again

---

## Epic 3: A Pet That Feels Alive

Between LLM turns the pet visibly lives — blink, idle, time-of-day mood — driven by resident reflexes reading a persistent state struct, independent of the brain and working offline. Delivers the expressive face as a first-class deliverable, not the skeleton's placeholder.

### Story 3.1: Persistent personality-state struct

As the owner,
I want the pet to have an inner state (mood, energy, last-interaction) that survives restarts,
So that it has continuity of self — it isn't reborn blank every reboot.

**Acceptance Criteria:**

**Given** core
**When** it starts
**Then** a personality-state struct (mood/energy/last-interaction) lives in RAM and is restored from its last checkpoint, defaulting cleanly on first run

**Given** the struct changes
**When** writes occur
**Then** core is the sole writer, mutations are sparse patches over fixed dotted paths (e.g. `mood.valence`), and state is periodically checkpointed to one small file (not on every change — SD-wear)

**Given** an abrupt power loss between checkpoints
**When** the pet restarts
**Then** it restores the last checkpoint without a corrupt-state crash (worst case: loses only changes since the last checkpoint)

### Story 3.2: Resident reflex loop

As the owner,
I want the pet to blink, idle, and drift mood on its own between messages,
So that it feels like a living creature, not a frozen screen — even with the network down.

**Acceptance Criteria:**

**Given** no LLM turn is active (including network offline)
**When** time passes
**Then** in-core reflexes (blink, idle animation, time-of-day mood drift) run on a basic in-core tick and mutate the personality-state struct in-process, with no LLM call

**Given** reflexes are running
**When** an LLM turn begins
**Then** reflexes and the turn coexist without fighting over state (single-writer core serializes mutations)

**Given** the reflex tick
**When** the scheduler arrives in Epic 5
**Then** this tick is structured so it can later be subsumed as a cost-tier "reflex job" without changing reflex behavior (no forward dependency — it works standalone now)

### Story 3.3: Expressive face via the display compositor

As the owner,
I want the E-Ink to show real expressions that match the pet's mood,
So that I can read how my pet feels at a glance — the soul on the screen.

**Acceptance Criteria:**

**Given** the display
**When** it renders
**Then** it is a compositor of regions with a closed/registered `region-id` type in `contracts/`, and core owns the `face` region (plugin-claimed regions come in Epic 7)

**Given** a personality-state change
**When** core pushes a face snapshot (monotonic `seq`)
**Then** the display maps mood/energy to a distinct expression and renders it latest-wins, using partial-refresh/layered-sprite techniques (lifted conceptually from v1) to stay within E-Ink's seconds-scale refresh

**Given** a defined starter emotion set — **content, sleepy, curious, grumpy, excited, low-battery** (the agreed vocabulary; mood→face mapping lives in `contracts/`/display)
**When** each state is set
**Then** each renders a visibly distinct, recognizable face — not a single static placeholder, and not a vaguer "some faces" interpretation

> **Owner decision 2026-06-17 — faces are self-modifiable data, not a hardcoded enum.** The starter set is *seeded* into an editable `~/.shelldon/faces.toml` registry; **core** (sole writer of soul data, AD-5) owns it, maps mood→face, and pushes the token (the display stays a dumb renderer — a sanctioned deviation from "display maps"). Story 3.3 builds the substrate + the in-core `apply_add_face` (validate + atomic comment-preserving write). The **chat-driven** half is split to Story 3.4.

### Story 3.4: Self-modify faces via chat

> **Deferred 2026-06-17 — rides the write-back wire (Story 4.5).** The chat-driven path needs the worker-proposes-over-`Result` wire (the turn-topology reshape, decision: worker-emits-Result), which is **Story 4.5**. 3.4 = "when the LLM proposes an `add_face` op, core applies it via 3.3's `apply_add_face`" — a thin add once 4.5 exists. The "core applies" half (`apply_add_face`) already shipped in Story 3.3.

As the owner,
I want to tell the pet (in chat) to add or tweak a face, and have it do so,
So that its expressions grow with it — the v1 capability I loved, made safe under single-writer core.

**Acceptance Criteria:**

**Given** an owner message asking for a new/changed face
**When** the turn runs
**Then** the worker proposes a structured `add_face` memory-op in its `Result` (the first AD-6 memory-op — `Result` carries *proposed* changes, no free-text writes), with **no credentials and no direct write** (workers never write — AD-5)

**Given** a proposed `add_face`
**When** core receives the `Result`
**Then** core **validates and applies** it via Story 3.3's `apply_add_face` (atomic, comment-preserving `faces.toml` write), rejecting a malformed proposal without mutating anything, and the new face is selectable on the next mood match

**Given** this is the first memory-op
**When** Epic 4 builds the full memory-op suite (`remember`/`rewrite_about`/`log_episode`/`capture_learning`, AD-6)
**Then** they **reuse** this `Result`-carries-proposed-ops + core-validates-and-applies machinery — 3.4 seeds the pattern, it is not a throwaway

---

## Epic 4: Memory & Continuity

The pet remembers — conversation history (sqlite, ordered, FTS5 recall) and a curated markdown layer (rewritable `about.md`, `facts/`, `people/`, broker-gated `vault/`) — so context survives across ephemeral turns and restarts, with core as sole writer.

### Story 4.1: Conversation-history store

As the owner,
I want every message stored in order with keyword recall,
So that the pet can remember and reference what we've said.

**Acceptance Criteria:**

**Given** core
**When** a turn completes
**Then** core writes both the owner message and the pet reply to a sqlite store (`~/.shelldon/history.db`) in WAL mode with batched commits, ordered and timestamped
**And** an FTS5 index supports keyword recall over message content

**Given** a worker building a prompt
**When** it needs context
**Then** it reads the history store read-only and cannot write to it

**Given** the single-owner schema
**When** designed
**Then** it is shaped so a `chat_id`/`user_id` key can be added later without a breaking migration (architected, not implemented)

### Story 4.2: Curated markdown memory and memory-ops

> **Split + topology decision 2026-06-17 (Epic 4 planning gate).** 4.2 builds the **"core applies" half**: the closed memory-op schemas in `contracts/`, the `about.md`/`facts/`/`people/` markdown tree written atomically by `core.apply_memory_op`, and the read-only `DIRECTIVE.md` accessor (disjoint writers). The **"worker proposes" wire** is split to a follow-up (**Story 4.5**) carrying the recorded topology decision: **worker-emits-Result** — the broker returns the completion to the worker, which parses → `Result.proposed_ops` → core; the broker stays pure egress (AD-2). **Story 3.4 rides 4.5** too. Mirrors the 3.3→3.4 split.

As the owner,
I want the pet to keep a human-readable, LLM-curated record of what matters,
So that durable knowledge about me persists and shapes how it behaves.

**Acceptance Criteria:**

**Given** the memory-ops contract in `contracts/` (`remember`, `rewrite_about`, `log_episode`) with fixed arg schemas
**When** a worker proposes a memory-op in a `Result`
**Then** core validates and applies it, writing the markdown tree (`about.md`, `facts/`, `people/`) atomically (temp file + rename); workers never write directly

**Given** `about.md` (bot-owned; core sole writer)
**When** the LLM proposes a rewrite
**Then** the new curated doc persists and is injected into later prompts — the owner does not hand-edit `about.md`

**Given** a human-only `DIRECTIVE.md` (the owner's "constitution"; owner is sole writer)
**When** any turn or dream cycle runs
**Then** the bot reads it as authoritative and injects it first, and NEVER writes it — it is not a memory-op target and not on core's write path (disjoint writers, no conflict)

**Given** `people/`
**When** the owner mentions a person in conversation
**Then** that person can be recorded there (people the owner mentions — not BLE-detected)

### Story 4.3: Vault with OS-level isolation

As the owner,
I want sensitive memory the pet can't leak even if its brain is manipulated,
So that a prompt-injected worker can't read or surface my secrets.

**Acceptance Criteria:**

**Given** workers running under a less-privileged uid than core/broker
**When** a worker tries to read `vault/`
**Then** the OS denies it — `vault/` permissions exclude the worker uid (not a self-policed path filter)

**Given** a worker needs vault content in a prompt
**When** it requests surfacing
**Then** the decision is broker-gated; only the broker may authorize vault content into a prompt

### Story 4.4: Memory shapes the turn

As the owner,
I want what the pet knows to actually change how it replies,
So that memory is real, not just stored.

**Acceptance Criteria:**

**Given** stored history and curated memory
**When** a worker assembles a prompt
**Then** it includes `about.md` + a recent-turn window + relevant FTS5 recall

**Given** a fact established in an earlier turn
**When** a later, related turn runs
**Then** the reply demonstrably reflects that fact (CAP-6 success)

### Story 4.5: Worker proposes ops over the Result (the write-back wire)

> **Added 2026-06-17 — split out of 4.2.** The shared write-back protocol for ALL proposed ops (memory-ops AND faces). Build after 4.2; unblocks 3.4.

As the owner,
I want the pet to actually act on what it decides to remember or change,
So that the apply-halves built in 4.2 (`apply_memory_op`) and 3.3 (`apply_add_face`) are reachable from a real turn.

**Acceptance Criteria:**

**Given** a turn completes (topology decision: **worker-emits-Result**)
**When** the broker returns the completion to the worker
**Then** the worker parses its own reply into a structured `Result` (`payload` + a closed `proposed_ops` list), and sends `Result → core` — reshaping the fire-and-forget worker + `RESULT→CORE` routing from 1.5/1.8, while preserving `turn_id` fencing (AD-12) and the ≤1-worker bound (AD-9); the broker stays a pure egress/safety boundary (no pet-domain parsing — AD-2)

**Given** a `Result` carrying `proposed_ops`
**When** core fences and accepts it
**Then** core validates and applies each op via the existing apply paths (`apply_memory_op` for memory-ops, `apply_add_face` for faces — Story 3.4), sole-writer (AD-5); an invalid/oversized proposal is rejected without side effects, and the user-facing reply is unaffected

---

## Epic 5: Autonomous Life

The pet acts on its own within a battery + credit budget: a core-resident scheduler runs named jobs at independent cadences (cost-tiered, battery-aware), and the pet initiates proactive behavior with no prompt.

### Story 5.1: Core scheduler with named multi-cadence jobs

As the owner,
I want the pet's background behaviors to run on independent schedules,
So that mood drift, reflection, and checks each fire at the right cadence — not all gated behind one slow heartbeat.

**Acceptance Criteria:**

**Given** the core scheduler
**When** jobs are registered
**Then** each job has a name, a cadence (interval / cron-style / idle-triggered), and a cost tier (reflex vs. turn)

**Given** Epic 3's reflex tick
**When** the scheduler exists
**Then** reflexes run as cost-tier "reflex jobs" on the scheduler with unchanged behavior, and "heartbeat" is just one job among many

**Given** an incoming message or event
**When** it arrives
**Then** it is handled immediately, bypassing the scheduler (events are not gated behind a tick)

### Story 5.2: Cost-tier gating and credit budget

As the owner,
I want background LLM activity capped,
So that the pet can't quietly burn through my API credits.

**Acceptance Criteria:**

**Given** turn jobs (reflection, dreaming, proactive)
**When** they become due
**Then** the arbiter runs at most one turn at a time, gated by a cooldown, and bounded by a daily credit/turn budget

**Given** the daily budget is exhausted
**When** a non-essential turn job is due
**Then** it is skipped (or deferred) rather than run; reflex jobs (no LLM) continue unaffected

### Story 5.3: Battery-aware backoff

As the owner,
I want the pet to ease off when it's on battery,
So that autonomy doesn't drain the PiSugar2 pack.

**Acceptance Criteria:**

**Given** the scheduler reading PiSugar2 power state
**When** the pet is on battery or at low charge
**Then** it stretches job cadences and skips non-essential LLM turn jobs

**Given** the pet is plugged in / charging
**When** power is ample
**Then** it returns to livelier cadences

**Given** simulated battery/low-charge state in a test
**When** the scheduler evaluates jobs
**Then** the backoff is demonstrable (cadences stretched, non-essential turns skipped — CAP-10 success)

### Story 5.4: Proactive action

As the owner,
I want the pet to reach out on its own sometimes,
So that it feels like a companion with initiative, not just a responder.

**Acceptance Criteria:**

**Given** personality state and environment
**When** a proactive trigger fires (e.g. greeting opportunity, mood-driven idle) within cooldown and budget
**Then** the pet initiates an action with no preceding owner input (CAP-4 success)

**Given** the proactive cooldown or budget is not satisfied
**When** a proactive trigger fires
**Then** the pet does not initiate a turn (reflexes still carry the in-between)

---

## Epic 6: Dreaming & Learning

The pet improves over time: it captures learnings cheaply during conversation, and in a scheduled dream cycle classifies and promotes the durable ones into curated memory, pruning the rest.

### Story 6.1: Capture learnings on the hot path

As the owner,
I want the pet to jot down things worth remembering as we talk,
So that nothing notable is lost before it can be consolidated.

**Acceptance Criteria:**

**Given** the `capture_learning(observation, pattern_key?)` memory-op in `contracts/`
**When** a worker proposes it during a normal turn
**Then** core writes a row to a sqlite `learnings` table (created here), dedup by `pattern_key`, incrementing `recurrence_count`, status `pending` — with no extra LLM call

**Given** a recurring observation
**When** it is captured again
**Then** its `recurrence_count` increments rather than creating a duplicate row

### Story 6.2: Dream cycle — classify, promote, prune

As the owner,
I want the pet to periodically reflect on what it captured and keep what matters,
So that recurring, high-value learnings become durable memory and the rest is cleared.

**Acceptance Criteria:**

**Given** the dream cycle as a scheduled introspective worker turn (an Epic 5 job, within budget/battery rules)
**When** it runs
**Then** the LLM classifies pending learnings and promotes durable/high-value ones (judged by impact + recurrence, not a rigid count) into curated markdown (`about.md`/`facts/`) via memory-ops, routes sensitive ones to the broker-gated vault, and prunes the rest

**Given** a learning promoted in a dream cycle
**When** a later, related turn runs
**Then** the reply demonstrably reflects it (CAP-11 success)

**Given** the dream cycle
**When** it runs
**Then** it also consolidates recent conversation history (e.g. a running summary) so context stays bounded — light scope only (no ERRORS/FEATURE_REQUESTS taxonomy, no promotion-to-CLAUDE.md / skill-extraction)

---

## Epic 7: Extensibility & Optional Embodiment

Anyone can extend the pet without touching `core/`: one generalized plugin model where plugins emit/subscribe events, own private state, and claim display regions — exercised by the optional XP plugin and optional physical sensing. Explicitly optional; the core pet is complete after Epic 6.

### Story 7.1: Plugin-host and the generalized plugin contract

As a developer extending shelldon,
I want one plugin contract and a host that loads plugins as modules,
So that I can add capabilities (hardware or behavioral) without modifying core.

**Acceptance Criteria:**

**Given** the plugin-host process
**When** it starts
**Then** it loads plugins as modules from `plugins/`, each with a manifest declaring subscribed event kinds, claimed resources (GPIO/BLE), and claimed display regions

**Given** two plugins claiming the same resource or display region
**When** the host loads them
**Then** it rejects the conflicting claim at load (no two writers to one region/resource)

**Given** any plugin
**When** it runs
**Then** it is a bus client speaking only the bus contract and never imports `core/`; adding it leaves the import-linter passing (CAP-7 success)

### Story 7.2: Broadcast event subscriptions

As a developer extending shelldon,
I want plugins to react to things that happen in the pet,
So that behavioral plugins can do their job off the same event stream.

**Acceptance Criteria:**

**Given** a closed set of broadcast `event` kinds (e.g. `message-answered`, `tool-used`, `day-alive`)
**When** the bus routes one
**Then** it fans out to all subscribed plugins, using a subscription registry built from plugin manifests at load (no runtime self-registration of new kinds)

### Story 7.3: XP / leveling plugin (optional)

As the owner,
I want an optional XP and leveling system shown on the display,
So that I get the gamified "it's growing" feel — built entirely as a plugin.

**Acceptance Criteria:**

**Given** the XP plugin
**When** subscribed events occur (message answered, tool used, day alive)
**Then** it updates its own private XP/level state and draws a status-bar widget in a claimed display region

**Given** the XP plugin is added or removed
**When** the build runs
**Then** `core/` is unchanged and the import-linter still passes (full v1 XP parity, zero core changes)

### Story 7.4: Optional physical sensing (button / BLE presence)

As the owner,
I want optional physical inputs to feed the pet,
So that, if I add the hardware, it reacts to presence and touch — while the chat pet works fully without it.

**Acceptance Criteria:**

**Given** the optional sensing plugin(s) enabled
**When** the PiSugar2 button is pressed or a paired BLE device comes into range
**Then** an event is emitted and the pet produces an observable reaction

**Given** BLE presence
**When** scanning
**Then** only previously-paired devices are tracked (pair-first); arbitrary nearby devices are never scanned or logged

**Given** the sensing plugins are absent
**When** the pet runs
**Then** the chat-bot pet functions fully (CAP-3 optionality)

---

## Deferred / Icebox

Ideas committed-to in principle but not yet scheduled into an epic. Tracked in `sprint-status.yaml` under `icebox:`.

### Self-coding tools (bot writes its own tools, human-reviewed)

Carry v1 openclawgotchi's ability for the bot to **propose and write new/modified executable code for itself**, gated by **human review** — moving self-modification beyond *data* (faces/memory/persona) to *behavior*.

- **Favored shape (A — PR/contributor):** the bot proposes a **tool + test**, the existing CI gate (`uv sync --locked → lint-imports → pytest`) validates it — the `import-linter` LLM-free-core contract auto-rejects any tool that pulls an LLM lib into `core/` — and a human merges; it ships on the next deploy. Reuses the per-story dev loop and the Story 4.5 "worker proposes ops on the `Result`" transport.
- **Later option (B — live runtime gate):** bot stages a tool, human approves in the running pet, it goes live next turn via fork-reimport. Keeps v1's in-the-moment magic; needs a runtime sandbox + quarantine.
- **Natural neighbor:** Epic 7 (extensibility) — self-coded tools are bot-authored plugins.
- **Not locked:** A-vs-B decision is open; don't schedule until Epic 4 is done. Full design note: memory `shelldon-self-coding-tools`.
