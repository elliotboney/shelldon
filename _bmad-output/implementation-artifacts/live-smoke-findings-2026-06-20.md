# Live-LLM smoke findings — 2026-06-20

**Retires (partially):** the dominant project risk — the memory/learning line was mechanism-proven but never run against a live LLM (Epic 6 retro action #1; re-affirmed by the Epic 7 retro).

**Model:** `glm-4.7` (GLM via Z.ai, Anthropic-compatible endpoint)
**Layer covered:** ELICITATION only — `provider.complete(assemble_prompt(...))` direct. The full-stack APPLY path (worker→broker→Result→core `_apply_proposed_ops`) is Story 8.0, still pending.
**Command:**
```
set -a; . ./.env; set +a
export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic GLM_MODEL=glm-4.7
uv run pytest -m live -s -k "live_turn or live_dream"
```
**Result:** 2 passed.

---

## Run 1 — live turn (`test_live_turn_elicits_a_memory_op`)

Owner message: *"Please remember this for later: my favorite database is BigQuery."*

**Reply:** "Got it! I'll remember that your favorite database is BigQuery."
**Ops emitted + decoded:**
- `Remember(collection='facts', name='favorite-db', content='BigQuery')`

**Verdict:** ✅ As designed. The real `SYSTEM_INSTRUCTION` + assembly elicits a single clean `remember` op into the `facts` collection, with a well-formed `name`/`content`. The ```ops fenced block parses cleanly via `parse_reply`.

## Run 2 — live dream (`test_live_dream_emits_resolve_and_summary`)

Seeded pending learnings: `[1]` ships features late at night · `[2]` strongly prefers terse replies · `[3]` random one-off musing about the weather. Driven through the **real** `_build_dream_prompt`.

**Reply:** "Memory tidied up."
**Ops emitted + decoded (6):**
- `Remember(facts, 'shipping-habit', 'Owner ships features late at night')`
- `RewriteAbout('Shelldon is a small AI pet… Owner prefers terse replies.')`
- `ResolveLearning(id=1, 'promoted')`
- `ResolveLearning(id=2, 'promoted')`
- `ResolveLearning(id=3, 'pruned')`
- `RewriteSummary('Owner ships late at night and likes short replies.')`

**Verdict:** ✅✅ **Exceeded the AC.** The test only required ≥1 `resolve_learning`; the model emitted the *entire* dream vocabulary with sound judgment:
- Classified all 3 learnings correctly — promoted the two real owner-facts (night-owl, terse), **pruned** the noise (weather musing).
- Split the promotions sensibly: a specific fact → `remember`/`facts`; broader self-knowledge → `rewrite_about`.
- Emitted `rewrite_summary` (the AC3 nicety the test treats as optional) **unprompted-by-necessity** — a compact running summary.

The dream directive works against a real brain, and the model's judgment about durability is good.

---

## What this verifies vs. what remains

| | Status |
|---|---|
| Real model emits decodable `remember` on a turn | ✅ verified live |
| Real model classifies learnings + emits `resolve_learning`/`rewrite_about`/`rewrite_summary` on the dream | ✅ verified live (exceeded) |
| Core *applies* those ops over the real wire (file written / learning row transitions) | ✅ **verified live** (Story 8.0, full-stack — below) |
| Deployment (real fork worker, Pi, E-Ink, hardware) | ⏳ later (the only remaining gap) |

## Live confirmation that `facts/` surfacing works (Epic 6 action #2 — already done)

The live dream promoted a specific owner-fact via **`remember` → `facts/`** (`shipping-habit`). **`facts/` (and `people/`) IS injected into prompts** — `worker/prompt.py` `gather_context` reads both collections into a bounded "# What you know" section right after `about.md` (commit `f930099`, "surface facts/ + people/ into the prompt (Epic 6 retro #2)"). So that promotion is **not** write-only — a later turn's prompt will carry it. CAP-11 works through the natural path the live model chose, AND via the `rewrite_about`/`rewrite_summary` it also used. (Correction: an earlier draft of this doc — and the Epic 6/7 retro action lists — called `facts/` surfacing an open gap; it was already implemented in `f930099` before Epic 7 started.)

## Full-stack run (Story 8.0) — ✅ GREEN (2026-06-20, glm-4.7)

The full-stack tests (`tests/test_full_stack_live_smoke.py`) drive the *whole* wire —
core → worker (`run_worker`, real assembly) → broker → GLM → `Result` → core **applies** the
ops — and assert the observable end state, not just elicitation:

- `test_full_stack_live_turn_applies_a_memory_op` — a `remember` turn ⇒ a file lands under
  the tmp `facts/` tree (core applied it), reply non-degraded.
- `test_full_stack_live_dream_applies_resolve_learning` — 3 seeded pending learnings + the
  real dream directive ⇒ at least one learning row transitions `pending`→`promoted`/`pruned`
  in sqlite (core applied the `resolve_learning`).

**Run it (paid, your key):**
```
set -a; . ./.env; set +a
export ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic GLM_MODEL=glm-4.7
uv run pytest -m live -s -k full_stack
```

**Result (`uv run pytest -m live -s -k full_stack` → 2 passed in 7.38s):**

| Test | Result | Model | Observed (applied `facts/` file / learning transition) | Gap |
|---|---|---|---|---|
| full_stack_live_turn | ✅ PASS | glm-4.7 | reply "BigQuery, got it! Saved to memory." · **core wrote `facts/favorite-db.md` = "BigQuery"** | none |
| full_stack_live_dream | ✅ PASS | glm-4.7 | reply "All tidy!…" · **pending learnings 3 → 0** (all `resolve_learning` applied in sqlite) | none |

**Green pair → the apply path is verified end-to-end against a live brain. The dominant project
risk (mechanism-proven, never live-LLM-tested) is RETIRED up to deployment.** Core admits the
turn, the worker assembles the real prompt, the broker runs the live GLM chain, the `Result`
returns over the bus, and **core decodes + applies the ops** — a `facts/` file is written, a
learning row transitions. Not just "the prompt elicits the op" (elicitation, above) but "core
acts on what the live model emits."

## Net

**The dominant risk is RETIRED (up to deployment).** Both layers are live-verified on `glm-4.7`:
elicitation (the prompt elicits decodable ops, with sound promote/prune judgment) AND full-stack
apply (core writes the `facts/` file / transitions the learning row from what the live model
emits, over the real wire). The whole memory/learning loop — built since Epic 4, mechanism-proven,
never field-proven — now runs correctly against a real brain end-to-end. The only remaining
unknown is **deployment** (real fork worker, Pi, E-Ink, hardware), which is its own later story.
