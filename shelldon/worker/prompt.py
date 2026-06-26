"""Worker-side prompt assembly (AD-3 / AD-6): the worker composes each turn's prompt
from the durable memory it can read — read-only — before proxying to the broker.

Order is binding (AD-6, spine line 100): the system instruction (`BOT_INSTRUCTIONS.md`),
then `DIRECTIVE.md` (the owner's authoritative constitution), then the persona files
`IDENTITY.md`/`SOUL.md`/`USER.md` (Story 10.1), then `about.md` (the bot's self-summary),
then the recent conversation window, then FTS5 recall, then the current owner message (last).
Story 10.1 moved the persona OUT of a hardcoded `SYSTEM_INSTRUCTION` constant into seed
markdown the worker reads each turn. The worker reads via the read-only handles built in
4.1/4.2/10.1 (`history.open_readonly`, `CuratedMemory.read_instructions/identity/soul/user/
about/directive`) and NEVER reads `vault/` (AD-6, OS-enforced).

Split like the rest of the codebase: `assemble_prompt` is PURE (data in → string out,
unit-tested without I/O, mirrors `parse_reply`); `gather_context` wraps the read-only
opens and FAILS SOFT — a missing/locked/syntactically-hostile read degrades the prompt
(worst case: just the system instruction + the owner message) and is logged, never
raised into the turn (best-effort parity with 4.1's guarded history write).
"""

import logging
import re
import sqlite3
from importlib import resources

from shelldon.core.history import DEFAULT_HISTORY_PATH, open_readonly
from shelldon.core.memory import DEFAULT_MEMORY_ROOT, CuratedMemory

log = logging.getLogger("shelldon.worker.prompt")

#: Bounded windows (512MB box — never assemble an unbounded backlog, NFR). Tests inject
#: small values. RECENT_TURNS turns ≈ 2×RECENT_TURNS messages (owner+pet per turn).
RECENT_TURNS = 10
RECALL_LIMIT = 5

#: Char budget for the surfaced curated knowledge (facts/+people/, Epic 6 retro). The dream
#: prunes the set so it stays small, but cap defensively — overflow facts are dropped with a
#: logged count (never silently truncated), newest-by-name kept within budget.
KNOWLEDGE_CHAR_BUDGET = 4000

#: Char budget per persona section (Story 10.1 — BOT_INSTRUCTIONS/IDENTITY/SOUL/USER). The seed
#: instruction is ~2.2KB and persona prose is small, but a runaway/bot-bloated file must not blow
#: the 416MB-box context — over-budget text is truncated with a logged warning (never silent).
PERSONA_CHAR_BUDGET = 8000

#: Cap the FTS5 query term count so a pathologically long message can't build a giant
#: MATCH expression.
_MAX_QUERY_TERMS = 32

#: Story 10.1 — the system instruction is no longer a hardcoded constant; it lives in
#: `BOT_INSTRUCTIONS.md`, seeded from the `shelldon.persona` package into the memory root and
#: read per turn. This helper returns the pristine repo SEED text (the recovery source) — used by
#: tests and live smokes that need the canonical system copy without reaching into a seeded root.
def seed_instructions() -> str | None:
    """The packaged `BOT_INSTRUCTIONS.md` seed text (the repo source of the system slot), or
    `None` if the template is unreadable. Runtime reads the *seeded* copy via `read_instructions`;
    this is the template, identical to a freshly-seeded root."""
    try:
        return resources.files("shelldon.persona").joinpath("BOT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError, UnicodeError):
        return None

#: Bare word tokens for a SAFE FTS5 query — raw owner text (quotes, parens, `*`, or
#: operators like NEAR/AND/OR) can make `MATCH` raise a syntax error. We quote each term
#: (defusing operators) and OR them, so recall is robust to arbitrary punctuation.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _fts_query(message: str) -> str | None:
    """Turn an owner message into a safe FTS5 MATCH query (`"t1" OR "t2" …`), or None
    when there are no word characters to search on."""
    terms = _WORD_RE.findall(message)[:_MAX_QUERY_TERMS]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def assemble_prompt(
    owner_message,
    *,
    directive=None,
    identity=None,
    soul=None,
    user=None,
    about=None,
    knowledge=(),
    summary=None,
    recent=(),
    recall=(),
    system=None,
) -> str:
    """PURE compose in the binding AD-6 order. `recent`/`recall` are iterables of
    `(role, content)`. A None/empty section is OMITTED entirely (no empty headers);
    the current `owner_message` is always last. `knowledge` (Epic 6 retro) is the curated
    `facts/`+`people/` the dream promotes — durable knowledge placed right after `about`, so a
    promoted fact actually shapes later replies. `summary` (Story 6.2) is the running summary.

    Story 10.1: `system` is the file-sourced `BOT_INSTRUCTIONS.md` (no longer a hardcoded
    constant), and `identity`/`soul`/`user` are the persona files injected right after the
    owner's authoritative `directive` and before the volatile memory layers, so persona shapes
    every reply. They ship empty (filled by onboarding, 10.4) → omitted while blank, which keeps
    day-one assembly byte-identical to the prior hardcoded prompt."""
    parts: list[str] = []
    if system:
        parts.append(system)
    if directive and directive.strip():
        parts.append(f"# Owner directive (authoritative)\n{directive.strip()}")
    if identity and identity.strip():
        parts.append(f"# Your identity\n{identity.strip()}")
    if soul and soul.strip():
        parts.append(f"# Your soul\n{soul.strip()}")
    if user and user.strip():
        parts.append(f"# Your owner\n{user.strip()}")
    if about and about.strip():
        parts.append(f"# About you\n{about.strip()}")
    know_lines = [f"- {name}: {content.strip()}" for name, content in knowledge if content.strip()]
    if know_lines:
        parts.append("# What you know\n" + "\n".join(know_lines))
    if summary and summary.strip():
        parts.append(f"# Conversation so far\n{summary.strip()}")
    recent_lines = [f"{role}: {content}" for role, content in recent]
    if recent_lines:
        parts.append("# Recent conversation\n" + "\n".join(recent_lines))
    recall_lines = [f"{role}: {content}" for role, content in recall]
    if recall_lines:
        parts.append("# Things you remember\n" + "\n".join(recall_lines))
    parts.append(f"# Owner says now\n{owner_message}")
    return "\n\n".join(parts)


def gather_context(
    memory_root=None,
    history_path=None,
    owner_message="",
    *,
    recent_n=RECENT_TURNS,
    recall_k=RECALL_LIMIT,
) -> dict:
    """Open the read-only handles, read DIRECTIVE/about/recent/recall, de-dup recall
    against the recent window (by row id), and return `assemble_prompt` kwargs. FAILS
    SOFT: any read/open/query failure logs and degrades (worst case → empty context, so
    the prompt is just the system instruction + the owner message)."""
    memory_root = DEFAULT_MEMORY_ROOT if memory_root is None else memory_root
    history_path = DEFAULT_HISTORY_PATH if history_path is None else history_path

    system = directive = identity = soul = user = about = summary = None
    knowledge: list[tuple[str, str]] = []
    try:
        mem = CuratedMemory(memory_root)
        # Story 10.1: the persona files (seeded copy-if-absent by CuratedMemory init), each
        # char-budgeted so a runaway/bot-bloated file can't blow the box context. Read each
        # INDEPENDENTLY fail-soft (AC6): one corrupt persona file degrades only its own section,
        # never the others (a corrupt SOUL.md must not also drop the system instruction).
        system = _bounded_text(_safe_read(mem.read_instructions), "BOT_INSTRUCTIONS.md")
        identity = _bounded_text(_safe_read(mem.read_identity), "IDENTITY.md")
        soul = _bounded_text(_safe_read(mem.read_soul), "SOUL.md")
        user = _bounded_text(_safe_read(mem.read_user), "USER.md")
        directive = mem.read_directive()
        about = mem.read_about()
        summary = mem.read_summary()  # Story 6.2: the dream's running summary (may be None)
        # Epic 6 retro: surface the curated collections the dream promotes, so a promoted
        # fact actually shapes later replies (it was durable-but-invisible before). Bounded.
        knowledge = _bounded_knowledge(mem.read_all_collections())
    except (OSError, UnicodeError) as exc:
        # UnicodeError (a corrupt non-UTF-8 about.md/DIRECTIVE.md/summary.md) subclasses
        # ValueError, not OSError — so it must be listed explicitly here, or a decode error
        # would escape this handler and raise instead of degrading (AC3).
        log.warning("memory read failed during assembly (%s); degrading", exc)

    recent_rows: list = []
    recall_rows: list = []
    reader = None
    try:
        reader = open_readonly(history_path)
        recent_rows = reader.recent(recent_n * 2)  # ~2 messages per turn
        query = _fts_query(owner_message)
        if query is not None:
            try:
                recall_rows = reader.search(query, recall_k)
            except sqlite3.OperationalError as exc:
                # A malformed/over-complex MATCH (despite sanitising) must never crash
                # the turn — just skip recall this turn.
                log.warning("recall query failed (%s); no recall this turn", exc)
    except (sqlite3.Error, OSError) as exc:
        # Missing db (first run, opened read-only), locked, or a corrupt store → degrade.
        log.warning("history read failed during assembly (%s); degrading", exc)
    finally:
        if reader is not None:
            try:
                reader.close()
            except sqlite3.Error:
                pass

    recent_ids = {row["id"] for row in recent_rows}
    recall_rows = [row for row in recall_rows if row["id"] not in recent_ids]
    return {
        "system": system,
        "identity": identity,
        "soul": soul,
        "user": user,
        "directive": directive,
        "about": about,
        "knowledge": knowledge,
        "summary": summary,
        "recent": [(row["role"], row["content"]) for row in recent_rows],
        "recall": [(row["role"], row["content"]) for row in recall_rows],
    }


def _safe_read(reader) -> str | None:
    """Call a persona read accessor, returning None on a missing/corrupt file (Story 10.1, AC6)
    — so a single corrupt persona file degrades only its own section, never sibling reads."""
    try:
        return reader()
    except (OSError, UnicodeError) as exc:
        log.warning("persona read failed (%s); degrading that section", exc)
        return None


def _bounded_text(text: str | None, label: str) -> str | None:
    """Cap a persona section at `PERSONA_CHAR_BUDGET` chars (Story 10.1). `None`/empty pass
    through unchanged (omitted downstream); over-budget text is truncated with a logged
    warning (never silent). Mirrors `_bounded_knowledge`'s drop-with-log discipline."""
    if not text or len(text) <= PERSONA_CHAR_BUDGET:
        return text
    log.warning("persona %s over %d chars; truncating", label, PERSONA_CHAR_BUDGET)
    return text[:PERSONA_CHAR_BUDGET]


def _bounded_knowledge(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Cap the surfaced knowledge at `KNOWLEDGE_CHAR_BUDGET` chars — keep entries until the
    budget is hit, drop the rest with a logged count (no silent truncation)."""
    kept: list[tuple[str, str]] = []
    used = 0
    for name, content in items:
        used += len(name) + len(content)
        if used > KNOWLEDGE_CHAR_BUDGET:
            log.warning("knowledge over %d chars; dropping %d entr(ies)", KNOWLEDGE_CHAR_BUDGET, len(items) - len(kept))
            break
        kept.append((name, content))
    return kept


def build_prompt(owner_message, *, memory_root=None, history_path=None) -> str:
    """Convenience: gather (I/O) then assemble (pure) — the worker's default assembler."""
    return assemble_prompt(
        owner_message, **gather_context(memory_root, history_path, owner_message)
    )
