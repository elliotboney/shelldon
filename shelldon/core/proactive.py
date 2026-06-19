"""core/proactive — the self-initiated-turn prompt policies (Story 5.4 / 6.2, AD-1).

Pure functions that build the directive placed in the worker's owner-message slot when the
pet acts *on its own* — no owner message to reply to. Two self-initiated turns share this
module: the **proactive musing** (5.4 — speak up, mood-tinted) and the **dream cycle** (6.2 —
review pending learnings, promote/prune, summarize). Both framings are parenthetical
SELF-PROMPTS so the worker knows it is initiating, not replying.

The *policy* (what the directive says) lives here as pure, deterministic functions; the
*driver* (when to fire, where to read state) lives in the scheduler/runtime. Pure, no I/O,
no clock, never raises — mirrors `core/reflexes.py`. Extracting the dream directive here (it
was inline in `runtime.py`) is the Epic 6 retro's safe slice of the `runtime.py` coupling
reduction (the fuller dispatch/scheduler extract awaits Epic 7).

LLM-free (AD-1): imports only stdlib.
"""

#: The single tunable point. `{feeling}` is filled when a feeling is known; when it is
#: not, the whole sentence is dropped so we never emit "You're feeling ." or the literal
#: "None". Open-ended on purpose: "share whatever's on your mind" — the thought/observation/
#: hello cue keeps it from collapsing into a forced question.
_FEELING_SENTENCE = " You're feeling {feeling}."

_DIRECTIVE = (
    "(Self-prompt: there's no owner message to reply to right now — you're speaking up on "
    "your own.{feeling_sentence} Share whatever's on your mind: a passing thought, "
    "something you noticed, or just a hello. It doesn't have to be a question.)"
)


def build_proactive_prompt(feeling: str | None) -> str:
    """Build the open-ended proactive self-prompt directive.

    When `feeling` is a non-empty string it is woven in ("You're feeling {feeling}.");
    when it is None or blank/whitespace-only, a feeling-agnostic directive is returned with
    no dangling "feeling ." fragment and never the literal "None". Pure and deterministic.
    """
    feeling_sentence = ""
    if feeling and feeling.strip():
        feeling_sentence = _FEELING_SENTENCE.format(feeling=feeling.strip())
    return _DIRECTIVE.format(feeling_sentence=feeling_sentence)


#: The dream directive (Story 6.2). Promotion targets are both surfaced by the 4.4 assembly
#: (after the Epic 6 retro facts/ fix): a specific owner fact -> `remember` into facts/, broad
#: self-knowledge -> `rewrite_about`. The driver (runtime) reads the pending learnings.
_DREAM_DIRECTIVE = (
    "(Dream-time reflection: no owner message to reply to. Below are things you've noticed "
    "and jotted down. Decide which are durable enough to keep: for each one worth remembering, "
    "save it — a specific fact about the owner with a `remember` op (collection facts), or "
    "broader self-knowledge with `rewrite_about` — AND mark it resolved with `resolve_learning` "
    'status "promoted"; for the rest, mark them `resolve_learning` status "pruned". Then refresh '
    "a short running summary of recent conversation with `rewrite_summary` so your memory stays "
    "compact. Finally, reply with a brief note that you tidied up.)\n\n"
    "# Pending learnings\n{lines}"
)


def build_dream_prompt(pending: list[tuple[int, str, int]]) -> str:
    """Build the dream directive from the pending learnings (Story 6.2). Each item is
    `(id, observation, recurrence_count)`; the LLM resolves each by the baked `id`. Returns
    `""` when nothing is pending (→ the dispatch skips, no dream, no spend). An observation's
    newlines are flattened so each learning stays ONE line (the id<->text association can't
    scramble). Pure, deterministic, never raises."""
    if not pending:
        return ""
    lines = "\n".join(
        f"- [id={lid}] {' '.join(obs.split())} (seen {count}×)" for lid, obs, count in pending
    )
    return _DREAM_DIRECTIVE.format(lines=lines)
