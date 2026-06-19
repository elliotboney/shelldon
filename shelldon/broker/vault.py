"""The broker-only vault-read authority seam (AD-2 / AD-6).

The broker is the egress/safety authority — the privileged actor running as the
service uid — so it (and ONLY it) holds the authorized path to read `vault/`
(AD-2). The worker (untrusted brain) has no vault-read API and, on a dropped
uid, no OS permission. Vault surfacing is a broker-gated read+authorize GATE,
not an LLM-output parser (AD-6): no pet-domain parsing happens here, and this
seam is NOT yet wired into a prompt (that's Story 4.4).
"""

import logging
import re
from pathlib import Path

log = logging.getLogger("shelldon.broker")

# A conservative single-segment key: ASCII word chars and '-' only. `re.ASCII` keeps
# `\w` from matching Unicode (café/用户) — vault keys are internal identifiers, not
# human names, so ASCII is the clearer contract. Forbids separators, `..` and dots by
# construction, so the constructed path can never escape vault/.
_SAFE_KEY_RE = re.compile(r"[\w-]+", re.ASCII)


def authorize_surface(key: str) -> bool:
    """The authorization decision point. For 4.3 it gates on a safe, single-segment
    key — non-empty after stripping, no path separators, no `..` (traversal rejected).
    This is where Story 4.4 adds richer egress policy."""
    return bool(_SAFE_KEY_RE.fullmatch(key.strip()))


def surface_vault(memory_root, key: str) -> str | None:
    """The SOLE authorized vault read. Returns the text of `<memory_root>/vault/<key>.md`,
    or None if `key` is unauthorized or the file does not exist. Never reads on a
    rejected key. Because `authorize_surface` forbids separators/`..`, the slug carries
    no separator, so the constructed path can never escape `vault/` (belt-and-suspenders,
    like memory.py's `_safe_filename`)."""
    if not authorize_surface(key):
        log.warning("vault surface rejected: unsafe key %r", key)
        return None
    slug = key.strip()
    # Belt-and-suspenders: authorize_surface already forbids separators/`..`, but use a
    # real check (NOT `assert`, which `python -O` strips) before building the path.
    if "/" in slug or ".." in slug:
        return None
    path = Path(memory_root) / "vault" / f"{slug}.md"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        # A corrupt/unreadable vault file must not raise into the egress path — a
        # secret that can't be read cleanly is simply not surfaced.
        log.warning("vault surface failed to read %s (%s)", path, exc)
        return None
