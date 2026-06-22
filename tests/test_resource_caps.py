"""Story 9.5 (AC2): RLIMIT resource caps on the worker fork + the subprocesses it/core spawn.

Asserts the limits are SET (inject a recording `setrlimit` / inspect the `preexec_fn` kwarg) —
NOT the OOM/SIGXCPU kill behavior, which is cross-platform-flaky and Linux/Pi-specific.
"""

import resource

import pytest

from shelldon.core import limits


def test_apply_resource_caps_sets_as_and_cpu():
    calls = []
    limits.apply_resource_caps(as_bytes=123_456, cpu_seconds=7,
                               setrlimit=lambda which, lim: calls.append((which, lim)))
    by_which = dict(calls)
    # Every RLIMIT the platform supports was set; the soft limit never exceeds the requested value.
    for name, want in (("RLIMIT_AS", 123_456), ("RLIMIT_CPU", 7)):
        which = getattr(resource, name, None)
        if which is None:
            continue  # platform without this RLIMIT
        assert which in by_which, f"{name} not set"
        soft, _hard = by_which[which]
        assert soft <= want


def test_apply_resource_caps_is_fail_soft_on_error():
    def _boom(which, lim):
        raise ValueError("over hard limit")

    limits.apply_resource_caps(as_bytes=1, cpu_seconds=1, setrlimit=_boom)  # logs + continues, no raise


def test_resource_cap_preexec_returns_callable():
    fn = limits.resource_cap_preexec(as_bytes=10_000, cpu_seconds=3)
    assert callable(fn)


def test_run_subprocess_passes_a_preexec_fn(tmp_path, monkeypatch):
    from shelldon.worker import tools

    captured = {}

    class _FakePopen:  # Story 9.6 moved _run_subprocess from subprocess.run to Popen
        pid = 1234
        returncode = 0

        def __init__(self, argv, **kw):
            captured.update(kw)

        def communicate(self, timeout=None):
            return ("ok", "")

    monkeypatch.setattr(tools.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools.os, "killpg", lambda pgid, sig: None)
    tools._run_subprocess(["echo", "hi"], cwd=tmp_path)
    assert callable(captured.get("preexec_fn"))  # 9.5 RLIMIT preexec still wired


async def test_run_gate_passes_a_preexec_fn(tmp_path, monkeypatch):
    from shelldon.core import selfcode

    _GOOD = "DESCRIPTION='x'\nPARAMS_SCHEMA={}\ndef run():\n    return 'x'\n"
    selfcode.stage("ok", _GOOD, "def test_x():\n    pass\n", workspace_root=tmp_path)
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"passed", b"")

        async def wait(self):
            return 0

    async def fake_exec(*a, **kw):
        captured.update(kw)
        return FakeProc()

    monkeypatch.setattr(selfcode.asyncio, "create_subprocess_exec", fake_exec)
    passed, _ = await selfcode.run_gate("ok", workspace_root=tmp_path)
    assert passed and callable(captured.get("preexec_fn"))
