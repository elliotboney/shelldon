"""core/state — the persistent personality-state substrate (AD-7/AD-5/AD-10).

The pet's inner state (mood / energy / last-interaction) lives in RAM as the
working copy and is checkpointed *periodically* to one small file — RAM is never
the source of truth across a restart (AD-7). Core is the sole writer (AD-5/NFR11):
mutations are sparse patches over a **closed set** of fixed dotted paths, so a typo
is a rejected patch, not a silently-minted attribute (the `Region`-enum precedent).

The checkpoint is the first **atomic** write in the tree (AD-10): temp file in the
same dir → fsync → `os.replace`, so a crash mid-write leaves the prior good file
intact. Restore tolerates a missing/corrupt/partial checkpoint by falling back to
defaults rather than crashing (AC3).

Scope (binding): this is the state substrate ONLY. The reflex loop that drives
mood/energy drift is Story 3.2 (it *calls* `apply_patch` on a tick); the mood→face
mapping is 3.3; the scheduler that will own the checkpoint cadence is Epic 5.
"""

import logging
import os
import tempfile
from pathlib import Path

import msgspec

log = logging.getLogger("shelldon.core.state")

#: Default checkpoint location — one small file outside source (Structural Seed).
#: Always injectable; tests pass a `tmp_path` file and never touch real `$HOME`.
DEFAULT_CHECKPOINT_PATH = Path.home() / ".shelldon" / "state.json"


class Mood(msgspec.Struct):
    """Minimal affect dimensions. MUTABLE (not frozen) — core mutates it in place.
    A richer affect model is deferred; keep this to the two dimensions 3.2 needs."""

    valence: float = 0.0  # pleasant(+) ↔ unpleasant(-)
    arousal: float = 0.0  # activated(+) ↔ calm(-)


class PersonalityState(msgspec.Struct):
    """The pet's inner state. MUTABLE RAM state — unlike the `frozen` wire contracts
    in `contracts/`, this is mutated in place by core, so it is NOT a bus message and
    does NOT belong in `contracts/`. Defaults give a clean first run with no file."""

    mood: Mood = msgspec.field(default_factory=Mood)
    energy: float = 0.5  # 0.0 (depleted) .. 1.0 (full); mid by default
    last_interaction: str | None = None  # ISO-8601 UTC; None until the first interaction


#: The closed set of writable dotted paths (AD-5 "fixed dotted paths"). A patch key
#: outside this set is rejected — mirrors the `Region` enum's typo-rejection.
WRITABLE_PATHS = frozenset({"mood.valence", "mood.arousal", "energy", "last_interaction"})

_decoder = msgspec.json.Decoder(PersonalityState)


class PersistentState:
    """Owns the in-RAM `PersonalityState`, a sparse-patch writer over the closed path
    set, and the atomic checkpoint/restore. A `_dirty` flag lets the periodic flush
    skip a no-op write (so high-churn reflex writes stay off the SD card — NFR7)."""

    def __init__(self, state: PersonalityState) -> None:
        self._state = state
        self._dirty = False

    @property
    def state(self) -> PersonalityState:
        return self._state

    @property
    def dirty(self) -> bool:
        return self._dirty

    @classmethod
    def load(cls, path) -> "PersistentState":
        """Restore from `path`, or default cleanly. Absent file → defaults (first
        run). Corrupt / partial / schema-mismatched file → log a warning and fall
        back to defaults (AC3 "without a corrupt-state crash") — never raise."""
        path = Path(path)
        if not path.exists():
            return cls(PersonalityState())
        try:
            state = _decoder.decode(path.read_bytes())
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError, OSError) as exc:
            # Corrupt/partial/schema-mismatched decode (ValueError family) OR an
            # unreadable file — a PermissionError, or a TOCTOU delete between the
            # exists() check and the read (both OSError). Either way, fall back to
            # defaults rather than crashing the caller (AC3 "without a crash").
            log.warning("unusable checkpoint at %s (%s); falling back to defaults", path, exc)
            return cls(PersonalityState())
        return cls(state)

    def apply_patch(self, patch: dict) -> None:
        """Apply a sparse patch of `dotted.path -> value`. Validates EVERY key against
        the closed set first and rejects the whole patch on an unknown path (fail
        fast — no half-apply). A successful patch marks the state dirty."""
        for key in patch:
            if key not in WRITABLE_PATHS:
                raise KeyError(
                    f"unknown state path {key!r}; writable paths are {sorted(WRITABLE_PATHS)}"
                )
        for key, value in patch.items():
            head, _, tail = key.partition(".")
            if tail:
                setattr(getattr(self._state, head), tail, value)
            else:
                setattr(self._state, head, value)
        if patch:
            self._dirty = True

    def checkpoint(self, path) -> None:
        """Serialize and write atomically: a temp file in the SAME directory →
        flush → fsync → `os.replace` (a same-filesystem atomic rename). A failure
        before the replace leaves the prior file intact (AD-10) and `_dirty` set.
        `_dirty` clears only after a successful replace."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = msgspec.json.encode(self._state)

        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            # Crash/interrupt before the rename: don't leave a stray temp behind, and
            # leave the prior good checkpoint (and the dirty flag) untouched.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._dirty = False
