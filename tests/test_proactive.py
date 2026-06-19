"""Story 5.4 — the proactive self-prompt policy (AD-1).

Pure policy: given an optional feeling word, build the directive that goes in the worker's
owner-message slot when the pet speaks up on its own. Open-ended (a thought / observation /
hello, NOT a forced question); a known feeling is woven in; None/blank yields a still-valid
feeling-agnostic directive with no "None" and no dangling "feeling ." fragment. No clock, no
I/O — instantaneous over one input. The scheduler/runtime is the driver.
"""

from shelldon.core.proactive import build_dream_prompt, build_proactive_prompt


def test_known_feeling_is_woven_in():
    """A feeling word the caller supplies appears verbatim in the directive."""
    assert "content" in build_proactive_prompt("content")
    assert "sleepy" in build_proactive_prompt("sleepy")
    assert "curious" in build_proactive_prompt("curious")


def test_distinct_feelings_produce_distinct_text():
    assert build_proactive_prompt("grumpy") != build_proactive_prompt("content")


def test_none_is_valid_and_never_literal_none():
    """No feeling -> a non-empty directive that never leaks the literal string 'None'."""
    out = build_proactive_prompt(None)
    assert out
    assert "None" not in out


def test_blank_and_whitespace_behave_like_none():
    """Empty and whitespace-only feelings are treated as 'no feeling': valid, no 'None', and
    no dangling 'feeling .' fragment from a half-filled template."""
    baseline = build_proactive_prompt(None)
    for blank in ("", "   ", "\t", "\n  "):
        out = build_proactive_prompt(blank)
        assert out == baseline
        assert "None" not in out
        assert "feeling ." not in out


def test_directive_is_open_ended_not_a_forced_question():
    """The proactive framing must invite a thought/observation/hello — not exclusively a
    question. Assert on a substring we deliberately include so this is robust to copy tweaks."""
    out = build_proactive_prompt("content")
    assert "on your mind" in out
    assert not out.rstrip(")").endswith("?")


def test_never_raises_for_any_input():
    for feeling in (None, "", "   ", "content", "grumpy", "a really long mood word here"):
        build_proactive_prompt(feeling)


# --- Story 6.2 / Epic 6 retro: the dream directive, now a pure function ---


def test_build_dream_prompt_empty_is_blank():
    assert build_dream_prompt([]) == ""  # nothing pending -> "" so the dispatch skips


def test_build_dream_prompt_bakes_ids_and_counts():
    out = build_dream_prompt([(3, "owner codes late", 5), (7, "stray musing", 1)])
    assert "[id=3] owner codes late (seen 5×)" in out
    assert "[id=7] stray musing (seen 1×)" in out
    # the directive instructs promote/prune/summary + the surfaced targets (facts + about)
    assert "resolve_learning" in out and "rewrite_summary" in out
    assert "remember" in out and "rewrite_about" in out


def test_build_dream_prompt_flattens_newlines_in_observation():
    out = build_dream_prompt([(1, "line one\nline two\n  line three", 1)])
    line = next(ln for ln in out.splitlines() if ln.startswith("- [id="))
    assert "\n" not in line and "line one line two line three" in line
