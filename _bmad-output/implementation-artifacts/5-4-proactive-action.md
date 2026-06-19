---
baseline_commit: 8a5610370620f6fae946b33c6a2fc7179829d266
---

# Story 5.4: Proactive action

Status: review

<!-- Final feature story of Epic 5. Builds on 5.1 (scheduler + Idle cadence), 5.2 (turn-dispatch budget/cooldown gate), 5.3 (battery gate). The capstone: the pet acts with no owner input. -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to reach out on its own sometimes,
so that it feels like a companion with initiative, not just a responder.

**Why now / what it unblocks:** This is the **capstone of Epic 5** — the first turn the pet initiates with **no preceding owner message** (CAP-4). Every guardrail it needs already exists: 5.1 built the scheduler + the `Idle` cadence (fires once after N seconds of owner silence — exactly a "greeting opportunity / mood-driven idle"), 5.2 made a due turn job **cooldown- + daily-budget-gated**, and 5.3 made it **battery-gated**. So 5.4 does **not** re-build any gating — it **registers the first real `turn` job** (the proactive musing) and wires the trigger signal the scheduler was missing. The two ACs ("within cooldown and budget → initiates" / "cooldown or budget not satisfied → does not initiate") are **already enforced** by the 5.2/5.3 dispatch path the job rides; 5.4's new work is the **trigger + the proactive prompt + the no-owner-input plumbing**.

**What's genuinely new here (read first).** (1) The scheduler's `tick()` is currently called with **no `last_interaction`** (the 5.3-review-noted 5.4 deferral), so the `Idle` cadence can never fire — 5.4 **wires the idle signal** from `state.last_interaction` into the tick. (2) A proactive turn has **no owner message**, so the job's prompt is **built at dispatch from live personality state** (mood-shaped — owner decision) rather than a static string — the `Job` model gains a **prompt builder** seam. (3) The turn must record sensibly in history with **no real owner utterance** — recorded with a synthetic owner-side marker so continuity holds without a self-directive masquerading as the owner.

## Acceptance Criteria

### AC1 — A proactive trigger within cooldown + budget initiates a turn with no owner input (CAP-4)

**Given** personality state and the idle signal (the "environment")
**When** a proactive trigger fires (owner idle past the threshold — a greeting/musing opportunity) **and** the cooldown, daily budget, and battery state all allow it
**Then** the pet **initiates a turn with no preceding owner message** — a worker turn spawns, its reply leaves as an `OUTBOUND_MSG`, and the face reacts — exactly the normal turn lifecycle, just self-initiated (CAP-4 success).

- The trigger is an **`Idle`-cadence `turn`-tier `Job`** (reusing 5.1's `Idle`, which fires once per idle stretch after `proactive_idle_interval` seconds since `state.last_interaction`), registered in `Core.__init__` alongside the reflex/checkpoint jobs (the composition precedent — a general plugin job-registration API is Epic 7).
- The job's prompt is **built at dispatch from live personality state** (owner decision: mood shapes the prompt). The proactive directive is **open-ended — the pet shares whatever's on its mind (a passing thought, an observation, or a hello), NOT necessarily a question/reach-out** (owner clarification). The directive carries a **feeling word derived from current mood/energy via the existing `faces.select(...)`** (reuse the face vocabulary as the single mood-label source — no second, divergent mood classifier).
- Admission rides the **unchanged 5.2/5.3 path**: the scheduler applies the 5.3 battery gate (skip on LOW / non-essential-on-EASED), then `_dispatch_turn_job` applies the 5.2 arbiter-idle + cooldown + daily-budget gate. The proactive job is `essential=False` (skipped first under battery backoff). No new gate is written.

### AC2 — When cooldown or budget is not satisfied, the pet does not initiate (reflexes carry the in-between)

**Given** the proactive cooldown has not elapsed, OR the daily turn budget is exhausted, OR a turn is already in flight, OR the pet is backed off on battery
**When** the proactive trigger fires
**Then** the pet **does not initiate a turn** — the job is deferred/skipped by the existing 5.2/5.3 gates — and the **reflex jobs (mood drift, the mood-face push) continue unaffected**, carrying the pet's between-turn aliveness.

- This AC is satisfied **for free** by the 5.2/5.3 dispatch path (a deferred/skipped turn job spawns nothing, spends nothing). 5.4's obligation is only to **not bypass** that path — the proactive job goes through `_dispatch_turn_job`, never a direct spawn (AD-14: the scheduler never forks directly).

### AC3 — The proactive turn records with no real owner utterance (continuity without pollution)

**Given** a completed proactive turn (the pet spoke unprompted)
**When** it is recorded to conversation history (AD-6)
**Then** it is stored as the **pet's reply paired with a synthetic owner-side marker** (e.g. `"(shelldon spoke up on its own)"`) — so the next turn's recent-window knows the pet reached out, **without** the self-directive being recorded as if the owner had typed it. The worker runs the real proactive directive; history records the marker.

### Out of scope (explicit — later stories / runtime)

- **A worker-side proactive prompt section** in `worker/prompt.py` — the proactive directive rides the **existing `owner_message` slot** ("# Owner says now"), framed parenthetically as a self-prompt. A dedicated proactive section/headers in the assembler is a noted future refinement, **not** this story (no `worker/prompt.py` change).
- **Mood-based suppression/gating** (a grumpy/tired pet staying silent) — owner chose "mood shapes the **prompt**," not "shapes prompt **and** gates." 5.4 shapes the directive by mood; it does **not** add a mood predicate that blocks the turn. (Battery/budget/cooldown remain the only gates.)
- **A configurable prompt TEMPLATE** — the directive text lives in one pure policy function (easy to tune); only the **idle threshold** is exposed as an injectable `Core` param (owner: "configurable"). A fully data-driven prompt template is a future refinement.
- **Proactive trigger types beyond idle** (greeting-on-boot, event/context-pressure triggers, mood-threshold triggers) — 5.4 ships the **idle musing**; other trigger cadences are added later as additional registered jobs (the scheduler already supports interval/daily/idle).
- **Dreaming / learning-consolidation turn content** (AD-15) — Epic 6. 5.4 proves the self-initiated-turn mechanism; the dream job reuses the same `prompt_builder` + dispatch seam later.
- **Multiple/competing proactive jobs**, per-job proactive policy — one idle musing job now.

## Tasks / Subtasks

- [x] **Task 1 — The proactive prompt policy (`core/proactive.py`, new) (AC1)**
  - [x] `build_proactive_prompt(feeling: str | None) -> str`: PURE (no I/O, no clock — mirrors `reflexes.py`/`budget.py`/`power.py`). Returns the open-ended proactive directive, parenthetically framed as a self-prompt with no owner message to reply to, and woven with the `feeling` word when present (a `None`/empty feeling degrades to a feeling-agnostic directive — never raises, never interpolates "None"). Framing is **share-a-thought**, not a forced question (owner clarification).
  - [x] Keep the directive copy minimal and in ONE place (the single tunable point); LLM-free (AD-1): imports only stdlib. Import-linter stays **KEPT**.
- [x] **Task 2 — `Job` gains a prompt-builder + a history-owner marker seam (`core/scheduler.py`) (AC1, AC3)**
  - [x] `Job` gains `prompt_builder: Callable[[], str] | None = None` (keyword-only) — a no-arg callable resolved **at dispatch** to a live prompt (a proactive job builds from current state; the existing static `prompt` is used when no builder is set). A job may carry a builder **or** a static `prompt`, not both required; reflex jobs carry neither.
  - [x] `Job` gains `history_owner_text: str | None = None` (keyword-only) — the owner-side text to record for a turn that has no real owner message (AC3). `None` ⇒ record the resolved prompt (the existing owner-turn behavior is unchanged).
  - [x] No change to the cadence/cost-tier/essential model; these are additive optional fields (the 5.2 `cost` / 5.3 `essential` precedent of building the mechanism on the carrier).
- [x] **Task 3 — Resolve the builder + record-marker in dispatch (`core/runtime.py`) (AC1, AC3)**
  - [x] `_dispatch_turn_job`: resolve the prompt as `job.prompt_builder()` (guarded — a builder that raises logs + skips, never wedges the slot) when a builder is set, else `job.prompt`; a `None`/empty resolved prompt skips (the existing promptless guard). Everything else (is_idle → cooldown/budget `evaluate` → admit → spend → `_start_turn`) is **unchanged**.
  - [x] `_start_turn(prompt, *, record_owner_text=None)`: stash `self._current_prompt = record_owner_text if record_owner_text is not None else prompt` (history pairing only — the worker still gets the real `prompt` via `spawn_turn`). All existing callers (owner turn, folded catch-up, timeout) pass no `record_owner_text` and are behavior-identical. Dispatch passes `record_owner_text=job.history_owner_text`.
- [x] **Task 4 — Wire the idle signal + register the proactive job (`core/runtime.py`) (AC1, AC2)**
  - [x] `_scheduler_loop`: pass the parsed idle signal into the tick — `await self.scheduler.tick(last_interaction=self._last_interaction_dt())`. Add `_last_interaction_dt()` mirroring `reflexes._idle_seconds`' defensive parse (`datetime.fromisoformat(state.last_interaction)`; `None`/unparseable/tz-naive → `None`, warned, never raised). This closes the 5.3-review-noted gap so the `Idle` cadence can fire.
  - [x] Add `DEFAULT_PROACTIVE_IDLE_INTERVAL = 3600.0` (1 hr; owner: injectable/configurable) + `proactive_idle_interval` `Core.__init__` param (validate positive, the `reflex_interval` precedent). Add `PROACTIVE_OWNER_MARKER = "(shelldon spoke up on its own)"`.
  - [x] Register the proactive job in `Core.__init__` (after reflex/checkpoint): `Job("proactive", Idle(self.proactive_idle_interval), CostTier.TURN, prompt_builder=self._build_proactive_prompt, history_owner_text=PROACTIVE_OWNER_MARKER)`. Add `_build_proactive_prompt(self) -> str`: `feeling = self.faces.select(mood.valence, mood.arousal, energy)`; `return build_proactive_prompt(feeling)`.
- [x] **Task 5 — Tests (AC1, AC2, AC3)**
  - [x] `tests/test_proactive.py` (new): pure `build_proactive_prompt` — contains the feeling word when given one; a feeling-agnostic but valid directive when `None`/empty (never the literal "None"); distinct feelings yield distinct text; the framing is open-ended (a smoke assertion it isn't hard-coded as a question only).
  - [x] Dispatch tests (extend `tests/test_turn_dispatch.py` or a new `tests/test_proactive.py` section): a `prompt_builder` job builds its prompt from live state and spawns it (the worker receives the built directive, NOT the marker); the turn records history with the **marker** as the owner side, not the directive (spy on `history.record_turn`); a `prompt_builder` that raises is skipped (no spawn, no spend, slot not wedged); `Job` round-trips `prompt_builder`/`history_owner_text`.
  - [x] Integration (new `tests/test_proactive.py` section — CAP-4 / AC1+AC2): a `Core` with a controllable clock/idle signal — owner idle past `proactive_idle_interval` ⇒ `scheduler.tick(last_interaction=...)` initiates a turn **with no INBOUND_MSG ever delivered** (CAP-4 proof: a spawn happened + budget spent with zero owner input); within cooldown / budget-exhausted / turn-in-flight ⇒ **no** initiation (AC2), and a reflex job still runs. Test `_last_interaction_dt()` parse (valid / None / garbage → None, never raises). Deterministic — injected clock + idle signal, no `asyncio.sleep` anchors.
- [x] **Task 6 — Soak + full-suite + contracts**
  - [x] Soak (`-m soak`) green/unchanged: the proactive job is now registered in every `Core`, but the soak **parks the scheduler** (`scheduler_interval` far out) so it never ticks in the measurement window — confirm `_seq`/`_bg`/heap unaffected and the background-emitter rule still holds (the proactive job is a resident turn job; it must be parked, like reflex/checkpoint). If any soak construction relies on "no turn job registered," update its expectation in **this same change** (the 5.1 background-emitter discipline).
  - [x] Full `pytest` green incl. soak; both import-linter contracts **KEPT** (`core/proactive.py` LLM-free); apply `dev-loop-checklist.md`.

## Dev Notes

**This story registers the first self-initiated turn — it does not re-implement gating or dreaming.** The hard part is the no-owner-input plumbing: a prompt built from live state, a history row with no real owner utterance, and the idle signal the scheduler was never given. The gates (cooldown/budget/battery) and the turn lifecycle are **reused verbatim**. Read 5.1's `Idle` cadence, 5.2's `_dispatch_turn_job`, and 5.4's three new seams before writing code.

### The seams being filled (read these first)

- [Source: `shelldon/core/scheduler.py`] `Idle` cadence (fires once per idle stretch after `period_s` since `last_interaction`; battery `scale` stretches it — 5.3); the `Job` model (already carries `cost`/`prompt`/`essential`). **5.4 adds `prompt_builder` + `history_owner_text` to `Job`** (optional, keyword-only — the additive-field precedent).
- [Source: `shelldon/core/runtime.py`] `_scheduler_loop` calls `self.scheduler.tick()` **with no `last_interaction`** — the comment literally says "Story 5.4's idle greeting job will pass it (the signal already lives in state.last_interaction)." **This is your wiring.** `_dispatch_turn_job` is the 5.2 gate — change ONLY the prompt resolution (builder vs static) + pass `record_owner_text`; do not touch the gate logic. `_start_turn(prompt)` opens the fence / pushes thinking-face / spawns / arms timeout — add the optional `record_owner_text` and reuse the rest verbatim. The reflex/checkpoint jobs are registered at the end of `__init__` — **register the proactive job right there** (same precedent).
- [Source: `shelldon/core/reflexes.py`] `_idle_seconds` — the defensive `last_interaction` parse to mirror for `_last_interaction_dt()` (`datetime.fromisoformat`; `None`/`ValueError`/`TypeError` → `None`, warned, never raised). The proactive idle threshold and the reflex idle-settle threshold are independent knobs.
- [Source: `shelldon/core/faces.py`] `FaceRegistry.select(valence, arousal, energy) -> str` returns the pet's current emotion token (the starter emotion set — content/sleepy/curious/grumpy/…). **Reuse it as the feeling word** for the proactive prompt — one mood-label source, no second classifier. `Core` already holds `self.faces` and calls `select(...)` in `_maybe_push_mood_face`.
- [Source: `shelldon/worker/prompt.py`] `assemble_prompt` puts the `owner_message` last under "# Owner says now". **No change here** — the proactive directive rides that slot, parenthetically framed as a self-prompt. (A dedicated proactive section is a noted future refinement, explicitly out of scope.)
- [Source: `shelldon/core/history.py` via `runtime._record_turn`] `_record_turn(pet_text)` records `(self._current_prompt, pet_text)`. By routing the **marker** into `self._current_prompt` (via `_start_turn(record_owner_text=...)`) while the **directive** goes to the worker (`spawn_turn`), the proactive turn records cleanly with no `_record_turn` change.

### Proactive design (keep it minimal — one idle musing job)

- **Trigger = the `Idle` cadence** (owner decision 1). It fires **once per idle stretch** (won't re-fire until a fresh owner interaction re-arms it — so the pet musts once when the owner goes quiet, then stays silent, not nagging). The battery `scale` stretches the idle threshold on battery automatically (5.3) — the pet waits longer to speak up when unplugged. The idle threshold is injectable (`proactive_idle_interval`, default 1 hr).
- **Prompt = built at dispatch from live mood** (owner decision 2). A `prompt_builder` closure on the `Job` reads current state when the turn is admitted (no `await` — the admit critical section stays atomic, the 5.2 no-lock invariant holds). Guard the builder (a raise → skip, like a promptless job). The directive is **open-ended sharing of a thought**, not a forced question (owner clarification), tinted by the `faces.select` feeling word.
- **History = marker, not directive** (owner decision 3 / AC3). The worker runs the directive; history records `PROACTIVE_OWNER_MARKER` as the owner side, so the recent-window shows the pet spoke up on its own — continuity without a fake owner utterance.
- **No new gating.** The two ACs are about cooldown/budget — already enforced by `_dispatch_turn_job` (5.2) and the battery gate (5.3). 5.4's only obligation is to route the proactive job **through** that path (never a direct spawn — AD-14) and not regress it.

### Architecture invariants (binding)

[Source: `_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md`]
- **AD-9** (112-115): "proactive turns (CAP-4) are gated by a **minimum-interval cooldown**" and the daily budget + battery backoff — "the arbiter is the single gate that admits or drops them." 5.4 adds the proactive job; the arbiter/budget/battery gate it (reused from 5.2/5.3).
- **AD-14** (137-140): "Scheduler-proposed turn jobs go through the **arbiter** … the scheduler never forks directly." The proactive job dispatches via `_dispatch_turn_job` → `_start_turn`, never a direct `spawn_turn`.
- **CAP-4** (map line 216): "arbiter (cooldown + credit/battery-gated proactive turns); scheduler proposes." 5.4 is the scheduler-proposes half made real.
- **AD-12**: the proactive turn carries a `turn_id` and is fenced exactly like an owner turn (reusing `_start_turn` gives this for free).
- **AD-6/AD-5**: history is core-written, single-writer; the marker rides the existing `_record_turn` path (no new writer).
- **AD-1**: `core/proactive.py` is LLM-free (import-linter KEPT).

### Testing standards

- `pytest`; **deterministic clock + idle-signal injection, never `asyncio.sleep` anchors** (Epic 2 retro #1; 5.1–5.3 rule). The CAP-4 proof: drive `scheduler.tick(last_interaction=<long-ago>)` on a `Core` with **no INBOUND_MSG ever delivered** and assert a spawn + a budget spend happened — initiative with zero owner input.
- New `tests/test_proactive.py` for the pure prompt policy + the dispatch builder/marker behavior + the CAP-4/AC2 integration; reuse the `_RecordingSpawner`/`_teardown` pattern from `test_turn_dispatch.py` / `test_battery_backoff.py`.
- **Apply `dev-loop-checklist.md`**: guard the `prompt_builder` call (raise → skip, tested); assert real values (the worker received the **directive**, history got the **marker** — a spy, not truthiness); exercise the rejection/skip branches (builder raises, cooldown/budget not satisfied, turn in flight); `_last_interaction_dt` parse tested for None/garbage; reject non-positive `proactive_idle_interval`; the proactive job is parked in the soak (resident turn job — the background-emitter rule); conftest isolation unchanged (no new write path — the marker rides the existing history store). Confirm no false-positive masking (the marker token isn't matched by an unrelated string in the spy).
- Run the **soak** (`-m soak`) locally — green + unchanged (scheduler parked; the proactive job never ticks in-window). If a soak assertion assumed "no turn job registered," update it here.

### Project Structure Notes

- New: `shelldon/core/proactive.py`, `tests/test_proactive.py`. Modified: `shelldon/core/scheduler.py` (`Job` gains `prompt_builder` + `history_owner_text`), `shelldon/core/runtime.py` (`_scheduler_loop` passes `last_interaction`; `_last_interaction_dt`; `proactive_idle_interval` param + default; `PROACTIVE_OWNER_MARKER`; register the proactive job; `_build_proactive_prompt`; `_dispatch_turn_job` prompt-resolution; `_start_turn` `record_owner_text`), `tests/test_turn_dispatch.py` (optional — builder/marker dispatch cases) or all dispatch cases in `tests/test_proactive.py`.
- **Unchanged on purpose:** `shelldon/core/budget.py`, `shelldon/core/power.py`, `shelldon/core/state.py`, `shelldon/worker/prompt.py`, `shelldon/app.py` (jobs register in `Core.__init__`, not the composition root). The 5.2 gate + 5.3 battery gate + the turn lifecycle are reused unchanged.
- LLM-free core (AD-1) stays **KEPT**. No new real-`$HOME` write path (the marker rides the existing history store).

### Previous-story intelligence (5.3 — done; 5.2 — done; 5.1 — done)

- **5.3 explicitly left the `last_interaction` wiring for 5.4** (review defer: "`_scheduler_loop` passes no `last_interaction` to `tick()` — acknowledged 5.4 deferral"). The `Idle` cadence + the battery `scale` are done and tested — 5.4 just feeds the signal in.
- **5.2 built `_dispatch_turn_job` and the persisted budget**; **5.3 made battery an OUTER gate over it.** 5.4 adds NO gate — it registers the job and lets both gates do their work. Reuse: build the mechanism on the carrier (`prompt_builder`/`history_owner_text` like `cost`/`essential`); guard best-effort (the builder); reject non-positive config; deterministic tests for every branch.
- **5.0 made the turn lifecycle wedge-proof** — the proactive turn rides the same hardened `_start_turn`/`_handle_result`/timeout path; do **not** add a separate lifecycle. A builder-skip or gate-defer starts no worker, so there's nothing to reap (release-safety unaffected).
- **5.1/5.3 review lesson (apply preemptively):** pin the production seam (the proactive job actually registered + actually fired through the real gates) with an explicit test; guard the scaffolding (the builder call), not just the inner turn; validate the new numeric input at construction (fail fast).

### Resolved decisions (owner, 2026-06-18 — binding)

1. **Trigger = an `Idle`-cadence `turn` job** (reuse 5.1's `Idle`); wire `state.last_interaction` into `scheduler.tick()` (closes the 5.3-noted gap). Idle fires once per idle stretch.
2. **Mood shapes the PROMPT** (not gating). A pure `build_proactive_prompt(feeling)` policy; `feeling = faces.select(mood)` — reuse the face emotion vocabulary as the single mood-label source. No mood-suppression gate (battery/budget/cooldown remain the only gates).
3. **Framing = open-ended "share a thought / observation / hello," NOT necessarily a question/reach-out** (owner clarification). The directive invites the pet to voice what's on its mind.
4. **History: record with a synthetic owner-side marker** (`PROACTIVE_OWNER_MARKER`) — the worker runs the directive, history stores the marker (continuity without a fake owner utterance).
5. **Idle threshold injectable** (`proactive_idle_interval`), **default 1 hr** — configurable per owner. (Cooldown 30 min + daily budget 12 from 5.2 still bound frequency.)

## References

- [Source: `_bmad-output/planning-artifacts/epics.md`#Story-5.4 (lines 609-623)] — the two ACs verbatim.
- [Source: ARCHITECTURE-SPINE.md] AD-9 (112-115, cooldown-gated proactive), AD-14 (137-140, scheduler proposes / arbiter gates / never forks directly), AD-12, AD-6, AD-5, AD-1; CAP-4 map (216).
- [Source: `shelldon/core/scheduler.py`] `Idle` cadence + the `Job` model 5.4 extends.
- [Source: `shelldon/core/runtime.py`] `_scheduler_loop` (the `last_interaction` wiring gap), `_dispatch_turn_job` (the 5.2 gate, prompt-resolution point), `_start_turn`/`_record_turn` (the lifecycle + history pairing reused), the reflex/checkpoint job registration (the composition precedent).
- [Source: `shelldon/core/reflexes.py`] `_idle_seconds` defensive parse to mirror for `_last_interaction_dt`.
- [Source: `shelldon/core/faces.py`] `FaceRegistry.select` — the feeling-word source.
- [Source: `_bmad-output/implementation-artifacts/5-2-cost-tier-gating-and-credit-budget.md`] the dispatch gate this rides.
- [Source: `_bmad-output/implementation-artifacts/5-3-battery-aware-backoff.md`] the battery gate this rides + the `last_interaction` deferral note.
- [Source: `_bmad-output/implementation-artifacts/dev-loop-checklist.md`] the pre-review self-checklist.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Amelia / bmad-dev-story) — `core/proactive.py` + its pure tests built by a parallel python-expert subagent (file-ownership split; no shared-file conflict), the coupled scheduler/runtime wiring by the main agent.

### Debug Log References

- Full default suite: `uv run pytest -q` → **409 passed, 3 skipped** (platform-gated fork/privdrop), 3 deselected (`live`).
- Soak: `uv run pytest -m soak -q` → **2 passed**, 1 skipped (real-fork, macOS). The proactive job is now registered in every `Core`, but the soak parks the scheduler (`scheduler_interval` far out) so it never ticks in-window — `_seq`/`_bg`/heap unaffected, no soak-construction change needed.
- Contracts: `uv run lint-imports` → **2 kept, 0 broken** (`core/proactive.py` imports only stdlib — LLM-free KEPT).
- New tests (~13): `tests/test_proactive.py` (6, subagent — pure prompt policy), `tests/test_turn_dispatch.py` (+6 — builder/marker dispatch, builder-raises skip, proactive job registered, CAP-4 no-owner-input initiation, cooldown no-init, `_last_interaction_dt` parse), `tests/test_scheduler.py` (+1 — `Job` builder/marker round-trip).

### Completion Notes List

- **Parallel build (owner request):** the isolated pure module `core/proactive.py` + `tests/test_proactive.py` was built by a background python-expert subagent against a fixed signature (`build_proactive_prompt(feeling: str | None) -> str`) while the main agent did the coupled `scheduler.py`/`runtime.py` wiring — clean file-ownership split, zero shared-file contention. Both halves integrated on first run after fixing one missed assignment.
- **New `core/proactive.py`** (LLM-free, pure): `build_proactive_prompt(feeling)` returns an open-ended self-prompt directive — "share whatever's on your mind: a thought, an observation, or a hello; it doesn't have to be a question" — woven with the feeling word when present; a `None`/blank feeling drops the feeling sentence entirely (never emits "None" or a dangling fragment). Single tunable template.
- **`Job` gained `prompt_builder` + `history_owner_text`** (`scheduler.py`, additive keyword-only — the 5.2 `cost` / 5.3 `essential` precedent). Both default `None`: static-prompt turns and owner turns are byte-for-byte unchanged.
- **Dispatch resolves the prompt** (`runtime._resolve_job_prompt`): builder (guarded — a raise/empty → skip, never wedges) or static `prompt`. `_dispatch_turn_job`'s gate logic (is_idle → cooldown/budget → admit → spend) is **otherwise untouched**; it passes `record_owner_text=job.history_owner_text` to `_start_turn`.
- **`_start_turn` gained `record_owner_text`** — the worker runs the real `prompt` (`spawn_turn`), but `self._current_prompt` (history pairing) holds the marker for a proactive turn. Every existing caller passes nothing → unchanged. No `_record_turn`/history change.
- **Idle signal wired** (`_scheduler_loop` → `tick(last_interaction=self._last_interaction_dt())`) — closes the 5.3-review-noted 5.4 gap so the `Idle` cadence can fire. `_last_interaction_dt` mirrors `reflexes._idle_seconds`' defensive parse (None/garbage/tz-naive → None, warned, never raised).
- **Proactive job registered in `Core.__init__`** (the reflex/checkpoint precedent): `Job("proactive", Idle(proactive_idle_interval), TURN, prompt_builder=self._build_proactive_prompt, history_owner_text=PROACTIVE_OWNER_MARKER)`. `_build_proactive_prompt` reads live mood and reuses `faces.select(...)` as the single feeling-label source. `proactive_idle_interval` injectable (default 1 hr, validated positive).
- **No new gating** — AC2 ("cooldown/budget not satisfied → no init") is enforced by the reused 5.2/5.3 path; the proactive job is `essential=False` (eased off first on battery). CAP-4 proven: a turn spawns + budget spends with **no INBOUND_MSG ever delivered**.
- **Scope honored:** no `worker/prompt.py` change (directive rides the existing owner-message slot, framed as a self-prompt — a dedicated proactive section is a noted follow-on); no mood-suppression gate (mood shapes the prompt only); idle threshold is the one exposed knob.
- **dev-loop-checklist applied:** builder + idle-parse guarded and tested; tests assert real values via a `history.record_turn` spy (worker got the directive, history got the marker — not truthiness); rejection/skip branches covered (builder raises, cooldown, in-flight); `proactive_idle_interval` rejects non-positive; conftest isolation unchanged (marker rides the existing history store); soak parks the new resident turn job.

### File List

- `shelldon/core/proactive.py` (new — `build_proactive_prompt`; subagent)
- `tests/test_proactive.py` (new — pure prompt policy; subagent)
- `shelldon/core/scheduler.py` (modified — `Job` gains `prompt_builder` + `history_owner_text`)
- `shelldon/core/runtime.py` (modified — proactive defaults + marker, `proactive_idle_interval` param, register the proactive job, `_build_proactive_prompt`, `_last_interaction_dt`, `_scheduler_loop` idle signal, `_resolve_job_prompt`, `_start_turn` `record_owner_text`)
- `tests/test_turn_dispatch.py` (modified — proactive dispatch/marker + CAP-4 integration)
- `tests/test_scheduler.py` (modified — `Job` builder/marker round-trip)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (status tracking)

## Change Log

| Date | Change |
|------|--------|
| 2026-06-18 | Story 5.4 implemented: proactive action (CAP-4) — the Epic 5 capstone. New `core/proactive.py` (`build_proactive_prompt`, LLM-free, open-ended "share a thought" framing). `Job` gains `prompt_builder` + `history_owner_text` seams. Proactive turn = an Idle-cadence TURN job registered in `Core.__init__`, prompt built at dispatch from live mood (reuses `faces.select`), history recorded with a synthetic owner-side marker. Idle signal wired into `scheduler.tick` (closes the 5.3 gap). Rides the 5.2 cooldown/budget + 5.3 battery gates UNCHANGED — both ACs enforced by the reused path. CAP-4 proven: a turn initiates with no owner input. `worker/prompt.py`/`app.py` untouched. Built with a parallel subagent (pure module) + main agent (coupled wiring), file-ownership split. +13 tests; suite 409 pass / soak 2 pass; contracts KEPT. Owner decisions locked: Idle trigger, mood→prompt (not gate), thought-framing, marker recording, idle threshold injectable default 1 hr. |
