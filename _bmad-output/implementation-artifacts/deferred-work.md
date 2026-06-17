# Deferred Work

This file tracks work intentionally deferred from reviews, with reasons for why it was deferred and when it should be revisited.

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
