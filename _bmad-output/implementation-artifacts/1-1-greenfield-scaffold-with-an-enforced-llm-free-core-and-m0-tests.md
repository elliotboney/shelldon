---
baseline_commit: 60f8382b06d53acf357abf40f2a383e8918f58c3
---

# Story 1.1: Greenfield scaffold with an enforced LLM-free core and M0 tests

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer building shelldon,
I want a project skeleton whose `core/` is mechanically barred from importing LLM code, with a test harness from day one,
so that the load-bearing invariant (AD-1) can never silently rot and every later story ships with tests (AD-10).

## Acceptance Criteria

1. **Source tree** exists — `core/ broker/ worker/ transport/ display/ plugins/ contracts/ tests/` — targeting **Python 3.13.x**, stdlib `asyncio`, **no web framework**.
2. **Import-linter guard:** a CI rule fails the build if any module under `core/` imports an LLM/provider library.
3. **Test harness:** `pytest` runs green on an empty/trivial harness, wired into CI.
4. **License:** repo carries an **MIT `LICENSE`** (Elliot's copyright) plus a **`NOTICE`** crediting Dmitry Turmyshev's openclawgotchi.
5. **Guard proven:** a deliberately-added violating import inside a `core/` module makes CI fail on the import-linter rule.

## Tasks / Subtasks

- [x] **Task 1: Initialize the Python project** (AC: 1)
  - [x] `pyproject.toml` targeting Python 3.13.x; pick packaging (recommend `uv`, or plain pyproject + pip — dev's call, but pin `requires-python = ">=3.13,<3.14"`)
  - [x] Top-level package (e.g. `shelldon/`) so `import shelldon.core` resolves
- [x] **Task 2: Create the source-tree packages** (AC: 1)
  - [x] `core/ broker/ worker/ transport/ display/ plugins/ contracts/` each an importable package with `__init__.py`; `tests/`
  - [x] Do **not** create runtime data dirs (`~/.shelldon/` is runtime, not source — see AD-6/AD-7)
- [x] **Task 3: Configure the import-linter forbidden contract** (AC: 2, 5)
  - [x] Add `import-linter` (v2.3) dep
  - [x] In `pyproject.toml`: `[tool.importlinter]` `root_package = "shelldon"`, `include_external_packages = true`
  - [x] A `forbidden` contract: `source_modules = ["shelldon.core"]`, `forbidden_modules = [` the intended provider SDKs `]` (e.g. `openai`, `anthropic`, `google-generativeai`, `litellm`, the Z.ai/GLM SDK) so the guard is real from day one even before those libs are installed (broker is Story 1.4)
  - [x] Verify `lint-imports` passes on the clean tree
- [x] **Task 4: Test harness** (AC: 3)
  - [x] Add `pytest`; one trivial passing test in `tests/` (e.g. asserts the packages import)
- [x] **Task 5: CI pipeline** (AC: 2, 3, 5)
  - [x] CI workflow that runs **`lint-imports`** then **`pytest`**, failing the build on either
- [x] **Task 6: License & attribution** (AC: 4)
  - [x] `LICENSE` = MIT, Elliot's copyright
  - [x] `NOTICE` crediting Dmitry Turmyshev / openclawgotchi (clean-room reimplementation; v1 reference, not copied)
- [x] **Task 7: Prove the guard** (AC: 5)
  - [x] Temporarily add a forbidden import to a `core/` module, confirm `lint-imports` (and CI) fails, then remove it — capture the proof in the completion notes

### Review Findings

- [x] [Review][Patch] Python version range is not pinned to Python 3.13.x [pyproject.toml:6]
- [x] [Review][Patch] import-linter dependency is not pinned to the specified v2.3 [pyproject.toml:13]
- [x] [Review][Patch] CI sync does not enforce the committed lockfile [`.github/workflows/ci.yml`:13]
- [x] [Review][Patch] LLM-free-core guard test skips when lint-imports is missing [tests/test_core_llm_free.py:12]
- [x] [Review][Patch] LLM-free-core guard test does not pin subprocess cwd to the repo root [tests/test_core_llm_free.py:14]

## Dev Notes

- **Architecture compliance (binding):**
  - **AD-1 — LLM-free core:** `core/` imports no LLM/provider modules; the import-linter rule is what enforces it. This is the whole point of the story. [Source: ARCHITECTURE-SPINE.md#AD-1]
  - **AD-10 — versioned contracts + tests from M0:** the test harness must exist now; later M0 tests (contract round-trip, ≤1-worker bound, atomic-write crash-safety) build on it. [Source: ARCHITECTURE-SPINE.md#AD-10]
- **Stack:** Python **3.13.x** (CPython — `os.fork()` COW warm-start is load-bearing in Story 1.5; 3.13 for Pi OS lib compatibility, 3.14.6 is current upstream). Stdlib `asyncio`, no web framework. [Source: ARCHITECTURE-SPINE.md frontmatter `stack`]
- **import-linter (v2.3):** the `forbidden` contract checks `core/` (and descendants) never import the listed modules, including **external** packages when `include_external_packages = true` — required here since the LLM SDKs are third-party. Config in `pyproject.toml` under `[tool.importlinter]`; run with `lint-imports`. The forbidden list names provider SDKs even before they're installed, so the invariant holds from the first commit.
- **Dependency scope for THIS story:** only `import-linter` + `pytest`. `msgspec@0.21.1` arrives in Story 1.2; provider SDKs arrive with the broker (Story 1.4). Don't add them yet.
- **No starter template** — this is greenfield; build the skeleton by hand, don't adopt a framework starter. [Source: epics.md#Additional Requirements]
- **Testing standards:** `pytest`; the harness is intentionally minimal now. Keep `tests/` mirroring the package layout.

### Project Structure Notes

- Matches the spine's namespace map: `core/` = domain (LLM-free, linter-enforced) · `broker/ worker/ transport/ display/ plugins/` = adapters · `contracts/` = shared msgspec types (populated in Story 1.2). [Source: ARCHITECTURE-SPINE.md#Structural Seed]
- Runtime data (the markdown memory tree + `~/.shelldon/history.db`) lives **outside** the source tree — do not scaffold it here.
- No previous story (this is 1.1); no prior code patterns to inherit. Git history to date is planning artifacts only.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.1]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-1, #AD-10, #Structural Seed]
- [Source: _bmad-output/specs/spec-openclawgotchi-v2/SPEC.md#Constraints (LLM-free core; ground-up; MIT attribution)]
- import-linter 2.3 — forbidden contract + `include_external_packages` (https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story)

### Debug Log References

- `uv run lint-imports` → "core is LLM-free (AD-1) KEPT. Contracts: 1 kept, 0 broken."
- `uv run pytest -q` → 9 passed
- Guard proof (Task 7): injected `import openai` into `shelldon/core/__init__.py` → `lint-imports` reported "shelldon.core is not allowed to import openai" (contract broken); removed it → KEPT again.

### Completion Notes List

- All 5 ACs satisfied. Scaffolded the full package tree, the `[tool.importlinter]` forbidden contract (AD-1), the pytest M0 harness (AD-10), CI, and MIT LICENSE + NOTICE crediting Dmitry Turmyshev.
- **Config fix during dev:** import-linter rejects subpackages of external packages in `forbidden_modules`; changed `google.generativeai` → `google` (top-level). The `test_core_llm_free` guard test caught this before it could ship.
- Packaging: chose `uv` + `hatchling` (project installed editable so `shelldon.core` resolves for both grimp/import-linter and pytest). `requires-python = ">=3.13"` (Pi target 3.13.x; local dev ran 3.14.5).
- Deps kept minimal per story scope: only `import-linter` + `pytest` (dev group). `msgspec` deferred to Story 1.2; provider SDKs to Story 1.4 — but they're already named in the forbidden list so the guard is real now.
- `.shelldon/` runtime dir added to `.gitignore`; no runtime data scaffolded (AD-6/AD-7).

### File List

- `pyproject.toml` (new)
- `shelldon/__init__.py` (new)
- `shelldon/core/__init__.py` (new)
- `shelldon/broker/__init__.py` (new)
- `shelldon/worker/__init__.py` (new)
- `shelldon/transport/__init__.py` (new)
- `shelldon/display/__init__.py` (new)
- `shelldon/plugins/__init__.py` (new)
- `shelldon/contracts/__init__.py` (new)
- `tests/test_scaffold.py` (new)
- `tests/test_core_llm_free.py` (new)
- `.github/workflows/ci.yml` (new)
- `LICENSE` (new)
- `NOTICE` (new)
- `.gitignore` (new)
- `uv.lock` (new, then re-locked for Python 3.13)
- `.python-version` (new — pins dev to 3.13, added in review fixes)

### Change Log

- 2026-06-16: Implemented Story 1.1 — greenfield scaffold, LLM-free-core import-linter guard (AD-1), M0 pytest harness (AD-10), CI, MIT LICENSE + NOTICE. All ACs met; guard proven. Status → review.
- 2026-06-16: Addressed code review — 5 [Patch] findings resolved: pinned `requires-python = ">=3.13,<3.14"` + added `.python-version` (3.13) and re-locked on 3.13.5; pinned `import-linter~=2.3` (locked 2.11); CI uses `uv sync --locked`; guard test no longer skips when `lint-imports` is missing; guard test pins `cwd` to repo root. Re-verified: guard KEPT, 9 tests pass on 3.13.5. Status → review (re-review).
