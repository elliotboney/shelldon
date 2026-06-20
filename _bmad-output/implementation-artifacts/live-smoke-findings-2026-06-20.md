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
| Core *applies* those ops over the real wire (file written / learning row transitions) | ⏳ **Story 8.0** (full-stack) |
| Deployment (real fork worker, Pi, E-Ink, hardware) | ⏳ later |

## Finding worth acting on — reinforces Epic 6 action #2 (`facts/` surfacing)

The live dream's instinct is to promote a specific owner-fact via **`remember` → `facts/`** (`shipping-habit`). But **4.4 prompt assembly does not inject `facts/`/`people/` into prompts** — so that promotion is durable-but-write-only and would never shape a future reply. The model leans on `facts/` *more* than the Epic 6 workaround assumed (the dream was steered toward `rewrite_about` precisely because `about.md` IS injected). This is real live evidence that **`facts/` surfacing (Epic 6 action #2) matters** — without it, the dream's most natural promotions are invisible. CAP-11 currently works only via the `rewrite_about`/`rewrite_summary` path the model also (helpfully) used.

## Net

Mechanism-proven everywhere + **elicitation now live-verified on `glm-4.7`** (turn + dream, clean). The dominant risk is materially down. Remaining: full-stack apply (8.0) and deploy. Plus a sharpened, live-evidenced case for `facts/` surfacing.
