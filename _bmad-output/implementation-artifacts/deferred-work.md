# Deferred Work

This file tracks work intentionally deferred from reviews, with reasons for why it was deferred and when it should be revisited.

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
