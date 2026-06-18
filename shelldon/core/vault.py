"""core/vault — the OS-locked secrets dir beside the curated memory tree (AD-6/AD-5).

A sibling to `memory/` (`about.md`/`facts`/`people`) locked to mode 0o700 so a
less-privileged forked worker uid is OS-denied read/traverse (AD-6 — enforced by the
kernel, not a path filter). Core is the sole writer (AD-5); Story 4.3 only CREATES the
empty locked dir — content lands later via Epic 6, also through core.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("shelldon.core.vault")


def ensure_vault(memory_root) -> Path:
    """Ensure `<memory_root>/vault/` exists at mode 0o700, idempotently.

    `os.makedirs(..., mode=0o700, exist_ok=True)` is NOT enough: the process umask masks
    the requested mode on create, and `exist_ok` skips any chmod on an already-present dir.
    So ALWAYS follow with an explicit `os.chmod(path, 0o700)` — the only reliable lock.
    Accepts `str | Path`; returns the vault `Path`."""
    path = Path(memory_root) / "vault"
    existed = path.exists()
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    if not existed:
        log.debug("created vault dir %s (mode 0o700)", path)
    return path
