---
baseline_commit: d3e080f
---

# Story 4.3: Vault with OS-level isolation

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want sensitive memory the pet can't leak even if its brain is manipulated,
so that a prompt-injected worker can't read or surface my secrets — the OS, not a self-policed path filter, is the barrier (CAP, AD-2, AD-3, AD-6, AD-5).

## Acceptance Criteria

1. **Workers run under a less-privileged uid; `vault/` permissions OS-deny that uid (real separation, not a path filter):** Given the production composition root launches core + the fork-server + broker as real processes, when the fork-server forks a worker, then the worker **drops to a configured less-privileged uid/gid** (`setgid` then `setuid`, after `os.fork()`, before running the turn) and `~/.shelldon/memory/vault/` is created with **owner-only perms (0700, owned by the service uid)** so the worker uid **physically cannot read it** — a worker read of `vault/` raises `PermissionError` from the kernel, not from any app-level check. The drop is **configurable** (a worker uid/gid setting); when unset or the process is unprivileged (dev box), it is a **no-op with a clear warning** (dev mode = no isolation), never a crash.
2. **Surfacing vault content is broker-gated — the broker is the sole authority that can read it; the worker has no read path at all:** Given the worker is vault-blind (OS-denied), when vault content is needed, then **only the broker** (privileged, runs as the service uid alongside core) holds the authorized vault-read path — a `surface_vault` authority seam in `broker/` — and the worker has **no code path and no OS permission** to read `vault/`. *(Injecting the surfaced content into an actual prompt is **Story 4.4** — 4.3 builds the OS barrier + the broker's authority; 4.4 wires the worker's surface-request + the broker's injection at egress, since the prompt-assembly layer is 4.4's. 4.3 proves the worker can't read vault and the broker can.)*
3. **The production multi-process app root exists and wires the privilege model:** Given there was no composition root (everything ran in-process), when `shelldon/app.py` (runnable via `python -m shelldon`) starts, then it launches the actors as **real OS processes** — core (owns the bus + fork-server, service uid), broker (service uid, AD-2), display + transport — creates the memory tree incl. `vault/` with correct perms, and configures the fork-server with the worker uid/gid so forked workers drop privilege. Running unprivileged (dev) degrades to same-uid with a warning; the existing in-process test harness still works unchanged (the app root is additive, not a rewrite).

> **Scope seam (binding):** 4.3 builds the **OS-isolation substrate + the production process model**: the `shelldon/app.py` composition root launching real processes, the worker uid/gid drop in the fork-server child, `vault/` created owner-only (0700), and the broker's `surface_vault` **authority seam** (broker can read vault; worker can't — OS-enforced). It does **NOT** build: **injecting surfaced vault content into a prompt** — Story 4.4 (no prompt-assembly layer exists yet; 4.3 proves the barrier + the authority, 4.4 wires the request→inject path at egress); **writing vault content** — vault *promotions* come from the Epic 6 dream cycle (core sole writer, AD-5); 4.3 only creates the dir + perms; **a worker sandbox / seccomp / tool-call validation** — out of scope (4.3 is filesystem isolation only); **process supervision / systemd units / auto-restart** — the app root launches + tears down cleanly; full supervision is deploy-time. The single biggest mistake is building 4.4's prompt-injection or over-building a sandbox here.

> **⚠️ Reality & testability (binding — read before coding):** real two-uid enforcement requires **Linux + privilege** (root or `CAP_SETUID`). The dev box is **macOS**, which cannot safely `fork()`-without-exec and cannot `setuid` to another uid without root — exactly like the existing Linux-gated `test_real_fork_rss_stays_flat`. Therefore: **mechanism + perms are unit-tested on any platform** (vault mode bits, config parsing, that the child *invokes* the drop in the right order via an injected seam, the no-op+warn path); **real uid denial is an opt-in Linux+root integration test** (`@pytest.mark.skipif` not root/Linux) that actually forks a worker dropping to `nobody` and asserts `PermissionError` on a `vault/` read. CI on macOS exercises the mechanism; the Pi/Linux exercises the real barrier. Do not fake a green "isolation works" on macOS — gate honestly and `log`/skip what isn't run.

## Tasks / Subtasks

> **What exists today (reuse, don't reinvent) — verified against the code:**
> - **There is NO production app root yet.** All five actors run as in-process asyncio tasks in tests (`test_endurance_soak.py`, `test_end_to_end_turn.py` build the harness by hand). `core/runtime.py`'s own docstring flags the gap: "the composition root (the integration test, or a later `app.py`) injects the real `ForkServer`." 4.3 builds that `app.py`. [Source: shelldon/core/runtime.py:9-13, tests/test_end_to_end_turn.py:build_harness]
> - **The fork-server child is where the uid-drop goes.** `forkserver.py:_os_fork_spawn` does `pid = os.fork(); if pid == 0: asyncio.run(run_worker(...))` — the drop (`os.setgid` then `os.setuid`) goes in the child branch BEFORE `asyncio.run`, so the privileged parent never elevates. The `spawn`/`reap` seams already exist for testability; add the drop as an injectable seam too. macOS aborts real fork-without-exec, so this path is already Linux-gated in practice. [Source: shelldon/worker/forkserver.py:23-34, :52-56]
> - **`vault/` does not exist and NO permission bits are set anywhere.** `core/memory.py` creates `about.md`/`facts/`/`people/`/`episodes.md` via `_atomic_write_text` (which only `mkdir(parents=True)` + umask — no `mode=`). 4.3 adds explicit `vault/` creation at `0o700`. The closed `_COLLECTIONS = ("facts","people")` is the memory-op target set — `vault/` is NOT a memory-op target (it's dream-promotion-only, Epic 6), so do not add it there. [Source: shelldon/core/memory.py:_atomic_write_text, :_COLLECTIONS, :CuratedMemory]
> - **The broker is already "a separate process and the only holder of credentials + safety policy" (AD-2).** It runs as its own bus client (`run_broker`). Giving it the sole vault-read authority fits AD-2 (broker = safety boundary at egress). The broker does NOT parse pet-domain ops (AD-2 / Story 4.5) — the authority seam is a read+authorize gate, not a parser. [Source: shelldon/broker/service.py, ARCHITECTURE-SPINE.md#AD-2]
> - **Config is env-var driven** (the broker reads provider creds from env; `PROVIDER_CHAIN` etc.). Add the worker uid/gid as env settings (e.g. `SHELLDON_WORKER_UID`/`SHELLDON_WORKER_GID` or `SHELLDON_WORKER_USER`), resolved in the app root — no new config framework. [Source: README.md (PROVIDER_CHAIN/env config), shelldon/broker/* providers]
> - **The bus is core-resident; CORE-bound traffic is in-process, every other actor is a UDS client.** A multi-process app root launches core (hub) first, then connects broker/display/transport as UDS clients on `~/.shelldon/bus.sock` (the real `bus_socket_path()`), exactly as the in-process harness does but across processes. [Source: shelldon/core/bus/server.py:bus_socket_path, _route]

- [x] **Task 1: Production composition root `shelldon/app.py` (+ `python -m shelldon`)** (AC: 3)
  - [x] Create `shelldon/app.py` with a composition root that, on a privileged Linux host, launches the actors as **real OS processes**: core (starts the bus + owns the fork-server) and the broker as separate processes (AD-2), plus display + transport. Add `shelldon/__main__.py` so `python -m shelldon` runs it. Resolve the socket path via `bus_socket_path()`.
  - [x] Wire teardown: a signal handler (SIGINT/SIGTERM) that stops the bus and reaps child processes cleanly (no orphaned workers). Keep it minimal — full supervision/systemd is deploy-time, out of scope.
  - [x] The existing in-process test harness must keep working unchanged — `app.py` is an ADD (the production wiring), not a refactor of `Core`/`run_broker`/`ForkServer`.

- [x] **Task 2: Privilege model — worker uid/gid drop in the fork-server child** (AC: 1, 3)
  - [x] Add a configurable worker identity (env: `SHELLDON_WORKER_UID`/`SHELLDON_WORKER_GID`, or resolve a `SHELLDON_WORKER_USER` via `pwd`). Resolve it in `app.py` and pass it into `ForkServer`.
  - [x] In `forkserver.py`, add an injectable `drop_privileges` seam (default: a real `_real_drop(uid, gid)` that calls `os.setgid(gid)` **then** `os.setuid(uid)` — gid first, since after dropping uid you can't change gid). Call it in the `_os_fork_spawn` child branch BEFORE `asyncio.run(run_worker(...))`. When no worker identity is configured OR `os.geteuid() != 0` (unprivileged), the drop is a **no-op + a single WARNING** ("running workers same-uid; vault isolation OFF — dev mode"), never a crash.
  - [x] **Order + safety:** drop gid before uid; verify the drop took (`os.getuid() == uid` after) and refuse to run the turn if a configured drop silently failed (fail-closed — never run a turn still-privileged when isolation was requested).

- [x] **Task 3: `vault/` directory with owner-only perms** (AC: 1)
  - [x] On startup (app root, running as the service uid), ensure `~/.shelldon/memory/vault/` exists with mode **`0o700`** owned by the service uid (so the dropped worker uid has no read/traverse). Add a small `ensure_vault(root)` (in `core/vault.py`) — idempotent, sets the mode explicitly (`os.makedirs(..., mode=0o700)` + an explicit `os.chmod` to defeat umask). Do NOT make `vault/` a `MemoryOp`/`Remember` target (it is not owner/worker-writable via ops; dream-promotion writes it in Epic 6).
  - [x] Confirm the rest of the memory tree (`about.md`/`facts/`/`people/`) stays worker-READABLE (the worker reads the tree minus vault — AD-6) — only `vault/` is locked down. (memory.py untouched; only `vault/` gets the explicit 0o700.)

- [x] **Task 4: Broker-only `surface_vault` authority seam** (AC: 2)
  - [x] In `broker/`, add the **sole** authorized vault-read path — `surface_vault(root, key) -> str | None` (reads `vault/<key>.md` and returns it) plus an `authorize_surface(key)` decision point. This lives in `broker/` because the broker is the egress/safety authority (AD-2); core/worker do not get a vault-read API. It is NOT wired into a prompt yet (that's 4.4) — it exists + is unit-tested as the gate.
  - [x] **No worker vault API:** assert (in code structure + a test) that nothing in `worker/` can read `vault/` — the worker has neither an API (structural test: `surface_vault.__module__` is the sole `broker.vault`; `worker.*` has no such attribute) nor (on Linux, dropped) the OS permission.

- [x] **Task 5: Tests** (AC: 1, 2, 3)
  - [x] **Mechanism (any platform):** `ensure_vault` creates `vault/` at `0o700` (assert `stat.S_IMODE(...) == 0o700`); the fork-server child invokes `drop_privileges` with the configured `(uid, gid)` in **gid-then-uid order** (recording seam — no real setuid); the no-op+warn path runs when uid unset / `geteuid()!=0`; a configured-but-failed drop is fail-closed (propagates → child `_exit` before any turn).
  - [x] **Broker authority (any platform):** `surface_vault` reads a seeded `vault/secret.md`; there is no equivalent read path in `worker/` (import/structure assertion); the broker authority returns content, a worker-side attempt has no API; traversal keys rejected (no escape).
  - [x] **Real OS denial (Linux + root ONLY — `@pytest.mark.skipif(not (linux and geteuid()==0))`):** forks a worker that drops to `nobody` and reads a 0700 `vault/secret.md`; asserts `PermissionError` from the kernel. Skipped (logged reason) on the macOS dev box — never faked green.
  - [x] **App root smoke (any platform, unprivileged):** `app.py` starts the actors (same-uid dev mode), a turn completes end-to-end, teardown reaps every worker (no orphans) + unlinks the bus socket; the dev-mode warning is emitted. (Reuses the in-process harness patterns; the real multi-process launch is exercised on Linux.)
  - [x] Extend the autouse conftest isolation so no test touches a real `~/.shelldon` vault (redirect `app.DEFAULT_MEMORY_ROOT` too).

- [x] **Task 6: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts KEPT (`core/vault.py` is stdlib-only; the vault-read authority is in `broker/`; `app.py` is top-level, NOT in `core/`).
  - [x] `uv run pytest -q` → green (286 passed, 3 skipped); the Linux+root real-denial + real-fork tests skip on the dev box (logged), the mechanism + perms + broker-authority tests pass everywhere. No network, no real `$HOME`.

## Dev Notes

### Architecture compliance (binding)

- **AD-6 — `vault/` is OS-enforced, not a path filter.** "workers run under a less-privileged uid than core/broker and `vault/` permissions exclude that uid, so a prompt-injected worker physically cannot read it. Surfacing `vault/` contents into a prompt is a broker-gated decision." 4.3 builds exactly the uid-drop + perms + broker authority; the prompt injection is 4.4. [Source: ARCHITECTURE-SPINE.md#AD-6 (line ~100), epics.md#Story 4.3]
- **AD-2 — the broker is the egress/safety authority.** It is "a separate process and the only holder of credentials + safety policy and the only egress." The vault-surfacing gate belongs to the broker (the privileged egress), not the worker (untrusted brain) or core (sole writer, but not the egress decision-maker). The broker still does no pet-domain op parsing (AD-2 / 4.5). [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-3 — fork-server forks one worker per turn; the worker dies after.** The uid-drop happens in the child after `os.fork()`, before the turn — the parent (privileged) never elevates, and the dropped child dies at turn end. ≤1-worker bound (AD-9) and the reap are unchanged. [Source: ARCHITECTURE-SPINE.md#AD-3, shelldon/worker/forkserver.py]
- **AD-5 — core is the sole writer of memory.** 4.3 only *creates* `vault/` (a dir + perms); it writes no vault *content* (dream-cycle promotions do, Epic 6, via core). The worker never writes anything. [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-1 — LLM-free core.** `app.py` (composition root) is NOT in `core/` and may import every actor; the vault-read authority lives in `broker/`. The import-linter LLM-free-`core` contract stays KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1, pyproject.toml#tool.importlinter]

### Design guidance (what to build, minimally)

- **The load-bearing separation is worker-uid < service-uid, with `vault/` owned by the service uid at 0700.** Core + broker run as the SAME service uid (both "privileged" per AD-6 "less-privileged than core/broker"), so both can read `vault/`; the forked worker drops below them and cannot. That single relationship satisfies AC1 — don't over-build per-actor uid matrices.
- **Put `setuid` in the fork child, gid before uid, fail-closed.** `os.setgid` then `os.setuid` (irreversible once uid drops). After the drop, verify `os.getuid()==target`; if a *configured* drop didn't take, refuse the turn (never run privileged when isolation was asked for). Unconfigured/unprivileged → no-op + one warning (dev mode).
- **Gate the real test honestly.** Mechanism/perms/config are testable everywhere; the actual `PermissionError`-from-the-kernel test needs Linux+root — `skipif` it and `log` the skip. Mirror the existing `test_real_fork_rss_stays_flat` Linux gating. A faked macOS "isolation works" is the worst outcome.
- **The broker authority is a gate, not a parser.** `surface_vault`/`authorize_surface` reads a vault file and decides yes/no — it does not interpret LLM output (AD-2). 4.4 calls it at egress; 4.3 just builds + tests it in isolation.
- **`app.py` is additive.** Do not refactor `Core`/`run_broker`/`ForkServer` signatures to fit it; compose the existing pieces. The in-process test harness remains the unit/integration substrate.

### What 4.3 does NOT do

- **No prompt injection of vault content** — Story 4.4 wires the worker's surface-request + the broker's egress-time injection (the prompt-assembly layer is 4.4's). 4.3 proves the barrier + the authority only.
- **No vault content writing** — dream-cycle promotions (Epic 6) write vault via core. 4.3 creates the empty, locked dir.
- **No worker sandbox / seccomp / syscall filter / tool-call validation** — 4.3 is filesystem isolation (uid + mode bits) only.
- **No process supervision / systemd units / restart policy** — `app.py` launches + tears down cleanly; production supervision is deploy-time.
- **No change to the ≤1-worker bound, fencing, the bus, or the 4.5 ops wire.**

### Project Structure Notes

- **New:** `shelldon/app.py` (production composition root), `shelldon/__main__.py` (`python -m shelldon`). Possibly `shelldon/core/vault.py` OR a small `ensure_vault` in `core/memory.py`. A `broker/vault.py` (or a function in `broker/service.py`) for `surface_vault`/`authorize_surface`. New tests `tests/test_vault_isolation.py` (mechanism + perms + broker authority + the Linux-gated real-denial test) and `tests/test_app_root.py` (smoke).
- **Modified:** `shelldon/worker/forkserver.py` (the `drop_privileges` seam + child drop + fail-closed verify); possibly `core/memory.py` (vault creation). Config resolution (env → worker uid/gid) in `app.py`.
- **Boundaries:** the vault-read authority is in `broker/`, the app root is top-level (`shelldon/app.py`), `core/` gains only the (LLM-free) vault dir creation. Import-linter KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1/#AD-2]

### Testing standards

- `pytest` + `pytest-asyncio`. **Three tiers:** (1) mechanism/perms/config — run everywhere (assert `0o700`, gid-then-uid call order via a recording seam, no-op+warn, fail-closed); (2) broker authority — `surface_vault` reads a seeded vault file, no worker-side read path; (3) **real OS denial — `@pytest.mark.skipif(not Linux-or-not-root)`**, actually fork+setuid+attempt read, assert `PermissionError`, log the skip on the dev box. Inject the memory root via the conftest fixture; never touch real `~/.shelldon`. App-root smoke runs unprivileged (dev mode). Before done: `uv run lint-imports` (KEPT) + `uv run pytest -q` (green, with the Linux test skipped + logged). [Source: tests/test_forkserver_fork.py (Linux-gated real-fork precedent), tests/conftest.py]

### Previous story intelligence (Stories 4.2 / 4.5 / 1.5)

- **4.2 built the curated tree + atomic writes** (`CuratedMemory`, `_atomic_write_text`, the conftest `$HOME` redirect). 4.3 adds `vault/` beside `about.md`/`facts/`/`people/` but with explicit `0o700` (the existing writer sets no mode). Extend the conftest vault isolation in the same change (retro #3). [Source: shelldon/core/memory.py, tests/conftest.py]
- **1.5 built the fork-server with the `spawn`/`reap` seams and a Linux-gated real-fork test** — mirror that gating for the real uid-denial test, and add the `drop_privileges` seam the same way. [Source: shelldon/worker/forkserver.py, tests/test_forkserver_fork.py]
- **4.5 made the broker a pure egress boundary** — the vault authority is the broker's safety role (AD-2), but it stays a read+authorize gate, never an op parser. [Source: shelldon/broker/service.py]
- **Recurring review themes to pre-empt:** never fake green (gate the Linux test, log the skip); fail-closed on a requested-but-failed privilege drop; guard inputs (validate the configured uid/gid); never silently swallow (warn loudly in dev mode); value-not-truthiness asserts (`stat.S_IMODE == 0o700`); proactive `$HOME` isolation in the same change. [Source: epic-3-retro, 4-1/4-2/4-5 Review Findings]

### Open questions for the owner (raised, not blocking)

- **This is the largest story in the project** (it introduces the production process model). It could be split — (4.3a) vault dir + perms + worker uid-drop mechanism; (4.3b) the `app.py` multi-process root; (4.3c) the broker authority seam — if a smaller increment is preferred. (Owner chose the combined scope at the 2026-06-18 planning gate.)
- **Real uid enforcement is only verifiable on the Pi (Linux + privilege).** The dev box (macOS) exercises the mechanism + perms + broker authority; the kernel-denial test is `skipif`-gated. Confirm a Linux/root CI lane (or accept Pi-deploy verification) for the real barrier.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 4.3 (this story); #Story 4.2 (curated tree — done); #Story 4.4 (memory shapes the turn — wires vault surfacing into the prompt); #Story 1.5 (fork-server)]
- [Source: ARCHITECTURE-SPINE.md#AD-6 (vault OS-isolation + broker-gated surfacing, line ~100), #AD-2 (broker = separate process, egress/safety authority), #AD-3 (fork-server worker), #AD-5 (core sole writer), #AD-1 (LLM-free core), #State & cross-cutting ("`vault/` OS-unreadable to the worker uid", line ~154)]
- [Source: shelldon/worker/forkserver.py (`_os_fork_spawn` child branch — where setuid goes; spawn/reap seams), shelldon/core/memory.py (`_atomic_write_text`, the tree layout — add `vault/`), shelldon/broker/service.py (the egress process — add the vault authority), shelldon/core/runtime.py:9-13 (the "later app.py" gap), shelldon/core/bus/server.py (`bus_socket_path`, the hub the app root wires)]
- [Source: tests/test_forkserver_fork.py (Linux-gated real-fork test to mirror for real uid-denial), tests/test_end_to_end_turn.py / test_endurance_soak.py (in-process harness the app root composes across processes), tests/conftest.py (autouse `$HOME` isolation to extend for vault)]
- [Source: owner decisions 2026-06-18 (4.3 planning gate): (1) build the OS barrier + broker authority now, defer prompt-injection to 4.4; (2) build the multi-process app root + real uids now]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (composition + integration); 3 parallel python-expert subagents for the independent leaf modules (Tasks 2/3/4).

### Debug Log References

- App-root smoke first failed asserting `asyncio.all_tasks() == []` after teardown — the leak was the display service's internal `_intake`/`_render` loops (a pre-existing `run_display` cancel-path quirk, out of 4.3 scope), NOT an orphaned worker. Re-scoped the assertion to the AC's actual intent: every spawned worker task is `done()`, `worker_in_flight is False`, and the bus socket is unlinked. Green.

### Completion Notes List

- **OS barrier (AC1):** the uid drop lives in the fork child (`forkserver._maybe_drop_privileges`), gid-before-uid, with a fail-closed `getuid()` verify in `_real_drop`. Unconfigured/unprivileged → one WARNING + no-op (dev mode); a configured-but-failed drop propagates so the child `_exit`s before any turn runs. `vault/` is created at an explicit `0o700` (`core/vault.py:ensure_vault`, chmod defeats umask). Real kernel denial is proven by the Linux+root test in `test_vault_isolation.py` (skipped+logged on macOS — never faked green).
- **Broker authority (AC2):** `broker/vault.py:surface_vault` is the sole authorized read, gated by `authorize_surface` (rejects traversal keys). Structural test asserts no `worker/` module exposes a vault-read path; `surface_vault.__module__` is the only exporter. Not wired into a prompt (that's 4.4).
- **Process model (AC3):** `shelldon/app.py` is the additive composition root (`python -m shelldon`). Actor launch is an injectable seam: production `launch_multiprocess` (core+bus+fork-server in the service process; broker/display/transport as spawned children, SIGINT/SIGTERM teardown), test `launch_in_process` (the proven harness). `Core`/`run_broker`/`ForkServer` unchanged.
- **Test-file split (deviation from the story's single `test_vault_isolation.py`):** to parallelize the 3 independent leaf modules without merge conflicts, mechanism/perms/authority tests landed in `test_forkserver_privdrop.py` / `test_vault_perms.py` / `test_broker_vault_authority.py`; `test_vault_isolation.py` holds the cross-concern Linux+root real-denial test; `test_app_root.py` the smoke. Same coverage, more files.
- **Verify:** `lint-imports` 2 kept / 0 broken; `pytest -q` 286 passed, 3 skipped (2 macOS fork gates + 1 Linux-root vault gate), 3 deselected (live-smoke). No network, no real `$HOME`.

### File List

- **New:** `shelldon/app.py`, `shelldon/__main__.py`, `shelldon/core/vault.py`, `shelldon/broker/vault.py`, `tests/test_app_root.py`, `tests/test_vault_isolation.py`, `tests/test_forkserver_privdrop.py`, `tests/test_vault_perms.py`, `tests/test_broker_vault_authority.py`
- **Modified:** `shelldon/worker/forkserver.py` (privilege-drop seam + child drop + fail-closed verify), `tests/conftest.py` (redirect `app.DEFAULT_MEMORY_ROOT` for vault isolation)

## Change Log

| Date       | Change                                                                 |
|------------|------------------------------------------------------------------------|
| 2026-06-18 | Story 4.3 implemented: OS-isolation substrate (worker uid-drop in the fork child, `vault/` at 0o700), broker-only `surface_vault` authority, and the production `shelldon/app.py` composition root. All ACs satisfied; full suite green (real uid-denial Linux+root-gated). Status → review. |
| 2026-06-18 | Addressed code-review findings — 7 [Patch] fixes (uid-0 reject, gid-without-uid raise, gid-None fail-close, `-O`-safe path check, utf-8 read guard, vault-create reorder, single `getuid()`) + 4 [Decision] resolutions (`re.ASCII` keys; per-fork warning accepted + docstring; bus-coupling + multiprocess-launch documented as deploy-verified). 1 finding REJECTED as a false positive (logger name). +7 tests; suite 293 passed / 3 skipped, contracts kept. |

### Review Findings

- [x] [Review][Decision] `_await_bus_up` polls private `core.bus._server` (AC3) — fragile coupling to BusServer internals. **Resolved (B):** documented as accepted internal coupling (the in-process harness already uses the same idiom); a public `BusServer` readiness API noted as a deferred nice-to-have. [shelldon/app.py:_await_bus_up]
- [x] [Review][Decision] "vault isolation OFF" warning fires on every turn fork, not just once. **Resolved (B):** accepted as intentional + docstring corrected. It only fires when isolation was *requested* but the process is unprivileged (a loud, repeated misconfig signal), and a once-flag can't dedupe across forked worker processes anyway (each child starts fresh). [shelldon/worker/forkserver.py:_maybe_drop_privileges]
- [x] [Review][Decision] `launch_multiprocess` entirely `# pragma: no cover`, no Linux-gated integration test (AC3). **Resolved (B):** accepted as Pi/deploy-time verification, consistent with the existing Linux-gated real-fork/real-denial posture (no Linux CI yet). Tracked as a follow-up for when a Linux lane exists. [shelldon/app.py:launch_multiprocess]
- [x] [Review][Decision] `_SAFE_KEY_RE` accepts Unicode word chars. **Resolved (A):** added `re.ASCII` — vault keys are internal identifiers (not human names like memory.py filenames), so ASCII-only is the clearer contract. Added `test_unicode_keys_rejected`. [shelldon/broker/vault.py]
- [x] [Review][Patch] `assert "/" not in slug …` in `surface_vault` is disabled by `-O` — replaced with an explicit `if "/" in slug or ".." in slug: return None`. [shelldon/broker/vault.py]
- [x] ~~[Review][Patch] Wrong logger name in test~~ — **REJECTED (false positive).** The actual logger is `getLogger("shelldon.forkserver")` (forkserver.py:16), which the test matches; and caplog captures via the root handler regardless of the `logger=` filter, so the `len(warnings) == 1` assertion is non-vacuous. Verified by re-reading the code + test. [tests/test_forkserver_privdrop.py:66]
- [x] [Review][Patch] `ensure_vault()` before `resolve_worker_identity()` in `run_app` — reordered so a config error raises before the vault dir side effect. [shelldon/app.py:run_app]
- [x] [Review][Patch] `resolve_worker_identity` accepted `SHELLDON_WORKER_UID=0` — now rejects uid 0 (both the UID and USER paths) with RuntimeError. Added `test_uid_zero_root_rejected`. [shelldon/app.py:resolve_worker_identity]
- [x] [Review][Patch] `SHELLDON_WORKER_GID` without `SHELLDON_WORKER_UID` was silently ignored — now raises. Added `test_gid_without_uid_fails_fast`. [shelldon/app.py:resolve_worker_identity]
- [x] [Review][Patch] `_maybe_drop_privileges` now fail-closes when `worker_uid` is set but `worker_gid is None` (raises rather than `drop(uid, None)` → swallowed TypeError). Added `test_maybe_drop_fail_closed_when_uid_set_but_gid_none`. [shelldon/worker/forkserver.py:_maybe_drop_privileges]
- [x] [Review][Patch] `surface_vault` now reads with `encoding="utf-8"` and returns None (logged) on `UnicodeDecodeError`/`OSError` instead of raising into egress. Added `test_non_utf8_content_surfaces_none`. [shelldon/broker/vault.py]
- [x] [Review][Patch] `_real_drop` now captures `getuid()` once (`landed`) before the raise. [shelldon/worker/forkserver.py]
- [x] [Review][Defer] `_os_fork_spawn` has `drop=_real_drop` as a definition-time default — callers bypassing `ForkServer._default_spawn` skip the injected drop, but `_os_fork_spawn` is a private seam; `_default_spawn` routes correctly [shelldon/worker/forkserver.py] — deferred, pre-existing design
- [x] [Review][Defer] `os.fork()` OSError (ENOMEM/EAGAIN) not caught in `_os_fork_spawn` [shelldon/worker/forkserver.py] — deferred, pre-existing
- [x] [Review][Defer] `ensure_vault` raises `NotADirectoryError` with no context if a path component is a file — pre-existing concern for the whole memory tree [shelldon/core/vault.py] — deferred, pre-existing
- [x] [Review][Defer] `launch_multiprocess` mid-loop `child.start()` failure leaves already-started children running — production deployment concern; `# pragma: no cover` path [shelldon/app.py] — deferred, pre-existing
- [x] [Review][Defer] `child.join(timeout=5.0)` silently returns with child still alive — zombie/runaway processes; production deployment concern [shelldon/app.py] — deferred, pre-existing
- [x] [Review][Defer] `forkserver.preload()` raises after `ensure_vault` with no cleanup path — startup failure propagates cleanly; GC handles the partially initialized state [shelldon/app.py] — deferred, pre-existing
- [x] [Review][Defer] `ensure_vault` chmod no-ops if dir is owned by a different user — service always creates and owns `vault/`; edge case only in unusual multi-instance deployments [shelldon/core/vault.py] — deferred, pre-existing
