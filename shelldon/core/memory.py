"""core/memory — the curated, human-readable markdown memory tree (AD-6/AD-5).

The durable, LLM-curated half of memory: a small markdown tree under
`~/.shelldon/memory/` — `about.md` (a bot-owned self-summary), `facts/` and
`people/` (one file per remembered thing). Core is the **sole writer** (AD-5):
`apply_memory_op` takes a closed, fixed-arg memory-op from `contracts/`, validates
it, and writes the tree **atomically** (temp + fsync + `os.replace`, the AD-10 idiom
state.py/faces.py introduced) — rejecting an invalid op without touching disk. This
mirrors `faces.apply_add_face`: a synchronous, validated, core-only apply path.

`DIRECTIVE.md` is the owner's authoritative "constitution": read-only here and never
a memory-op target. Core's write set (`about.md`/`facts/`/`people/`) is **disjoint**
from the owner's (`DIRECTIVE.md`) — no writer ever conflicts (AD-6), and that
disjointness is structural: no op dispatch branch can name `DIRECTIVE.md`.

Scope (binding): the curated substrate + the "core applies" half ONLY. The
worker-proposes wire (`Result.proposed_ops`) is Story 4.5; injecting these reads into
a prompt is Story 4.4; vault/uid isolation is 4.3. Core stays LLM-free (AD-1) — this
is pure file I/O.
"""

import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path

from shelldon.contracts import LogEpisode, MemoryOp, Remember, RewriteAbout, RewriteSummary

log = logging.getLogger("shelldon.core.memory")

#: Default memory root — one tree beside the state checkpoint. Always injectable;
#: tests pass a `tmp_path` root and never touch real `$HOME`.
DEFAULT_MEMORY_ROOT = Path.home() / ".shelldon" / "memory"

#: The owner's authoritative doc — read as authoritative, NEVER written by the bot.
_DIRECTIVE_NAME = "DIRECTIVE.md"

#: The closed set of `Remember` target collections (mirrors the contract Literal).
_COLLECTIONS = ("facts", "people")

#: A name becomes a filename: keep Unicode word chars (so 'José'/CJK names survive),
#: collapse every other run — crucially path separators and dots — to a single '-'.
_UNSAFE_RE = re.compile(r"[^\w-]+")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (temp in same dir → fsync → os.replace) —
    the same crash-safety recipe as core/state.py and core/faces.py (AD-10). A failure
    before the replace leaves the prior file intact and no stray temp behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_filename(name: str) -> str:
    """`name` → a path-safe filename stem that preserves the name (incl. non-ASCII).

    Strips traversal by construction: every run that isn't a Unicode word char or '-'
    (so all separators, `..`, dots, control chars) collapses to a single '-', so `../etc`
    becomes `etc` and the result can never carry a separator. Casefolded + NFC-normalized
    so 'Alex'/'alex' map to one file (same-name overwrite is intended curation). Empty
    result (name was all separators/dots) is the caller's reject signal."""
    normalized = unicodedata.normalize("NFC", name).strip().casefold()
    return _UNSAFE_RE.sub("-", normalized).strip("-_.")


class CuratedMemory:
    """The curated markdown tree (core-owned, single writer per AD-5). Holds the root,
    applies validated memory-ops atomically, and exposes read accessors for `about.md`
    and the owner's `DIRECTIVE.md`. Parallel to `HistoryStore` (the sqlite half)."""

    def __init__(self, root=None) -> None:
        self._root = Path(root) if root is not None else DEFAULT_MEMORY_ROOT

    @property
    def root(self) -> Path:
        return self._root

    def apply_memory_op(self, op: MemoryOp) -> None:
        """Validate `op` against its closed schema, then atomically write the tree —
        rejecting an invalid op WITHOUT touching disk (the state.py/faces.py whole-reject
        discipline). Core is the sole, synchronous writer. Dispatch only ever targets
        `about.md`/`facts/`/`people/` — `DIRECTIVE.md` is structurally unreachable."""
        if isinstance(op, RewriteAbout):
            self._apply_rewrite_about(op)
        elif isinstance(op, Remember):
            self._apply_remember(op)
        elif isinstance(op, LogEpisode):
            self._apply_log_episode(op)
        elif isinstance(op, RewriteSummary):
            self._apply_rewrite_summary(op)
        else:
            raise ValueError(f"unknown memory-op {type(op).__name__!r} (not a closed MemoryOp)")

    def _apply_rewrite_about(self, op: RewriteAbout) -> None:
        if not op.content.strip():
            raise ValueError("rewrite_about: content must be non-empty")
        _atomic_write_text(self._root / "about.md", op.content)

    def _apply_rewrite_summary(self, op: RewriteSummary) -> None:
        """Overwrite the bot-owned running summary `summary.md` (Story 6.2) — the dream's
        bounded conversation summary the 4.4 assembly injects into later turns."""
        if not op.content.strip():
            raise ValueError("rewrite_summary: content must be non-empty")
        _atomic_write_text(self._root / "summary.md", op.content)

    def _apply_remember(self, op: Remember) -> None:
        if op.collection not in _COLLECTIONS:
            raise ValueError(f"remember: collection {op.collection!r} not in {_COLLECTIONS}")
        if not op.content.strip():
            raise ValueError("remember: content must be non-empty")
        stem = _safe_filename(op.name)
        if not stem:
            raise ValueError(f"remember: name {op.name!r} has no safe filename (all separators/dots)")

        collection_dir = (self._root / op.collection).resolve()
        path = (collection_dir / f"{stem}.md").resolve()
        # Belt-and-suspenders: a slug carries no separators, so this always holds — but
        # asserting it makes "physically unable to write outside the tree" structural.
        if path.parent != collection_dir:
            raise ValueError(f"remember: name {op.name!r} escapes {op.collection}/")
        _atomic_write_text(path, op.content)

    def _apply_log_episode(self, op: LogEpisode) -> None:
        if not op.content.strip():
            raise ValueError("log_episode: content must be non-empty")
        path = self._root / "episodes.md"
        prior = path.read_text() if path.exists() else ""
        header = f"## tags: {', '.join(op.tags)}\n\n" if op.tags else ""
        entry = f"{header}{op.content}\n"
        block = entry if not prior else f"{prior}\n---\n\n{entry}"
        _atomic_write_text(path, block)

    def read_about(self) -> str | None:
        """The bot-owned `about.md` content, or `None` if it has never been written.
        The read accessor Story 4.4 will inject into prompts (4.2 only exposes it)."""
        path = self._root / "about.md"
        return path.read_text() if path.is_file() else None

    def read_summary(self) -> str | None:
        """The bot-owned running summary `summary.md`, or `None` if never written (Story 6.2).
        The 4.4 prompt assembly injects it so later turns carry bounded context."""
        path = self._root / "summary.md"
        return path.read_text() if path.is_file() else None

    def read_directive(self) -> str | None:
        """The owner's authoritative `DIRECTIVE.md`, or `None` if absent (AC3). Read-only
        — there is no write path to this file anywhere in this module (disjoint writers,
        AD-6); Story 4.4 injects it first as authoritative context."""
        path = self._root / _DIRECTIVE_NAME
        return path.read_text() if path.is_file() else None
