"""core/proactive — the self-initiated-turn prompt policies (Story 5.4 / 6.2 / 10.3, AD-1).

Pure functions that build the directive placed in the worker's owner-message slot when the
pet acts *on its own* — no owner message to reply to. Two self-initiated turns share this
module: the **proactive musing** (5.4 — speak up, mood-tinted) and the **dream cycle** (6.2 —
review pending learnings, promote/prune, summarize). Both framings are parenthetical
SELF-PROMPTS so the worker knows it is initiating, not replying.

Story 10.3 moved the prompt PROSE out of this module into editable seed files
(`HEARTBEAT.md` / `DREAM.md`, seeded into the memory worktree by `core/memory.py` and read at
dispatch by `core/dispatch.py`). The *policy* here stays pure: each builder takes the loaded
template TEXT as an argument and fills it (`{feeling}` for the musing, `{lines}` for the dream).
The file READ lives in the driver (`core/dispatch.py`, which already reads state/history) — NOT
here. A missing/blank/malformed template degrades to a terse built-in fallback (logged) so a
self-initiated turn never fails.

Pure: no clock, no file I/O, never raises — mirrors `core/reflexes.py`. LLM-free (AD-1):
imports only stdlib.
"""

import logging

log = logging.getLogger("shelldon.core.proactive")

#: `HEARTBEAT.md` packs the directive body (with `{feeling_sentence}`) and the optional feeling
#: fragment (with `{feeling}`) in ONE file, split by a `\n---\n` sentinel — so ALL LLM-facing
#: prose lives in the worktree, none hardcoded (Story 10.3). `build_proactive_prompt` partitions
#: on this, fills the fragment when a feeling is known, then folds it into the body.
_HEARTBEAT_SENTINEL = "\n---\n"

#: Terse degrade fallbacks — used ONLY when the seed file is missing/blank/malformed (logged).
#: NOT the real copy (that lives in the seed files); a safety net so a self-initiated turn never
#: dies on a bad template (fail-soft, AD-1/4.1).
_FALLBACK_PROACTIVE = (
    "(Self-prompt: there's no owner message to reply to right now — you're speaking up on "
    "your own. Share whatever's on your mind; it doesn't have to be a question.)"
)
_FALLBACK_DREAM = (
    "(Dream-time reflection: no owner message to reply to. For each pending learning below, "
    "save the durable ones (`remember` / `rewrite_about`) and mark each `resolve_learning` "
    '"promoted", else "pruned"; refresh `rewrite_summary`. Then reply with a brief note.)\n\n'
    "# Pending learnings\n{lines}"
)


def build_proactive_prompt(feeling: str | None, template: str | None = None) -> str:
    """Build the open-ended proactive self-prompt from `template` (the `HEARTBEAT.md` text).

    `template` carries the directive body + the optional feeling fragment, split by the
    `\\n---\\n` sentinel. A known `feeling` is woven in ("You're feeling {feeling}."); None or
    blank/whitespace-only drops the whole sentence (never "feeling ." or the literal "None").
    A missing/blank/malformed `template` degrades to `_FALLBACK_PROACTIVE` (logged). Pure,
    deterministic, never raises."""
    if not template or not template.strip():
        log.warning("HEARTBEAT template missing/blank — using fallback proactive directive")
        return _FALLBACK_PROACTIVE
    body, sep, frag = template.partition(_HEARTBEAT_SENTINEL)
    feeling_sentence = ""
    if feeling and feeling.strip():
        if not sep:
            log.warning("HEARTBEAT template lacks sentinel — feeling will not be woven")
        else:
            try:
                feeling_sentence = frag.rstrip("\n").format(feeling=feeling.strip())
            except (KeyError, IndexError, ValueError):
                feeling_sentence = ""
    if feeling_sentence and "{feeling_sentence}" not in body:
        # An owner edit dropped the body's fill slot — the woven feeling would silently vanish.
        # Surface it (the directive still renders, just mood-less) rather than dropping it quietly.
        log.warning("HEARTBEAT body lacks {feeling_sentence} slot — mood not woven into this turn")
    try:
        return body.format(feeling_sentence=feeling_sentence)
    except (KeyError, IndexError, ValueError):
        log.warning("HEARTBEAT template malformed — using fallback proactive directive")
        return _FALLBACK_PROACTIVE


def build_dream_prompt(pending: list[tuple[int, str, int]], template: str | None = None) -> str:
    """Build the dream directive from `template` (the `DREAM.md` text) and the pending learnings
    (Story 6.2). Each item is `(id, observation, recurrence_count)`; the LLM resolves each by the
    baked `id`. Returns `""` when nothing is pending (→ the dispatch skips, no dream, no spend) —
    checked BEFORE the template so an empty dream needs no file. An observation's newlines are
    flattened so each learning stays ONE line (the id<->text association can't scramble). A
    missing/blank/malformed template degrades to `_FALLBACK_DREAM` (logged). Pure, never raises."""
    if not pending:
        return ""
    lines = "\n".join(
        f"- [id={lid}] {' '.join(obs.split())} (seen {count}×)" for lid, obs, count in pending
    )
    if not template or not template.strip() or "{lines}" not in template:
        # No template, or an owner edit dropped the `{lines}` slot — without it the pending
        # learnings would silently never reach the model (it can't resolve ids it can't see).
        # Fall to the fallback, which DOES bake the learnings in, rather than drop them quietly.
        log.warning("DREAM template missing/blank or lacks {lines} — using fallback dream directive")
        return _FALLBACK_DREAM.format(lines=lines)
    try:
        return template.format(lines=lines)
    except (KeyError, IndexError, ValueError):
        log.warning("DREAM template malformed — using fallback dream directive")
        return _FALLBACK_DREAM.format(lines=lines)
