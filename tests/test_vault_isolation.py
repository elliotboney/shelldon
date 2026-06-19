"""Story 4.3 — the REAL OS barrier: a dropped worker uid is kernel-denied vault/.

This is the only test that proves AC1's load-bearing claim — that the denial comes
from the OS, not an app-level path filter. It needs Linux + root (or CAP_SETUID) to
actually `setuid` to another uid, exactly like the Linux-gated real-fork test
(test_forkserver_fork.py). On the macOS dev box / unprivileged CI it is SKIPPED with
a logged reason — never faked green. The mechanism, perms, and broker authority are
proven cross-platform in test_forkserver_privdrop.py / test_vault_perms.py /
test_broker_vault_authority.py; this is the Pi/Linux-only kernel-denial proof.
"""

import os
import sys

import pytest

from shelldon.core.vault import ensure_vault

_ROOT_ON_LINUX = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0

pytestmark = pytest.mark.skipif(
    not _ROOT_ON_LINUX,
    reason="real uid-denial needs Linux + root (CAP_SETUID); mechanism is proven cross-platform elsewhere",
)


def test_dropped_worker_uid_is_kernel_denied_vault(tmp_path):
    """Fork a child, drop it to `nobody`, and assert reading a 0700 vault/ owned by
    the service uid (root) raises PermissionError FROM THE KERNEL — not from any
    app check (the child reads the file directly)."""
    import pwd

    from shelldon.worker.forkserver import _real_drop

    nobody = pwd.getpwnam("nobody")
    vault = ensure_vault(tmp_path / "memory")  # created 0o700, owned by root (this euid)
    secret = vault / "secret.md"
    secret.write_text("the owner's secret")

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child — drop to nobody, then try to read the vault
        os.close(read_fd)
        try:
            _real_drop(nobody.pw_uid, nobody.pw_gid)  # gid-then-uid + verify (real setuid)
            try:
                secret.read_text()
                msg = b"READ"  # leaked! the barrier failed
            except PermissionError:
                msg = b"DENIED"  # kernel refused — the OS is the barrier (AC1)
            except OSError as exc:
                msg = f"OTHER:{exc.errno}".encode()
        except Exception as exc:  # pragma: no cover - drop itself failed
            msg = f"DROPFAIL:{exc}".encode()
        os.write(write_fd, msg)
        os.close(write_fd)
        os._exit(0)

    # parent
    os.close(write_fd)
    report = os.read(read_fd, 256)
    os.close(read_fd)
    os.waitpid(pid, 0)
    assert report == b"DENIED", f"expected kernel PermissionError, got {report!r}"
