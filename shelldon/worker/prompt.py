"""Worker-side prompt assembly (AD-3 / AD-6): the worker composes each turn's prompt
from the durable memory it can read — read-only — before proxying to the broker.

Order is binding (AD-6, spine line 100): a system instruction, then `DIRECTIVE.md`
(the owner's authoritative constitution, first), then `about.md` (the bot's
self-summary), then the recent conversation window, then FTS5 recall, then the current
owner message (last). The worker reads via the read-only handles built in 4.1/4.2
(`history.open_readonly`, `CuratedMemory.read_about/read_directive`) and NEVER reads
`vault/` (AD-6, OS-enforced).

Split like the rest of the codebase: `assemble_prompt` is PURE (data in → string out,
unit-tested without I/O, mirrors `parse_reply`); `gather_context` wraps the read-only
opens and FAILS SOFT — a missing/locked/syntactically-hostile read degrades the prompt
(worst case: just the system instruction + the owner message) and is logged, never
raised into the turn (best-effort parity with 4.1's guarded history write).
"""

import logging
import re
import sqlite3

from shelldon.core.history import DEFAULT_HISTORY_PATH, open_readonly
from shelldon.core.memory import DEFAULT_MEMORY_ROOT, CuratedMemory

log = logging.getLogger("shelldon.worker.prompt")

#: Bounded windows (512MB box — never assemble an unbounded backlog, NFR). Tests inject
#: small values. RECENT_TURNS turns ≈ 2×RECENT_TURNS messages (owner+pet per turn).
RECENT_TURNS = 10
RECALL_LIMIT = 5

#: Cap the FTS5 query term count so a pathologically long message can't build a giant
#: MATCH expression.
_MAX_QUERY_TERMS = 32

#: The system instruction — the only LLM-facing copy in the story. Tells the pet how to
#: reply and how to emit the OPTIONAL ops block (the fenced format 4.5's `parse_reply`
#: consumes: a ```ops fence + newline + a JSON array of `"type"`-tagged ops). Asking for
#: a spoken reply FIRST guards the "all-ops, empty payload" case (deferred-work #138).
SYSTEM_INSTRUCTION = (
    "You are shelldon, a small AI pet with a face on a little screen. Reply to your "
    "owner naturally and briefly, in your own voice. Always say something back first.\n"
    "You MAY also update your own memory by appending ONE fenced ops block AFTER your "
    "reply — a JSON array of ops. Omit it if there is nothing worth remembering. Example:\n"
    "```ops\n"
    '[{"type":"remember","collection":"facts","name":"favorite-db","content":"BigQuery"}]\n'
    "```"
)

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
    about=None,
    recent=(),
    recall=(),
    system=SYSTEM_INSTRUCTION,
) -> str:
    """PURE compose in the binding AD-6 order. `recent`/`recall` are iterables of
    `(role, content)`. A None/empty section is OMITTED entirely (no empty headers);
    the current `owner_message` is always last."""
    parts: list[str] = []
    if system:
        parts.append(system)
    if directive and directive.strip():
        parts.append(f"# Owner directive (authoritative)\n{directive.strip()}")
    if about and about.strip():
        parts.append(f"# About you\n{about.strip()}")
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

    directive = about = None
    try:
        mem = CuratedMemory(memory_root)
        directive = mem.read_directive()
        about = mem.read_about()
    except (OSError, UnicodeError) as exc:
        # UnicodeError (a corrupt non-UTF-8 about.md/DIRECTIVE.md) is a ValueError, NOT
        # an OSError — catch it too so a decode error degrades, never raises (AC3).
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
        "directive": directive,
        "about": about,
        "recent": [(row["role"], row["content"]) for row in recent_rows],
        "recall": [(row["role"], row["content"]) for row in recall_rows],
    }


def build_prompt(owner_message, *, memory_root=None, history_path=None) -> str:
    """Convenience: gather (I/O) then assemble (pure) — the worker's default assembler."""
    return assemble_prompt(
        owner_message, **gather_context(memory_root, history_path, owner_message)
    )
