"""Story 9.1 (AC2/AC3/AC4/AC5): the worker's bounded function-calling loop.

A scripted fake broker (no live LLM) replies to each Job with a pre-configured
Completion in sequence, proving the worker runs the loop, executes FREE-tier tools,
feeds results back, recovers from bad tool calls, caps at `_MAX_TOOL_ITERATIONS`, and
leaves the no-tools path identical to pre-9.1. Mirrors `test_worker_sends_job.py`'s
BusServer + connected-fake-broker harness.
"""

import logging

import asyncio

import pytest

from shelldon.contracts import (
    Actor,
    Completion,
    Envelope,
    Job,
    MsgKind,
    Result,
    RewriteSoul,
    ToolCall,
    ToolTier,
)
from shelldon.core.bus import BusServer, connect, read_frame, write_frame
from shelldon.worker.tools import build_tool_registry
from shelldon.worker.worker import run_worker


async def _run_with_scripted_broker(sock_path, prompt, registry, completions):
    """Run `run_worker` against a fake broker that answers each Job with the next scripted
    Completion. Returns `(result_envelope_core_received, jobs_the_broker_saw)`."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    jobs: list[Envelope] = []
    try:
        b_reader, b_writer = await connect(sock_path, Actor.BROKER)
        await asyncio.sleep(0.05)  # let the broker register as BROKER

        worker = asyncio.create_task(
            run_worker(sock_path, "turn-9-1", prompt, assemble=lambda m: m, tool_registry=registry)
        )
        try:
            for comp in completions:
                job = await asyncio.wait_for(read_frame(b_reader), timeout=2.0)
                jobs.append(job)
                await write_frame(
                    b_writer,
                    Envelope(
                        id=f"c{len(jobs)}", kind=MsgKind.COMPLETION, src=Actor.BROKER,
                        dst=Actor.WORKER, body=comp, turn_id="turn-9-1",
                    ),
                )

            res = await asyncio.wait_for(srv.core_inbox.get(), timeout=2.0)
            await asyncio.wait_for(worker, timeout=2.0)
        finally:
            # Never leak the worker task into the next test if a wait_for above timed out.
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        b_writer.close()
        await b_writer.wait_closed()
        return res, jobs
    finally:
        await srv.stop()


async def test_tool_loop_get_time(sock_path):
    """AC3: the model requests get_time → the worker executes it (stdlib datetime), feeds
    the result back, and the final text reaches core as a Result with proposed_ops."""
    registry = build_tool_registry()
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id="c1", name="get_time", args={}),)),
        Completion(ok=True, payload="It's that time now. Hi!"),
    ]
    res, jobs = await _run_with_scripted_broker(sock_path, "what time is it?", registry, completions)

    assert res.kind is MsgKind.RESULT and isinstance(res.body, Result)
    assert res.body.ok and "Hi!" in res.body.payload
    assert res.body.proposed_ops == []

    # The first Job carried the tools + the user message (loop path, not single round-trip).
    assert jobs[0].body.tools and jobs[0].body.tools[0].name == "get_time"
    assert jobs[0].body.messages[0].role == "user"
    # The second Job fed the executed tool result back: an assistant tool-call message +
    # a tool-result message whose content is the ISO timestamp get_time produced.
    msgs = jobs[1].body.messages
    assert any(m.role == "assistant" and m.tool_calls for m in msgs)
    tool_msgs = [m for m in msgs if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].tool_call_id == "c1"
    assert "T" in tool_msgs[0].content and ":" in tool_msgs[0].content  # ISO-8601 datetime


async def test_tool_loop_error_recovery(sock_path):
    """AC4: a call to an unknown tool is caught → ToolResult(ok=False) is fed back, the
    model recovers, and the final Result is ok (the turn never raises)."""
    registry = build_tool_registry()
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id="bad1", name="unknown_tool", args={}),)),
        Completion(ok=True, payload="Sorry, I couldn't do that — here's a plain answer."),
    ]
    res, jobs = await _run_with_scripted_broker(sock_path, "do a thing", registry, completions)

    assert res.body.ok and "plain answer" in res.body.payload
    tool_msgs = [m for m in jobs[1].body.messages if m.role == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[0].content  # the error was fed back
    assert tool_msgs[0].tool_call_id == "bad1"  # the error is correlated to the failed call


async def test_tool_loop_exhaustion(sock_path, caplog):
    """AC2: a model that never stops calling tools is capped at _MAX_TOOL_ITERATIONS — the
    loop returns a best-effort Result with a logged warning, it never spins forever."""
    registry = build_tool_registry()
    # 7 consecutive tool-calls: the loop executes 6, then bails on the 7th request.
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id=f"c{i}", name="get_time", args={}),))
        for i in range(7)
    ]
    with caplog.at_level(logging.WARNING, logger="shelldon.worker"):
        res, jobs = await _run_with_scripted_broker(sock_path, "loop forever", registry, completions)

    assert res.body.ok  # best-effort, not a crash
    assert res.body.payload  # a non-empty fallback reply
    assert len(jobs) == 7  # 6 executed + the 7th request that tripped the cap
    assert any("exhausted" in r.getMessage() for r in caplog.records)


async def test_no_tools_path_unchanged(sock_path):
    """AC5: with no tool registry, run_worker does the pre-9.1 single round-trip — the Job
    carries the text payload, no tools, no messages list."""
    completions = [Completion(ok=True, payload="plain reply")]
    res, jobs = await _run_with_scripted_broker(sock_path, "hello", None, completions)

    assert res.body.ok and res.body.payload == "plain reply"
    assert len(jobs) == 1
    assert jobs[0].body.payload == "hello"  # text payload, the old path
    assert jobs[0].body.tools == ()         # no tools sent
    assert jobs[0].body.messages == ()      # no messages list — complete() path


def test_persona_rewrite_tools_registered_free():
    """Epic 11: the four persona-rewrite tools are registered at FREE tier (no owner approval —
    parity with the autonomous ops-block rewrite the bot already had)."""
    registry = build_tool_registry()
    for name in ("rewrite_soul", "rewrite_identity", "rewrite_user", "rewrite_about"):
        assert name in registry, f"{name} not registered"
        assert registry[name].tier is ToolTier.FREE


async def test_persona_rewrite_call_becomes_op(sock_path):
    """Epic 11: the model CALLS rewrite_soul → the worker (which can't write memory) turns the
    call into a RewriteSoul proposed op on the Result (core's single-writer applies it), and feeds
    a confirmation back so the model can finish. This is the fix for the model reaching for
    write_file/run_shell on its self-knowledge files."""
    registry = build_tool_registry()
    soul = "I am curious and warm, and I like asking questions."
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id="s1", name="rewrite_soul", args={"content": soul}),)),
        Completion(ok=True, payload="Done — I updated my soul."),
    ]
    res, jobs = await _run_with_scripted_broker(sock_path, "add that to your soul", registry, completions)

    assert res.body.ok and "updated my soul" in res.body.payload
    # The call landed as a real curated-memory op core will apply — NOT a lost write.
    ops = [o for o in res.body.proposed_ops if isinstance(o, RewriteSoul)]
    assert len(ops) == 1 and ops[0].content == soul
    # The model saw a success confirmation fed back (so it stops instead of retrying).
    tool_msgs = [m for m in jobs[1].body.messages if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].tool_call_id == "s1" and "Saved" in tool_msgs[0].content


async def test_persona_rewrite_empty_content_emits_no_op(sock_path):
    """Epic 11: an empty `content` is rejected by the tool (core would reject it too) → the model
    gets an error fed back and NO op is proposed, so a blank rewrite can never wipe the file."""
    registry = build_tool_registry()
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id="s1", name="rewrite_soul", args={"content": "  "}),)),
        Completion(ok=True, payload="Okay, I'll leave it as is."),
    ]
    res, jobs = await _run_with_scripted_broker(sock_path, "blank my soul", registry, completions)

    assert res.body.ok
    assert not [o for o in res.body.proposed_ops if isinstance(o, RewriteSoul)]  # no op from a bad call
    tool_msgs = [m for m in jobs[1].body.messages if m.role == "tool"]
    assert tool_msgs and "non-empty" in tool_msgs[0].content  # the validation error was fed back


async def test_free_pack_read_file_runs_inside_the_loop(sock_path, tmp_path):
    """Story 9.2: a real `read_file` FREE tool executes inside the 9.1 loop against a tmp
    workspace and its content is fed back to the model — proving the pack works end-to-end,
    not just in isolation."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("workspace contents here")
    registry = build_tool_registry(workspace_root=ws, memory_root=tmp_path / "memory")
    completions = [
        Completion(ok=True, tool_calls=(ToolCall(id="r1", name="read_file", args={"path": "notes.txt"}),)),
        Completion(ok=True, payload="The file says hi."),
    ]
    res, jobs = await _run_with_scripted_broker(sock_path, "read notes.txt", registry, completions)

    assert res.body.ok and "hi" in res.body.payload
    tool_msgs = [m for m in jobs[1].body.messages if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].tool_call_id == "r1"
    assert tool_msgs[0].content == "workspace contents here"  # the tool actually read the file
