---
type: spine-review
target: ARCHITECTURE-SPINE.md
spec: spec-openclawgotchi-v2/SPEC.md
reviewer: rubric-walker
date: 2026-06-15
verdict: PASS-WITH-FIXES
---

# Spine Review — shelldon (OpenClawGotchi v2)

Verdict: **PASS-WITH-FIXES**. The spine is unusually well-scoped: it fixes the real
divergence points, every AD names what it prevents, all eight capabilities are bound and
mapped, and Deferred is mostly hygienic. The fixes below are tightening, not rescue —
two of them close genuine cross-unit divergence holes that the current text leaves open.

---

## Checklist 1 — Does it fix the real divergence points and miss none?

**PASS.** The divergence surface for "the level below" (features/stories built on this
spine) is the set of decisions two independent implementers could make incompatibly. The
spine pins each one:

- **Process topology** — who is a process vs. a module (AD-1, AD-2, AD-3, AD-8). Without
  this, one story embeds the broker in core, another forks it; the spine forbids both.
- **Wire format** — `Envelope`/`Job`/`Result`, 4-byte BE length prefix, UDS, hub-routed,
  schema `v` field (AD-4, Conventions). This is the single highest-divergence surface in a
  multi-process system and it is fully nailed.
- **Write authority** — core is sole writer; workers propose deltas (AD-5). Closes the
  write-race / soul-corruption divergence directly.
- **Memory shape** — markdown tree, no sqlite/vectors, atomic temp+rename, vault gated
  (AD-6, AD-7). Two implementers cannot pick different stores.
- **Concurrency** — ≤1 worker in flight, coalesce-not-backlog, cooldown, reflex fallback
  (AD-9). This is the v1 OOM root cause, pinned as an invariant rather than a hope.
- **Trust** — single credential holder + single egress (AD-2). Closes the "two owners of
  secrets" divergence.

Each maps to a *documented v1 failure* (OOM, zero tests, 1513-line scattered safety,
subprocess-per-face), so the divergence set is grounded, not invented.

**Gap (low):** the **checkpoint cadence/format** of AD-7 ("checkpointed periodically to one
small file") is left open. Two units could checkpoint on different triggers (timer vs.
on-change) or schemas. Likely fine to defer to story-time since it is core-internal (single
writer, AD-5), so it cannot cause *cross-unit* divergence — but it is a latent invariant.
Acceptable as-is; flagging only.

## Checklist 2 — Is every AD's Rule enforceable, and does it actually prevent its stated divergence?

**MOSTLY PASS — two enforceability gaps.**

Mechanically enforced (best class): **AD-1** (CI import-linter), **AD-10** (versioned structs
+ M0 harness), **CAP-5/CAP-7** success criteria explicitly call the linter. Good.

Structurally enforced (prevented by construction): **AD-2** (separate process holds creds —
a worker physically cannot read them via COW per AD-3), **AD-4** (single seam), **AD-5** (one
writer), **AD-8** (plugin never imports core — *also* import-linter-checkable).

Weak enforcement — **the two fixes that matter:**

- **[HIGH] AD-9 has no enforcement mechanism named.** "≤1 worker in flight", "coalesce",
  "cooldown", "reflex fallback on exhaustion" are all *behaviors*, not invariants a tool
  checks. Nothing stops a future story from spawning a second fork or building a turn
  backlog — and that reintroduces the exact v1 OOM this spine exists to kill. AD-1 and
  AD-10 earn their `[ADOPTED]` tags by naming a check (linter, tests). AD-9 is the
  highest-stakes AD and names none.
  *Fix:* add to AD-9's Rule a required test: "the arbiter is covered by a concurrency test
  from M0 asserting ≤1 in-flight worker and coalesce-not-backlog under a burst of events."
  This converts AD-9 from intent to enforced invariant and ties it to the AD-10 harness.

- **[MEDIUM] AD-6 atomicity is asserted but untested.** "Every write is atomic (temp +
  rename)" prevents a half-written `about.md` corrupting the soul, but nothing verifies it.
  A worker-proposed memory-op path that forgets the rename diverges silently.
  *Fix:* one sentence in AD-6 requiring a test that a crash mid-write leaves the prior tree
  intact (or at minimum that all memory writes route through one atomic-write helper).

All other AD Rules pass: each names a divergence and the Rule structurally prevents it.

## Checklist 3 — Could anything under Deferred let two units diverge?

**MOSTLY PASS — one item to tighten.**

Safe defers (runtime/story detail, absorbed by an existing AD, cannot fork two units):
memory folder categories (AD-6 owns the contract), proactivity budget (AD-9 cooldown
holds the line), bounded pool N>1 (AD-3/AD-9 explicitly keep it at 1), extra plugins (AD-8
contract absorbs), exact model id (broker-internal, AD-2), reflex catalogue, sound/audio
(spec non-goals).

- **[MEDIUM] "BLE pairing UX flow" defer is under-constrained as written.** The defer says
  "pair-first already decided at spec level" — true (SPEC Constraint: pair-first, keyed on
  stable BLE address, friendly-name label, never scan arbitrary devices). But that
  *privacy invariant* is the load-bearing part and it lives only in the spec, not in any
  AD. A peripheral plugin author reading only the spine could implement promiscuous BLE
  scanning without violating AD-8. The *UX flow* is fine to defer; the *pair-first /
  no-arbitrary-scan privacy rule* is an invariant that should be pinned, not deferred.
  *Fix:* add a clause to AD-8 (or a Convention row): "BLE/presence plugins are pair-first —
  a device is 'present' only if previously paired by stable address; arbitrary devices are
  never scanned or logged." This is the privacy boundary for an always-on device and
  belongs in the spine.

Everything else under Deferred is correctly inert.

## Checklist 4 — Is named tech verified-current / nothing asserted without check?

**PASS (with the stated assumption).** Per instructions, the spine-invariant pins
(Python 3.13.x with 3.14.6 noted as upstream, msgspec 0.21.1, bleak 3.0.1, GLM-5.x) are
treated as author-web-verified. The spine is disciplined about *not* asserting versions it
hasn't pinned: omni-epd, spidev, PiSugar2 API, and per-provider SDKs are explicitly carved
out as "component-local / install-time deps … pin in each component's own manifest when the
hardware is in hand" — i.e. deliberately *not* asserted as current. That is the correct
move and removes them from this check's scope.

- **[LOW] One unverified quantitative claim:** "Python cold-start (0.3–1s)" appears in the
  SPEC (carried as the rationale for the fork-server) and is the only hard number not tied
  to a pinned tool version. It is a reasonable Pi Zero 2W figure and only motivates AD-3
  (which is sound regardless of the exact number), so it does not threaten an invariant.
  No action needed at spine level; noting for completeness.

No tech is asserted-current without either a pin or an explicit defer-to-manifest.

## Checklist 5 — Does it cover the driving spec's capabilities (CAP-1..CAP-8)?

**PASS — complete, no orphans, no over-binding.**

| CAP | Bound by frontmatter | Mapped in AD table | Notes |
| --- | --- | --- | --- |
| CAP-1 LLM response | yes | AD-3, AD-2, AD-9 | full path: fork→worker→broker, arbiter-governed |
| CAP-2 reflexes/aliveness | yes | AD-1, AD-5, AD-9 | offline-degrade covered (AD-9 reflex fallback) |
| CAP-3 physical sensing | yes | AD-8 | button + BLE via plugins |
| CAP-4 proactive | yes | AD-9 | cooldown-gated proactive turns |
| CAP-5 single broker | yes | AD-2, AD-4 | SPEC success ("impossible by construction") met by AD-2 separate-process + AD-3 no-creds-in-fork |
| CAP-6 cross-turn memory | yes | AD-5, AD-6, AD-7 | 3-layer + vault model present; vault gating in AD-6 |
| CAP-7 pluggable peripherals | yes | AD-8 | SPEC success ("add plugin w/o touching core, linter passes") met by AD-8 + AD-1 |
| CAP-8 fallback on error | yes | AD-2, AD-9 | provider chain + arbiter degradation to reflex |

Every CAP from the SPEC is bound in frontmatter, lives somewhere in the Structural Seed,
and is governed by at least one AD. The "3-layer memory + vault" wording of CAP-6 maps
cleanly onto AD-6 (durable tree) + AD-7 (RAM working memory) + vault gating. No capability
is invented beyond the spec; no spec capability is dropped.

## Checklist 6 — Greenfield, no parent spine?

**PASS.** `companions: []`, `sources:` points only at the SPEC, `altitude: initiative`,
and the prose treats v1 as conceptual reference only (matching SPEC non-goal "no copying v1
code"). No inherited invariants are assumed; nothing references a higher spine. Correct for
a greenfield build-substrate.

---

## Summary of findings

| # | Sev | Finding | Fix |
| --- | --- | --- | --- |
| 1 | HIGH | AD-9 (the OOM-prevention AD) names no enforcement; ≤1-in-flight / coalesce are behaviors, not checked invariants | Add a required M0 concurrency test to AD-9's Rule asserting ≤1 worker + coalesce-not-backlog under burst |
| 2 | MEDIUM | BLE pair-first / no-arbitrary-scan privacy rule lives only in the spec; a plugin author reading the spine could scan promiscuously | Pin pair-first as a clause in AD-8 (or a Convention row); keep only the *UX flow* deferred |
| 3 | MEDIUM | AD-6 atomic-write invariant is asserted but unverified | Add a one-line test requirement (crash-mid-write leaves prior tree intact / single atomic-write helper) |
| 4 | LOW | AD-7 checkpoint cadence/format unspecified | Acceptable to defer (core-internal, single writer → no cross-unit divergence); flag only |
| 5 | LOW | "Python cold-start 0.3–1s" is the one unverified number | No action; motivates AD-3 but threatens no invariant |

Net: a strong spine. Item 1 is the one that genuinely matters — it leaves the spine's
reason-for-existing (v1 OOM) enforced only by good intentions. Items 2 and 3 close real but
narrower divergence/safety holes. Everything else is sound.
