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
from importlib import resources
from pathlib import Path

from shelldon.contracts import (
    LogEpisode,
    MemoryOp,
    Remember,
    RewriteAbout,
    RewriteIdentity,
    RewriteInstructions,
    RewriteSoul,
    RewriteSummary,
    RewriteUser,
)

log = logging.getLogger("shelldon.core.memory")

#: Default memory root — one tree beside the state checkpoint. Always injectable;
#: tests pass a `tmp_path` root and never touch real `$HOME`.
DEFAULT_MEMORY_ROOT = Path.home() / ".shelldon" / "memory"

#: Story 10.1 — the persona lives in markdown the bot reads every turn, NOT a hardcoded
#: constant. Pristine seed templates ship in the `shelldon.persona` package; on init they
#: are copied into the memory root copy-if-absent (the `faces.FaceRegistry.load` idiom),
#: so the owner/bot can then edit the worktree copy without touching source. `BOT_INSTRUCTIONS.md`
#: carries the system instruction (the only LLM-facing copy); `SOUL/IDENTITY/USER` ship empty
#: and are populated by onboarding (Story 10.4). The repo template doubles as the recovery seed.
_PERSONA_PKG = "shelldon.persona"
_PERSONA_SEED_FILES = ("BOT_INSTRUCTIONS.md", "SOUL.md", "IDENTITY.md", "USER.md")

#: Story 10.3 — the self-initiated-turn prompt templates (proactive musing + dream cycle),
#: shipped in the same `shelldon.persona` package and seeded copy-if-absent alongside the
#: persona files. UNLIKE the persona files they are NOT bot-rewritable (no `rewrite_*` op
#: targets them) — they are prompt policy the owner may hand-edit, read at dispatch by
#: `core/dispatch.py` and filled by the pure `core/proactive.py` builders.
#: Story 10.4 — `BOOTSTRAP.md` is the first-run interview directive the WORKER injects while
#: `USER.md` is blank; also a read-only, owner-editable prompt template (not a rewrite target).
#: Story 10.5 — `TOOLS.md`/`ARCHITECTURE.md` are heavy reference docs the worker LAZY-LOADS by
#: keyword (injected only when the owner message is about the bot's tools/internals), so they cost
#: tokens only when relevant. Like the other prompt templates they are owner-editable, NOT rewrite
#: targets (no `rewrite_*` op names them).
_PROMPT_TEMPLATE_SEED_FILES = ("HEARTBEAT.md", "DREAM.md", "BOOTSTRAP.md", "TOOLS.md", "ARCHITECTURE.md")

#: The owner's authoritative doc — written ONLY via the owner-approval gate (Story 10.2),
#: never autonomously (it is not a MemoryOp).
_DIRECTIVE_NAME = "DIRECTIVE.md"

#: Story 10.2 — the protocol markers a `rewrite_instructions` MUST keep, or `parse_reply` breaks.
#: `THOUGHT:`/`FACE:` are the caption/face directive lines the worker parses+strips; the ```ops
#: fence is how memory-ops travel. A rewrite dropping any of these is rejected (validate-on-apply).
_REQUIRED_INSTRUCTION_MARKERS = ("THOUGHT:", "FACE:", "```ops")

#: The closed set of `Remember` target collections (mirrors the contract Literal).
_COLLECTIONS = ("facts", "people", "preferences", "capabilities")

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
        self._seed_persona()

    @property
    def root(self) -> Path:
        return self._root

    def _seed_persona(self) -> None:
        """Copy any ABSENT persona seed file from the shipped templates into the root —
        the copy-if-absent half of `faces.FaceRegistry.load`'s absent→seed behavior. A
        present file is left untouched (never overwrites an owner/bot edit). FAILS SOFT:
        a missing template or write error logs and is swallowed — construction NEVER raises
        (this runs at core boot AND per fork worker; a failed seed just degrades that section
        later). Only ADDS absent files, so core stays the sole writer of present files (AD-5)."""
        for name in _PERSONA_SEED_FILES + _PROMPT_TEMPLATE_SEED_FILES:
            dest = self._root / name
            if dest.exists():
                continue
            try:
                text = resources.files(_PERSONA_PKG).joinpath(name).read_text(encoding="utf-8")
                _atomic_write_text(dest, text)
            except (OSError, ModuleNotFoundError, UnicodeError) as exc:
                log.warning("persona seed for %s failed (%s); skipping", name, exc)

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
        elif isinstance(op, RewriteSoul):
            self._apply_rewrite_persona(op.content, "SOUL.md", "rewrite_soul")
        elif isinstance(op, RewriteIdentity):
            self._apply_rewrite_persona(op.content, "IDENTITY.md", "rewrite_identity")
        elif isinstance(op, RewriteUser):
            self._apply_rewrite_persona(op.content, "USER.md", "rewrite_user")
        elif isinstance(op, RewriteInstructions):
            self._apply_rewrite_instructions(op)
        else:
            # NB: `RewriteDirective` is intentionally NOT a MemoryOp — it has no autonomous apply
            # path; core gates it through owner approval (Story 10.2). If one reaches here it is
            # rejected, defense-in-depth against an accidental autonomous directive write.
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

    def _apply_rewrite_persona(self, content: str, filename: str, op_name: str) -> None:
        """Story 10.2 — replace a bot-owned persona file (SOUL/IDENTITY/USER) atomically. Mirrors
        `_apply_rewrite_about`: reject empty content, then temp+fsync+replace. Core sole writer (AD-5)."""
        if not content.strip():
            raise ValueError(f"{op_name}: content must be non-empty")
        _atomic_write_text(self._root / filename, content)

    def _apply_rewrite_instructions(self, op: RewriteInstructions) -> None:
        """Story 10.2 — replace `BOT_INSTRUCTIONS.md` with a VALIDATE-ON-APPLY guardrail: reject a
        rewrite that drops any required protocol marker (`THOUGHT:`/`FACE:`/the ops fence), so the
        bot can re-voice its character but cannot break the contract `parse_reply` depends on. A
        rejected rewrite is a no-op (raises; the caller logs + skips), leaving the prior file intact.
        The pristine repo seed (`shelldon/persona/`) is the always-available recovery."""
        if not op.content.strip():
            raise ValueError("rewrite_instructions: content must be non-empty")
        missing = [m for m in _REQUIRED_INSTRUCTION_MARKERS if m not in op.content]
        if missing:
            raise ValueError(
                f"rewrite_instructions: drops required protocol marker(s) {missing} — refusing "
                "(would break parse_reply); keep THOUGHT:/FACE:/the ops fence"
            )
        _atomic_write_text(self._root / "BOT_INSTRUCTIONS.md", op.content)

    def _apply_rewrite_directive(self, content: str) -> None:
        """Story 10.2 — write the owner's `DIRECTIVE.md`. CRITICAL: reachable ONLY from core's
        owner-approval branch (`runtime._handle_approval_decision`), NEVER from `apply_memory_op`
        dispatch — `RewriteDirective` is not a `MemoryOp`. The owner stays the single AUTHORITY on
        the constitution (an unapproved change can never land); core is the single WRITER (AD-5)."""
        if not content.strip():
            raise ValueError("rewrite_directive: content must be non-empty")
        _atomic_write_text(self._root / _DIRECTIVE_NAME, content)

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

    def read_instructions(self) -> str | None:
        """The system instruction `BOT_INSTRUCTIONS.md` (Story 10.1) — the persona's machine
        contract (THOUGHT/FACE/ops), seeded from the repo template, injected as the prompt's
        system slot (replacing the old hardcoded `SYSTEM_INSTRUCTION`). `None` if absent."""
        path = self._root / "BOT_INSTRUCTIONS.md"
        return path.read_text() if path.is_file() else None

    def read_soul(self) -> str | None:
        """The bot-owned `SOUL.md` (voice/values), or `None` if absent (Story 10.1). Ships empty;
        filled by onboarding (10.4). Injected after IDENTITY; omitted while empty."""
        path = self._root / "SOUL.md"
        return path.read_text() if path.is_file() else None

    def read_identity(self) -> str | None:
        """The bot-owned `IDENTITY.md` (who/hardware/mission), or `None` if absent (Story 10.1).
        Ships empty; filled by onboarding (10.4). Injected after DIRECTIVE; omitted while empty."""
        path = self._root / "IDENTITY.md"
        return path.read_text() if path.is_file() else None

    def read_user(self) -> str | None:
        """The bot-owned `USER.md` (owner profile), or `None` if absent (Story 10.1). Ships empty;
        the onboarding (10.4) is the mechanism that creates it. Injected after SOUL; omitted while empty."""
        path = self._root / "USER.md"
        return path.read_text() if path.is_file() else None

    def read_heartbeat(self) -> str | None:
        """The proactive self-prompt template `HEARTBEAT.md` (Story 10.3), or `None` if absent.
        Read at dispatch and filled by the pure `build_proactive_prompt`. No write path (not a
        rewrite-op target); the owner may hand-edit. Fail-soft: absent or unreadable → `None` →
        builder fallback."""
        path = self._root / "HEARTBEAT.md"
        try:
            return path.read_text() if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def read_dream(self) -> str | None:
        """The dream-cycle prompt template `DREAM.md` (Story 10.3), or `None` if absent. Read at
        dispatch and filled by `build_dream_prompt`. No write path; the owner may hand-edit.
        Fail-soft: absent or unreadable → `None` → builder fallback."""
        path = self._root / "DREAM.md"
        try:
            return path.read_text() if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def read_bootstrap(self) -> str | None:
        """The first-run interview directive `BOOTSTRAP.md` (Story 10.4), or `None` if absent. The
        WORKER reads it and injects it while `USER.md` is blank, then stops once onboarding fills the
        owner profile. No write path (not a rewrite-op target); the owner may hand-edit. Fail-soft:
        absent or unreadable → `None` → onboarding section omitted, the turn proceeds normally."""
        path = self._root / "BOOTSTRAP.md"
        try:
            return path.read_text() if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def read_tools(self) -> str | None:
        """The lazy-loaded reference doc `TOOLS.md` (Story 10.5) — what tools the bot has. The
        worker injects it only when the owner message matches the tools/capability keywords. No
        write path (not a rewrite-op target); the owner may hand-edit. Fail-soft: absent or
        unreadable → `None` → the section is omitted, the turn proceeds normally."""
        path = self._root / "TOOLS.md"
        try:
            return path.read_text() if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def read_architecture(self) -> str | None:
        """The lazy-loaded reference doc `ARCHITECTURE.md` (Story 10.5) — the bot's hardware/
        internals, the "how do you work" answer. The worker injects it only when the owner message
        matches the hardware/architecture keywords. No write path (not a rewrite-op target); the
        owner may hand-edit. Fail-soft: absent or unreadable → `None` → section omitted."""
        path = self._root / "ARCHITECTURE.md"
        try:
            return path.read_text() if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            return None

    def read_collection(self, collection: str) -> list[tuple[str, str]]:
        """The `(name, content)` pairs of a curated collection (`facts`/`people`), sorted by
        name — the accessor the prompt assembly injects so a promoted fact reaches later turns
        (Epic 6 retro: close the `facts/` surfacing gap). Closed to `facts`/`people` so it can
        never read `vault/` or `DIRECTIVE.md`. A missing dir → `[]`; an unreadable file is
        skipped (fail-soft, never raises)."""
        if collection not in _COLLECTIONS:
            raise ValueError(f"read_collection: {collection!r} not in {_COLLECTIONS}")
        out: list[tuple[str, str]] = []
        for path in sorted((self._root / collection).glob("*.md")):
            try:
                out.append((path.stem, path.read_text()))
            except (OSError, UnicodeError) as exc:
                log.warning("skipping unreadable %s (%s)", path, exc)
        return out

    def read_all_collections(self) -> list[tuple[str, str]]:
        """The `(name, content)` pairs across every curated collection, name-sorted within
        each — the single accessor the prompt assembly surfaces so a memory the model filed
        under any collection (facts/people/preferences/capabilities) reaches later turns.
        Iterates the closed `_COLLECTIONS`, so a new collection surfaces with no caller edit."""
        out: list[tuple[str, str]] = []
        for collection in _COLLECTIONS:
            out.extend(self.read_collection(collection))
        return out

    def read_directive(self) -> str | None:
        """The owner's authoritative `DIRECTIVE.md`, or `None` if absent (AC3). Read-only
        — there is no write path to this file anywhere in this module (disjoint writers,
        AD-6); Story 4.4 injects it first as authoritative context."""
        path = self._root / _DIRECTIVE_NAME
        return path.read_text() if path.is_file() else None
