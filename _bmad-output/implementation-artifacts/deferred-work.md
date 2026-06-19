# Deferred Work

This file tracks work intentionally deferred from reviews, with reasons for why it was deferred and when it should be revisited.

---

## 🔭 Deliberate tradeoffs to revisit (post-epic / trigger-gated)

> Conscious architecture choices that are RIGHT for now but should be reconsidered if a specific trigger fires. Unlike the per-story review punts below (picked up mid-epic), these are "look again once the daily-driver is real / all epics are done." Check this section during epic retrospectives.

- **Native Gemini adapter dropped in favor of the OpenAI-compatible endpoint (Story 2.1, 2026-06-17).**
  - **Decision:** Gemini is reached via its OpenAI-compatible endpoint (`.../v1beta/openai/`) as a plain `OpenAIProvider` preset — no `google-genai` SDK, no per-provider adapter. Chosen because the broker chain's interface is provider-agnostic (`complete(prompt) -> str`), so native-only features can't be carried without special-casing Gemini and breaking the abstraction; and GLM (Z.ai) is the primary, Gemini a fallback.
  - **What we lose without the native adapter:** (1) **thinking/reasoning-budget control** (`thinking_config`) on 2.5 models; (2) **safety-threshold tuning** (`safety_settings` — e.g. loosening harm categories so the pet can be edgier/more playful); (3) **native Google Search grounding**; (4) **explicit context caching** (cost savings on repeated context, e.g. injected memory).
  - **Re-add trigger:** if we want **Gemini free-tier-first / Gemini as the PRIMARY provider** (its free tier is generous — 1,500 req/day, 1M context), OR Epic 3 wants Gemini-specific safety loosening, OR Epic 4/5 wants explicit context caching for cost. Then re-introduce `GeminiProvider` (`google-genai`) as its own story with real ACs — and note this needs a way to pass per-provider options through the chain (the abstraction question above). Re-add cost is ~50 lines + the dep; it was never committed, so reconstruct rather than revert.

---

## Resolved from: code review of 1-7-display-service-shows-the-pets-face-from-core-state (2026-06-16)

- **[RESOLVED] `BusServer.stop()` hung on an idle client connection whose peer never disconnects** — surfaced while writing the 1.7 display tests. Root cause: `Server.wait_closed()` (3.13) blocks until all handler tasks finish, and a client parked in `read_frame` never EOFs just because its writer is closed. **Fix** (`core/bus/server.py`): the hub now tracks handler tasks (`_handlers`) and `stop()` **cancels** them deterministically — looping so a handler that registers during the gather-yield is still caught — then closes connections, then closes the listening server last (close-last avoids an asyncio `Server._wakeup` race under force-close on 3.13). Regression test: `tests/test_bus_disconnect.py::test_stop_with_idle_connected_client_does_not_hang` (idle settled + racing-mid-registration clients; `stop()` must return promptly). 77 pass / 1 skipped, both import-linters KEPT.

## Resolved (2026-06-16 — deferred-item sweep, test-only hardening)

Cheap coverage gaps over already-shipped code, completed in parallel without scope expansion (full suite 69 passed / 1 skipped, both import-linters KEPT):

- **[1.6] Hub-disconnect teardown (`read_frame → None`)** — now tested: `tests/test_cli_transport.py::test_outbound_loop_exits_on_hub_disconnect`.
- **[1.6] `_outbound_loop` `ValidationError→skip`** — now tested: `tests/test_cli_transport.py::test_outbound_loop_skips_invalid_frame_and_continues`. *(The framing `ValueError→clean-exit` sub-branch remains untested — see Still deferred below.)*
- **[1.5] `TurnFence` eviction boundary** — now tested: `tests/test_turn_fence.py::test_closed_set_eviction_is_bounded` (closes `max_closed + 1` ids, asserts the oldest is evicted from `_closed` and the cap holds).
- **[1.4] `service.py` non-Job skip + clean-EOF branches** — now tested: `tests/test_broker_service_branches.py` (`test_non_job_envelope_is_skipped`, `test_clean_eof_ends_connection`), driving `_serve_connection` directly with a fed `StreamReader`.
- **[1.3] Oversized-frame `ValueError` cap** — found **already covered**: `tests/test_bus_frame.py::test_oversized_length_raises_before_allocating` (+ `tests/test_bus_errors.py::test_oversized_frame_closes_connection_but_hub_survives`). No new test needed.

### Rejected (kept as-is, with reason)

- **[1.3] `conftest.py` `/tmp` → `tempfile.gettempdir()`** — **rejected.** `/tmp` is hardcoded *because* macOS's default `$TMPDIR` (`/var/folders/…`) overflows the AF_UNIX ~104-char path cap; `gettempdir()` would reintroduce that exact failure. The original deferred note's rationale is incorrect. If/when a Linux CI target is defined, gate the dir on platform rather than switching unconditionally.

### Still deferred (sweep judged not worthy now)

- **[1.6] framing-`ValueError → clean-exit` in `_outbound_loop` untested** — only reachable via a corrupt length prefix on the adapter's own connection (the hub validates upstream); low value, add alongside 1.8 end-to-end wiring.
- All other items below remain deferred per their original reasons (resilience/Epic 2, or by-design-until-1.8).

## Deferred from: code review of 1-8-end-to-end-turn-message-in-reply-out-face-reacts (2026-06-16)

- **Timeout + pending catch-up prompt dropped on `WorkerBusyError`** — when P1 catches `WorkerBusyError` in `_start_turn`, the coalesced catch-up prompt is lost (not re-queued). Full watchdog/reschedule with guaranteed delivery is Epic 2 scope.
- **`_handle_result` — `arbiter.complete()` not called if `bus.deliver` raises** — `_route` already catches `OSError` so risk is low; handle in resilience hardening story.
- **AC1 test missing face token assertions** — `len(renderer.rendered) >= 1` is checked but not the face token values (`FACE_THINKING` then `FACE_REPLY`). AC is met; add token assertions with display integration test.
- **Timeout test timing flakiness** — `asyncio.sleep(0.8)` after degrade assert has no anchor to turn start; fix with direct `fence.current` state assertion when test hardening begins.
- **`_await` helper poor diagnostics** — raises bare `AssertionError("condition not met")` with no registry state; add context when CI debugging is needed.

## Deferred from: code review of 1-6-one-chat-transport-adapter-over-a-transport-agnostic-contract (2026-06-16)

- **`_default_inbound` executor thread leak on cancellation** — `sys.stdin.readline` in `run_in_executor` cannot be interrupted; thread blocks until process exit if the transport is torn down before stdin closes. Fix requires custom executor or non-blocking stdin approach (e.g. `aioconsole`). Revisit when production CLI use cases are hardened.
- **Hub-disconnect path (`read_frame → None`) untested** — outbound loop exiting first (hub gone) and cancelling the inbound loop is a valid teardown path that has no test. Add in 1.8 end-to-end wiring.
- **`ValidationError→skip` / `ValueError→clean-exit` in `_outbound_loop` untested** — resilience branches exist in code but have no test coverage. Add when integration testing expands.
- **Both asyncio tasks done simultaneously → second exception silently lost** — `for task in done: task.result()` raises on first and skips second. Fix when error reporting is hardened.
- **`outbound()` callable not protected from exceptions** — if a non-trivial sink (socket-backed stdout) raises, the outbound loop crashes. Protect when such sinks are wired.

## Deferred from: code review of 1-5-fork-server-worker-that-runs-one-turn-and-dies (2026-06-16)

- **Child exits 0 on `asyncio.run()` exception in fork child** — `forkserver.py:_os_fork_spawn` try/finally always calls `os._exit(0)`; a failed job send is invisible to the parent. Add exit-code handling when supervisor/error path is scoped in Epic 2.
- **`_os_waitpid_reap` has no timeout** — if child is unkillable (debugger, stuck kernel sleep), `reap_current()` loops forever. Add watchdog/SIGKILL escalation in resilience hardening.
- **`Arbiter` and `ForkServer.worker_in_flight` are independent and never connected** — by design for the 1.5 skeleton; wire together in 1.8 when the full arbiter is built.
- **`Arbiter.try_begin` not async-safe** — no `asyncio.Lock` between read-check and write. Safe while no `await` exists between them; add a lock when 1.8 defines the concurrency model.
- **Child inherits parent FDs after fork** — acknowledged fork-without-exec risk; production fix is `os.closerange(3, os.sysconf('SC_OPEN_MAX'))` before `asyncio.run()` in child. Defer to resilience/hardening story.
- **No `TurnFence` eviction boundary test** — closing exactly `max_closed + 1` distinct IDs is untested; manual inspection confirms correctness. Add coverage when TurnFence is extended.
- **`gc.disable()` not re-enabled on `preload()` exception path** — intentional for COW fork pattern; test teardown re-enables for isolation. Revisit if process lifecycle changes.

## Deferred from: code review of 4-5-worker-proposes-ops-wire (2026-06-17)

- **`write_frame` for outbound Result (worker→core) has no timeout** — `run_worker` in `worker/worker.py` has a 120s timeout on `read_frame` for the Completion but no timeout on the subsequent `write_frame` to core. If the hub stalls, the worker blocks indefinitely past the 120s window. Pre-existing write-path issue; core timeout is the backstop. Address in resilience hardening.
- **`parse_reply` `.strip()` on assembled payload destroys intentional leading/trailing whitespace** — After stripping the ops block, `payload = (text[:start] + text[end:]).strip()` silently removes leading/trailing whitespace. Low risk for current plain-text replies but may matter once Story 4.4 introduces formatted prompts. Revisit during 4.4 prompt-format definition.
- **COMPLETION dropped at hub + 90s `worker_in_flight` freeze asymmetry** — If a worker times out (fires at 120s) but core already degraded (typically ~30s), the `worker_in_flight` slot stays locked for 90 extra seconds, blocking all new turns. The asymmetry is intentional (120s is generous to avoid false timeouts) but unguarded. Consider reducing `_COMPLETION_TIMEOUT_S` to align with core's turn timeout, or expose it as an injectable config, in a resilience story.
- **ops block with no `\n` after opening fence silently unmatched — no warning logged** — `_OPS_BLOCK_RE` requires `\n` after the opening fence. A malformed fence with content immediately after the backticks (no newline) silently produces `(full_text, [])` with no log warning. Extremely unlikely from a well-prompted LLM; Story 4.4 owns the prompt format. Add a pre-match heuristic warning if needed.

## Deferred from: code review of 1-4-capability-broker-with-one-provider-and-basic-retry (2026-06-16)

- **Potential credential leak via `str(sdk_exc)` in `Result.error`** — SDK error messages don't typically include the API key, but no runtime value test verifies this. Revisit if credential hygiene audit is done.
- **No backoff between transient retries** — Immediate retry into a rate-limited endpoint wastes the only retry budget. Add exponential backoff when retry logic is enhanced in Epic 2.
- **`connect()` has no timeout** — A hung server blocks the broker indefinitely. Add `asyncio.wait_for` wrapper when resilience hardening begins.
- **Sequential job processing in `run_broker`** — `await handle_job` blocks the read loop; concurrent Jobs require a task pool. Address in Epic 2 or when throughput becomes a concern.
- **No test for non-Job envelope path or hub-disconnect path in `service.py`** — The `log.warning + continue` and `env is None: break` branches are untested. Add coverage when integration testing expands.
- **`run_broker` has no reconnect logic** — A transient hub restart kills the broker permanently. Add supervisor/reconnect loop when resilience story is scoped.

## Deferred from: code review of 1-3-envelope-bus-over-unix-domain-sockets-hub-routed-through-core (2026-06-16)

- **Tests synchronize via `asyncio.sleep(0.05)`** — Timing-based sync is fragile on slow CI; no event-based mechanism to confirm hub has processed a disconnect or registered an actor. Revisit if tests start flaking in CI.
- **No test for oversized-frame `ValueError` path** — The 8 MiB cap in `read_frame` is untested. If finding #1 (oversized-frame connection handling) is patched, a test should accompany that fix.
- **`conftest.py` hardcodes `/tmp`** — `tempfile.gettempdir()` would respect `TMPDIR` and work in constrained CI environments. Low risk for current dev setup; revisit when CI target is defined.

## Deferred from: code review of story 1-1 (2026-06-16)

- **CI workflow python-version parameter** — Need to verify `astral-sh/setup-uv@v5` supports this input; uv manages Python differently than standard setup-python actions. Revisit when CI is tested or when Python version issues arise.
- **Missing uv cache in CI** — Optimization for CI speed, not a correctness issue. Can be added when CI runtimes become a concern.
- **No error context parsing in test failure** — Usability improvement; test already shows stdout/stderr in assertion. Revisit if debugging becomes difficult.
- **Test only imports packages, doesn't validate structure** — AC3 is met; structural file-system validation is extra scope. Revisit if package structure issues arise.
- **Dynamic imports not validated** — Out of scope for static import-linter; would need runtime guard. Revisit if dynamic imports become a concern.
- **No platform-specific Pi validation** — ARM/Raspberry Pi specific testing will be added in later stories when hardware integration begins.
- **No verification after LLM SDK installation** — Guard will be verified when LLM SDKs are actually added as dependencies in Story 1.4 (broker).

## Deferred from: code review of 2-3-degrade-to-reflex-only-when-the-whole-chain-fails (2026-06-17)

> The 2.3 review's in-scope findings (private-attr asserts → `is_idle` properties; test_ac2 idle assertion; 2s→5s timeout slack; readability of the `build_harness` guard; the `reset()` docstring contradiction the de-placeholder introduced) were **fixed in this story**. The items below touch pre-existing broker tests from Stories 2.1/2.2 — out of 2.3's scope (tests-only + comment de-placeholder), so they're deferred rather than expanded into here.

- **`conftest._no_broker_backoff` docstring overstates coverage** — claims `_RECONNECT_BACKOFF_S` is "exercised explicitly in test_broker_reconnect.py", but that file never references the constant; reconnect backoff *timing* is untested anywhere. Either add a timing assertion or correct the docstring. Pre-existing (2.2). Revisit when reconnect timing is hardened.
- **`test_broker_reconnect` implicitly depends on `_no_broker_backoff`** — `test_reconnects_after_a_transient_connect_failure` only stays within its ~1s poll budget because the autouse fixture zeros `_RECONNECT_BACKOFF_S`. If that fixture is ever removed/rescoped, the test goes sporadically flaky. Make the dependency explicit (e.g. monkeypatch within the test) when reconnect tests are revisited. Pre-existing (2.2).
- **`_Collector` duplicated byte-for-byte** across `test_broker_service.py` and `test_broker_service_branches.py` — extract to a shared fixture/`conftest.py` so a writer-interface change patches one place. Pre-existing (2.1/2.2) cleanup.
- **`test_broker_chain_fallback` asserts `primary.calls == 2`** — hardcodes the per-provider retry count (1 attempt + 1 retry) rather than the AC contract; a legit retry-count change breaks it with a misleading "fallback didn't fire" failure. Assert on the fallback contract instead. Pre-existing (2.2).
- **`build_harness` startup `_await` masks actor startup failures** — a broken import/port conflict makes an actor task raise immediately, but the registration `_await` consumes its full timeout and raises a generic "condition not met" instead of surfacing the real exception. Pre-existing (1.8); add task-exception inspection when test diagnostics are hardened (relates to the 1.8-deferred "`_await` poor diagnostics" item).

## Deferred from: code review of 2-2-automatic-fallback-through-the-chain (2026-06-17)

> Filed by the reviewer under "design notes for retrospective" — hypothetical / known tradeoffs, not fix-now. Recorded here per the review.

- **Empty-chain guard only in `run_broker`, not `_serve_connection`** — `_serve_connection` accepts `list[LLMProvider]` with no guard; called directly with `[]` it would let `handle_job_chain` return `Result(ok=False, error="empty provider chain")` with no service-layer log. No caller bypasses `run_broker` (which raises on empty) + `build_chain` (raises on empty), so it's hypothetical. Revisit if a second caller of `_serve_connection` is added — an `assert chain` at entry would make the invariant explicit. Not added now (no defensive code for an impossible path).
- **`name: str` on the `LLMProvider` Protocol is metadata coupling** — `name` is used only for audit logging in `handle_job_chain`, not a behavioral capability, yet the Protocol now rejects any structurally valid provider that lacks a label. Deliberate choice (Story 2.2 Task 2 / Dev Notes "Audit record"). Revisit if a third-party/test provider is added that doesn't naturally carry a name — a `(name, provider)` tuple/NamedTuple at the chain layer would be a cleaner seam than putting the label on the provider itself.

## Deferred from: code review of 2-1-provider-abstraction-and-an-ordered-chain (2026-06-17)

- **`OpenAIProvider.complete()` — `message=None` → AttributeError** — OpenAI SDK contract says `message` is never None; defensive guard is theoretical. Revisit if an OpenAI-compat endpoint is found that violates this.
- **`build_chain` catches only `RuntimeError`** — All known builder errors are RuntimeErrors; other exceptions lose preset-name context. Revisit if builders start raising ValueError/TypeError.
- **Missing model env var error doesn't name the env var** — Usability: error says "requires a model" but not which env var. Revisit when improving DX for first-time setup.
- **Duplicate preset names in `PROVIDER_CHAIN` silently builds duplicate providers** — e.g. `"glm,glm"` wastes a fallback slot. Deduplication or warning belongs in Story 2.2 when chain iteration is implemented.

## Deferred from: code review of 3-1-persistent-personality-state-struct (2026-06-17)

- **`_checkpoint_task.cancel()` not awaited before shutdown flush** [`runtime.py:_cleanup`] — `_cleanup()` is sync; structurally can't await. Low-severity "Task destroyed but pending" warning risk. Revisit when _cleanup is refactored to async or Epic 5 scheduler takes over.
- **Type mismatch in `apply_patch` values not validated** [`state.py:apply_patch`] — nan/inf/wrong-type values pass `setattr` but may cause `msgspec.json.encode` to raise at checkpoint time. Mitigated by the loop-recovery patch (finding #1). Revisit when value-range invariants are formalized (3.2 will define drift bounds).
- **Mutable `Mood`/`PersonalityState` structs allow direct attribute bypass of `apply_patch`** [`state.py:Mood`] — Design tradeoff: mutable by spec, enforced by convention. Story 3.2's reflex loop must call `apply_patch`. Revisit if multiple writers are added beyond core.
- **`Core.checkpoint_path` is a public mutable attribute** [`runtime.py:__init__`] — Hygiene: caller can change the path mid-run, causing loop and shutdown to diverge. Revisit if Core grows a public API surface.

## Deferred from: code review of 3-4-self-modify-faces-via-chat (2026-06-18)

All five items are pre-existing issues in `faces.py` or `worker/worker.py` — not introduced by 3.4.

- **Whitespace-only face name passes `_validate_face`** [`shelldon/core/faces.py:88`] — `not face.name` is False for `"   "` (truthy whitespace), so a space-only name slips through and lands in `faces.toml` as an unremovable entry. Fix: `if not face.name.strip()`. Revisit in a faces-hardening pass or before Story 4.4 adds LLM-generated names.
- **Point-range `lo==hi` silently accepted** [`shelldon/core/faces.py:79`] — `_validate_range` checks `lo > hi` (inverted) but not `lo == hi`. A face with `valence=(0.5, 0.5)` passes validation and writes to disk but can only be selected at one exact float — effectively dead. Fix: add `if lo == hi: raise ValueError(...)`. Revisit in a faces-hardening pass.
- **`replace=True` on catch-all `content` face corrupts selection-order invariant** [`shelldon/core/faces.py:215`] — `add_face` replaces in-place by index, so replacing the last catch-all `content` with a narrow face leaves no broad fallback in the list. `select_face` still returns the hardcoded `DEFAULT_FACE_TOKEN` string, so the pet doesn't crash, but the semantic catch-all is gone from the list. Guard: skip or warn when `replace=True` targets a `STARTER_NAMES` entry and the result would leave nothing matching a broad range. Revisit before any face-management UI is added.
- **No per-field size limit on `token`/`name` in `AddFace`** [`shelldon/core/faces.py:193`] — A worker can propose a face with a multi-KB `token` or `name`, bloating `faces.toml` with no bound beyond LLM output limits. Fix: add `len(name) <= 64` and `len(token) <= 256` guards in `_validate_face`. Revisit before Story 4.4 when the LLM actually starts proposing faces.
- **All-ops reply produces empty `payload` delivered silently** [`shelldon/worker/worker.py:parse_reply`] — A worker that proposes ops but says nothing else in its reply results in `payload=""` on a `Result(ok=True)`. No guard exists in `_send_reply` or `_handle_result` to detect or warn about an empty successful payload. Revisit in 4.4 when the LLM is prompted to actually emit ops — ensure the system prompt always asks for both a reply and an ops block.

## Deferred from: code review of 4-4-memory-shapes-the-turn (2026-06-18)

- **FTS implicit safety invariant** [`shelldon/worker/prompt.py:53-62`] — `_fts_query` safety relies on `\w+` never matching FTS5 metacharacters; a future regex loosening would silently break injection safety. The invariant should be documented in a comment. Low risk while the regex stays as-is.
- **FTS common-word recall noise** [`shelldon/worker/prompt.py:56-62`] — 32-term OR query without stopword filtering; common words (`is`, `my`, `the`) match nearly every row, making `recall_k` the only noise guard. Acceptable for v1; revisit if recall quality becomes a concern in Epic 6 (dream cycle / relevance tuning).

## Deferred from: code review of 4-3-vault-with-os-level-isolation (2026-06-18)

- **`_os_fork_spawn` default `drop=_real_drop` bypasses injected drop if called directly** [`shelldon/worker/forkserver.py`] — Callers going around `ForkServer._default_spawn` skip the injected drop. Pre-existing design; `_default_spawn` routes correctly. Revisit if `_os_fork_spawn` ever acquires a public caller.
- **`os.fork()` OSError (ENOMEM/EAGAIN) not caught in `_os_fork_spawn`** [`shelldon/worker/forkserver.py`] — Pre-existing; unhandled OSError propagates up the async call chain. Low-frequency production concern.
- **`ensure_vault` raises `NotADirectoryError` with no context if a path component is a file** [`shelldon/core/vault.py`] — Pre-existing concern for the whole memory tree; no context added for vault specifically. Revisit in a startup-error-quality pass.
- **`launch_multiprocess` mid-loop `child.start()` failure leaves already-started children running** [`shelldon/app.py`] — Production deployment concern; `# pragma: no cover` path. Revisit before Pi deployment hardening.
- **`child.join(timeout=5.0)` silently returns with child still alive** [`shelldon/app.py`] — Zombie/runaway child processes after shutdown timeout. Revisit before Pi deployment hardening.
- **`forkserver.preload()` raises after `ensure_vault` with no cleanup path** [`shelldon/app.py`] — Startup failure propagates; partially initialized state is GC'd. Acceptable for now; revisit with supervision/restart logic.
- **`ensure_vault` chmod no-ops if vault dir is owned by a different user** [`shelldon/core/vault.py`] — Service always creates and owns `vault/`; edge case only in unusual multi-instance deployments. Revisit if multi-instance deployment is ever planned.

## Deferred from: code review of 5-0-resilience-hardening-prep (2026-06-18)
- **`test_fork_oserror_becomes_runtime_error` patches `os.fork` globally** [`tests/test_resilience.py:233`] — works correctly now; fragile only if import style in forkserver.py changes from `os.fork()` to `from os import fork`. Revisit if that import style changes.

## Deferred from: code review of 5-2-cost-tier-gating-and-credit-budget (2026-06-18)

- **`Daily` cadence uses UTC-day; budget uses local-day** [`shelldon/core/scheduler.py:104`] — Pre-existing from Story 5.1. `Daily.is_due` compares `.date()` on tz-aware UTC datetimes (UTC calendar day) while `BudgetGate._local_date` uses `now.astimezone().date()` (owner local day). For UTC-offset owners these predicates can diverge: a daily job could fire twice in one owner-local day (UTC day flip before local day does) or the budget reset day could disagree with the cadence's fire day. Low impact until a real daily turn job is registered in 5.4+.
- **DEFER vs SKIP paths not distinguished by log assertion in integration tests** [`tests/test_turn_dispatch.py`] — Both `test_defers_within_the_cooldown` and `test_skips_when_daily_budget_exhausted` end with identical assertions (`spawns == []`, slot/budget unchanged); the paths are separated only by setup, not by asserting distinct log output. Minor test expressiveness gap; not a functional bug.

## Deferred from: code review of 6-2-dream-cycle-classify-promote-prune (2026-06-19)

- **`facts/` surfacing follow-on** [`shelldon/core/runtime.py:_build_dream_prompt`, `shelldon/worker/prompt.py:gather_context`] — `facts/` and `people/` content written via `remember` op is durable in the markdown tree but NOT injected by 4.4 prompt assembly (`gather_context` reads only `about.md`/`summary.md`). The dream directive should not offer `remember` as a promotion path until `facts/` surfacing is built. Trigger: when 4.4 is extended to inject `facts/`/`people/` into prompts, reinstate the `remember` option in `_build_dream_prompt` and add tests.
- **Promoted learning silently lost if process crashes between `resolve_learning` and `remember`/`rewrite_about`** [`shelldon/core/runtime.py:_apply_proposed_ops`] — ops applied in list order; if `resolve_learning(promoted)` fires before the corresponding markdown write and the process crashes, the learning is gone (excluded from future dream prompts, never written to markdown). Same crash risk as all ops-loop paths. Trigger: if crash-safety across a multi-op atomic write becomes a requirement.
- **Embedded newlines in observations break dream directive line format** [`shelldon/core/runtime.py:_build_dream_prompt`] — `f"- [id={row['id']}] {row['observation']} (seen N×)"` interpolates raw observation text; a `\n` in the observation splits the bullet entry. Safe now (synthetic tests, no live LLM). Fix before model wiring: add `.replace('\n', ' ').replace('\r', ' ')` in the f-string line + a test.
- **Observation length unbounded in `_build_dream_prompt`** [`shelldon/core/runtime.py:_build_dream_prompt`] — `capture_learning` has no max-len cap; 50 multi-KB observations produce an oversize dream prompt. Acceptable now (no real LLM). Add per-observation truncation before production model integration.
- **Blocking `pending_learnings()` sqlite read in async event loop** [`shelldon/core/runtime.py:_build_dream_prompt → _dispatch_turn_job`] — synchronous `fetchall()` in the critical section blocks the event loop. Safe on Pi Zero with single-writer WAL and minimal concurrent disk activity. Revisit when Epic 7 plugin-host introduces concurrent disk pressure.
- **Dream op vocab outside the ``` fence in `SYSTEM_INSTRUCTION`** [`shelldon/worker/prompt.py`] — `resolve_learning` and `rewrite_summary` examples are placed after the formal ops fence (same pattern as 6.1's `capture_learning` defer). Fix when live-LLM prompt tuning is introduced in a later epic.

## Deferred from: code review of 6-1-capture-learnings-on-the-hot-path (2026-06-19)

- **Unbounded `observation`/`pattern_key` string lengths** [`shelldon/core/history.py:capture_learning`, `shelldon/contracts/__init__.py`] — no length cap or truncation on LLM-generated strings; single-owner + 6.2 pruning keep table size manageable now. Add guards at the Epic 7 plugin-host boundary, or trigger: when table-growth is first measured on a real device.
- **tz-naive `datetime` silently accepted by `capture_learning(now: datetime)`** [`shelldon/core/history.py:capture_learning`] — same risk as `record_turn`; all callers currently pass `datetime.now(UTC)`. Enforce with type annotation or assertion when systematic type coverage is added.
- **`capture_learning` prompt example is after the closing ` ``` ` fence** [`shelldon/worker/prompt.py`] — real-model uptake of the op is unverifiable without a live LLM (noted in spec); tested mechanism is correct. Revise prompt copy when live-LLM integration testing is introduced in a later epic.
- **`CREATE TABLE IF NOT EXISTS` doesn't migrate an existing `learnings` table with a different schema** [`shelldon/core/history.py:_SCHEMA`] — pre-existing pattern across all tables; no migration framework exists. Add `ALTER TABLE ADD COLUMN` guards before Pi deployment where a pre-6.1 `history.db` may exist on device.
- **Integration tests access `core.history._conn` directly to assert learnings rows** [`tests/test_proposed_ops.py`] — no public read API for learnings until 6.2. Replace with a `HistoryStore.list_learnings()` or `pending_learnings()` method when 6.2 adds the dream read path.

## Deferred from: code review of 5-4-proactive-action (2026-06-18)

- **TURN job with neither `prompt` nor `prompt_builder` not rejected at construction** [`shelldon/core/scheduler.py:Job.__init__`] — silently skips every tick with a WARNING log; graceful skip is the designed behavior (AD-14 guard path). Add a `ValueError` at registration time to fail fast. Trigger: when a second TURN job type is added (Epic 6 dream job) and misconfiguration risk increases.
- **`power` param lacks type annotation on `Core.__init__` and `Scheduler.__init__`** — `Callable[[], PowerState] | None` is the correct type; passing a bare `PowerState` value crashes deep in `tick`. Matches existing unannotated-param convention. Add annotation when type coverage is systematically improved, or when a new `power` caller is written (Epic 7 PiSugar2 plugin).

## Deferred from: code review of 5-3-battery-aware-backoff (2026-06-18)

- **Budget rollover clock-skew** [`shelldon/core/budget.py:77-100`] — `evaluate` and `admission_patch` take `now` at different call sites; a midnight-crossing admission could double-count or miss. Extremely rare for a solo pet.
- **Silent permanent SKIP when `job.cost > daily_turn_budget`** [`shelldon/core/budget.py:82`] — a misconfigured job (cost > cap) silently SKIPs every tick with no warning. Add a diagnostic log in 5.4 when the first real turn job is registered.
- **`Idle.is_due` exact-timestamp re-fire** [`shelldon/core/scheduler.py:93`] — pre-existing from 5.1; `last_run <= last_interaction` re-fires on exact timestamp collision; cosmically rare.
- **`PowerState.charge` accepts negative values** [`shelldon/core/power.py:23-31`] — faulty hardware reader returning negative charge forces LOW permanently. Validate at the Epic 7 plugin-host boundary.
- **Missing test: `eased_scale=1.0` accepted** [`shelldon/core/power.py:46`] — guard is `not (x >= 1.0)`, so 1.0 passes; add a test if the guard is ever tightened to `> 1.0`.
- **`turns_used > daily_turn_budget` after config decrease → SKIP until rollover** [`shelldon/core/budget.py:77`] — lowering the cap between restarts blocks all turn jobs until midnight. Low operational risk; document in config notes.
- **`apply_patch` after `arbiter.submit()` with no explicit rollback** [`shelldon/core/runtime.py:465-471`] — if `apply_patch` raises unexpectedly the arbiter slot leaks until the 30s turn timeout clears it. budget.* paths cannot raise in setattr; theoretical only.
- **Hardcoded date `2026-06-18` in `test_cadence_stretch_is_demonstrable_on_battery`** [`tests/test_battery_backoff.py:125`] — style gap; no functional impact. Could use the `_at()` helper from `test_scheduler.py`.

## Deferred from: code review of 5-1-core-scheduler-with-named-multi-cadence-jobs (2026-06-18)
- **`Idle` cadence never fires until 5.4 wires `last_interaction`** [`runtime.py:366-369`] — `_scheduler_loop` hardcodes `last_interaction=None`; any Idle job registered before 5.4 updates the call silently never fires. Story 5.4 must parse `state.state.last_interaction` to a `datetime` and pass it to `scheduler.tick()`.
- **`Cadence` base class uses `NotImplementedError` not `abc.ABC`** [`scheduler.py:44`] — a subclass that forgets `is_due()` only fails at runtime. Change to `abc.ABC` + `@abstractmethod` when adding future cadence types.
- **`Daily` cadence no clock-jump guard** [`scheduler.py:92-95`] — NTP backward correction suppresses the daily job for up to 24h silently. Revisit if clock reliability becomes an issue (not on a Pi).
- **`_cleanup()` does not await `_scheduler_task` cancellation** [`runtime.py:464-470`] — same pattern as the old `_reflex_task`/`_checkpoint_task`; a job that swallows `CancelledError` could zombie on shutdown. Revisit if clean shutdown becomes a requirement.

## Deferred from: code review of 7-1-plugin-host-and-the-generalized-plugin-contract (2026-06-19)
- **`_idle` sentinel bypasses `validate_claims`** — `LoadedPlugins` doesn't include the idle placeholder; inconsistent with the struct the spec defines as the canonical registry. Zero impact in 7.1 (no subscriptions); revisit when Story 7.2 builds on `loaded.subscriptions`.
- **`LoadedPlugins` lacks `__eq__`/`__repr__`** — plain class vs the `msgspec.Struct` style used everywhere else; awkward to test or debug the load result directly. Add if a 7.2+ test needs equality comparison.
- **`emits` field on `PluginManifest` declared but never consumed** — intentional in 7.1. Emit registry / conflict checking is a future story concern; add when it becomes needed.
- **Tests access `srv._registry` (private `BusServer` attribute)** — pre-existing pattern across all bus-client lifecycle tests. Trigger: `BusServer` restructures or exposes a public "is actor connected?" API.
- **`connect()` in `run_plugin_host` has no retry/timeout** — consistent with transport/display bus-client adapter pattern. Trigger: hub startup ordering becomes a production pain.
- **`BasePlugin.run` doesn't catch `OSError`/`asyncio.IncompleteReadError` from `read_frame`** — consistent with transport/display frame-loop pattern. Trigger: `read_frame` starts surfacing `OSError` explicitly.
- **Multiple `done` tasks: second exception silently discarded** — pre-existing `asyncio.wait(FIRST_COMPLETED)` teardown pattern. Trigger: multi-plugin simultaneous failure is observable in 7.3+.
- **`pkgutil.iter_modules` discovery order is filesystem-dependent** — test name assertions control insertion order today. Trigger: 7.3+ real plugins expose ordering sensitivity in conflict messages.
- **`_plugin_host_proc` doesn't pass `dict(os.environ)` to child unlike `_broker_proc`** — env is inherited on spawn; no plugins in 7.1 need explicit env. Trigger: 7.4 hardware plugin requires a credential not inherited.
- **`package.__path__` is None for namespace packages — silently discovers nothing** — theoretical; shelldon.plugins is a regular package. Trigger: plugins are ever installed as namespace packages.

## Deferred from: code review of 7-0-extract-turn-dispatch-from-runtime (2026-06-19)
- **`.strip()` outside try/except in `resolve_job_prompt`** [`dispatch.py:117`] — truthy non-str return from a `prompt_builder` (e.g. `list`, `int`) hits `.strip()` outside the guard → unhandled `AttributeError`. Pre-existing from `runtime.py:605`; fix if future prompt builders return structured types.
- **`apply_patch` raises after `arbiter.submit` reserves slot → wedged arbiter** [`dispatch.py:102`] — no rollback if `state.apply_patch` raises after the slot is reserved; slot stays occupied until turn timeout. Pre-existing from `runtime.py:587-590`; also tracked in 5-3 defers. Revisit if async state writes are ever introduced.
- **`pending_learnings()` row missing key → unguarded `KeyError` in `build_dream_prompt`** [`dispatch.py:59-60`] — pre-existing from `runtime.py:540-541`; only reachable if the DB schema diverges from the `learnings` table definition.
- **`faces.select` raises → caught as "builder failed" in `resolve_job_prompt`** [`dispatch.py:51`] — pre-existing from `runtime.py:532`; the error path is correct but the log message is misleading (says "builder failed" rather than "faces lookup failed").
- **No-await invariant in admit section asserted only in comment** [`dispatch.py:62-103`] — the concurrency correctness invariant (`is_idle → apply_patch → submit` await-free) has no static enforcement. Pre-existing pattern; add a `mypy`/lint rule or async-safety test if the invariant is ever at risk.
- **`_start_turn` bound-method injection order undocumented** [`runtime.py:~248`] — `self._dispatcher` is constructed after `self._start_turn` is resolvable as a bound method; if `__init__` order changes the bound method captures a partially-initialized `Core`. Pre-existing pattern (same as Scheduler's `dispatch_turn` injection); add an ordering comment if `Core.__init__` is ever refactored.
