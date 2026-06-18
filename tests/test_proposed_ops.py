"""Story 4.5 — the worker's reply→ops parse and core's fenced apply loop.

Covers the two halves of the write-back wire that aren't an over-the-bus topology
assertion: the worker `parse_reply` (ops block → proposed_ops, stripped payload) and
core `_handle_result` applying proposed_ops AFTER the reply, rejecting invalid/oversized
proposals without side effects. Core apply is driven by constructing a Result directly —
no real worker/broker needed (the apply path is core-only).
"""

import pytest

from shelldon.contracts import (
    Actor,
    AddFace,
    Envelope,
    LogEpisode,
    MsgKind,
    Remember,
    RewriteAbout,
    Result,
)
from shelldon.core.runtime import MAX_PROPOSED_OPS, Core
from shelldon.worker.worker import parse_reply


class _NoopSpawner:
    async def ready(self):  # pragma: no cover - turn loop never driven here
        pass

    async def spawn_turn(self, turn_id, prompt):  # pragma: no cover
        pass

    async def reap_current(self):  # pragma: no cover
        pass


# --- worker parse_reply ---


def test_parse_reply_plain_text_no_ops():
    payload, ops = parse_reply("just a normal reply")
    assert payload == "just a normal reply"
    assert ops == []


def test_parse_reply_extracts_ops_and_strips_block():
    reply = (
        "Sure, noting that.\n"
        "```ops\n"
        '[{"type":"remember","collection":"people","name":"Alex","content":"owner friend"}]\n'
        "```\n"
    )
    payload, ops = parse_reply(reply)
    assert "```ops" not in payload and payload == "Sure, noting that."
    assert len(ops) == 1
    assert type(ops[0]) is Remember and ops[0].name == "Alex"


def test_parse_reply_malformed_block_yields_no_ops_unchanged_reply():
    """A malformed ops block must NOT corrupt the reply — the whole text stays the
    payload and no ops are proposed (whole-reject)."""
    reply = "Hi.\n```ops\n{not valid json\n```\n"
    payload, ops = parse_reply(reply)
    assert ops == []
    assert payload == reply  # untouched — we couldn't trust the block


def test_parse_reply_unknown_op_tag_rejects_whole_block():
    reply = '```ops\n[{"type":"obliterate","x":1}]\n```'
    payload, ops = parse_reply(reply)
    assert ops == []
    assert payload == reply


def test_parse_reply_handles_multiple_blocks_without_leaking():
    """Two ops blocks: both decode and BOTH are stripped — the second never leaks into
    the user-facing payload."""
    reply = (
        "first.\n"
        '```ops\n[{"type":"rewrite_about","content":"a"}]\n```\n'
        "middle.\n"
        '```ops\n[{"type":"log_episode","content":"b"}]\n```\n'
        "end."
    )
    payload, ops = parse_reply(reply)
    assert "```ops" not in payload
    assert payload.startswith("first.") and "middle." in payload and payload.endswith("end.")
    assert [type(o) for o in ops] == [RewriteAbout, LogEpisode]


# --- core applies proposed_ops (fenced path) ---


def _core(sock_path, tmp_path):
    return Core(sock_path, _NoopSpawner(), memory_root=tmp_path / "memory")


def _open_turn(core, turn_id, prompt="owner says hi"):
    core.arbiter.submit(prompt)  # mark a turn in flight so complete() is balanced
    core._current_prompt = prompt
    core._current_turn_id = turn_id
    core.fence.open(turn_id)


def _result_env(turn_id, ops, *, ok=True, payload="ok"):
    return Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=ok, payload=payload, proposed_ops=ops), turn_id=turn_id,
    )


async def test_core_applies_valid_op_after_reply(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t1")
    op = Remember(collection="people", name="Alex", content="owner friend")

    # Spy the order: the user-facing reply MUST be delivered before any op is applied
    # (AC2 — a bad/slow op can never block or precede the reply).
    order: list[str] = []
    orig_reply, orig_apply = core._send_reply, core.memory.apply_memory_op

    async def _spy_reply(text):
        order.append("reply")
        await orig_reply(text)

    def _spy_apply(o):
        order.append("apply")
        orig_apply(o)

    core._send_reply = _spy_reply
    core.memory.apply_memory_op = _spy_apply

    await core._handle_result(_result_env("t1", [op]))
    assert (tmp_path / "memory" / "people" / "alex.md").read_text() == "owner friend"
    assert order == ["reply", "apply"]  # reply strictly before the op
    assert core.fence.is_idle  # turn closed cleanly


async def test_core_skips_invalid_op_but_keeps_turn_and_other_ops(sock_path, tmp_path):
    """An invalid op is logged+skipped — it never crashes the turn loop, and a valid
    op alongside it still applies (skip, not abort)."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t2")
    bad = Remember(collection="bogus", name="x", content="c")  # bad collection
    good = RewriteAbout(content="my self-summary")
    await core._handle_result(_result_env("t2", [bad, good]))
    assert not (tmp_path / "memory" / "bogus").exists()
    assert (tmp_path / "memory" / "about.md").read_text() == "my self-summary"
    assert core.fence.is_idle


async def test_core_caps_oversized_proposal(sock_path, tmp_path):
    """More than MAX_PROPOSED_OPS ops → only the cap is applied (overflow dropped, not
    silently — the rest of the turn is unaffected)."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t3")
    ops = [
        Remember(collection="facts", name=f"fact-{i}", content="c")
        for i in range(MAX_PROPOSED_OPS + 4)
    ]
    await core._handle_result(_result_env("t3", ops))
    written = list((tmp_path / "memory" / "facts").iterdir())
    assert len(written) == MAX_PROPOSED_OPS
    assert core.fence.is_idle


# --- Story 3.4: the add_face op rides the same wire ---


def test_parse_reply_extracts_add_face_op():
    reply = (
        "Adding that face!\n"
        "```ops\n"
        '[{"type":"add_face","name":"proud","valence":[0.4,1.0],'
        '"arousal":[0.3,1.0],"energy":[0.5,1.0],"token":":>"}]\n'
        "```"
    )
    payload, ops = parse_reply(reply)
    assert payload == "Adding that face!"
    assert len(ops) == 1 and type(ops[0]) is AddFace
    assert ops[0].name == "proud" and ops[0].token == ":>"


def test_parse_reply_malformed_add_face_rejects_whole_block():
    """A bad range shape (3 elements) fails the closed schema → whole block rejected,
    reply left intact (Story 4.5 discipline)."""
    reply = '```ops\n[{"type":"add_face","name":"x","valence":[0,0,0],"arousal":[0,1],"energy":[0,1]}]\n```'
    payload, ops = parse_reply(reply)
    assert ops == [] and payload == reply


async def test_core_applies_add_face_and_it_becomes_selectable(sock_path, tmp_path):
    """A proposed AddFace is applied via apply_add_face (3.3) and selectable on the next
    matching mood (AC2)."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "f1")
    op = AddFace(name="smug", valence=(0.3, 1.0), arousal=(-0.2, 0.2), energy=(0.4, 1.0), token=">:)")
    await core._handle_result(_result_env("f1", [op]))
    assert "smug" in [f.name for f in core.faces.faces]
    assert core.faces.select(0.5, 0.0, 0.7) == ">:)"  # selectable on a matching mood
    assert core.fence.is_idle


async def test_core_rejects_malformed_add_face_without_mutating(sock_path, tmp_path):
    """An out-of-range AddFace is rejected by apply_add_face's validation → registry
    unchanged, reply already delivered, turn survives."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "f2")
    before = [f.name for f in core.faces.faces]
    bad = AddFace(name="broken", valence=(0.0, 5.0), arousal=(0.0, 0.1), energy=(0.0, 0.1))  # valence hi > 1.0
    await core._handle_result(_result_env("f2", [bad]))
    assert [f.name for f in core.faces.faces] == before  # nothing added
    assert core.fence.is_idle


async def test_core_rejects_empty_name_add_face(sock_path, tmp_path):
    """AC2: an empty-name AddFace is rejected by validation — registry unchanged."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "f4")
    before = [f.name for f in core.faces.faces]
    await core._handle_result(_result_env("f4", [AddFace(name="", valence=(0.0, 1.0), arousal=(0.0, 1.0), energy=(0.5, 1.0))]))
    assert [f.name for f in core.faces.faces] == before
    assert core.fence.is_idle


async def test_core_rejects_duplicate_add_face_without_replace(sock_path, tmp_path):
    """AC2: a duplicate name without replace=True is rejected — the existing face is not
    mutated or duplicated."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "f5")
    before = [f.name for f in core.faces.faces]
    assert "content" in before  # a seeded default
    dup = AddFace(name="content", valence=(0.0, 0.1), arousal=(0.0, 0.1), energy=(0.9, 1.0), replace=False)
    await core._handle_result(_result_env("f5", [dup]))
    assert [f.name for f in core.faces.faces] == before  # not duplicated, not reordered
    assert core.fence.is_idle


async def test_core_mixed_batch_routes_each_op_to_its_writer(sock_path, tmp_path):
    """A batch with an AddFace AND a Remember proves both dispatch paths off the one
    wire (AC3): the face goes to the registry, the memory-op to the curated tree."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "f3")
    face = AddFace(name="curiouser", valence=(0.0, 1.0), arousal=(0.5, 1.0), energy=(0.5, 1.0))
    mem = Remember(collection="people", name="Alex", content="owner friend")
    await core._handle_result(_result_env("f3", [face, mem]))
    assert "curiouser" in [f.name for f in core.faces.faces]
    assert (tmp_path / "memory" / "people" / "alex.md").read_text() == "owner friend"
    assert core.fence.is_idle


async def test_core_failure_result_skips_ops(sock_path, tmp_path):
    """A failure Result degrades — even if it carries ops, the failure branch never
    applies them (they're skipped, nothing is written)."""
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t4")
    op = Remember(collection="people", name="Ghost", content="should not persist")
    await core._handle_result(_result_env("t4", [op], ok=False, payload=""))
    assert not (tmp_path / "memory" / "people").exists()  # ops skipped on the failure path
    assert core.fence.is_idle
