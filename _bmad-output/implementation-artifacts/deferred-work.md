# Deferred Work

This file tracks work intentionally deferred from reviews, with reasons for why it was deferred and when it should be revisited.

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
