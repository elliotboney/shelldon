"""Story 5.4 / 6.2 / 10.3 — the self-initiated-turn prompt policies (AD-1).

Pure policy: each builder takes a template (the `HEARTBEAT.md`/`DREAM.md` seed text, Story 10.3)
plus its variable inputs and fills it. A known feeling is woven into the proactive musing; the
dream bakes the pending learnings. A missing/blank template degrades to a terse built-in fallback
(never raises). No clock, no I/O in this module — the file read lives in the dispatch driver.
"""

from importlib import resources

from shelldon.core.proactive import build_dream_prompt, build_proactive_prompt


def _heartbeat() -> str:
    """The shipped HEARTBEAT.md seed (the same text seeded into the worktree at boot)."""
    return resources.files("shelldon.persona").joinpath("HEARTBEAT.md").read_text(encoding="utf-8")


def _dream() -> str:
    """The shipped DREAM.md seed."""
    return resources.files("shelldon.persona").joinpath("DREAM.md").read_text(encoding="utf-8")


# --- Story 5.4: the proactive musing, now filled from HEARTBEAT.md ---


def test_known_feeling_is_woven_in():
    """A feeling word the caller supplies appears verbatim in the directive."""
    assert "content" in build_proactive_prompt("content", _heartbeat())
    assert "sleepy" in build_proactive_prompt("sleepy", _heartbeat())
    assert "curious" in build_proactive_prompt("curious", _heartbeat())


def test_distinct_feelings_produce_distinct_text():
    assert build_proactive_prompt("grumpy", _heartbeat()) != build_proactive_prompt("content", _heartbeat())


def test_none_is_valid_and_never_literal_none():
    """No feeling -> a non-empty directive that never leaks the literal string 'None'."""
    out = build_proactive_prompt(None, _heartbeat())
    assert out
    assert "None" not in out


def test_blank_and_whitespace_behave_like_none():
    """Empty and whitespace-only feelings are treated as 'no feeling': valid, no 'None', and
    no dangling 'feeling .' fragment from a half-filled template."""
    baseline = build_proactive_prompt(None, _heartbeat())
    for blank in ("", "   ", "\t", "\n  "):
        out = build_proactive_prompt(blank, _heartbeat())
        assert out == baseline
        assert "None" not in out
        assert "feeling ." not in out


def test_directive_is_open_ended_not_a_forced_question():
    """The proactive framing must invite a thought/observation — not exclusively a question.
    Assert a question is offered as ONE option (not the whole ask) and the directive itself
    doesn't end as a question. Robust to copy tweaks."""
    out = build_proactive_prompt("content", _heartbeat())
    assert "ask a question" in out  # a question is one option among several, not forced
    assert not out.rstrip(")").endswith("?")


def test_never_raises_for_any_input():
    for feeling in (None, "", "   ", "content", "grumpy", "a really long mood word here"):
        build_proactive_prompt(feeling, _heartbeat())
        build_proactive_prompt(feeling)  # no template -> fallback, still never raises


# --- Story 6.2: the dream directive, filled from DREAM.md ---


def test_build_dream_prompt_empty_is_blank():
    assert build_dream_prompt([], _dream()) == ""  # nothing pending -> "" so the dispatch skips
    assert build_dream_prompt([]) == ""  # ... even with no template (short-circuits first)


def test_build_dream_prompt_bakes_ids_and_counts():
    out = build_dream_prompt([(3, "owner codes late", 5), (7, "stray musing", 1)], _dream())
    assert "[id=3] owner codes late (seen 5×)" in out
    assert "[id=7] stray musing (seen 1×)" in out
    # the directive instructs promote/prune/summary + the surfaced targets (facts + about)
    assert "resolve_learning" in out and "rewrite_summary" in out
    assert "remember" in out and "rewrite_about" in out


def test_build_dream_prompt_flattens_newlines_in_observation():
    out = build_dream_prompt([(1, "line one\nline two\n  line three", 1)], _dream())
    line = next(ln for ln in out.splitlines() if ln.startswith("- [id="))
    assert "\n" not in line and "line one line two line three" in line


# --- HEARTBEAT.md structural contract (the anti-repetition rewrite deliberately changed the copy) ---


def test_proactive_from_seed_file_holds_the_contract():
    """The proactive directive built from HEARTBEAT.md must: weave the feeling when present, DROP
    it entirely when None, wrap in the self-prompt parens, and carry the anti-repetition steer
    (the point of the rewrite). Not byte-pinned — copy is meant to evolve — but these invariants
    the fill logic + face-vocab weave depend on must hold."""
    with_feeling = build_proactive_prompt("content", _heartbeat())
    assert with_feeling.startswith("(Self-prompt") and with_feeling.rstrip().endswith(")")
    assert "You're feeling content" in with_feeling  # the mood is woven when known
    assert "Do NOT reuse" in with_feeling  # the anti-repeat steer is present

    without = build_proactive_prompt(None, _heartbeat())
    assert "feeling" not in without  # no mood woven, no dangling "feeling ." fragment
    assert "Do NOT reuse" in without


def test_dream_from_seed_file_preserves_old_and_adds_persona_invite():
    """AC3+AC5: DREAM.md keeps the entire prior dream instruction (promote/prune/remember/about/
    summary + the {lines} block) AND adds the autonomous SOUL/IDENTITY/USER invitation. The dream
    is NOT byte-identical (AC5 deliberately grows the copy) — so assert preservation + the new ops."""
    out = build_dream_prompt([(3, "owner codes late", 5)], _dream())
    # prior dream behavior preserved (the 6.2 instruction set)
    for token in ("resolve_learning", "remember", "rewrite_about", "rewrite_summary",
                  '"promoted"', '"pruned"', "# Pending learnings", "[id=3] owner codes late (seen 5×)"):
        assert token in out, f"dream lost prior token {token!r}"
    # new in 10.3: the dream invites autonomous persona evolution (no chat instruction)
    for op in ("rewrite_soul", "rewrite_identity", "rewrite_user"):
        assert op in out, f"dream missing persona-edit invite {op!r}"


# --- Story 10.3: a missing/blank/malformed template degrades safe (fail-soft, never raises) ---


def test_proactive_fallback_on_missing_template():
    """AC4: no template (None/blank) -> a terse valid fallback directive, never raises, no 'None'."""
    for tmpl in (None, "", "   "):
        out = build_proactive_prompt("content", tmpl)
        assert out and "None" not in out
        assert out.startswith("(Self-prompt:")
    # the fallback still produces a feeling-agnostic (no dangling 'feeling .') directive
    assert "feeling ." not in build_proactive_prompt(None, None)


def test_dream_fallback_on_missing_template_still_bakes_lines():
    """AC4: no DREAM template -> the terse fallback, but the pending learnings are still baked in."""
    out = build_dream_prompt([(9, "owner likes terse replies", 2)], None)
    assert out and "[id=9] owner likes terse replies (seen 2×)" in out
    assert "resolve_learning" in out  # fallback still drives promote/prune
    # an empty pending list with no template is still "" (short-circuits before the fallback)
    assert build_dream_prompt([], None) == ""


def test_malformed_template_degrades_not_raises():
    """AC4/AC7: a template with a stray brace (would break str.format) degrades to fallback, never raises."""
    assert build_proactive_prompt("content", "broken {oops} body").startswith("(Self-prompt:")
    assert "[id=1]" in build_dream_prompt([(1, "x", 1)], "broken {oops} dream {lines}")


def test_dream_template_without_lines_slot_falls_back_and_keeps_learnings(caplog):
    """Review (edge-case #3): an owner edit that drops the `{lines}` slot would silently lose the
    pending learnings (the model can't resolve ids it can't see). The builder must NOT drop them —
    it falls to the fallback (which bakes them in) and logs the degrade."""
    import logging
    out = build_dream_prompt([(5, "owner codes late", 3)], "Dream away. No data slot here.")
    assert "[id=5] owner codes late (seen 3×)" in out  # learnings preserved, not dropped
    with caplog.at_level(logging.WARNING):
        build_dream_prompt([(5, "x", 1)], "no slot")
    assert any("lines" in r.message for r in caplog.records)


def test_heartbeat_body_without_feeling_slot_logs_dropped_mood(caplog):
    """Review (edge-case #2): an owner edit dropping the `{feeling_sentence}` slot would silently
    discard the woven mood. The directive still renders (no crash), but the drop is logged."""
    import logging
    # body has no {feeling_sentence}; the fragment is present so a feeling IS computed then dropped
    tmpl = "(Self-prompt: just speak up.)\n---\n You're feeling {feeling}."
    with caplog.at_level(logging.WARNING):
        out = build_proactive_prompt("grumpy", tmpl)
    assert out == "(Self-prompt: just speak up.)"  # renders, mood-less, never raises
    assert any("feeling_sentence" in r.message for r in caplog.records)
