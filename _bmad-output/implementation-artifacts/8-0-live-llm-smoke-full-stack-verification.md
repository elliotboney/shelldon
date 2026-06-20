---
baseline_commit: 498a0a0
---
# Story 8.0: Live-LLM smoke — full-stack verification against a real brain

Status: ready-for-dev

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

- [ ] **Task 1 — Full-stack live turn test** (AC1, AC4)
  - [ ] New `tests/test_full_stack_live_smoke.py` (or extend `test_turn_dream_live_smoke.py`): `pytest.mark.live` + `skipif(not GLM key)`. Build the 1.8 harness with `chain=build_chain(os.environ)` + `Spawns(worker=run_worker)` (reuse `build_harness`/`Harness` from `test_end_to_end_turn.py`); long `turn_timeout` for real latency.
  - [ ] Feed a memory-inviting owner message; assert reply out + non-degraded face + a `remember` APPLIED (curated `facts/` file exists, redirected to tmp via the conftest autouse). Print reply + ops.
  - [ ] verify: skips cleanly with no key; default `uv run pytest -q` unaffected (still 537/network-free).
- [ ] **Task 2 — Full-stack live dream test** (AC2)
  - [ ] Seed 3 pending learnings via `core.history.capture_learning`; drive `_build_dream_prompt` through the live wire; assert a seeded learning's sqlite status transitioned (`pending`→`promoted`/`pruned`). Print directive + reply + applied ops; observe (don't gate) `rewrite_summary`.
  - [ ] verify: skips without key; on a green run the learning row actually changed state.
- [ ] **Task 3 — Run + capture findings** (AC3)
  - [ ] Owner runs (paid, manual): the 2 new full-stack tests + the 2 existing elicitation smokes + the provider smoke, with `.env` loaded + `GLM_MODEL` set.
  - [ ] Fill `live-smoke-findings-{date}.md`: per run — model, reply, ops emitted vs applied, gaps. Green → "verified"; gaps → follow-on action items.
- [ ] **Task 4 — Boundary gate**
  - [ ] verify: `uv run pytest -q` 537 green (live deselected); `uv run lint-imports` 3 KEPT; `uv sync --locked` 0 new deps; `git status -- shelldon/core/` empty (no product change unless a finding forces a separately-scoped fix).

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

### Debug Log References

### Completion Notes List

### File List

### Review Findings

### Change Log

- 2026-06-19 — Story 8.0 created (retro-born; Epic 6 action #1, re-affirmed binding by the Epic 7 retro). Full-stack live-LLM verification: a real owner turn + a real dream driven through the actual core→worker→broker→GLM→Result→apply wire (reusing the 1.8 `build_harness` with `chain=build_chain(os.environ)`), asserting core APPLIES the ops (a `facts/` file / a learning-row transition) — beyond the existing elicitation-only smokes — plus a committed findings doc. Opt-in `-m live`, network-gated, out of CI; 0 new deps; no `core/` change. Status → ready-for-dev.
