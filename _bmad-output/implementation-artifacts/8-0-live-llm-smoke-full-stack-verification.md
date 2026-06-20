---
baseline_commit: 498a0a0
---
# Story 8.0: Live-LLM smoke — full-stack verification against a real brain

Status: done

<!-- Retro-born (Epic 6 action #1, re-affirmed as THE binding next move by the Epic 7 retro 2026-06-19). The dominant project risk: the whole memory/learning/autonomy line is mechanism-proven but NEVER run against a live LLM. NOT in any epics.md — a verification story, like 5.0/7.0/7.5 were born outside the plan. Proposed as the first story of a "Verify & Deploy" phase (Epic 8) — owner may rename/re-slot. -->
<!-- KEY DISCOVERY (do not rebuild): the ELICITATION smoke already exists — tests/test_turn_dream_live_smoke.py (test_live_turn_elicits_a_memory_op + test_live_dream_emits_resolve_and_summary) and a provider smoke (test_provider_live_smoke.py), both `-m live`, network-gated, NEVER RUN. The GLM-via-Z.ai chain is wired (broker/chain.py _glm, default glm-4.7, GLM_MODEL override). This story adds the FULL-STACK layer those tests bypass + captures findings. -->

## Story

As the owner,
I want to run shelldon end-to-end against a real LLM (GLM via Z.ai) — a real owner turn AND a real dream — through the actual core→worker→broker→provider→Result→apply wire, and capture what the model actually does,
so that the project's dominant risk (the whole brain is mechanism-proven but never field-proven) is finally retired or its real gaps are documented.

**Why this story exists (the dominant risk, two epics overdue):** every behavior built since Epic 4 — turns emitting memory-ops, the dream classifying learnings, plugin events nudging mood — is verified only against *synthetic* `Result`s. Whether a real model, given the *actual* `SYSTEM_INSTRUCTION` + assembled prompt + dream directive, emits **decodable ops that core then applies**, is untested. The Epic 6 retro made this action #1; Epic 7 was built instead; the Epic 7 retro re-affirmed it as the binding next move before any further extend or deploy.

**What already exists (DO NOT rebuild):** the *elicitation* layer is covered — `tests/test_turn_dream_live_smoke.py` calls `provider.complete(assemble_prompt(...))` directly and asserts the model emits a `remember` (turn) / `resolve_learning` (dream) that `parse_reply` decodes. Those prove *the prompt elicits the op*. They do **not** exercise the real wire: the worker fork assembling the prompt, the broker injecting creds + running the chain, the `Result` returning over the bus, and **core actually applying the op** (writing the memory file / transitioning the learning row) + pushing a face. This story adds that full-stack layer and a captured findings record.

## Acceptance Criteria

### AC1 — A full-stack live turn applies a real memory-op end-to-end

**Given** the real provider chain (`build_chain(os.environ)` → GLM via Z.ai) and the real worker (`run_worker`, real `assemble_prompt`) on the Story 1.8 in-process harness — `pytest -m live`, **skipped without `GLM_API_KEY`/`ANTHROPIC_API_KEY`**, creds resolved ONLY from the broker env (AD-2)
**When** a real owner message that strongly invites a fact-memory is fed in (e.g. "remember my favorite database is BigQuery")
**Then** the full wire runs — core admits the turn → worker assembles the prompt + calls the broker → broker runs the GLM chain → `Result` returns → core applies the proposed ops — and the test asserts the **observable end state**, not just elicitation: a reply reached the outbound sink, a non-degraded face was pushed to the display, **and core actually applied a `remember`** (a file appeared under the curated `facts/` tree OR the applied op is observed on the bus `Result`)
**And** the test prints the reply + the parsed/applied ops so the run is inspectable; an empty-ops or degrade outcome is a logged **FINDING** (the prompt/wire didn't elicit/apply the behavior), not a silent pass.

### AC2 — A full-stack live dream applies a real `resolve_learning` end-to-end

**Given** pending learnings seeded into the real `history.db` (3.1/6.1) and the **real** dream directive (`_build_dream_prompt`, learnings baked by id) driven through the same live wire
**When** the dream turn runs against GLM
**Then** core applies at least one `resolve_learning` — the test asserts the **soft status transition actually landed in sqlite** (a seeded `pending` learning is now `promoted`/`pruned`), not merely that the op decoded; `rewrite_summary`/`rewrite_about` promotion is printed + observed but **not gated** (AC3-nicety, the model may skip it)
**And** the directive + reply + applied ops are printed; no `resolve_learning` applied is a logged **FINDING** (the single most-unverified behavior in the project).

### AC3 — Findings are captured in a committed record

**Given** the runs above + the pre-existing elicitation smokes (`test_turn_dream_live_smoke.py`) + the provider smoke (`test_provider_live_smoke.py`)
**When** the owner runs them with real creds
**Then** a committed `_bmad-output/implementation-artifacts/live-smoke-findings-{date}.md` records, per run: the model (`GLM_MODEL`), the reply text, the ops emitted vs ops applied, and **every gap surfaced** (prompt didn't elicit, op didn't decode, op didn't apply, directive needs tightening). A green run is recorded as "verified"; a red/partial run's gaps become follow-on action items (this is the retro's literal ask: *surface the gaps*).

### AC4 — The live lane stays opt-in, paid, and out of CI

**Given** these are real, paid, non-deterministic network calls
**When** the suite runs normally (`uv run pytest -q`)
**Then** the new full-stack live tests are `pytest.mark.live` + `skipif` on the key (like the existing smokes) — the default suite stays **537 green / network-free**; the live lane runs only on `-m live` with the broker env loaded
**And** `uv sync --locked` 0 new deps (the `openai`/`anthropic` SDKs + the GLM chain already exist); import-linter 3 contracts KEPT; `core/` byte-unchanged (this is a test + the broker env, no product change) — UNLESS a finding requires a prompt/directive fix, which is then its own scoped change, not smuggled in here.

### Out of scope (explicit)

- **Rebuilding the elicitation smokes or the GLM provider/chain** — both already exist; this story consumes them.
- **A real `os.fork()` worker / Pi hardware run** — the in-process spawn seam (`Spawns(worker=run_worker)`) exercises the real prompt + provider + apply without the Linux-gated fork; real-fork + E-Ink + PiSugar is a later deployment story.
- **Fixing whatever the smoke surfaces** — a prompt/directive that under-elicits becomes a *follow-on* action item (AC3), not in-scope here. The deliverable is the verified run + the findings, not a prompt-tuning loop.
- **Deployment** — this gates deploy; it is not deploy.

## Tasks / Subtasks

- [x] **Task 1 — Full-stack live turn test** (AC1, AC4)
  - [x] New `tests/test_full_stack_live_smoke.py`: `pytestmark = pytest.mark.live` + `skipif(not _GLM_KEY)`. Builds the 1.8 harness with `chain=build_chain(os.environ)` + `Spawns(worker=run_worker)` (imports `build_harness`/`Spawns`/`_await` from `test_end_to_end_turn.py`, same as `test_endurance_soak`); `turn_timeout=60s` for real latency.
  - [x] `test_full_stack_live_turn_applies_a_memory_op`: feeds a memory-inviting owner message; asserts reply out + non-degraded face (`FACE_DEGRADED not in renderer.rendered`) + a `remember` APPLIED (a `facts/` `.md` file under the conftest-redirected tmp tree). Prints reply + applied facts/ contents.
  - [x] verify: collects + **skips cleanly with no key**; default `uv run pytest -q` unaffected (537 pass, now 7 deselected).
- [x] **Task 2 — Full-stack live dream test** (AC2)
  - [x] `test_full_stack_live_dream_applies_resolve_learning`: seeds 3 pending learnings via `core.history.capture_learning`; drives the REAL `core._build_dream_prompt()` directive through the live wire; asserts `len(pending_learnings())` dropped (a seeded learning transitioned `pending`→`promoted`/`pruned` in sqlite). Prints directive + reply + before/after pending. `rewrite_summary` observed, not gated.
  - [x] verify: skips without key; collects clean.
- [x] **Task 3 — Run + capture findings** (AC3)
  - [x] **OWNER ran** `uv run pytest -m live -s -k full_stack` (glm-4.7) → **2 passed in 7.38s**. Turn: reply + `facts/favorite-db.md` written = "BigQuery". Dream: reply + pending learnings 3→0 (all `resolve_learning` applied). Both GREEN — no gaps.
  - [x] `live-smoke-findings-2026-06-20.md` results table filled; elicitation + full-stack both recorded GREEN; "what remains" updated (apply path now ✅, only deployment left).
- [x] **Task 4 — Boundary gate**
  - [x] verify: `uv run pytest -q` → **537 pass / 3 skip / 7 deselected** (the 2 new live tests deselected); `uv run lint-imports` → **3 KEPT**; `uv sync --locked` → **0 dep changes**; `git status -- shelldon/core/` → **empty** (test-only, no product change).

## Dev Notes

### The owner runs the paid calls — the dev builds the scaffold

The live network calls cost real tokens and use the owner's Z.ai key; **the dev writes the test scaffold + the findings-doc template, the OWNER executes the `-m live` run** and the findings doc is filled from that run. The dev can write everything and self-skip (no key) up to the point of the real run.

### What's already built (consume, don't rebuild)

- **Elicitation smokes** — `tests/test_turn_dream_live_smoke.py`: `test_live_turn_elicits_a_memory_op` (real `complete(assemble_prompt(...))` → expects a `Remember`), `test_live_dream_emits_resolve_and_summary` (real `_build_dream_prompt` → expects a `ResolveLearning`). Both `pytestmark = pytest.mark.live` + `skipif(not _GLM_KEY)`. These hit the provider DIRECTLY — they do NOT run the core/worker/broker wire or APPLY the op. That's the gap this story fills.
- **GLM chain** — `shelldon/broker/chain.py` `_glm(env)`: `AnthropicProvider(api_key=GLM_API_KEY|ANTHROPIC_API_KEY, base_url=GLM_BASE_URL|ANTHROPIC_BASE_URL|https://api.z.ai/api/anthropic, model=GLM_MODEL|ANTHROPIC_MODEL|"glm-4.7")`. `PROVIDER_CHAIN` defaults to `"glm"`. **Model note:** default is `glm-4.7`; the retro/owner referenced GLM-5.2 — set `GLM_MODEL` to whatever the Z.ai account actually serves (the test prints `provider._model`, so the findings doc records exactly what ran).
- **Full-stack harness** — `tests/test_end_to_end_turn.py`: `build_harness(sock_path, *, chain=..., spawns=..., turn_timeout=...)` already accepts a real `chain=` (mutually exclusive with the fake `provider=`); `Spawns(worker=run_worker)` runs the REAL prompt assembly (the default `_passthrough_worker` uses identity assembly — use `run_worker` to exercise `SYSTEM_INSTRUCTION` + `assemble_prompt`). `Harness.teardown()` cancels cleanly. Park the scheduler (`scheduler_interval=3600`) so no background job perturbs the run.

### Verified seams (line refs)

- `shelldon/broker/chain.py:26` (`_glm`), `:100` (`build_chain`) — the live chain; `PROVIDER_CHAIN` default `"glm"`.
- `tests/test_end_to_end_turn.py:191` (`build_harness`, `chain=`), `:139` (`Spawns`, `worker=run_worker`), `:165` (`Harness`) — the full-stack wire to reuse.
- `tests/test_turn_dream_live_smoke.py` — the elicitation smokes to run alongside (and the `_glm_provider()`/print pattern to mirror).
- `shelldon/worker/worker.py` `parse_reply` + `run_worker`; `shelldon/worker/prompt.py` `SYSTEM_INSTRUCTION`/`assemble_prompt` — the real assembly + op-parse the wire exercises.
- `shelldon/core/runtime.py` `_apply_proposed_ops` (`:~650`) — where core applies the `remember`/`resolve_learning` the test then observes (a `facts/` file / a learning-row transition). Memory root + history are conftest-redirected to tmp (`_isolate_state_checkpoint`).
- `shelldon/core/history.py` `capture_learning`/`resolve_learning` — seed pending learnings (AC2) + assert the soft status transition landed.

### Testing standards summary

- Default lane: `uv run pytest -q` must stay **537 green, network-free** — the live tests are `-m live` + `skipif` (deselected by default, like the existing 5 deselected). Live lane: `set -a; . ./.env; set +a; export GLM_MODEL=...; uv run pytest -m live -s`.
- Success = AC1–AC4: a real turn applies a real memory-op + a real dream applies a real `resolve_learning` (or the gaps are documented in the findings doc); default suite unaffected; 0 new deps; contracts KEPT; no `core/` change.

### Open questions for the owner (do not block dev — defaults chosen)

1. **Model:** `glm-4.7` is the chain default; the retro said GLM-5.2. Which `GLM_MODEL` does your Z.ai account serve? (The test prints + the findings doc records whatever ran — set it before the live run.)
2. **Findings doc as a gate:** is a documented run with logged gaps enough to mark this `done` (recommended — the retro asked to *surface* gaps, not to achieve a perfect green), or must both full-stack tests be green to close it?
3. **Epic slotting:** keyed `8-0` as the first story of a proposed "Epic 8: Verify & Deploy" — rename/re-slot if you'd rather it be a standalone verification task.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- `uv run pytest -m live -k full_stack --collect-only` → 2 collected (imports resolve: `build_harness`/`Spawns`/`_await` from `test_end_to_end_turn`, `build_chain`, `run_worker`).
- `uv run pytest -m live -k full_stack` (no key in env) → **2 skipped** (skipif fires; no network, no cost).
- `uv run pytest -q` → **537 passed, 3 skipped, 7 deselected** (was 5 deselected; +2 = the new live tests, correctly deselected by `addopts = -m 'not live'`).
- `uv run lint-imports` → 3 KEPT / 0 broken · `uv sync --locked` → 0 dep changes · `git status -- shelldon/core/` → empty.

### Completion Notes List

- **No product code, no TDD red/green cycle — by design.** 8.0 adds only a test scaffold that rides already-verified infrastructure (`build_harness`, the GLM chain, `_apply_proposed_ops`, the learnings table — all tested elsewhere). There is nothing to implement-then-make-pass; the deliverable IS the live test + the findings record. Correctness is verified by (a) clean collection + skip without a key, (b) the default suite staying green, and (c) the owner's `-m live` run.
- **The full-stack tests assert the APPLY path, not just elicitation.** The turn test asserts a `facts/` `.md` file actually lands under the (tmp-redirected) curated tree — i.e. core decoded the model's `remember` and wrote it. The dream test seeds 3 pending learnings, feeds the REAL `_build_dream_prompt()` directive through the wire, and asserts `pending_learnings()` shrank — i.e. core applied a `resolve_learning` soft transition in sqlite. Both print the reply + the observable end state so a red run is a legible FINDING, not a silent pass.
- **Reuses the 1.8 harness verbatim** — `Spawns(worker=run_worker)` runs the REAL `assemble_prompt` (not the identity `_passthrough_worker`), so the live model sees the real `SYSTEM_INSTRUCTION` + memory-shaped prompt; `chain=build_chain(os.environ)` is the real GLM/Z.ai chain; the conftest autouse redirects memory/history to `tmp_path` so the asserted artifacts never touch real `$HOME`.
- **⏳ OWNER ACTION REMAINING (the actual verification):** the dev cannot run a paid network call. The one step between this and a closed story is the owner running `uv run pytest -m live -s -k full_stack` (`.env` loaded, `GLM_MODEL` set) and recording the two rows in `live-smoke-findings-2026-06-20.md`. A green pair retires the dominant risk up to deployment; a red/partial run's gap is logged + scoped as a follow-on (the retro's ask). **Status is `review` for the scaffold; flip to `done` after that run.**
- Elicitation half already GREEN (2026-06-20, glm-4.7): a real turn emits a decodable `remember`; the real dream directive emits the full vocabulary with correct promote/prune judgment. The full-stack run extends that from "the prompt elicits the op" to "core applies the op end-to-end."

### File List

- `tests/test_full_stack_live_smoke.py` — NEW. 2 `-m live` full-stack tests (turn→`facts/` file applied; dream→learning-row transition), gated on a GLM key, built on the 1.8 harness with the real chain + `run_worker`.
- `_bmad-output/implementation-artifacts/live-smoke-findings-2026-06-20.md` — MODIFIED. Added the "Full-stack run (Story 8.0)" section + a results table to fill from the owner's run (the elicitation half was already recorded green).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `8-0 → in-progress → review`.

### Review Findings

- [x] [Review][Patch] Face-degraded assertion is a no-op — `FACE_DEGRADED not in h.renderer.rendered` compares a string to `list[StateSnapshot]`, always True, never catches a degraded face [`tests/test_full_stack_live_smoke.py:74`] — **FIXED 2026-06-20**: `not any(s.face == FACE_DEGRADED for s in h.renderer.rendered)` (mirrors `test_end_to_end_turn.py:317`). Still collects+skips clean; suite 537 green.
- [x] [Review][Defer] `_now()` hardcoded to 2026-06-20 [`tests/test_full_stack_live_smoke.py:47–48`] — deferred, pre-existing; doesn't affect test correctness (timestamps are metadata, `pending_learnings()` doesn't filter by date), but use `datetime.now(UTC)` if the test is long-lived
- [x] [Review][Defer] Timeout expiry yields opaque `AssertionError` from `_await`, not a labeled `(FINDING)` message [`tests/test_full_stack_live_smoke.py:65,103`] — deferred, pre-existing `_await` contract; a guard `assert h.outbound, "no reply — chain never responded"` before `h.outbound[0]` would improve failure legibility

### Change Log

- 2026-06-20 — **DONE: owner ran the full-stack live smoke (glm-4.7) → 2 passed.** Turn applied a `remember` (core wrote `facts/favorite-db.md`); dream applied 3 `resolve_learning` ops (pending 3→0 in sqlite). Both GREEN, no gaps. The apply path is verified end-to-end against a live brain — **the dominant project risk (mechanism-proven, never live-LLM-tested) is RETIRED up to deployment.** Findings doc filled. Status → done.
- 2026-06-20 — Review patch applied: the face-degraded assertion was a no-op (`str not in list[StateSnapshot]` is always True) → fixed to `not any(s.face == FACE_DEGRADED for s in h.renderer.rendered)`. 2 review items accepted as deferred (`_now()` hardcoded date; opaque `_await` timeout message — both pre-existing, non-correctness). Still collects+skips clean; suite 537 green. Owner run still gates `done`.
- 2026-06-20 — Story 8.0 implemented (scaffold): `tests/test_full_stack_live_smoke.py` — 2 opt-in `-m live` full-stack tests driving a real owner turn + a real dream through the WHOLE wire (core→worker[`run_worker`]→broker→GLM→Result→`_apply_proposed_ops`) and asserting the APPLY end state (a `facts/` file written / a learning row transitioned), built on the 1.8 `build_harness(chain=build_chain(os.environ))`. Collects + skips cleanly without a key; default suite 537 green (7 deselected); 0 new deps; 3 contracts KEPT; no `core/` change. Findings doc extended with a full-stack section to fill from the owner's run. The paid `-m live` execution + findings-fill is the one remaining OWNER action before `done`. Status → review.
- 2026-06-19 — Story 8.0 created (retro-born; Epic 6 action #1, re-affirmed binding by the Epic 7 retro). Full-stack live-LLM verification: a real owner turn + a real dream driven through the actual core→worker→broker→GLM→Result→apply wire (reusing the 1.8 `build_harness` with `chain=build_chain(os.environ)`), asserting core APPLIES the ops (a `facts/` file / a learning-row transition) — beyond the existing elicitation-only smokes — plus a committed findings doc. Opt-in `-m live`, network-gated, out of CI; 0 new deps; no `core/` change. Status → ready-for-dev.
