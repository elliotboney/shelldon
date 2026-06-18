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
    MemoryOp,
    MsgKind,
    Result,
)
from shelldon.core.bus import connect, read_frame, write_frame

log = logging.getLogger("shelldon.worker")

#: The reply→ops wire format: a single fenced ```ops block holding a JSON array of
#: tagged memory-ops. 4.5 owns this PARSE; Story 4.4 owns the prompt that elicits it
#: (the format may be co-adjusted there). A reply with no such block is a plain reply.
_OPS_BLOCK_RE = re.compile(r"```ops[ \t]*\n(.*?)```", re.DOTALL)

#: Decoder for the ops block payload — a closed list of the MemoryOp union. A malformed
#: or unknown op fails the WHOLE block (the 3.1/3.3/4.2 whole-reject discipline).
_OPS_DECODER = msgspec.json.Decoder(list[MemoryOp])

#: Anti-wedge backstop: the worker waits at most this long for the broker's Completion,
#: then emits a failure Result and exits — so a crashed/absent broker can never leave the
#: worker blocked forever (which would never reap → stick the ≤1 bound, AD-9). Generous
#: (the broker bounds its own provider calls); module-level so tests inject a small value.
_COMPLETION_TIMEOUT_S = 120.0


def parse_reply(text: str) -> tuple[str, list[MemoryOp]]:
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


async def _result_from_broker(reader, turn_id: str) -> Result:
    """Read the broker's Completion and turn it into a Result (parsing proposed_ops).
    A missing/invalid completion (broker gone or bad frame) becomes a failure Result so
    core degrades — never a hang (AD-12 timeout is the backstop)."""
    try:
        env = await asyncio.wait_for(read_frame(reader), timeout=_COMPLETION_TIMEOUT_S)
    except asyncio.TimeoutError:
        return Result(ok=False, error="broker did not answer in time")
    except (msgspec.ValidationError, ValueError, OSError, EOFError) as exc:
        # Bad frame (ValidationError/ValueError) OR a hard transport failure
        # (OSError/IncompleteReadError) → degrade, never crash the worker task.
        return Result(ok=False, error=f"bad completion frame: {exc}")
    if env is None or env.kind is not MsgKind.COMPLETION or not isinstance(env.body, Completion):
        return Result(ok=False, error="no completion from broker")
    comp: Completion = env.body
    if not comp.ok:
        return Result(ok=False, error=comp.error)
    payload, ops = parse_reply(comp.payload)
    return Result(ok=True, payload=payload, proposed_ops=ops)


async def run_worker(socket_path: str, turn_id: str, prompt: str) -> None:
    """Connect as WORKER, send one Job, await the broker's Completion, parse it into a
    Result, send `Result→core`, then exit (the child dies, its RAM reclaimed)."""
    reader, writer = await connect(socket_path, Actor.WORKER)
    try:
        await write_frame(
            writer,
            Envelope(
                id=turn_id,
                kind=MsgKind.JOB,
                src=Actor.WORKER,
                dst=Actor.BROKER,
                body=Job(payload=prompt),
                turn_id=turn_id,
            ),
        )
        result = await _result_from_broker(reader, turn_id)
        await write_frame(
            writer,
            Envelope(
                id=uuid4().hex,
                kind=MsgKind.RESULT,
                src=Actor.WORKER,
                dst=Actor.CORE,
                body=result,
                turn_id=turn_id,
            ),
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
