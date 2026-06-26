"""Story 10.2 — bot-writable persona ops + the instructions guardrail + the owner-gated directive.

No live LLM. The autonomous persona ops are tested at the contract+memory layer (parse → apply);
the `rewrite_instructions` guardrail at the apply layer; and the `rewrite_directive` gate at the
core layer (park / approve-applies-in-core / deny / dream-drop / slot-balance), reusing the 9.3
fake-spawner pattern from `test_risky_approval.py`.
"""

import os
from datetime import UTC, datetime

import msgspec
import pytest

from shelldon.contracts import (
    Actor,
    Envelope,
    MsgKind,
    Result,
    RewriteDirective,
    RewriteIdentity,
    RewriteInstructions,
    RewriteSoul,
    RewriteUser,
    ToolCall,
)
from shelldon.core.memory import CuratedMemory
from shelldon.core.runtime import Core
from shelldon.worker.prompt import seed_instructions
from shelldon.worker.worker import parse_reply


# ============================ contracts + memory (autonomous persona ops) ============================


def _mem(tmp_path):
    return CuratedMemory(tmp_path / "memory")


@pytest.mark.parametrize(
    "tag, content, filename, reader",
    [
        ("rewrite_soul", "curious and warm", "SOUL.md", "read_soul"),
        ("rewrite_identity", "a Pi Zero pet", "IDENTITY.md", "read_identity"),
        ("rewrite_user", "owner is Elliot", "USER.md", "read_user"),
    ],
)
def test_persona_op_roundtrip_parse_to_apply(tmp_path, tag, content, filename, reader):
    """AC1: an ops block → parse_reply → core apply writes the target file; read accessor returns it."""
    mem = _mem(tmp_path)
    reply = f'ok!\n```ops\n[{{"type":"{tag}","content":"{content}"}}]\n```'
    payload, ops, _, _ = parse_reply(reply)
    assert payload == "ok!" and len(ops) == 1
    mem.apply_memory_op(ops[0])
    assert (tmp_path / "memory" / filename).read_text() == content
    assert getattr(mem, reader)() == content


@pytest.mark.parametrize("op", [RewriteSoul(content="  "), RewriteIdentity(content=""), RewriteUser(content="\n")])
def test_persona_op_empty_content_rejected(tmp_path, op):
    """AC1: empty/blank content is rejected without writing (mirrors rewrite_about)."""
    with pytest.raises(ValueError):
        _mem(tmp_path).apply_memory_op(op)


# ---- rewrite_instructions guardrail (AC2) ----


def _valid_instructions():
    # A re-voice that KEEPS the protocol markers parse_reply needs.
    return "You are a brand new pet.\nThought line: THOUGHT: ok\nFACE: happy\nOps: ```ops\n[]\n```\n"


def test_rewrite_instructions_valid_revoice_applies(tmp_path):
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteInstructions(content=_valid_instructions()))
    assert mem.read_instructions() == _valid_instructions()


def test_rewrite_instructions_roundtrip_parse_to_apply(tmp_path):
    """AC1: rewrite_instructions also round-trips through the worker parse path (parse_reply →
    apply), not just direct construction. Built via json so the marker-bearing content escapes."""
    import json

    mem = _mem(tmp_path)
    content = _valid_instructions()
    reply = "ok!\n```ops\n" + json.dumps([{"type": "rewrite_instructions", "content": content}]) + "\n```"
    payload, ops, _, _ = parse_reply(reply)
    assert payload == "ok!" and len(ops) == 1 and isinstance(ops[0], RewriteInstructions)
    mem.apply_memory_op(ops[0])
    assert mem.read_instructions() == content


@pytest.mark.parametrize(
    "bad",
    [
        "no markers at all, just vibes",
        "THOUGHT: ok\nFACE: happy\n(no ops fence)",  # drops ```ops
        "THOUGHT: ok\n```ops\n```",  # drops FACE:
        "FACE: happy\n```ops\n```",  # drops THOUGHT:
    ],
)
def test_rewrite_instructions_dropping_markers_rejected(tmp_path, bad):
    """AC2: a rewrite that drops THOUGHT:/FACE:/the ops fence is rejected; prior file intact."""
    mem = _mem(tmp_path)
    before = mem.read_instructions()  # the seeded original
    with pytest.raises(ValueError):
        mem.apply_memory_op(RewriteInstructions(content=bad))
    assert mem.read_instructions() == before  # no-op on reject


def test_persona_rewrite_atomic_crash_leaves_prior(tmp_path, monkeypatch):
    """AC7: an interrupted os.replace on a persona rewrite leaves the prior file + no stray temp."""
    mem = _mem(tmp_path)
    mem.apply_memory_op(RewriteSoul(content="first"))
    soul = tmp_path / "memory" / "SOUL.md"
    good = soul.read_text()

    def boom(src, dst):
        raise OSError("crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        mem.apply_memory_op(RewriteSoul(content="second"))
    assert soul.read_text() == good
    assert not [p for p in (tmp_path / "memory").iterdir() if ".tmp" in p.name]


# ---- awareness (AC3) ----


def test_bot_instructions_advertises_rewrite_ops():
    """AC3: the seed BOT_INSTRUCTIONS advertises every self-knowledge rewrite op (incl. the
    previously-unadvertised rewrite_about) so the awareness copy can't silently regress."""
    text = seed_instructions()
    for op in ("rewrite_soul", "rewrite_identity", "rewrite_user", "rewrite_about", "rewrite_instructions"):
        assert op in text, f"BOT_INSTRUCTIONS no longer advertises {op}"
    assert "self-knowledge files" in text.lower()


# ============================ core: the owner-gated directive ============================


class _RecordingSpawner:
    def __init__(self):
        self.resumed = []

    async def ready(self):  # pragma: no cover
        pass

    async def spawn_turn(self, turn_id, prompt):  # pragma: no cover
        pass

    async def spawn_resume(self, turn_id, messages, call, approved):
        self.resumed.append((turn_id, tuple(messages), call, approved))

    async def reap_current(self):  # pragma: no cover
        pass


def _core(sock_path, tmp_path, spawner):
    return Core(sock_path, spawner, memory_root=tmp_path / "memory",
                history_path=tmp_path / "history.db", checkpoint_path=tmp_path / "s.json")


def _open_owner_turn(core, turn_id, *, owner=True):
    core.arbiter.submit("owner says hi")
    core._current_prompt = "owner says hi"
    core._current_turn_id = turn_id
    core._current_turn_is_owner = owner
    core.fence.open(turn_id)


def _result_env(turn_id, ops, *, payload="I'd like to update your directive — ok?"):
    return Envelope(id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
                    body=Result(ok=True, payload=payload, proposed_ops=ops), turn_id=turn_id)


async def test_directive_on_owner_turn_parks_and_tags_not_applied(sock_path, tmp_path):
    """AC4: a rewrite_directive on an owner turn does NOT apply — it parks an approval and the
    reply is tagged for the Approve/Deny surface."""
    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_owner_turn(core, "t1")
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append((text, approval_turn_id))

        core._send_reply = _rec
        await core._handle_result(_result_env("t1", [RewriteDirective(content="always be concise")]))

        assert core.memory.read_directive() is None  # NOT applied
        assert core.history.take_approval("t1", datetime.now(UTC)) is not None  # parked
        assert sent[-1][1] == "t1"  # reply tagged → keyboard renders
    finally:
        core._cleanup()


async def test_directive_on_dream_turn_dropped(sock_path, tmp_path):
    """AC6: a rewrite_directive proposed on an unattended (dream) turn is DROPPED — not parked,
    reply not tagged — while a SOUL rewrite on the same turn IS applied (only directive is barred)."""
    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_owner_turn(core, "t1", owner=False)  # unattended
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append((text, approval_turn_id))

        core._send_reply = _rec
        ops = [RewriteDirective(content="seize control"), RewriteSoul(content="evolved soul")]
        await core._handle_result(_result_env("t1", ops))

        assert core.memory.read_directive() is None  # directive dropped
        assert core.history.take_approval("t1", datetime.now(UTC)) is None  # never parked
        assert sent[-1][1] is None  # reply NOT tagged
        assert core.memory.read_soul() == "evolved soul"  # autonomous persona still applied
    finally:
        core._cleanup()


async def test_dream_applies_rewrite_user_autonomously_no_chat(sock_path, tmp_path):
    """Story 10.3 AC6: on an unattended (dream) turn — no owner present, no chat instruction —
    a `rewrite_user` proposed by the model (here a hand-crafted Result, the fake-provider stand-in)
    is APPLIED autonomously by core (USER.md written), proving the dream's self-update path. On the
    SAME turn a `rewrite_directive` is still barred (the 10.2 gate holds: dropped, not parked)."""
    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_owner_turn(core, "t1", owner=False)  # dream/unattended
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append((text, approval_turn_id))

        core._send_reply = _rec
        ops = [
            RewriteUser(content="owner prefers terse, concise replies"),
            RewriteDirective(content="rewrite the constitution"),
        ]
        await core._handle_result(_result_env("t1", ops))

        assert core.memory.read_user() == "owner prefers terse, concise replies"  # applied, no chat
        assert core.memory.read_directive() is None  # directive still barred on the dream
        assert core.history.take_approval("t1", datetime.now(UTC)) is None  # never parked
        assert sent and sent[-1][1] is None  # reply sent, not tagged for Approve/Deny
    finally:
        core._cleanup()


async def test_two_parking_ops_in_one_result_do_not_clobber(sock_path, tmp_path):
    """Review fix: RequestToolApproval + RewriteDirective in ONE Result both park under the turn_id
    key (INSERT OR REPLACE). Only the FIRST may park — the second is skipped, not silently clobbered
    so the owner approves what they were shown."""
    from shelldon.contracts import Message, RequestToolApproval, ToolCall as _TC

    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_owner_turn(core, "t1")

        async def _rec(text, *, approval_turn_id=None):
            pass

        core._send_reply = _rec
        rta = RequestToolApproval(
            call=_TC(id="w1", name="write_file", args={"path": "x", "content": "y"}),
            summary="write_file", messages=(Message(role="user", content="hi"),),
        )
        # RTA first → it parks; the directive must be skipped (not overwrite the RTA blob).
        await core._handle_result(_result_env("t1", [rta, RewriteDirective(content="be concise")]))

        blob = core.history.take_approval("t1", datetime.now(UTC))
        assert blob is not None
        _, call = msgspec.msgpack.decode(blob, type=tuple[tuple[Message, ...], ToolCall])
        assert call.name == "write_file"  # the FIRST op's blob survived, not the directive
        assert core.memory.read_directive() is None  # directive never applied
    finally:
        core._cleanup()


async def test_directive_first_then_rta_directive_parks_rta_skipped(sock_path, tmp_path):
    """Symmetric to test_two_parking_ops_in_one_result_do_not_clobber: when RewriteDirective arrives
    FIRST it parks; the subsequent RequestToolApproval is skipped (not the other way around)."""
    from shelldon.contracts import Message, RequestToolApproval, ToolCall as _TC

    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_owner_turn(core, "t1")

        async def _rec(text, *, approval_turn_id=None):
            pass

        core._send_reply = _rec
        rta = RequestToolApproval(
            call=_TC(id="w1", name="write_file", args={"path": "x", "content": "y"}),
            summary="write_file", messages=(Message(role="user", content="hi"),),
        )
        # Directive first → it parks; RTA must be skipped (not overwrite the directive blob).
        await core._handle_result(_result_env("t1", [RewriteDirective(content="be concise"), rta]))

        blob = core.history.take_approval("t1", datetime.now(UTC))
        assert blob is not None
        _, call = msgspec.msgpack.decode(blob, type=tuple[tuple[Message, ...], ToolCall])
        assert call.name == "rewrite_directive"  # the FIRST op's blob survived, not RTA
        assert core.memory.read_directive() is None  # not applied (parked, awaiting owner)
    finally:
        core._cleanup()


def _park_directive(core, turn_id, content):
    call = ToolCall(id=turn_id, name="rewrite_directive", args={"content": content})
    core.history.park_approval(turn_id, msgspec.msgpack.encode(((), call)), datetime.now(UTC))


async def test_directive_approve_applies_in_core_no_resume(sock_path, tmp_path):
    """AC5: Approve → core applies the directive DIRECTLY (no worker resume), and the slot is not
    leaked (arbiter idle after)."""
    spawner = _RecordingSpawner()
    core = _core(sock_path, tmp_path, spawner)
    try:
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append(text)

        core._send_reply = _rec
        _park_directive(core, "t1", "always be kind and concise")

        await core._handle_approval_decision("t1", True)

        assert core.memory.read_directive() == "always be kind and concise"  # applied in core
        assert spawner.resumed == []  # NO worker resume
        assert core.arbiter.is_idle  # slot not leaked
        assert core.history.take_approval("t1", datetime.now(UTC)) is None  # consumed
        assert sent and "directive" in sent[0].lower()  # AC5: confirmation sent
    finally:
        core._cleanup()


async def test_directive_deny_leaves_unchanged(sock_path, tmp_path):
    """AC5: Deny → DIRECTIVE.md unchanged, no resume, no leaked slot."""
    spawner = _RecordingSpawner()
    core = _core(sock_path, tmp_path, spawner)
    try:
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append(text)

        core._send_reply = _rec
        _park_directive(core, "t1", "do something drastic")

        await core._handle_approval_decision("t1", False)

        assert core.memory.read_directive() is None  # unchanged
        assert spawner.resumed == []
        assert core.arbiter.is_idle
        assert sent and "left" in sent[0].lower()  # AC5: "left as-is" denial note
    finally:
        core._cleanup()
