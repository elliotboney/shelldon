---
baseline_commit: 11a46563bedd05583dd23c81cfc7d862fa124eca
---

# Story 2.3: Degrade to reflex-only when the whole chain fails

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the pet to stay alive when every provider is down,
so that a full outage makes it quiet, not frozen or crashed (AD-9, CAP-8).

## Acceptance Criteria

1. **Whole-chain exhaustion degrades the turn:** Given an exhausted provider chain (every provider failed), when the broker returns a terminal failure `Result`, then core degrades the turn to a reflex behavior (a "can't think right now" expression + reply) instead of replying with model text — and the process keeps running (bus/core loop alive, ready for the next turn).
2. **Offline → acknowledge, don't hang:** Given the network is fully offline (every provider's call raises a connection error), when the owner sends a message, then the pet acknowledges via the degraded reflex state **promptly** (no hang — it does not wait out the full turn timeout when the chain returns a fast failure).
3. **Auto-recovery:** Given the pet has degraded during an outage, when a provider becomes reachable again, then a subsequent turn completes normally with model text — with **no latched "degraded mode"** to clear; recovery is automatic because each turn independently re-attempts the chain.

> **Scope seam (binding):** The degrade **mechanism** already exists from Story 1.8 — `Core._degrade()` sends `DEGRADE_TEXT` + pushes `FACE_DEGRADED`, called on any failure `Result` and on turn timeout (`runtime.py`). 2.2 made the broker **return that terminal failure `Result`** when the whole chain is exhausted. So 2.3 is **not a new build** — it is: (a) **prove** whole-*chain* exhaustion (not just one provider) degrades end-to-end; (b) **prove** offline-acknowledge and **auto-recovery** (the genuinely new behaviors); (c) **de-placeholder** the 1.8 "full degrade is Epic 2" comments now that Epic 2 delivers it. The literal **resident reflex LOOP** (blink/idle/mood drift) is **Epic 3 (Story 3.2)** and does NOT exist yet — 2.3's "reflex behavior" is the degraded **ack/expression**, not a live reflex loop. See Dev Notes "What 2.3 does NOT do".

## Tasks / Subtasks

> **What already exists (reuse, do NOT reinvent — this is most of the story):**
> - `core/runtime.py::Core._degrade()` — sends `DEGRADE_TEXT` (`"…can't think right now…"`) over OUTBOUND_MSG and pushes `FACE_DEGRADED` (`"cant-think"`). **Already called** in both failure paths: `_handle_result` when `not result.ok`, and `_timeout_watch` on timeout. **Do NOT add a second degrade path.**
> - `core/runtime.py` constants `FACE_THINKING`/`FACE_REPLY`/`FACE_DEGRADED`/`DEGRADE_TEXT` — reuse; do not rename or add new ones.
> - `core/arbiter.py::Arbiter` — `complete()` releases the slot (or folds one catch-up); `reset()` releases on spawn failure. After a degrade, `_handle_result` already calls `arbiter.complete()`, so the slot is freed and the **next turn proceeds normally** — this IS the auto-recovery mechanism (nothing latches degraded mode). Verify, don't rebuild.
> - `broker/broker.py::handle_job_chain` (Story 2.2) — returns the last failure `Result` on chain exhaustion. The producer of the terminal failure. Unchanged here.
> - `tests/test_end_to_end_turn.py` — the in-process harness (`build_harness`, `_await`, `Spawns`, `OkProvider`, `AlwaysTransientProvider`) and the existing degrade tests (`test_ac3_degrade_on_failure_result` = single-provider exhaustion; `test_ac3_turn_timeout_no_hang_and_late_result_discarded`). **Extend this harness/pattern; do not write a new one.**

- [x] **Task 1: Harness accepts a multi-provider chain** (AC: 1)
  - [x] In `tests/test_end_to_end_turn.py::build_harness`, add an optional `chain: list | None = None` param. When given, pass it straight to `run_broker(sock_path, chain)`; otherwise keep today's behavior (`run_broker(sock_path, [provider])`). Keep `provider=` working for every existing caller — **no signature break**. (`provider` and `chain` are mutually exclusive; if both omitted, that's a test error.)
  - [x] No production code changes in this task — it only unlocks a real multi-element chain for the AC1 test.

- [x] **Task 2: Prove whole-CHAIN exhaustion degrades end-to-end** (AC: 1)
  - [x] New test `test_ac1_whole_chain_exhaustion_degrades` (in `test_end_to_end_turn.py` near the existing degrade tests): build the harness with a **2-provider all-failing chain** (e.g. `chain=[AlwaysTransientProvider(), AlwaysTransientProvider()]`). Feed a message; assert the outbound sink receives exactly `[DEGRADE_TEXT]` and the display rendered a `FACE_DEGRADED` snapshot — proving the broker iterates **both** providers, exhausts, returns the terminal failure `Result`, and core degrades. (The existing `test_ac3_degrade_on_failure_result` covers the 1-element chain; this covers AC1's literal "whole chain".)
  - [x] Assert the process stays alive after degrade: `h.core.arbiter.worker_in_flight is False` and `h.core.fence.current is None` (no latch, ready for the next turn).

- [x] **Task 3: Prove auto-recovery — resume normal turns when a provider returns** (AC: 3)
  - [x] Add a `RecoverableProvider` fake to `test_end_to_end_turn.py`: `name = "fake"`, a `self.down = True` flag; `complete()` raises `TransientProviderError("offline")` while `down`, else returns `f"reply to: {prompt}"`.
  - [x] New test `test_ac3_auto_recovers_when_provider_returns`: build the harness with this provider (single-element chain is fine). Feed "are you there?" → assert outbound becomes `[DEGRADE_TEXT]` (degraded during outage). Flip `provider.down = False`. Feed "you back?" → assert outbound becomes `[DEGRADE_TEXT, "reply to: you back?"]` and `spawns.count == 2`. This proves **no latched degraded mode** — the arbiter/fence were clean after the degrade and the next turn ran the chain normally.

- [x] **Task 4: Prove offline-acknowledge is prompt (no hang)** (AC: 2)
  - [x] New test `test_ac2_offline_acknowledges_without_hanging`: build the harness with an all-failing chain **and a long `turn_timeout`** (e.g. `turn_timeout=30.0`, the default). Feed a message; assert the degrade reply arrives **fast** (well under the turn timeout — the chain returns a failure `Result` quickly, so degrade must come from the failure path, NOT the timeout path). A small `_await(..., timeout=2.0)` on `outbound == [DEGRADE_TEXT]` demonstrates "acknowledges rather than hanging." (Backoffs are zeroed in tests by the existing `conftest._no_broker_backoff` autouse fixture, so the failure is near-instant.)

- [x] **Task 5: De-placeholder the degrade as the official Epic-2 behavior** (AC: 1)
  - [x] Now that Epic 2 delivers the full chain + fallback + degrade, update the **comments/docstrings** that still call degrade a 1.8 placeholder — **without changing behavior**:
    - `core/runtime.py` module docstring (lines ~15–19): the "degrade-on-failure … the full provider chain are Epic 2" note is now done — reword to state degrade-on-chain-exhaustion is the live behavior (Story 2.3); the personality-state/real-expression caveats (Epic 3 / Story 3.3) **remain**.
    - `core/runtime.py` `DEGRADE_TEXT` comment (line ~47): drop "Full degrade-to-reflex is Epic 2"; state it's the chain-exhaustion reflex ack (real reflex loop is Epic 3 / Story 3.2).
    - `core/arbiter.py` docstring (lines ~7–8): "the full degrade-to-reflex chain are Epic 2 / Epic 5" — the degrade half is now Story 2.3; keep the cooldown/credit/battery caveats (Epic 5).
  - [x] **Surgical only** — comment/docstring text, no logic, no renames. The import-linter must stay KEPT (`core/` LLM-free) and every existing test must stay green.

- [x] **Task 6: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (`core/` still LLM-free; 2.3 touches only `core/` comments + `tests/`).
  - [x] `uv run pytest -q` → green (existing degrade/round-trip tests unchanged + the 3 new tests). Default run hits no network.

## Dev Notes

### Architecture compliance (binding)

- **AD-9 — the arbiter governs the brain; degrade on chain exhaustion:** "on provider-chain exhaustion the arbiter **falls back to a reflex behavior** so the pet never freezes." In the current design the **degrade decision lives in `Core._handle_result`/`_timeout_watch`** (the runtime's failure paths), which call `_degrade()` and then `arbiter.complete()`. This satisfies AD-9 at the core layer. **Decision (binding for 2.3): keep the degrade in the runtime — do NOT relocate it into `Arbiter`** (that would refactor working, tested 1.8 code for conceptual purity with no behavior change). If the owner wants the decision physically inside `Arbiter`, that's a separate, explicit refactor (saved question Q1). [Source: ARCHITECTURE-SPINE.md#AD-9]
- **CAP-8 — LLM fallback on error = broker provider chain + arbiter degradation:** 2.1 = chain, 2.2 = fallback-through-chain + terminal failure on exhaustion, **2.3 = the degradation half** (terminal failure → reflex ack). This completes CAP-8. [Source: ARCHITECTURE-SPINE.md#CAP-8]
- **AD-1 — LLM-free core:** 2.3 changes only `core/` comments + `tests/`; no provider import enters `core/`. Import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **NFR6 — graceful offline degradation:** "resident reflexes keep running with no network/brain." 2.3 delivers the **degrade ack** half (the pet doesn't freeze/crash offline); the **resident-reflex-loop** half is Epic 3 (the loop doesn't exist yet). [Source: epics.md#NFR6]

### The degrade path already exists (do not duplicate)

`Core._degrade()` (`runtime.py`) sends `DEGRADE_TEXT` + `FACE_DEGRADED`. It is already invoked on (a) a failure `Result` in `_handle_result` (line ~125) and (b) turn timeout in `_timeout_watch` (line ~152). With Story 2.2, a fully-exhausted chain **is** a failure `Result`, so AC1 already flows through path (a). 2.3's job is to **prove it with a real multi-provider chain** and to prove offline/auto-recovery — not to add code. The single biggest mistake here would be building a second degrade mechanism. [Source: shelldon/core/runtime.py:113-185]

### Auto-recovery is structural, not a feature (AC3)

There is **no "degraded mode" flag** anywhere. After a degrade, `_handle_result` calls `arbiter.complete()` which releases the worker slot (or folds one catch-up), and `fence.close()` clears the turn. The next INBOUND_MSG → `arbiter.submit` → new turn → worker → broker re-attempts the chain. So the pet **auto-recovers the instant a provider answers again** — 2.3 only needs to demonstrate it (Task 3). Do not add a recovery timer, health check, or circuit breaker — none is in scope (and a circuit breaker would be a forward-looking feature, not an AC). [Source: shelldon/core/arbiter.py, runtime.py:126-128]

### What 2.3 does NOT do

- **No resident reflex LOOP** (blink/idle/time-of-day mood drift) — that is **Epic 3, Story 3.2**, and the personality-state struct is **Story 3.1**; neither exists yet. 2.3's "reflex behavior" is strictly the degraded **ack/expression**. A later Epic 3 story confirms degrade coexists with a live reflex loop (no forward dependency — degrade works standalone now). [Source: epics.md#Epic 3]
- **No real expression vocabulary / mood→face mapping** — `FACE_DEGRADED` stays the placeholder token; the real expressions are **Story 3.3**. [Source: epics.md#Story 3.3]
- **No relocation of the degrade decision into `Arbiter`** — keep it in the runtime (see AD-9 note; Q1).
- **No circuit breaker / health-check / recovery timer** — auto-recovery is the existing stateless per-turn retry (Task 3 proves it).
- **No change to `broker/`** — 2.2's terminal-failure `Result` is the input to this story, unchanged.

### Reuse / preserve (from Stories 1.8 + 2.2)

- `DEGRADE_TEXT` / `FACE_DEGRADED` — the exact tokens existing tests assert on; reuse verbatim. [Source: runtime.py:43-48]
- `build_harness` / `_await` / `Spawns` / `AlwaysTransientProvider` — the in-process test harness; extend with a `chain=` param + a `RecoverableProvider`, nothing more. [Source: tests/test_end_to_end_turn.py:101-181]
- The fence/timeout discipline (AD-12): a degrade closes the turn so a late Result is discarded — already covered by `test_ac3_turn_timeout_no_hang_and_late_result_discarded`; don't regress it. [Source: runtime.py:140-155, turn.py]

### Project Structure Notes

- Modified (comments/docstrings only): `shelldon/core/runtime.py`, `shelldon/core/arbiter.py`.
- Modified (test harness + new tests): `tests/test_end_to_end_turn.py` (`build_harness` +`chain=`; +`RecoverableProvider`; +3 tests). No new source files. `core/` logic untouched → import-linter KEPT. [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Drive everything through the existing in-process `build_harness`; assert on `outbound` (the CLI sink) and `renderer.rendered` faces, using `_await` for liveness with a bounded timeout. No network (fakes only). Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: 1.8/2.2 tests; Epic 1 retro]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2 / Story 2.3 (this story); #Story 2.2 (terminal failure producer — done); #Epic 3 (reflex loop — later)]
- [Source: ARCHITECTURE-SPINE.md#AD-9 (arbiter degrades on exhaustion), #CAP-8 (line 220), #AD-1 (LLM-free core), #NFR6 (graceful offline)]
- [Source: shelldon/core/runtime.py:113-185 (_handle_result/_timeout_watch/_degrade — the existing degrade), :43-48 (tokens); core/arbiter.py (complete/reset — auto-recovery mechanism); core/turn.py (fence)]
- [Source: shelldon/broker/broker.py::handle_job_chain (Story 2.2 terminal failure on exhaustion)]
- [Source: tests/test_end_to_end_turn.py:153-181 (build_harness), :235-277 (existing degrade tests to extend, not duplicate)]
- [Source: _bmad-output/implementation-artifacts/2-2-automatic-fallback-through-the-chain.md ("What 2.2 deliberately does NOT do" → "No arbiter reflex-degrade on chain exhaustion → Story 2.3")]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m]

### Debug Log References

- `uv run pytest tests/test_end_to_end_turn.py -q` → 7 passed (4 existing degrade/round-trip + 3 new).
- `uv run lint-imports` → 2 contracts KEPT (core LLM-free; transport holds no creds).
- `uv run pytest -q` → 140 passed, 2 skipped, 3 deselected (skips/deselects pre-existing, unrelated).

### Completion Notes List

- **No new degrade mechanism built** (as the scope seam demanded). The story was proof + de-placeholder: the 1.8 `Core._degrade()` path, fed by 2.2's terminal-failure `Result` on whole-chain exhaustion, already satisfies all three ACs. Verified, not rebuilt.
- **Task 1 (harness only):** `build_harness` gained a mutually-exclusive `chain=` param alongside `provider=` (both now default `None`; passing neither or both raises). No existing caller changed — all still pass `provider=`.
- **AC1** proven with a real 2-element `[AlwaysTransientProvider, AlwaysTransientProvider]` chain: broker iterates both, exhausts, returns terminal failure, core degrades to `[DEGRADE_TEXT]` + `FACE_DEGRADED`; arbiter/fence confirmed clean afterward (no latch).
- **AC3 auto-recovery** proven structurally with `RecoverableProvider`: turn 1 degrades during outage, `provider.down=False`, turn 2 returns model text — `spawns.count == 2`, no latched mode (each turn re-attempts the chain).
- **AC2 no-hang** proven with a long `turn_timeout=30.0`: degrade arrives within a bounded 2s wait, so it comes from the fast failure `Result` path, not the timeout path.
- **Task 5** de-placeholdered three comment sites only (runtime module docstring, `DEGRADE_TEXT` comment, arbiter docstring) — no logic, no renames. Epic 3/3.2/3.3 (resident reflex loop, real expressions) and Epic 5 (cooldown/budget) caveats preserved.

### File List

- `tests/test_end_to_end_turn.py` — `build_harness` `chain=` param; `RecoverableProvider` fake; 3 new tests (`test_ac1_whole_chain_exhaustion_degrades`, `test_ac3_auto_recovers_when_provider_returns`, `test_ac2_offline_acknowledges_without_hanging`).
- `shelldon/core/runtime.py` — comments/docstrings only (module scope note + `DEGRADE_TEXT` comment).
- `shelldon/core/arbiter.py` — module docstring; `reset()` docstring (review fix); new `is_idle` property (review fix).
- `shelldon/core/turn.py` — new `TurnFence.is_idle` property (review fix).

## Change Log

- 2026-06-17: Story 2.3 implemented — proved whole-chain-exhaustion degrade (AC1), auto-recovery (AC3), and prompt offline-acknowledge (AC2) via 3 new end-to-end tests; de-placeholdered the 1.8 "degrade is Epic 2" comments. No production logic changed. All tests green, import-linter KEPT.
- 2026-06-17: Addressed code review — 5 in-scope findings resolved (see Review Resolution); 5 pre-existing broker-test findings deferred to `deferred-work.md`. Added read-only `Arbiter.is_idle`/`TurnFence.is_idle` properties (no behavior change). All tests green, import-linter KEPT.

## Code Review

Reviewed 2026-06-17. 8-angle scan (line-by-line, removed-behavior, cross-file, reuse, simplification, efficiency, altitude, conventions) × verify pass. 4 confirmed/plausible findings, 6 cleanup findings. Ranked most-severe first.

```json
[
  {
    "file": "tests/test_end_to_end_turn.py",
    "line": 321,
    "summary": "`worker_in_flight` and `fence.current` are implementation details accessed directly — neither is public API and both will break with AttributeError under plausible Epic 5 refactors.",
    "failure_scenario": "If Arbiter gains a semaphore slot (natural for cooldown/budget in Epic 5) or TurnFence.current is renamed/encapsulated, these assertions break even with no behavior change. Fix: add `Arbiter.is_idle: bool` and `TurnFence.is_idle: bool` thin properties (one line each) and assert on those instead."
  },
  {
    "file": "tests/test_end_to_end_turn.py",
    "line": 172,
    "summary": "`(provider is None) == (chain is None)` is a correct but non-idiomatic XOR guard that invites a future 'fix' to `!=` that would invert the condition.",
    "failure_scenario": "A developer scanning test code reads `== (chain is None)` as 'both are the same state (both set)', concludes the guard is backwards, changes it to `!=`, and silently allows both-None or both-set through without raising. Idiom: `if not (provider is None) ^ (chain is None)` or split into two explicit `if` guards."
  },
  {
    "file": "tests/test_end_to_end_turn.py",
    "line": 369,
    "summary": "`_await(lambda: h.outbound == [DEGRADE_TEXT], timeout=2.0)` with a 30s turn_timeout could produce spurious CI failures on a loaded host.",
    "failure_scenario": "The failure Result traverses ~4 asyncio task hops (broker → bus → core → cli-transport). On a CPU-starved CI host each hop can stall; 4 × stall > 2s fails the assert even though the code is correct. The 500× margin is generous under normal load but the test has no slack for pathological scheduling. Increasing to 5.0s or adding a dedicated assertion on the failure path (not a timing bound) would eliminate the class."
  },
  {
    "file": "tests/test_end_to_end_turn.py",
    "line": 372,
    "summary": "`test_ac2_offline_acknowledges_without_hanging` never asserts `arbiter.worker_in_flight is False` — a regression that skips `arbiter.complete()` after `_degrade()` would pass this test.",
    "failure_scenario": "An exception between `_degrade()` (line 125 of runtime.py) and `arbiter.complete()` (line 128) leaves the slot stuck True permanently; subsequent messages coalesce forever. `test_ac1` catches this on the same code path, so the gap is partially mitigated — but `test_ac2` is also the turn-timeout variant (turn_timeout=30s) which is NOT covered by test_ac1's default-timeout run."
  },
  {
    "file": "tests/conftest.py",
    "line": 19,
    "summary": "Conftest docstring claims `_RECONNECT_BACKOFF_S` is 'exercised explicitly in test_broker_reconnect.py' — that file never references the constant; reconnect backoff timing is untested anywhere.",
    "failure_scenario": "A future story changes the reconnect backoff from 1s to 10s; no test catches the regression because the one test that exercises reconnect (`test_reconnects_after_a_transient_connect_failure`) only checks that reconnection happens, not when."
  },
  {
    "file": "tests/test_broker_service_branches.py",
    "line": 21,
    "summary": "`_Collector` class is byte-for-byte identical to the one in `tests/test_broker_service.py`.",
    "failure_scenario": "A change to the writer interface (e.g., adding `wait_closed`) requires patching the same class in two files. Extract to a shared fixture or `conftest.py`."
  },
  {
    "file": "tests/test_broker_chain_fallback.py",
    "line": 47,
    "summary": "`assert primary.calls == 2` hardcodes the broker's internal per-provider retry count (1 attempt + 1 retry = 2), not the AC contract.",
    "failure_scenario": "A legitimate change to retry count (e.g., 3 attempts) breaks `assert primary.calls == 2` with a misleading failure that looks like the fallback chain didn't fire, not like a retry-count change."
  },
  {
    "file": "tests/test_end_to_end_turn.py",
    "line": 196,
    "summary": "`build_harness` startup `_await` calls mask actor startup failures behind a generic timeout assertion.",
    "failure_scenario": "A broken provider import or port conflict causes an actor task to raise immediately; `_await(lambda: all(registered))` consumes the full 2s before raising `AssertionError: condition not met within timeout`, masking the real exception. The actual cause requires inspecting task exception state manually."
  },
  {
    "file": "tests/test_broker_reconnect.py",
    "line": 31,
    "summary": "`test_reconnects_after_a_transient_connect_failure` implicitly depends on the autouse `_no_broker_backoff` fixture zeroing `_RECONNECT_BACKOFF_S` to stay within its 1s poll budget.",
    "failure_scenario": "If `_no_broker_backoff` is ever removed or scoped to a subset of tests, the reconnect loop sleeps its default backoff (1s) between attempts; the test's 100 × sleep(0.01) poll budget (~1s) expires before the second connect fires, producing a sporadic timeout failure."
  },
  {
    "file": "shelldon/core/arbiter.py",
    "line": 51,
    "summary": "`reset()` docstring says dropping the pending message is 'accepted degraded behavior; guaranteed redelivery is Epic 2' — but the module docstring now says Epic 2 is live (Story 2.3), creating a false impression that the pending-drop gap is resolved.",
    "failure_scenario": "A developer reading `arbiter.py` sees 'Epic 2 is live' in the module header and concludes all failure paths are covered. They add a new failure path that calls `reset()` without realizing the pending-drop is intentional-but-deferred (still not redelivery-safe), and silently drops messages in production."
  }
]
```

### Review Resolution (2026-06-17)

**Fixed in this story (in-scope — my new code or a contradiction I introduced):**

- ✅ **arbiter `reset()` docstring contradiction** (finding @201) — the de-placeholder made the module header say "Epic 2 live" while `reset()` still implied redelivery ships in Epic 2. Reworded: redelivery is *still deferred* (Epic 2 delivered chain + degrade, NOT redelivery).
- ✅ **Private-attr asserts** (finding @149) — added thin read-only `Arbiter.is_idle` and `TurnFence.is_idle` properties; `test_ac1` now asserts on intent (`is_idle`) instead of `worker_in_flight`/`current` internals.
- ✅ **`test_ac2` missing idle assert** (finding @165) — added `await _await(lambda: h.core.arbiter.is_idle)` so a slot-stuck regression on the *timeout-variant* path (turn_timeout=30s) is caught, not just the default path in `test_ac1`.
- ✅ **Flaky 2s bound** (finding @159) — `test_ac2` degrade wait bumped 2.0s → 5.0s (still 6× headroom vs the 30s timeout, more slack for loaded CI).
- ✅ **XOR guard readability** (finding @153) — `build_harness` guard split into two explicit `if` checks (neither / both) so it can't be inverted by a future edit.

**Deferred (out of 2.3 scope — pre-existing broker tests from Stories 2.1/2.2), logged in `deferred-work.md`:**

- conftest docstring overstates reconnect-backoff coverage (@171); `test_broker_reconnect` implicit fixture dependency (@195); duplicate `_Collector` (@177); hardcoded `primary.calls == 2` retry count (@183); `build_harness` startup `_await` masks actor failures (@189, pre-existing 1.8).
