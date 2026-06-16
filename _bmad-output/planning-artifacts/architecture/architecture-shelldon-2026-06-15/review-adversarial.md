# Adversarial Spine Review — shelldon (OpenClawGotchi v2)

**Date:** 2026-06-15
**Method:** Construct concrete pairs of units one level below the spine. Each unit obeys *every* AD to the letter. If the pair still builds incompatibly, the spine has a hole that a new/tightened AD must close.
**Scope reviewed:** ARCHITECTURE-SPINE.md (AD-1…AD-10 + Consistency Conventions + Structural Seed).
**Constraint:** Did not modify the spine file.

---

## Verdict

The spine is structurally sound on *ownership* (one writer, one trust boundary, one core hub) but **under-specifies the wire-level and lifecycle contracts** that two independently-built units must agree on. The strong invariants ("only X may do Y") prevent *ownership* races but not *shape/ordering/encoding* races. Eight holes below; five are CRITICAL or HIGH — each is a place two compliant teams ship code that passes import-linter and contract tests yet fails to interoperate at runtime.

The recurring theme: **the spine fixes WHO and WHETHER, but rarely HOW MANY, IN WHAT ORDER, or IN WHAT SHAPE.** Those are exactly the seams two units diverge on.

---

## HOLE 1 — Envelope addressing/routing is undefined (CRITICAL)

**Units:** `peripheral-host` (emits event envelopes) vs `core` bus hub (routes them) — and symmetrically `core`→`display` snapshot push.

**Both obey the ADs:** AD-4 says comms are "versioned msgspec `Envelope`/`Job`/`Result` over UDS with a length-prefixed frame, **hub-routed through core**." It says components "address each other through the bus only." But it never defines the **addressing field** inside `Envelope`. The Consistency table fixes the *frame* (4-byte BE length + msgspec bytes) and that envelopes carry `v` — nothing about `to`/`from`/`topic`/`kind`.

**How they diverge:**
- peripheral-host team models routing as **topic-based pub/sub**: `Envelope.topic = "presence.arrived"`, hub fans out to subscribers.
- worker/broker team models routing as **explicit recipient addressing**: `Envelope.to = "core"`, point-to-point through the hub.
- display team assumes **kind-based dispatch**: hub routes by `Envelope.kind == "snapshot"` with no addressee at all.

Three units, three mutually-incompatible routing models, all "hub-routed through core" and all "addressing each other through the bus." The hub literally cannot route because there is no agreed field telling it where a message goes. Contract tests pass per-unit (each defines its own `Envelope` extension) right up until M-integration.

**Tightening — NEW AD-11 (Envelope addressing):**
> `Envelope` carries a fixed header: `v:int`, `kind:str` (closed enum: `event|job|result|snapshot|command|control`), `src:str` (actor role name), `corr_id:str` (correlation id, see Hole 5). Routing is **kind+role based**: the hub delivers by a static routing table keyed on `kind` (e.g. `event→core`, `snapshot→display`, `command→peripheral-host`). No free-form `topic`; no per-unit header extension. Payload is a `kind`-specific typed body in `contracts/`.

---

## HOLE 2 — `Result` delta/memory-op schema is owner-split (CRITICAL)

**Units:** `worker` (produces proposed deltas + memory-ops in a `Result`) vs `core` (validates and applies them).

**Both obey the ADs:** AD-5: "a `Result` envelope carries *proposed* deltas + memory-ops (`remember`/`rewrite_about`/`log_episode`), which core validates and applies." AD-10: contracts are versioned and tested. But the **shape of a "delta"** and the **argument schema of each memory-op** is nowhere fixed. AD-5 names the three verbs; it does not say what `remember` *takes*.

**How they diverge:**
- worker team emits `remember(text="...", category="people")` — free text, core does the filing.
- core team expects `remember(path="people/elliot.md", content="...", mode="append")` — worker must resolve the path.
- For state deltas: worker emits a **full proposed state struct** ("here's the new personality-state"); core expects a **sparse patch** (`{mood: +0.1}`). AD-5 says "proposed deltas" — a full struct *is* a delta from core's view, and a patch *is* a delta. Both compliant.

Result: core's validator rejects every worker `Result`, or silently mis-applies. This is *the* hottest seam in the system (every turn crosses it) and AD-5 only nails ownership, not the contract.

**Tightening — AMEND AD-5 (or NEW AD in `contracts/`):**
> The `Result` body is a fixed `contracts/` struct: `state_delta` is a **sparse patch** (named fields → new values; absent = unchanged), `memory_ops` is a list of tagged unions with fixed arg schemas — `remember{category, text}`, `rewrite_about{section, text}`, `log_episode{summary, ts}`. Core owns all path resolution and filing; workers never compute paths. Unknown fields in a delta are a validation error, not ignored.

---

## HOLE 3 — Coalescing rule is ambiguous on shape and bound (HIGH)

**Units:** `arbiter` (coalesces events during an in-flight turn) vs the **next worker** (consumes the coalesced context).

**Both obey the ADs:** AD-9: events during a turn "**coalesce** into the next turn's context (never a turn backlog)." That fixes *no backlog of turns* — it does not fix what coalescing *produces*.

**How they diverge:**
- arbiter team implements coalesce = **keep latest only** (last button press wins; older events dropped).
- arbiter team B implements coalesce = **append all into a list** (worker sees every event since last turn).
- arbiter team C implements coalesce = **merge by type** (one slot per event-kind, latest per kind).

All three "coalesce" and none creates a turn backlog. But the worker that assembles the prompt gets a wildly different context object in each case (scalar vs unbounded list vs map). Worse: "append all" with no cap reintroduces an **unbounded buffer** under poke-stampede — the exact RAM problem AD-9 claims to prevent, while technically obeying "never a turn backlog" (it's one turn, just with N events). The unit building the prompt and the unit building the arbiter diverge on the type *and* on whether it's bounded.

**Tightening — AMEND AD-9:**
> Coalescing produces a bounded `CoalescedContext`: at most one entry per event-`kind` (latest wins), plus a monotonic `dropped_count` per kind. Total size is O(number of event kinds), not O(events). The next turn's prompt-assembly consumes exactly this struct. "Coalesce" = merge-latest-by-kind, not append.

---

## HOLE 4 — Snapshot push ordering vs display has no sequencing (HIGH)

**Units:** `core` (pushes state snapshots on change) vs `display` (renders them on the E-Ink).

**Both obey the ADs:** AD-5: "Display never reads shared memory; **core pushes a state snapshot** on change." AD-4: hub-routed envelopes. Neither fixes **ordering or coalescing of snapshots** at the display, and E-Ink refresh is *slow* (seconds for a full refresh on Waveshare V4).

**How they diverge:**
- core team pushes a snapshot on *every* state change (reflex churn → many snapshots/sec; recall AD-7 says state lives in RAM with high-frequency churn).
- display team renders each snapshot it receives, in arrival order, blocking ~2s per full refresh.

Under reflex churn the display falls arbitrarily behind and renders **stale snapshots** long after state moved on — or, if display drops to "render latest," core and display disagree on whether a given snapshot was shown. There is no snapshot sequence number, no "render-latest-wins" rule, no rate contract. Two compliant units produce a display that lags or flickers unboundedly. (This is the classic snapshot-push-ordering race the prompt names.)

**Tightening — AMEND AD-5 (snapshot contract):**
> Snapshots carry a monotonic `seq:int`. Display is **render-latest-wins**: while a refresh is in flight, intermediate snapshots are discarded; on completion it renders the highest `seq` seen. Core MAY rate-limit snapshot push (min-interval), but correctness must not depend on core's rate — display dropping intermediates is mandatory, not optional.

---

## HOLE 5 — Worker death vs in-flight Result has no fencing (HIGH)

**Units:** `arbiter`/`core` (governs ≤1 worker, applies `Result`) vs the **ephemeral worker** (forked per turn, dies after).

**Both obey the ADs:** AD-3: "the worker dies after its turn"; "**at most one worker in flight**." AD-9: "≤1 worker turn in flight." AD-2: worker proxies the call to broker. None of these fixes the **race between worker death and the broker's late reply / the worker's emitted `Result`.**

**How they diverge:**
- Worker emits `Result`, then dies. Core applies it. Fine.
- Worker times out / is killed by arbiter (provider-chain exhaustion → AD-9 reflex fallback). Arbiter declares the turn over and starts the *next* turn. The dead worker's broker call was still in flight; broker's reply now has nowhere to go, OR a slow worker emits a `Result` *after* the arbiter already fell back to reflex and started turn N+1. Core now has **two state-mutation paths racing** (the late `Result` and the reflex fallback), violating the spirit of AD-5's single-writer-no-races while obeying its letter (core is still the only writer — it just applies a stale delta on top of a newer state).

"≤1 in flight" governs *spawning*, not *settling*. Without a turn-fence/correlation id, core cannot tell a current `Result` from a zombie one.

**Tightening — NEW AD (turn fencing):**
> Each turn has a monotonic `turn_id`. `Job`, `Result`, and the worker's broker call all carry it. Core **rejects any `Result` whose `turn_id` is not the current open turn** (fences zombies). A turn is closed exactly once — by `Result`, timeout, or fallback — and closing is idempotent on `turn_id`. The arbiter does not open turn N+1 until turn N is closed.

---

## HOLE 6 — Vault read-gating has a direct-filesystem bypass (HIGH)

**Units:** `worker` (reads the memory tree directly, minus `vault/`) vs `broker` (gates surfacing `vault/` into a prompt).

**Both obey the ADs:** AD-6: "Workers **read the tree directly, minus `vault/`**; surfacing `vault/` contents into a prompt is a **broker-gated** decision." AD-2: broker is the sole egress. But the worker reads the tree *directly from the filesystem* — there is no enforcement that "minus `vault/`" actually holds. It's a stated convention, not a mechanism.

**How they diverge:**
- worker-prompt-assembly team implements "minus vault" as **a path filter in worker code** (skip any path under `vault/`). Honest, but it's the fox guarding the henhouse — a prompt-injected worker (the exact AD-2 threat model) just reads `vault/` anyway; nothing stops it. The filesystem grants read.
- broker team assumes vault is unreachable by workers and applies its gating only to its own surfacing path — leaving the worker's direct read entirely ungated.

The gate exists in *one* unit's good behavior and is bypassable by the *other* unit's filesystem access. AD-2's whole premise ("a prompt-injected worker reaching secrets") is defeated: `vault/` is on the same filesystem the worker reads directly. Two compliant units, and the trust boundary leaks.

**Tightening — AMEND AD-6 + AD-2:**
> `vault/` is **not on the filesystem path the worker can read** — enforced by OS permissions (worker process runs as a uid/gid without read access to `vault/`), not by worker-side path filtering. Vault contents reach a prompt only as a broker-injected payload after broker gating. "Minus vault" is an OS-enforced boundary, not a convention.

---

## HOLE 7 — Plugin manifest schema is underspecified (MEDIUM)

**Units:** two `peripheral-host` plugins built independently — e.g. `pisugar2-button` (input) vs `ble-presence` (input), or a future output plugin — plus the host loader.

**Both obey the ADs:** AD-8: a plugin has "a manifest declaring event types + GPIO/BLE resources," is "a bus client speaking only `Envelope`," "never imports core." The manifest's *format and fields* are named ("event types + GPIO/BLE resources") but not schematized, and resource-conflict arbitration is undefined.

**How they diverge:**
- button plugin manifest: `{events: ["button.press"], gpio: [4]}`.
- presence plugin manifest: `{event_types: {"presence": "..."}, ble: {adapter: "hci0"}}` — different key names, different shape.
- The host loader written against one shape can't parse the other. And if two plugins both declare `gpio: [4]`, **nothing in AD-8 says who wins** or that the host must detect the conflict — both load, both grab GPIO 4, one silently fails at runtime.

Also: AD-8 says plugins emit/consume envelopes but doesn't say a plugin **declares which `Envelope.kind`s it produces/consumes** in the manifest — so the host can't validate that an output plugin actually handles the command kinds routed to it.

**Tightening — AMEND AD-8 (manifest schema):**
> The manifest is a fixed `contracts/` struct: `name`, `kind:input|output`, `emits:[event-kind]` / `consumes:[command-kind]`, `resources:{gpio:[int], ble_adapter:str?}`. The host **fails to load** on resource conflict (two plugins claiming the same GPIO pin / BLE adapter) and on a plugin whose declared kinds aren't registered in `contracts/`. Manifest is validated at discovery, before the module is imported.

---

## HOLE 8 — Fork-server startup vs first turn / checkpoint ownership race (MEDIUM)

**Units:** `fork-server` parent (pre-imports LLM libs) vs `arbiter` (spawns workers) — and separately `core` checkpoint writer (AD-7) vs `core` durable memory writer (AD-6) both writing under `~/.shelldon/`.

**Both obey the ADs (startup):** AD-3: fork-server "pre-imports LLM libs… and `os.fork()`s one worker per turn." AD-9: arbiter governs turns. Nothing fixes **what happens if the arbiter wants a turn before the fork-server has finished warming libs** (cold boot, slow import on a Pi Zero). The arbiter unit assumes fork-server is ready; the fork-server unit assumes nobody asks until it signals ready. No readiness handshake is specified → first-turn-after-boot either races or drops.

**Both obey the ADs (checkpoint vs tree):** AD-7: volatile state checkpoints to "**one small file**." AD-6: durable memory is the markdown tree, atomic writes. Both are core-owned (AD-5) so no cross-process race — but the spine never says the checkpoint file lives *outside* the markdown tree's atomic-write discipline, nor who reconciles checkpoint vs tree on restart. One core sub-unit (state) and one (memory) could both decide they own `~/.shelldon/state.*` with different formats/locations.

**Tightening — NEW AD or AMEND AD-3/AD-7:**
> (a) Fork-server emits a `control` ready envelope; the arbiter holds turns (degrading to reflex per AD-9) until ready is observed. Readiness is part of the control protocol, not assumed. (b) The checkpoint file path and format are fixed in the structural seed (`~/.shelldon/state.checkpoint`, single file, temp+rename), explicitly **outside** the markdown tree; on restart core loads checkpoint first, then the tree — checkpoint is authoritative for volatile state, tree for durable.

---

## Summary table

| # | Hole | Severity | Units | One-line fix |
|---|------|----------|-------|--------------|
| 1 | Envelope addressing/routing undefined | CRITICAL | peripheral-host / core hub / display | New AD-11: fixed `Envelope` header + kind-based static routing table |
| 2 | `Result` delta/memory-op schema owner-split | CRITICAL | worker / core | Amend AD-5: sparse-patch delta + fixed memory-op arg schemas in `contracts/` |
| 3 | Coalescing shape & bound ambiguous | HIGH | arbiter / next worker | Amend AD-9: merge-latest-by-kind into bounded `CoalescedContext` |
| 4 | Snapshot push ordering vs display | HIGH | core / display | Amend AD-5: `seq` + mandatory render-latest-wins |
| 5 | Worker death vs in-flight Result | HIGH | arbiter / worker | New AD: `turn_id` fence, reject stale Results, idempotent close |
| 6 | Vault read-gating filesystem bypass | HIGH | worker / broker | Amend AD-6/AD-2: OS-enforced vault unreadability, not path filter |
| 7 | Plugin manifest underspecified | MEDIUM | two plugins / host | Amend AD-8: fixed manifest struct + resource-conflict fail-to-load |
| 8 | Fork-server readiness + checkpoint ownership | MEDIUM | fork-server / arbiter; state / memory | New AD/amend: ready handshake; fix checkpoint path outside tree |

**Bottom line:** Close Holes 1, 2, 5 before any M0 contract work — they sit on the per-turn hot path and the trust boundary. Hole 6 is the only one that's a *security* regression rather than an interop one; fix it at the OS layer or AD-2's threat model is theater.
