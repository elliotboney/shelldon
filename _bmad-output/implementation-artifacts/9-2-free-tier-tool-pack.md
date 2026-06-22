---
baseline_commit: c8c13cf
---
# Story 9.2: Free-tier tool pack (inline execution)

Status: done

## Story

As the owner,
I want shelldon to read files and run pure computations on its own, with no approval friction,
so that it can actually help with safe coding/info tasks in the moment.

## Acceptance Criteria

### AC1 — `read_file` and `list_dir`, path-jailed to one workspace root

**Given** the FREE tier and a registered tool registry
**When** the brain calls `read_file(path=...)` or `list_dir(path=...)`
**Then** the tool runs synchronously in the worker (inside the 9.1 bounded loop, no approval) and returns the file text / directory listing as a `ToolResult(ok=True, content=...)`
**And** every file path is resolved to its REAL path (symlinks followed) and confirmed to stay under a single `WORKSPACE_ROOT` (default `~/.shelldon/workspace`); a path that escapes the root is rejected fail-closed as `ToolResult(ok=False, ...)` and logged — it never reads outside the jail

### AC2 — `python_eval` runs pure compute in a restricted namespace, time-bounded

**Given** the FREE tier
**When** the brain calls `python_eval(code=...)`
**Then** the snippet is evaluated in a RESTRICTED namespace with no access to `open`, `os`, `subprocess`, `__import__`, or any side-effecting builtin, and the string result reaches the model as `ToolResult(ok=True, content=...)`
**And** the evaluation is wall-clock time-bounded (default ~2s via `signal.SIGALRM` in the worker's main thread); a snippet that exceeds the bound is killed and returned as `ToolResult(ok=False, ...)` — the turn never hangs or crashes

### AC3 — Side-effecting / malformed snippets fail closed

**Given** a `python_eval` snippet that tries `open(...)`, `import os`, `__import__(...)`, or otherwise touches the filesystem/network/shell, OR a syntactically invalid snippet
**When** it is evaluated
**Then** it fails closed — raises inside the restricted namespace, is caught, and is fed back as `ToolResult(ok=False, content=<error>)`; nothing escapes the namespace and the model recovers

### AC4 — `vault/` and credential files are always denied (defense in depth)

**Given** any FREE file tool and a path pointing at the secrets tree or a credential file (`vault/`, `*.env`, `.env`, files containing API keys)
**When** the tool resolves the path
**Then** access is denied fail-closed regardless of the path-jail result — `vault/` lives at `~/.shelldon/memory/vault` (OS-locked 0o700, AD-6) and is structurally outside `WORKSPACE_ROOT`, AND the tool ALSO explicitly refuses any path whose resolved location is the vault or a `.env`-shaped credential file
**And** `core/` still imports no LLM/provider code (import-linter 3 contracts KEPT)

### AC5 — FREE-tier inline only; spine unchanged

**Given** the 9.1 foundation
**When** 9.2 lands
**Then** all three tools are `ToolTier.FREE` and execute inline in the existing worker loop — there are NO changes to `contracts/`, `broker/`, or the worker's `_agentic_loop` (9.2 only adds tools to `worker/tools.py`, threads a workspace root, and ensures the workspace dir exists)
**And** all existing tests pass (575+), import-linter 3 contracts green, `uv sync --locked` 0 new deps (`pathlib`/`signal`/`ast`/`builtins` are stdlib)

---

## Tasks / Subtasks

- [x] **Task 1 — Workspace root + path-jail helper** (AC1, AC4)
  - [x] Added `DEFAULT_WORKSPACE_ROOT = Path.home() / ".shelldon" / "workspace"` to `shelldon/worker/tools.py`
  - [x] `_resolve_in_jail(path, workspace_root)` — `(root / path).resolve()` + `is_relative_to(root)`; raises `ValueError` on escape (caught by `execute_tool`). Symlink + absolute-path escapes both caught (`.resolve()` dereferences before the check)
  - [x] `_deny_sensitive(candidate, memory_root)` — refuses `<memory_root>/vault` (and anything under it) + `.env`/`*.env` files, fail-closed (AC4)
  - [x] Verify: no LLM/provider imports added (imports only `core.memory.DEFAULT_MEMORY_ROOT` + stdlib); import-linter 3 KEPT

- [x] **Task 2 — `read_file` + `list_dir` FREE tools** (AC1)
  - [x] `_read_file(path, *, workspace_root, memory_root)` — jail-resolve, deny sensitive, read capped at `_MAX_READ_BYTES = 64KB` (inline truncation marker + logged), missing file → raise → `ToolResult(ok=False)`
  - [x] `_list_dir(path, *, workspace_root, memory_root)` — jail-resolve, deny sensitive, newline-joined listing (dirs marked `/`), `(empty)` for empty, missing dir → raise
  - [x] Both registered with a required `path: string` JSON-schema, `tier=ToolTier.FREE`
  - [x] `fn` binds `workspace_root`/`memory_root` via `functools.partial` so `execute_tool`'s `fn(**call.args)` passes only `path`

- [x] **Task 3 — `python_eval` restricted + time-bounded** (AC2, AC3)
  - [x] `_python_eval(code, *, timeout_s=_EVAL_TIMEOUT_S)` — `eval(compile(code, "<python_eval>", "eval"), {"__builtins__": _SAFE_BUILTINS}, {})`; `_SAFE_BUILTINS` is a pure-compute allowlist (no `open`/`__import__`/`eval`/`exec`/`compile`/`globals`/`getattr`/`type`/`object`)
  - [x] Time bound via `signal.signal(SIGALRM)` + `signal.setitimer(ITIMER_REAL, timeout_s)`, restored in `finally` (fires in the worker fork child's main thread)
  - [x] Expression-only baseline (eval mode) — a multi-statement snippet is a `SyntaxError` → fail-closed (kept simple per CLAUDE.md §2)
  - [x] All failures (SyntaxError / NameError from blocked builtin / TimeoutError) raise → `execute_tool` → `ToolResult(ok=False)` (AC3)

- [x] **Task 4 — Register the pack in `build_tool_registry`** (AC5)
  - [x] `build_tool_registry(workspace_root=None, memory_root=None)` — defaults to `DEFAULT_WORKSPACE_ROOT`/`DEFAULT_MEMORY_ROOT`; adds `read_file`/`list_dir`/`python_eval` alongside `get_time` (all FREE)
  - [x] `forkserver.py` bare `build_tool_registry()` call unchanged (default applies in prod) — confirmed compiles + suite green
  - [x] NO edits to `contracts/`, `broker/`, or `worker/worker.py::_agentic_loop` (AC5 — verified)

- [x] **Task 5 — Ensure the workspace dir exists (prod)** (AC1)
  - [x] `shelldon/app.py`: after `ensure_vault(memory_root)`, `os.makedirs(DEFAULT_WORKSPACE_ROOT, exist_ok=True)` with NORMAL perms (no 0o700) so the dropped worker uid can read it
  - [x] Broadened the `worker/prompt.py` tools line to name `read_file`/`list_dir`/`python_eval` (one sentence)

- [x] **Task 6 — Tests** (AC1–AC5)
  - [x] NEW `tests/test_free_tools.py` (17 tests): read_file happy/subdir/missing/relative-escape/absolute-escape/symlink-escape/truncation; list_dir happy/escape/missing; vault denial (overlapping root) + `.env`/`*.env` denial; python_eval happy/blocks-open/blocks-import(stmt+`__import__`)/syntax-error/time-bound(`TimeoutError` via tiny injected timeout on an interruptible genexpr)
  - [x] Extended `tests/test_tool_loop.py`: `test_free_pack_read_file_runs_inside_the_loop` — real `read_file` executes inside the 9.1 loop against a tmp workspace, content fed back
  - [x] Boundary gate: `uv run pytest -q` → **593 passed / 3 skipped / 7 deselected (live)**; `uv run lint-imports` → **3 KEPT**; `uv sync --locked` → **0 new deps**

---

## Dev Notes

### What 9.1 already built (read first — this is the foundation you extend)

Story 9.1 (status: review, same epic) shipped the function-calling spine. The relevant, ALREADY-WORKING pieces you build on — do NOT re-implement them:

- **`shelldon/worker/tools.py`** (read fully) — already has `ToolSpec(name, description, params_schema, tier, fn)` (frozen dataclass, worker-only), `execute_tool(call, registry) -> ToolResult` (catches ALL exceptions from `fn(**call.args)` → `ToolResult(ok=False, content=repr(exc))`; unknown tool → `ok=False`), `build_tool_registry() -> dict[str, ToolSpec]` (currently just `get_time`), and `_get_time`. **9.2 ADDS three tools here and threads a workspace root — nothing else moves.**
- **`execute_tool` calls `spec.fn(**call.args)`** — the model supplies the args as kwargs. So `read_file`'s registered `fn` must accept ONLY `path` (the model's arg); bind `workspace_root` via `functools.partial`/closure at registry-build time (the model never supplies it).
- **`execute_tool` is the catch-all** — a tool `fn` that RAISES is already turned into `ToolResult(ok=False, content=repr(exc))`. So your tools should just RAISE on bad input (jail escape, missing file, blocked builtin, timeout); you do NOT need to construct `ToolResult` inside the tools — `execute_tool` does it. This is the AC3/AC4 fail-closed path for free.
- **The worker loop** (`worker/worker.py::_agentic_loop`) already executes FREE tools, appends `ToolResult`, and loops — capped at `_MAX_TOOL_EXECUTIONS = 6` within the 25s budget. **9.2 does NOT touch the loop.**
- **Contracts** (`contracts/__init__.py`) already have `ToolTier.FREE/RISKY`, `ToolCall`, `ToolResult`, `ToolDefinition`, `Message`. **9.2 adds NO contracts** (per the 9.1 Dev Notes: "9.2 only adds tools to `build_tool_registry()` … makes NO changes to contracts/broker/loop").

### Architecture constraints (mandatory — these are the spine invariants)

- **Core stays LLM-free.** `worker/tools.py` is in the `worker/` package, which the `core is LLM-free` import-linter contract does NOT cover — so adding tools here is fine. Add NO imports of `openai`/`anthropic`/etc. The new tools use only stdlib (`pathlib`, `signal`, `functools`, `builtins`).
- **Broker never executes tools (AD-2).** Tools run ONLY in the worker. No broker change in 9.2.
- **Fail-soft discipline.** Every tool RAISES on bad input; `execute_tool` converts to `ToolResult(ok=False)`. The turn NEVER crashes (AC3/AC4).
- **Single-writer (AD-5).** 9.2 is READ-ONLY + pure compute. `read_file`/`list_dir` never write; `python_eval` is side-effect-free. File WRITES are RISKY-tier (`write_file`) and belong to Story 9.3 — do NOT add any write tool here.
- **416MB Pi.** Bound what you read (cap `read_file` bytes) and bound compute time (`python_eval` SIGALRM). 9.5 deepens caps; 9.2 sets sane defaults.

### Path jail — exact pattern to reuse

`core/memory.py:129-134` is the canonical jail in this codebase:
```python
collection_dir = (self._root / op.collection).resolve()
path = (collection_dir / f"{stem}.md").resolve()
if path.parent != collection_dir:
    raise ValueError(...)  # escapes the tree
```
For 9.2 file tools, resolve the candidate and confirm containment (symlinks followed by `.resolve()`):
```python
def _resolve_in_jail(path, workspace_root):
    root = workspace_root.resolve()
    candidate = (root / path).resolve()        # relative paths resolve under root
    if not candidate.is_relative_to(root):     # Python 3.9+; project is 3.13
        raise ValueError(f"path escapes workspace: {path!r}")
    return candidate
```
Note: an ABSOLUTE `path` passed by the model — `(root / abspath)` yields `abspath` (pathlib drops `root` when the right side is absolute), so the `is_relative_to` check still catches it. Test this case explicitly. A symlink inside the workspace pointing OUTSIDE is caught because `.resolve()` dereferences it before the check — test this too.

### `vault/` location (AC4)

`vault/` is `<memory_root>/vault` = `~/.shelldon/memory/vault`, created 0o700 by `core/vault.py::ensure_vault` (OS-denies the dropped worker uid — AD-6). It is a sibling of `memory/`, NOT under `~/.shelldon/workspace`, so it is already outside the jail. AC4 still wants an EXPLICIT refusal (defense in depth, because uid-drop is currently a no-op on the non-root Pi): reject any resolved path under `<memory_root>/vault` or matching `*.env`/`.env`. Pass `memory_root` (default `DEFAULT_MEMORY_ROOT` from `core/memory.py`) into the jail helper or check the filename shape.

### `python_eval` restriction (AC2/AC3)

Restricted-namespace eval is NOT a true sandbox (design §6 accepts this for single-owner; `python_eval` is FREE precisely because it's blocked from side effects). Approach:
- `eval(compile(code, "<python_eval>", "eval"), {"__builtins__": _SAFE_BUILTINS}, {})`. Setting `__builtins__` to a curated dict removes `open`/`__import__`/`exec`/etc. — a snippet doing `open(...)` raises `NameError`, `import os` raises (no `__import__`), both → `ToolResult(ok=False)`.
- `_SAFE_BUILTINS`: a small allowlist of pure builtins only (math/seq/type constructors). Do NOT include `open`, `__import__`, `eval`, `exec`, `compile`, `globals`, `locals`, `getattr`, `setattr`, `vars`, `input`.
- Time bound via `signal.signal(signal.SIGALRM, handler)` + `signal.setitimer`/`signal.alarm(int)` (use `setitimer` for sub-second), restored in `finally`. SIGALRM fires in the worker fork child's MAIN thread (it runs `asyncio.run` directly — `forkserver.py:140`), and `execute_tool` runs the tool synchronously, so the alarm interrupts the eval. Caveat: a tight C-level loop may not be interruptible — acceptable for 9.2 (9.5 adds real resource caps); the loop's own 25s ceiling is the backstop.
- Keep `timeout_s` a parameter (default ~2s) so the test can inject a tiny value and stay fast.

### `build_tool_registry` threading (AC5)

Currently `build_tool_registry()` takes no args and `forkserver.py:140-142` calls it bare. Change the signature to `build_tool_registry(workspace_root=None)` defaulting to `DEFAULT_WORKSPACE_ROOT` — the bare prod call keeps working (default applies), and tests inject a `tmp_path`. The file tools' `fn` binds `workspace_root` (and `memory_root` for the vault check) at build time via `functools.partial`, so `execute_tool`'s `fn(**call.args)` still passes only the model's `path`.

### Workspace dir creation (AC1, prod)

`app.py::run_app` creates the memory tree + vault before any worker forks (`app.py:223 ensure_vault`). Add a sibling line creating `DEFAULT_WORKSPACE_ROOT` with NORMAL perms (`os.makedirs(..., exist_ok=True)` — NO chmod 0o700, unlike vault) so the dropped worker uid can read it. A missing workspace must also fail-soft in the tools (`list_dir` on a missing root → `ToolResult(ok=False)`), so tests that don't run `app.py` still behave.

### Testing pattern

Follow `tests/test_tool_normalizer.py` / the 9.1 `tests/test_free_tools.py`-style pure-unit pattern: build a `tmp_path` workspace, seed files, call the tool `fn` (or `execute_tool` with a `ToolCall`) directly, assert `ToolResult.ok`/`content`. No bus, no LLM. For the optional loop test, reuse `tests/test_tool_loop.py::_run_with_scripted_broker` (scripted fake broker, real bus) and pass a registry built with `build_tool_registry(workspace_root=tmp_path)`.

### No new dependencies

- `pathlib`, `signal`, `functools`, `builtins`, `ast`/`compile` — all stdlib.
- `uv sync --locked` must show 0 changes after 9.2.

### What 9.3 builds next (do NOT pull it in)

Story 9.3 adds the RISKY tier (`write_file`, `run_shell`, `http_get`, `git`) + the 2-phase Telegram approval flow + `RequestToolApproval`. 9.2 ships ONLY the three read-only/pure-compute FREE tools. No writes, no shell, no network, no approval plumbing here.

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (dev-story workflow)

### Debug Log References

- Boundary gate green: `uv run pytest -q` → 593 passed, 3 skipped, 7 deselected (live); `uv run lint-imports` → 3 KEPT, 0 broken; `uv sync --locked` → 0 new deps.

### Completion Notes List

- **Tiny blast radius held.** 9.2 touched only `worker/tools.py` (the tool pack), `app.py` (one workspace-mkdir line + import), and `worker/prompt.py` (one nudge sentence). ZERO changes to `contracts/`, `broker/`, or the worker loop — exactly as the 9.1 dev notes promised. AC5 verified.
- **Fail-closed came free from 9.1.** The new tools just RAISE on bad input (jail escape, missing file, blocked builtin, timeout, sensitive path); 9.1's `execute_tool` catch-all converts every raise to `ToolResult(ok=False, content=repr(exc))`. No `ToolResult` construction inside the tools.
- **`python_eval` decision.** Kept EXPRESSION-ONLY (`compile(..., "eval")`) per the story baseline — a multi-statement snippet is a `SyntaxError` → fail-closed. `import os` is also blocked this way (import is a statement, invalid in eval mode); `__import__('os')` and `open(...)` fail via the restricted `_SAFE_BUILTINS` (NameError). Both AC3 cases covered.
- **Timeout test reliability.** SIGALRM only interrupts at Python bytecode boundaries, so a tight C loop (`sum(range(10**12))`) wouldn't be interruptible and would be slow. The test uses an interruptible genexpr (`sum(1 for _ in range(10**9))`, O(1) memory, Python bytecode per item) with a 0.1s injected timeout — fast and deterministic. Documented the C-loop caveat (9.5 deepens resource caps; the 25s loop ceiling backstops).
- **`vault/` denial is defense-in-depth.** Vault lives at `~/.shelldon/memory/vault`, structurally outside `~/.shelldon/workspace`, so the jail already blocks it. The explicit `_deny_sensitive` check matters only if the workspace overlaps the memory root (and because uid-drop is a no-op on the non-root Pi) — tested by pointing both roots at one dir.
- **No new deps:** `pathlib`/`signal`/`functools`/`builtins` are stdlib. `uv sync --locked` clean.
- **9.3 NOT pulled in:** no write/shell/network tools, no approval plumbing — those are the RISKY tier (Story 9.3).

### File List

- shelldon/worker/tools.py (UPDATE — DEFAULT_WORKSPACE_ROOT, _MAX_READ_BYTES, _SAFE_BUILTINS, _resolve_in_jail, _deny_sensitive, _read_file, _list_dir, _python_eval, build_tool_registry signature + 3 new tools)
- shelldon/app.py (UPDATE — import DEFAULT_WORKSPACE_ROOT + os.makedirs workspace at startup)
- shelldon/worker/prompt.py (UPDATE — tools line names read_file/list_dir/python_eval)
- tests/test_free_tools.py (NEW — 17 tests)
- tests/test_tool_loop.py (UPDATE — FREE-pack loop integration test)

### Review Findings — Round 2 (2026-06-21, adversarial 3-layer)

- [x] [Review][Decision→A] **`python_eval` dunder/MRO sandbox escape** — RESOLVED with option **[A]**: `_python_eval` now AST-parses the snippet and `_assert_eval_safe` rejects (i) any `Attribute` whose name starts with `_` (all dunders/privates — kills `().__class__.__mro__[-1].__subclasses__()`), (ii) any dunder `Name` (`__builtins__`/`__import__`), and (iii) the `format`/`format_map` methods + dropped the `format` builtin (closes the `"{0.__class__}".format(x)` getattr vector). Normal compute (`'h'.upper()`, `sum(1 for _ in range(n))`) still works. NOT a true sandbox (design §6) — this makes the common, model-likely escapes fail-closed; hard isolation stays RISKY-tier/9.5. Tests: `test_python_eval_blocks_dunder_mro_escape`, `_blocks_format_getattr_escape`, `_allows_normal_method_calls`. [shelldon/worker/tools.py:_assert_eval_safe]
- [x] [Review][Patch] **SIGALRM `finally` disarm order** — FIXED, but NOT by the literal swap suggested (restoring `previous` while the timer is still armed risks a SIGALRM firing into the default handler, whose default action TERMINATES the process). Instead: disarm first, then restore inside a NESTED `finally` so the handler is ALWAYS restored even if a late tick fires during teardown — no stale `_on_timeout` is ever left installed. Test: `test_python_eval_restores_sigalrm_handler_after_run_and_timeout`. [shelldon/worker/tools.py:_python_eval]
- [x] [Review][Patch] **`execute_tool` log drops exception message** — FIXED: now logs `type(exc).__name__` AND `exc`. [shelldon/worker/tools.py:execute_tool]
- [x] [Review][Defer] **Credential file blocklist gaps** — `.env.bak`, `.env.backup`, `.env.old`, `.pem`, `.key`, `id_rsa` etc. pass the `_deny_sensitive` check. Structural jail + vault-outside-workspace is the real defense; broader credential patterns → Story 9.5. [`shelldon/worker/tools.py:_deny_sensitive`] — deferred
- [x] [Review][Defer] **Memory tree readable if workspace overlaps memory_root outside vault/** — facts/people/prefs files under memory_root but outside vault/ are accessible if workspace_root == memory_root. DEFAULT paths keep them separate structurally; misconfiguration needed to trigger. [`shelldon/worker/tools.py:_deny_sensitive`] — deferred
- [x] [Review][Defer→done] **Missing test: `list_dir("")` / workspace root path** — ADDRESSED: added `test_list_dir_empty_string_lists_root`. [`tests/test_free_tools.py`]
- [x] [Review][Defer→done] **Missing test: SIGALRM handler restored after timeout** — ADDRESSED: added `test_python_eval_restores_sigalrm_handler_after_run_and_timeout`. [`tests/test_free_tools.py`]
- [x] [Review][Defer] **`_list_dir` TOCTOU between `iterdir()` and `is_dir()` per entry** — file could change between calls; harmless on single-owner workspace. [`shelldon/worker/tools.py:_list_dir`] — deferred, single-owner mitigates

### Review Findings — Round 1 (2026-06-21)

Code review (3 adversarial layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor). Acceptance Auditor: all 5 ACs PASS. 3 patches applied, 2 deferred, 3 dismissed.

Patches applied (verified, suite green):
- [x] [Review][Patch] **Argument injection (HIGH)** — `execute_tool` did `fn(**call.args)` with fully model-controlled args, letting the model inject a tool's privately-bound kwargs (`workspace_root`/`memory_root` jail roots, `python_eval`'s `timeout_s`) and override the safety binding (verified: `timeout_s` override returned ok). FIX: `execute_tool` now filters `call.args` to the tool's declared `params_schema` properties before calling `fn` — undeclared keys are dropped, sealing the binding. [shelldon/worker/tools.py:execute_tool]
- [x] [Review][Patch] **`python_eval` output uncapped (MED)** — a big result (`'x'*10**7` → 10MB ToolResult) could bloat the bus/messages on the 416MB Pi (verified). FIX: cap the result string at `_MAX_EVAL_OUTPUT_CHARS = 16KB` with a truncation marker (mirrors `read_file`'s cap). [shelldon/worker/tools.py:_python_eval]
- [x] [Review][Patch] **`.env` denial case-sensitive (LOW-MED)** — `.ENV`/`config.ENV` bypassed the credential-shape check. FIX: case-insensitive (`.lower()`) name/suffix check. [shelldon/worker/tools.py:_deny_sensitive]

Deferred:
- [x] [Review][Defer] **`python_eval` CPU/true-memory caps for C-level ops (MED)** — SIGALRM can't interrupt a tight C call (`bytearray(10**10)`, `pow(10**8,10**8)`); a real bound needs RLIMIT. Explicitly Story 9.5's scope ("resource caps: python_eval/run_shell get CPU/time/memory bounds"); the Dev Notes already state the time bound is best-effort. The output cap above mitigates the bus-bloat half. [shelldon/worker/tools.py:_python_eval → Story 9.5]
- [x] [Review][Defer] **`memory_root` not threaded to `build_tool_registry` in prod (LOW)** — `forkserver.py` calls it bare, so `_deny_sensitive` always validates against `DEFAULT_MEMORY_ROOT`, not a custom `run_app(memory_root=...)`. Prod uses the default and the vault check is defense-in-depth on top of the structural jail, so prod is correct; only a non-default-root deployment would point the vault check at the wrong dir. [shelldon/worker/forkserver.py]

Dismissed (3): SIGALRM off-main-thread crash (the fork-child worker runs the loop synchronously in its main thread — prod-safe — and fails closed otherwise); resolve-then-open TOCTOU in `read_file` (single synchronous writer = the model; nil window); `.env/` directory-side gap (`.env/secrets` — implausible layout, and `vault/` is the real secret store, structurally jailed out).

### Change Log

- 2026-06-21: Implemented Story 9.2 free-tier tool pack — `read_file`/`list_dir` (workspace path-jail + vault/.env denial) and `python_eval` (restricted-namespace, SIGALRM time-bound), all FREE-tier in the 9.1 loop. Added to `worker/tools.py` only (+ app.py workspace-create, prompt nudge). 18 new tests; 593 pass / 3 import-linter contracts KEPT / 0 new deps.
- 2026-06-21: Addressed code review — 3 patches applied (arg-injection seal in execute_tool, python_eval output cap, case-insensitive .env denial), 2 deferred (python_eval RLIMIT caps → 9.5; memory_root threading), 3 dismissed. +3 regression tests; 596 pass / 3 contracts KEPT / 0 new deps.
- 2026-06-21: Addressed code review ROUND 2 — `python_eval` hardened against the dunder/MRO + `str.format` sandbox escapes (AST guard `_assert_eval_safe` blocking underscore attributes / dunder names / format methods, `format` builtin dropped); SIGALRM teardown made stale-handler-proof (disarm-then-restore in a nested finally — not the literal swap, which risks SIG_DFL process kill); `execute_tool` log now includes the exception message; picked up 2 cheap deferred tests (`list_dir("")`, SIGALRM-handler-restored). +5 regression tests; 601 pass / 3 contracts KEPT / 0 new deps. `ast` is stdlib.
