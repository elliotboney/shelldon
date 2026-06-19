"""core/proactive — the proactive self-prompt policy (Story 5.4, AD-1).

A pure function that builds the directive placed in the worker's owner-message slot
when the pet speaks up *on its own* — no owner message to reply to. The framing is a
parenthetical SELF-PROMPT so the worker knows it is initiating, not replying, and it is
OPEN-ENDED: the pet shares whatever is on its mind (a passing thought, an observation, a
hello) — deliberately NOT a forced "ask your owner a question" (owner wants thoughts, not
an interrogation).

The *policy* (what the directive says) lives here as a pure, deterministic function; the
*driver* (when to fire a proactive turn, where to read the feeling from) lives in the
scheduler/runtime. Pure, no I/O, no clock, never raises — mirrors `core/reflexes.py`.

LLM-free (AD-1): imports only stdlib (nothing — single-template compose).
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
