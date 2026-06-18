"""Story 4.3 Task 3 — the vault dir is created locked to mode 0o700 (AD-6).

Asserts: `ensure_vault` creates `<root>/vault/` owner-only (0o700), is idempotent, and
DEFEATS the process umask in both directions (a tight 0o077 and a wide 0o000) — proving
the explicit chmod, not luck. Every test uses an injected tmp_path root, never real $HOME.
"""

import os
import stat

from shelldon.core.vault import ensure_vault


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_ensure_vault_creates_owner_only_dir(tmp_path):
    """Creates `<root>/vault/` at exactly 0o700."""
    root = tmp_path / "memory"
    p = ensure_vault(root)
    assert p == root / "vault"
    assert p.is_dir()
    assert _mode(p) == 0o700


def test_ensure_vault_is_idempotent(tmp_path):
    """A second call doesn't raise and the mode stays exactly 0o700."""
    root = tmp_path / "memory"
    p1 = ensure_vault(root)
    p2 = ensure_vault(root)
    assert p1 == p2
    assert _mode(p2) == 0o700


def test_ensure_vault_defeats_tight_umask(tmp_path):
    """A tight 0o077 umask would mask makedirs' mode — the explicit chmod wins anyway."""
    old = os.umask(0o077)
    try:
        p = ensure_vault(tmp_path / "memory")
        assert _mode(p) == 0o700
    finally:
        os.umask(old)


def test_ensure_vault_defeats_wide_umask(tmp_path):
    """A wide 0o000 umask would leave makedirs world-open — the explicit chmod tightens it."""
    old = os.umask(0)
    try:
        p = ensure_vault(tmp_path / "memory")
        assert _mode(p) == 0o700
    finally:
        os.umask(old)


def test_ensure_vault_accepts_str_root(tmp_path):
    """`str` root is coerced to Path; result is still 0o700."""
    root = str(tmp_path / "memory")
    p = ensure_vault(root)
    assert p.is_dir()
    assert _mode(p) == 0o700
