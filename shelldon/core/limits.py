"""core/limits — RLIMIT resource caps for the worker fork + the subprocesses it/core spawn
(Epic 9, Story 9.5 AC2; AD-3/AD-8/AD-1).

The 416MB Pi must not be OOM'd or CPU-pegged by a tool. The per-turn worker is an ephemeral
fork (AD-3), so capping its `RLIMIT_AS` (address space) + `RLIMIT_CPU` bounds EVERYTHING it runs
this turn — `python_eval`, a FREE self-coded tool, the loop — and the fork dies each turn, so the
cap is per-turn-clean. The RISKY `run_shell`/`git` runner (worker) and the gate `pytest`
subprocess (core) set the same caps via a `preexec_fn` so a spawned child can't escape them.

Layering: `python_eval`'s SIGALRM wall-bound (Story 9.2) still trips at Python-bytecode
boundaries; RLIMIT is the C-level/memory backstop SIGALRM can't give (a tight C call like
`bytearray(10**10)` never yields to SIGALRM). The systemd `MemoryMax=400M` cgroup (Story 8.4) is
the hard PHYSICAL backstop; `RLIMIT_AS` here catches a gross in-process allocation EARLY with a
clean `MemoryError`/`SIGSEGV`-free failure the loop can degrade on.

LLM-free (AD-1): imports only stdlib `resource`. Lives in `core/` so both `worker/` (the fork +
`run_shell`) and `core/selfcode` (the gate) import ONE helper (worker→core / core→core are fine).

Linux is the enforcement target (the worker fork is Linux-only in practice — macOS aborts
fork-without-exec). Each setrlimit is guarded: a platform without a given RLIMIT, or a value over
the hard limit, logs + continues rather than aborting the turn.
"""

import logging

log = logging.getLogger("shelldon.core.limits")

#: Default address-space cap (bytes). Generous on purpose — well above the worker's realistic VMS
#: (~244MB RSS peak on the Pi, Story 8.1) so it never false-kills normal operation, but low enough
#: to stop a gross runaway (`'x'*10**10`, a multi-GB allocation) with a clean MemoryError. The
#: systemd MemoryMax=400M cgroup is the hard physical backstop; this is the early in-process catch.
DEFAULT_RLIMIT_AS_BYTES = 1024 * 1024 * 1024  # 1 GiB

#: Default CPU-seconds cap. The worker turn is mostly I/O-bound (waiting on the broker), so legit
#: work uses little CPU; an infinite/heavy compute loop (a runaway self-coded tool or python_eval)
#: burns CPU and trips SIGXCPU here. Sized above the 25s loop ceiling (wall) with slack — CPU time
#: ≤ wall time for a single-threaded worker, so 30 CPU-seconds never cuts a legit turn short.
DEFAULT_RLIMIT_CPU_S = 30


def apply_resource_caps(
    *, as_bytes: int = DEFAULT_RLIMIT_AS_BYTES, cpu_seconds: int = DEFAULT_RLIMIT_CPU_S, setrlimit=None
) -> None:
    """Set `RLIMIT_AS` + `RLIMIT_CPU` on the CURRENT process (call in the fork child, or in a
    subprocess `preexec_fn`). Each is guarded: an unsupported RLIMIT or an over-hard-limit value
    logs + continues (never aborts). `setrlimit` is injectable for tests (default: real `resource`).
    The soft limit is set to the requested value but never above the existing HARD limit."""
    import resource  # stdlib; imported lazily so a non-resource platform never hard-fails at import

    _set = setrlimit if setrlimit is not None else resource.setrlimit
    for name, limit in (("RLIMIT_AS", as_bytes), ("RLIMIT_CPU", cpu_seconds)):
        which = getattr(resource, name, None)
        if which is None:  # platform without this RLIMIT
            continue
        try:
            # Don't raise the soft limit above the inherited hard limit (would raise ValueError).
            _soft, hard = resource.getrlimit(which)
            soft = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            _set(which, (soft, hard))
        except (ValueError, OSError) as exc:
            log.warning("could not set %s=%s (%s); continuing without it", name, limit, exc)


def resource_cap_preexec(*, as_bytes: int = DEFAULT_RLIMIT_AS_BYTES, cpu_seconds: int = DEFAULT_RLIMIT_CPU_S):
    """Return a `preexec_fn` for `subprocess`/`asyncio.create_subprocess_exec` that applies the
    caps in the spawned CHILD (after fork, before exec) — so `run_shell`/`git`/the gate `pytest`
    can't escape the worker's bound. `preexec_fn` is POSIX-only; on a platform without it the
    caller simply passes None (the worker-fork cap still covers in-process tools)."""
    def _preexec() -> None:  # pragma: no cover - runs in the spawned child, after fork
        apply_resource_caps(as_bytes=as_bytes, cpu_seconds=cpu_seconds)

    return _preexec
