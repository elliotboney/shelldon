"""The ephemeral worker child (AD-3): assemble a prompt, proxy to the broker, parse
the reply into proposed ops, emit the Result, die.

The worker is the brain adapter (AD-3): it owns BOTH ends of the LLM exchange — it
sends a Job to the broker and, when the broker returns the raw Completion, it parses
its own reply into a structured `Result` (`payload` + a closed `proposed_ops` list)
and sends `Result→core` (Story 4.5). Core (sole writer, AD-5) validates+applies the
ops; the worker never writes. The broker stays a pure egress boundary — it does no
pet-domain parsing (AD-2). Real prompt assembly (history + memory) is Story 4.4; here
the prompt is whatever core handed in, and the only ops are whatever the reply carries.

Fence-friendly: the Result echoes the turn's `turn_id` so core fences it (AD-12). A
broker that never answers (disconnect / bad frame) yields a failure Result so core
degrades rather than hanging — the core turn timeout is the backstop.
"""

import asyncio
import logging
import re
from uuid import uuid4

import msgspec

from shelldon.contracts import (
    Actor,
    Completion,
    Envelope,
    Job,
    Message,
    MsgKind,
    ProposedOp,
    Result,
    ToolDefinition,
)
from shelldon.core.bus import connect, read_frame, write_frame
from shelldon.worker.prompt import build_prompt
from shelldon.worker.tools import ToolSpec, execute_tool

log = logging.getLogger("shelldon.worker")

#: Bound on the agentic tool loop (Story 9.1 AC2): the worker executes at most this many
#: rounds of tool-calls before giving up with a best-effort reply — a model that loops
#: forever can never wedge the turn. (After the Nth execution the worker still does ONE
#: more round-trip so the model can produce a final text answer; if THAT still returns
#: tool-calls the cap trips.) Paired with the `_COMPLETION_TIMEOUT_S` budget below
#: (whichever trips first ends the loop).
_MAX_TOOL_EXECUTIONS = 6

#: The reply→ops wire format: a single fenced ```ops block holding a JSON array of
#: tagged memory-ops. 4.5 owns this PARSE; Story 4.4 owns the prompt that elicits it
#: (the format may be co-adjusted there). A reply with no such block is a plain reply.
_OPS_BLOCK_RE = re.compile(r"```ops[ \t]*\n(.*?)```", re.DOTALL)

#: Decoder for the ops block payload — a closed list of the ProposedOp union (memory-ops
#: + the face op, Story 3.4). A malformed or unknown op fails the WHOLE block (the
#: 3.1/3.3/4.2 whole-reject discipline).
_OPS_DECODER = msgspec.json.Decoder(list[ProposedOp])

#: Anti-wedge backstop: the worker waits at most this long for the broker's Completion,
#: then emits a failure Result and exits — so a crashed/absent broker can never leave the
#: worker blocked forever (which would never reap → stick the ≤1 bound, AD-9).
#:
#: Coherent-timeout invariant (Story 5.0): this MUST stay below core's
#: `DEFAULT_TURN_TIMEOUT` (runtime.py) and the fork-server's `_REAP_TIMEOUT_S` — the
#: ordering is W < R < T (worker self-report < reap SIGKILL < core degrade). Inverting it
#: (the old 120s vs core's 30s) is what let a silent broker hold the fork ~90s past core's
#: degrade and freeze every new turn. With W < T the worker self-reports a failure Result
#: BEFORE core abandons the turn, so the slot frees in lockstep. Module-level so tests
#: inject a small value. See `tests/test_resilience.py::test_timeout_chain_is_coherent`.
_COMPLETION_TIMEOUT_S = 25.0

#: The worker's outbound `Result → core` write is bounded too: if core (or the hub) stalls
#: and stops reading, the worker must not block forever on the write past its window — it
#: logs and exits (the core turn-timeout is then the backstop). Story 5.0.
#:
#: Kept SHORT (and strictly < `_COMPLETION_TIMEOUT_S`): a healthy core reads the Result
#: instantly, so a write that takes more than a few seconds means core is gone. A long
#: write window (e.g. matching the 25s completion timeout) would let a broker reply that
#: landed near t=25 stall the write until the reaper's SIGKILL (R=28) cut it off mid-write,
#: silently dropping a valid reply.
_RESULT_WRITE_TIMEOUT_S = 5.0


def parse_reply(text: str) -> tuple[str, list[ProposedOp]]:
    """Split a raw completion into (user-facing payload, proposed_ops).

    No ops block → the whole text is the reply, no ops. EVERY well-formed ```ops block is
    decoded (ops accumulated) and stripped from the reply, so a second block can't leak
    into the user-facing text. A malformed block is left in place with NO ops from it (the
    reply is never corrupted by a bad block, and a bad block stays visible — never silently
    swallowed)."""
    ops: list = []
    parsed_spans: list[tuple[int, int]] = []
    for m in _OPS_BLOCK_RE.finditer(text):
        try:
            ops.extend(_OPS_DECODER.decode(m.group(1).encode()))
        except msgspec.DecodeError as exc:
            log.warning("worker: ignoring malformed ops block (%s)", exc)
            continue  # leave the untrusted block in the text rather than strip it blindly
        parsed_spans.append((m.start(), m.end()))
    if not parsed_spans:
        return text, ops
    payload = text
    for start, end in reversed(parsed_spans):  # reverse so earlier indices stay valid
        payload = payload[:start] + payload[end:]
    return payload.strip(), ops


async def _read_completion(reader, timeout: float) -> Completion:
    """Read one Completion frame from the broker, degrading a missing/invalid/late frame
    into a failure `Completion` (broker gone or bad frame) so the caller never hangs —
    AD-12 timeout is the backstop. Shared by the single-round-trip and the tool loop."""
    try:
        env = await asyncio.wait_for(read_frame(reader), timeout=timeout)
    except asyncio.TimeoutError:
        return Completion(ok=False, error="broker did not answer in time")
    except (msgspec.ValidationError, ValueError, OSError, EOFError) as exc:
        # Bad frame (ValidationError/ValueError) OR a hard transport failure
        # (OSError/IncompleteReadError) → degrade, never crash the worker task.
        return Completion(ok=False, error=f"bad completion frame: {exc}")
    if env is None or env.kind is not MsgKind.COMPLETION or not isinstance(env.body, Completion):
        return Completion(ok=False, error="no completion from broker")
    return env.body


async def _single_round_trip(reader, writer, turn_id: str, job_payload: str) -> Result:
    """The pre-9.1 path (no tools): send one text Job, await the Completion, parse it into
    a Result (proposed_ops). Behavior is identical to before 9.1 — `Job.tools` is empty so
    the broker calls `complete()` (Story 9.1 AC5)."""
    await write_frame(
        writer,
        Envelope(
            id=turn_id,
            kind=MsgKind.JOB,
            src=Actor.WORKER,
            dst=Actor.BROKER,
            body=Job(payload=job_payload),
            turn_id=turn_id,
        ),
    )
    comp = await _read_completion(reader, _COMPLETION_TIMEOUT_S)
    if not comp.ok:
        return Result(ok=False, error=comp.error)
    payload, ops = parse_reply(comp.payload)
    return Result(ok=True, payload=payload, proposed_ops=ops)


async def _agentic_loop(
    reader, writer, turn_id: str, job_payload: str, registry: dict[str, ToolSpec]
) -> Result:
    """The bounded function-calling loop (Story 9.1 AC2). Send the running messages +
    tools to the broker; if the Completion carries tool-calls, execute the FREE-tier tools
    synchronously, append the results, and loop; if it is text, parse it into the Result.

    Bounded two ways: at most `_MAX_TOOL_EXECUTIONS` tool rounds, and within the SAME
    `_COMPLETION_TIMEOUT_S` total budget (W < R < T coherent-timeout invariant) — it breaks
    with < 2s left so a final Job never has too little time to get an answer. Either bound,
    a bad tool, or an unknown tool yields a best-effort reply — the turn NEVER crashes."""
    tool_defs = tuple(
        ToolDefinition(
            name=s.name, description=s.description, params_schema=s.params_schema, tier=s.tier
        )
        for s in registry.values()
    )
    messages: tuple[Message, ...] = (Message(role="user", content=job_payload),)
    loop = asyncio.get_running_loop()
    loop_start = loop.time()

    for iteration in range(_MAX_TOOL_EXECUTIONS + 1):
        elapsed = loop.time() - loop_start
        remaining = _COMPLETION_TIMEOUT_S - elapsed
        if remaining < 2.0:
            log.warning("worker: tool loop budget exhausted (%.1fs elapsed)", elapsed)
            return Result(ok=True, payload="I'm running short on time — let me answer directly.")

        await write_frame(
            writer,
            Envelope(
                id=turn_id,
                kind=MsgKind.JOB,
                src=Actor.WORKER,
                dst=Actor.BROKER,
                body=Job(payload="", tools=tool_defs, messages=messages),
                turn_id=turn_id,
            ),
        )
        comp = await _read_completion(reader, remaining)
        if not comp.ok:
            return Result(ok=False, error=comp.error)
        if not comp.tool_calls:
            payload, ops = parse_reply(comp.payload)
            return Result(ok=True, payload=payload, proposed_ops=ops)
        if iteration >= _MAX_TOOL_EXECUTIONS:
            log.warning("worker: tool loop exhausted after %d executions", iteration)
            return Result(ok=True, payload="I've used too many steps. Let me try a different approach.")

        # Execute the FREE-tier tools (errors → ToolResult(ok=False), never raise) and
        # extend the running conversation with the assistant's calls + the tool results.
        # Preserve any assistant text that came alongside the tool-calls (Anthropic can
        # interleave) — replaying tool_use without its leading text is a provider 400.
        assistant_msg = Message(role="assistant", content=comp.payload, tool_calls=comp.tool_calls)
        tool_msgs = [
            Message(role="tool", content=tr.content, tool_call_id=tr.id)
            for tr in (execute_tool(tc, registry) for tc in comp.tool_calls)
        ]
        messages = messages + (assistant_msg,) + tuple(tool_msgs)

    # Unreachable (the loop returns on text/exhaustion), but stay fail-soft.
    return Result(ok=True, payload="I've used too many steps. Let me try a different approach.")


async def run_worker(
    socket_path: str,
    turn_id: str,
    prompt: str,
    *,
    memory_root=None,
    history_path=None,
    assemble=None,
    tool_registry: dict[str, ToolSpec] | None = None,
) -> None:
    """Connect as WORKER, ASSEMBLE the prompt from memory (Story 4.4), run the turn against
    the broker, send `Result→core`, then exit.

    `prompt` is the current owner message; `assemble` turns it into the Job payload by
    reading DIRECTIVE/about/history read-only and composing them in the AD-6 order
    (default: `build_prompt`, bound to `memory_root`/`history_path`). The seam lets
    turn-lifecycle tests inject an identity assembler so they stay about fencing/
    coalescing, not prompt content.

    `tool_registry` (Story 9.1) selects the path: None/empty → the pre-9.1 single
    round-trip (AC5); a non-empty registry → the bounded function-calling loop (AC2). The
    sole production caller (`forkserver`) passes `build_tool_registry()`."""
    if assemble is None:
        def assemble(message):
            return build_prompt(message, memory_root=memory_root, history_path=history_path)
    job_payload = assemble(prompt)
    reader, writer = await connect(socket_path, Actor.WORKER)
    try:
        if not tool_registry:
            result = await _single_round_trip(reader, writer, turn_id, job_payload)
        else:
            result = await _agentic_loop(reader, writer, turn_id, job_payload, tool_registry)
        try:
            await asyncio.wait_for(
                write_frame(
                    writer,
                    Envelope(
                        id=uuid4().hex,
                        kind=MsgKind.RESULT,
                        src=Actor.WORKER,
                        dst=Actor.CORE,
                        body=result,
                        turn_id=turn_id,
                    ),
                ),
                timeout=_RESULT_WRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # Core/hub stopped reading — don't block forever on the write. Exit; core's
            # turn timeout reclaims the slot (Story 5.0).
            log.warning("worker: core did not read the Result in %.1fs; exiting", _RESULT_WRITE_TIMEOUT_S)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
