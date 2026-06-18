"""Story 4.3 Task 2: worker uid/gid privilege-drop seam (AD-3 fork child / AD-6 vault).

All cross-platform — no real setuid/fork. The os calls are injected so the
gid-then-uid ordering and the fail-closed verify are unit-testable on macOS.
"""

import logging

import pytest

from shelldon.worker.forkserver import _maybe_drop_privileges, _real_drop


def test_real_drop_sets_gid_before_uid():
    """gid MUST drop before uid — you cannot setgid once uid is dropped."""
    calls = []

    def fake_setgid(gid):
        calls.append(("setgid", gid))

    def fake_setuid(uid):
        calls.append(("setuid", uid))

    _real_drop(
        1001,
        2002,
        setgid=fake_setgid,
        setuid=fake_setuid,
        getuid=lambda: 1001,
    )

    assert [name for name, _ in calls] == ["setgid", "setuid"]
    assert calls == [("setgid", 2002), ("setuid", 1001)]


def test_real_drop_fail_closed_when_uid_not_dropped():
    """Post-drop verify: if getuid() != target uid, raise (never run un-dropped)."""
    with pytest.raises(RuntimeError):
        _real_drop(
            1001,
            2002,
            setgid=lambda gid: None,
            setuid=lambda uid: None,
            getuid=lambda: 0,  # still root → drop did not take
        )


def test_maybe_drop_noop_when_unconfigured():
    """worker_uid None (dev / no isolation requested) → drop never called."""
    called = []

    _maybe_drop_privileges(
        None,
        None,
        drop=lambda uid, gid: called.append((uid, gid)),
        geteuid=lambda: 0,
    )

    assert called == []


def test_maybe_drop_noop_and_warns_when_unprivileged(caplog):
    """Configured but euid != 0 (can't drop) → dev-mode no-op, exactly one warning."""
    called = []

    with caplog.at_level(logging.WARNING, logger="shelldon.forkserver"):
        _maybe_drop_privileges(
            1001,
            2002,
            drop=lambda uid, gid: called.append((uid, gid)),
            geteuid=lambda: 1000,  # unprivileged
        )

    assert called == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "vault isolation OFF" in warnings[0].getMessage()


def test_maybe_drop_calls_drop_when_configured_and_privileged():
    """Configured AND euid 0 → drop(uid, gid) exactly once."""
    called = []

    _maybe_drop_privileges(
        1001,
        2002,
        drop=lambda uid, gid: called.append((uid, gid)),
        geteuid=lambda: 0,
    )

    assert called == [(1001, 2002)]


def test_maybe_drop_propagates_when_drop_fails():
    """Fail-closed: a raised drop propagates so the requested turn never runs."""
    def boom(uid, gid):
        raise RuntimeError("setuid failed")

    with pytest.raises(RuntimeError):
        _maybe_drop_privileges(1001, 2002, drop=boom, geteuid=lambda: 0)


def test_maybe_drop_fail_closed_when_uid_set_but_gid_none():
    """uid configured without a gid is fail-closed: raise rather than drop(uid, None),
    which would TypeError in the child and be swallowed by os._exit."""
    called = []

    with pytest.raises(RuntimeError, match="without worker_gid"):
        _maybe_drop_privileges(1001, None, drop=lambda uid, gid: called.append((uid, gid)), geteuid=lambda: 0)

    assert called == []
