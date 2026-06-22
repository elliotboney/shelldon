"""Story 9.3: the RISKY-tier 2-phase approval flow.

Two layers, no live LLM:
  * WORKER side — a scripted fake broker over the real bus proves the loop PAUSES on a RISKY
    call (emits RequestToolApproval, executes nothing) and RESUMES correctly on approve/deny.
  * CORE side — a fake spawner proves core parks the state in sqlite, tags the reply, routes a
    decision to a fresh resume, and drops an expired/unknown decision (AC4).
"""

from datetime import UTC, datetime

import asyncio

import msgspec
import pytest

from shelldon.contracts import (
    Actor,
    Completion,
    Envelope,
    Message,
    MsgKind,
    RequestToolApproval,
    Result,
    ToolCall,
)
from shelldon.core.bus import BusServer, connect, read_frame, write_frame
from shelldon.core.runtime import Core
from shelldon.worker.tools import build_tool_registry
from shelldon.worker.worker import ResumeState, run_worker


# ============================ WORKER side (scripted broker) ============================


async def _run_worker_scripted(sock_path, registry, completions, *, resume=None):
    """Run run_worker against a fake broker that answers each Job with the next scripted
    Completion. Returns (result_env_core_received, jobs_the_broker_saw)."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    jobs = []
    try:
        b_reader, b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)
        worker = asyncio.create_task(
            run_worker(sock_path, "t-risky", "do it", assemble=lambda m: m,
                       tool_registry=registry, resume=resume)
        )
        try:
            for comp in completions:
                job = await asyncio.wait_for(read_frame(b_reader), timeout=2.0)
                jobs.append(job)
                await write_frame(b_writer, Envelope(
                    id=f"c{len(jobs)}", kind=MsgKind.COMPLETION, src=Actor.BROKER,
                    dst=Actor.WORKER, body=comp, turn_id="t-risky",
                ))
            res = await asyncio.wait_for(srv.core_inbox.get(), timeout=2.0)
            await asyncio.wait_for(worker, timeout=2.0)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        b_writer.close()
        await b_writer.wait_closed()
        return res, jobs
    finally:
        await srv.stop()


def _reg(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    return build_tool_registry(workspace_root=ws, memory_root=tmp_path / "memory"), ws


def _write_call():
    return ToolCall(id="w1", name="write_file", args={"path": "out.txt", "content": "hi"})


async def test_worker_pauses_on_risky_call(sock_path, tmp_path):
    """AC1: a RISKY call ends the turn with a RequestToolApproval — and is NOT executed."""
    reg, ws = _reg(tmp_path)
    comp = Completion(ok=True, tool_calls=(_write_call(),))
    res, jobs = await _run_worker_scripted(sock_path, reg, [comp])

    approvals = [o for o in res.body.proposed_ops if isinstance(o, RequestToolApproval)]
    assert approvals and approvals[0].call.name == "write_file"
    assert "approve" in res.body.payload.lower()
    assert not (ws / "out.txt").exists()  # paused, never executed
    # The parked messages carry the assistant's tool-call (so resume can answer it).
    assert any(m.role == "assistant" and m.tool_calls for m in approvals[0].messages)


async def test_resume_approve_executes_and_finishes(sock_path, tmp_path):
    """AC3/AC5: approve → the fresh worker executes the pending call + finishes the turn."""
    reg, ws = _reg(tmp_path)
    call = _write_call()
    messages = (Message(role="user", content="write out.txt"),
                Message(role="assistant", content="", tool_calls=(call,)))
    resume = ResumeState(messages=messages, call=call, approved=True)
    res, jobs = await _run_worker_scripted(sock_path, reg, [Completion(ok=True, payload="Done.")], resume=resume)

    assert res.body.ok and "Done" in res.body.payload
    assert (ws / "out.txt").read_text() == "hi"  # executed on approve
    tool_msgs = [m for m in jobs[0].body.messages if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].tool_call_id == "w1"  # result fed back


async def test_resume_deny_skips_and_feeds_back(sock_path, tmp_path):
    """AC3: deny → the call is NOT executed; a 'denied' ToolResult is fed back."""
    reg, ws = _reg(tmp_path)
    call = _write_call()
    messages = (Message(role="user", content="write out.txt"),
                Message(role="assistant", content="", tool_calls=(call,)))
    resume = ResumeState(messages=messages, call=call, approved=False)
    res, jobs = await _run_worker_scripted(sock_path, reg, [Completion(ok=True, payload="Skipped.")], resume=resume)

    assert res.body.ok
    assert not (ws / "out.txt").exists()  # NOT executed on deny
    tool_msgs = [m for m in jobs[0].body.messages if m.role == "tool"]
    assert tool_msgs and "denied" in tool_msgs[0].content.lower()


# ============================ CORE side (fake spawner) ============================


class _RecordingSpawner:
    def __init__(self):
        self.resumed = []

    async def ready(self):  # pragma: no cover
        pass

    async def spawn_turn(self, turn_id, prompt):  # pragma: no cover
        pass

    async def spawn_resume(self, turn_id, messages, call, approved):
        self.resumed.append((turn_id, tuple(messages), call, approved))

    async def reap_current(self):
        pass


def _core(sock_path, tmp_path, spawner):
    return Core(sock_path, spawner, memory_root=tmp_path / "memory",
                history_path=tmp_path / "history.db", checkpoint_path=tmp_path / "s.json")


def _open_turn(core, turn_id):
    core.arbiter.submit("owner says hi")
    core._current_prompt = "owner says hi"
    core._current_turn_id = turn_id
    core.fence.open(turn_id)


def _result_env(turn_id, ops, *, payload="approve?"):
    return Envelope(id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
                    body=Result(ok=True, payload=payload, proposed_ops=ops), turn_id=turn_id)


async def test_core_parks_approval_and_tags_reply(sock_path, tmp_path):
    """AC1: core persists the resumable state to sqlite + tags the reply with the turn_id."""
    core = _core(sock_path, tmp_path, _RecordingSpawner())
    try:
        _open_turn(core, "t1")
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append((text, approval_turn_id))

        core._send_reply = _rec
        op = RequestToolApproval(call=_write_call(), summary="write_file: out.txt",
                                 messages=(Message(role="user", content="hi"),))
        await core._handle_result(_result_env("t1", [op]))

        assert core.history.take_approval("t1", datetime.now(UTC)) is not None  # parked
        assert sent[-1] == ("approve?", "t1")  # reply tagged with the turn id
    finally:
        core._cleanup()


async def test_core_decision_resumes_fresh_worker(sock_path, tmp_path):
    """AC3: a tap takes the parked state and spawns a fresh resume with the decoded args."""
    spawner = _RecordingSpawner()
    core = _core(sock_path, tmp_path, spawner)
    try:
        call = _write_call()
        messages = (Message(role="user", content="hi"),)
        core.history.park_approval("t1", msgspec.msgpack.encode((messages, call)), datetime.now(UTC))

        await core._handle_approval_decision("t1", True)

        assert len(spawner.resumed) == 1
        tid, msgs, c, approved = spawner.resumed[0]
        assert approved is True and c.name == "write_file" and msgs == messages
        # Consumed: the row is gone (a second take finds nothing).
        assert core.history.take_approval("t1", datetime.now(UTC)) is None
    finally:
        core._cleanup()


async def test_core_decision_expired_or_unknown_is_dropped(sock_path, tmp_path):
    """AC4: an unknown/expired decision NEVER resumes — it's dropped with a note."""
    spawner = _RecordingSpawner()
    core = _core(sock_path, tmp_path, spawner)
    try:
        sent = []

        async def _rec(text, *, approval_turn_id=None):
            sent.append(text)

        core._send_reply = _rec
        await core._handle_approval_decision("does-not-exist", True)

        assert spawner.resumed == []  # never resumed
        assert sent and ("expired" in sent[-1].lower() or "pending" in sent[-1].lower())
    finally:
        core._cleanup()
