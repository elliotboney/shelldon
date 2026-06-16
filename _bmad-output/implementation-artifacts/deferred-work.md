# Deferred Work

This file tracks work intentionally deferred from reviews, with reasons for why it was deferred and when it should be revisited.

## Deferred from: code review of story 1-1 (2026-06-16)

- **CI workflow python-version parameter** — Need to verify `astral-sh/setup-uv@v5` supports this input; uv manages Python differently than standard setup-python actions. Revisit when CI is tested or when Python version issues arise.
- **Missing uv cache in CI** — Optimization for CI speed, not a correctness issue. Can be added when CI runtimes become a concern.
- **No error context parsing in test failure** — Usability improvement; test already shows stdout/stderr in assertion. Revisit if debugging becomes difficult.
- **Test only imports packages, doesn't validate structure** — AC3 is met; structural file-system validation is extra scope. Revisit if package structure issues arise.
- **Dynamic imports not validated** — Out of scope for static import-linter; would need runtime guard. Revisit if dynamic imports become a concern.
- **No platform-specific Pi validation** — ARM/Raspberry Pi specific testing will be added in later stories when hardware integration begins.
- **No verification after LLM SDK installation** — Guard will be verified when LLM SDKs are actually added as dependencies in Story 1.4 (broker).
