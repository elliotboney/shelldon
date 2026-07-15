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
from dataclasses import dataclass
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
    RequestToolApproval,
    Result,
    RewriteAbout,
    RewriteIdentity,
    RewriteSoul,
    RewriteUser,
    ToolCall,
    ToolDefinition,
    ToolResult,
    ToolTier,
)
from shelldon.core.bus import connect, read_frame, write_frame
from shelldon.timeouts import COMPLETION_TIMEOUT
from shelldon.worker.prompt import build_prompt
from shelldon.worker.tools import ToolSpec, execute_tool, summarize_call

log = logging.getLogger("shelldon.worker")


@dataclass(frozen=True)
class ResumeState:
    """A paused RISKY-tool turn being resumed after the owner's decision (Story 9.3).

    Passed to `run_worker` in the FORKED child (in-process inheritance — never serialized
    across the bus): `messages` is the conversation up to and including the assistant's
    tool-call (with any FREE results already appended), `call` is the pending RISKY call,
    `approved` is the owner's choice. The worker resolves the call (execute on approve, a
    'denied' ToolResult on deny) and continues the loop to a final reply."""

    messages: tuple[Message, ...]
    call: ToolCall
    approved: bool


#: Bound on the agentic tool loop (Story 9.1 AC2): the worker executes at most this many
#: rounds of tool-calls before giving up with a best-effort reply — a model that loops
#: forever can never wedge the turn. (After the Nth execution the worker still does ONE
#: more round-trip so the model can produce a final text answer; if THAT still returns
#: tool-calls the cap trips.) Paired with the `_COMPLETION_TIMEOUT_S` budget below
#: (whichever trips first ends the loop).
#:
#: Story 9.5 (credit gating): this is ALSO the runaway-spend backstop. It is the HARD per-turn
#: model-call cap, so total self-driven spend is bounded by `daily_turn_budget` (Story 5.2) ×
#: this ceiling model-calls/day — a runaway loop can't burn the budget. Owner turns aren't
#: budget-gated (5.2 design) but are still bounded per-turn by this ceiling. Keep it conservative.
_MAX_TOOL_EXECUTIONS = 6

#: The reply→ops wire format: a single fenced ```ops block holding a JSON array of
#: tagged memory-ops. 4.5 owns this PARSE; Story 4.4 owns the prompt that elicits it
#: (the format may be co-adjusted there). A reply with no such block is a plain reply.
#: The closing fence MUST be at line-start (`\n` before it). Story 10.2: this defends against
#: fence NESTING — an op whose JSON `content` itself contains a literal "```ops" (a
#: `rewrite_instructions` carrying the protocol example) would otherwise close the block early at
#: the inner backticks. Valid JSON escapes every real newline inside a string value, so an embedded
#: fence is always mid-line and can't match `\n```` — only the true closing fence (on its own line)
#: does. Multi-block support (findall) is unchanged: each block still closes on its own line.
_OPS_BLOCK_RE = re.compile(r"```ops[ \t]*\n(.*?)\n```", re.DOTALL)

#: Decoder for the ops block payload — a closed list of the ProposedOp union (memory-ops
#: + the face op, Story 3.4). A malformed or unknown op fails the WHOLE block (the
#: 3.1/3.3/4.2 whole-reject discipline).
_OPS_DECODER = msgspec.json.Decoder(list[ProposedOp])

#: Reasoning-model hygiene: GLM (glm-4.x) wraps its chain-of-thought in `<think>…</think>`.
#: Strip whole blocks AND any orphan tag (a stray `</think>` GLM sometimes merges onto a line)
#: so the private reasoning never leaks into the owner's reply OR the screen thought.
_REASONING_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_ORPHAN_THINK_RE = re.compile(r"</?think>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove `<think>…</think>` reasoning + orphan tags. Returns the text UNCHANGED (no
    re-strip) when there was nothing to remove, so a tag-free reply is byte-for-byte
    preserved (the existing whole-reject/passthrough behavior). Pure."""
    cleaned = _ORPHAN_THINK_RE.sub("", _REASONING_RE.sub("", text))
    return cleaned.strip() if cleaned != text else text


#: B.3: the on-screen directives the model adds, each a single `KEYWORD: value` line that the
#: worker pulls out + strips (like the ops block) so they never leak into the chat reply:
#:   THOUGHT: <a few words>   — a short distilled thought for the caption strip
#:   FACE: <expression>       — the expression it picks as its reaction to the message
def _directive_re(keyword: str) -> re.Pattern:
    return re.compile(rf"^[ \t]*{keyword}:[ \t]*(.*?)[ \t]*$", re.MULTILINE)


_THOUGHT_RE = _directive_re("THOUGHT")
_FACE_RE = _directive_re("FACE")


def _extract_line(text: str, pattern: re.Pattern) -> tuple[str, str]:
    """Pull the first matching `KEYWORD:` line out → (text_without_it, value). No match →
    (text, "") unchanged. Pure; mirrors the ops-block strip."""
    m = pattern.search(text)
    if not m:
        return text, ""
    value = m.group(1).strip()
    cleaned = (text[: m.start()] + text[m.end():]).strip()
    return cleaned, value

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
#: Derived from the single `SHELLDON_TURN_TIMEOUT` knob (shelldon.timeouts) as W = T-5;
#: unset keeps the historical 25s.
_COMPLETION_TIMEOUT_S = COMPLETION_TIMEOUT

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


def parse_reply(text: str) -> tuple[str, list[ProposedOp], str, str]:
    """Split a raw completion into (user-facing payload, proposed_ops, thought, face).

    No ops block → the whole text is the reply, no ops. EVERY well-formed ```ops block is
    decoded (ops accumulated) and stripped from the reply, so a second block can't leak
    into the user-facing text. A malformed block is left in place with NO ops from it (the
    reply is never corrupted by a bad block, and a bad block stays visible — never silently
    swallowed). The `THOUGHT:` line (B.3) is likewise pulled out + stripped → the screen
    caption, never leaking into the chat reply. Reasoning tags are stripped first so neither
    the reply nor the thought carries a `<think>`/`</think>` leak."""
    text = _strip_reasoning(text)
    ops: list = []
    parsed_spans: list[tuple[int, int]] = []
    for m in _OPS_BLOCK_RE.finditer(text):
        try:
            ops.extend(_OPS_DECODER.decode(m.group(1).encode()))
        except msgspec.DecodeError as exc:
            log.warning("worker: ignoring malformed ops block (%s)", exc)
            continue  # leave the untrusted block in the text rather than strip it blindly
        parsed_spans.append((m.start(), m.end()))
    payload = text
    for start, end in reversed(parsed_spans):  # reverse so earlier indices stay valid
        payload = payload[:start] + payload[end:]
    if parsed_spans:
        payload = payload.strip()
    payload, thought = _extract_line(payload, _THOUGHT_RE)
    payload, face = _extract_line(payload, _FACE_RE)
    return payload, ops, thought, face


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
    payload, ops, thought, face = parse_reply(comp.payload)
    return Result(ok=True, payload=payload, proposed_ops=ops, blurb=thought, face=face)


def _record_tool_failure(failures, registry, tc, tr) -> None:
    """Story 9.5: if a SELF-CODED tool returned `ok=False`, note its name for the Result's
    `tool_failures` (→ core's quarantine ledger). Built-in failures never strike. No-op when
    `failures` is None (the loop wasn't asked to collect)."""
    if failures is None or tr.ok:
        return
    spec = registry.get(tc.name)
    if spec is not None and spec.self_coded:
        failures.add(tc.name)


#: Epic 11: the persona-rewrite tools (`tools._PERSONA_REWRITE_TOOLS`) are first-class function
#: calls, but the worker (fork child) can NEVER write memory (AD-5) — so a call becomes the matching
#: curated-memory op that core's single-writer applies after the turn, exactly like an inline ```ops
#: block. This maps the tool NAME → its ProposedOp class; the loop builds `op_cls(content=…)` from
#: the call's args on SUCCESS (an empty/rejected content already yielded `tr.ok=False` → no op).
PERSONA_REWRITE_OPS: dict[str, type] = {
    "rewrite_soul": RewriteSoul,
    "rewrite_identity": RewriteIdentity,
    "rewrite_user": RewriteUser,
    "rewrite_about": RewriteAbout,
}


def _collect_persona_op(persona_ops: list, tc: ToolCall, tr: ToolResult) -> None:
    """If `tc` is a persona-rewrite tool that SUCCEEDED, append the matching curated-memory op to
    `persona_ops` (built from the call's `content`). The op — not the tool body — is what actually
    edits the file, applied by core after the turn. A failed/empty call (`tr.ok=False`) adds nothing."""
    op_cls = PERSONA_REWRITE_OPS.get(tc.name)
    if op_cls is not None and tr.ok:
        persona_ops.append(op_cls(content=tc.args.get("content", "")))


async def _agentic_loop(
    reader, writer, turn_id: str, registry: dict[str, ToolSpec], messages: tuple[Message, ...],
    failures: set[str] | None = None,
) -> Result:
    """The bounded function-calling loop (Story 9.1 AC2 + 9.3 RISKY pause). Send the running
    `messages` + tools to the broker; on tool-calls, execute the FREE-tier tools and loop; on
    text, parse it into the Result. When a completion contains a RISKY-tier call, PAUSE: end
    the turn emitting a `RequestToolApproval` (the worker never blocks on a human, 9.3 AC1).

    Bounded two ways: at most `_MAX_TOOL_EXECUTIONS` tool rounds, and within the SAME
    `_COMPLETION_TIMEOUT_S` total budget (W < R < T coherent-timeout invariant) — it breaks
    with < 2s left so a final Job never has too little time to get an answer. Either bound,
    a bad tool, or an unknown tool yields a best-effort reply — the turn NEVER crashes.

    `messages` is the starting conversation: `(user,)` for a fresh turn, or the restored +
    resolved state for a resumed RISKY turn (Story 9.3)."""
    tool_defs = tuple(
        ToolDefinition(
            name=s.name, description=s.description, params_schema=s.params_schema, tier=s.tier
        )
        for s in registry.values()
    )
    loop = asyncio.get_running_loop()
    loop_start = loop.time()
    # Epic 11: persona-rewrite tool calls (rewrite_soul/…) accumulate here as curated-memory ops —
    # the worker can't write memory (AD-5), so each successful call becomes an op core applies after
    # the turn. Attached to the Result on EVERY success exit so a "saved" edit is never dropped.
    persona_ops: list[ProposedOp] = []

    for iteration in range(_MAX_TOOL_EXECUTIONS + 1):
        elapsed = loop.time() - loop_start
        remaining = _COMPLETION_TIMEOUT_S - elapsed
        if remaining < 2.0:
            log.warning("worker: tool loop budget exhausted (%.1fs elapsed)", elapsed)
            return Result(ok=True, payload="I'm running short on time — let me answer directly.", proposed_ops=persona_ops)

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
            payload, ops, thought, face = parse_reply(comp.payload)
            return Result(ok=True, payload=payload, proposed_ops=persona_ops + ops, blurb=thought, face=face)
        if iteration >= _MAX_TOOL_EXECUTIONS:
            log.warning("worker: tool loop exhausted after %d executions", iteration)
            return Result(ok=True, payload="I've used too many steps. Let me try a different approach.", proposed_ops=persona_ops)

        # 9.3: PAUSE on the first RISKY call. Execute any FREE calls that precede it (so the
        # protocol's tool_use↔tool_result pairing holds), keep the assistant message to those
        # answered calls + the risky one, park (messages, risky_call) for owner approval, and
        # end the turn. Calls AFTER the first risky one are dropped (the model re-requests).
        risky_idx = next(
            (i for i, tc in enumerate(comp.tool_calls)
             if registry.get(tc.name) is not None and registry[tc.name].tier == ToolTier.RISKY),
            None,
        )
        if risky_idx is not None:
            risky_call = comp.tool_calls[risky_idx]
            kept_calls = comp.tool_calls[: risky_idx + 1]  # FREE prefix + the risky call
            free_results = []
            for tc in comp.tool_calls[:risky_idx]:
                tr = execute_tool(tc, registry)
                _record_tool_failure(failures, registry, tc, tr)
                _collect_persona_op(persona_ops, tc, tr)
                free_results.append(Message(role="tool", content=tr.content, tool_call_id=tr.id))
            assistant_msg = Message(role="assistant", content=comp.payload, tool_calls=kept_calls)
            paused = messages + (assistant_msg,) + tuple(free_results)
            summary = summarize_call(risky_call, registry[risky_call.name])
            log.info("worker: pausing turn %s for approval of %r", turn_id, summary)
            return Result(
                ok=True,
                payload=f"I'd like to run `{summary}` — approve?",
                # Approval op FIRST so it can't be dropped by the op cap; any persona edits made in
                # the FREE prefix before this risky call ride along (core applies each independently).
                proposed_ops=[RequestToolApproval(call=risky_call, summary=summary, messages=paused), *persona_ops],
            )

        # All-FREE completion: execute + loop (9.1/9.2 behavior). Preserve any assistant text
        # alongside the tool-calls (Anthropic can interleave) — replaying tool_use without its
        # leading text is a provider 400.
        assistant_msg = Message(role="assistant", content=comp.payload, tool_calls=comp.tool_calls)
        tool_msgs = []
        for tc in comp.tool_calls:
            tr = execute_tool(tc, registry)
            _record_tool_failure(failures, registry, tc, tr)
            _collect_persona_op(persona_ops, tc, tr)
            tool_msgs.append(Message(role="tool", content=tr.content, tool_call_id=tr.id))
        messages = messages + (assistant_msg,) + tuple(tool_msgs)

    # Unreachable (the loop returns on text/exhaustion), but stay fail-soft.
    return Result(ok=True, payload="I've used too many steps. Let me try a different approach.", proposed_ops=persona_ops)


async def _resume_loop(reader, writer, turn_id: str, registry, resume: "ResumeState",
                       failures: set[str] | None = None) -> Result:
    """Resume a paused RISKY turn (Story 9.3): resolve the pending call — EXECUTE it on
    approve, or feed a 'denied by owner' `ToolResult` on deny — append the result so every
    `tool_use` in the restored assistant message is answered, then continue the loop to a
    final reply (or another RISKY pause)."""
    if resume.approved:
        tr = execute_tool(resume.call, registry)
        _record_tool_failure(failures, registry, resume.call, tr)
    else:
        log.info("worker: owner DENIED %r on resumed turn %s", resume.call.name, turn_id)
        tr = ToolResult(id=resume.call.id, ok=False, content="denied by owner")
    messages = resume.messages + (Message(role="tool", content=tr.content, tool_call_id=resume.call.id),)
    return await _agentic_loop(reader, writer, turn_id, registry, messages, failures)


async def run_worker(
    socket_path: str,
    turn_id: str,
    prompt: str,
    *,
    memory_root=None,
    history_path=None,
    assemble=None,
    tool_registry: dict[str, ToolSpec] | None = None,
    resume: "ResumeState | None" = None,
    import_failures: tuple[str, ...] = (),
) -> None:
    """Connect as WORKER, ASSEMBLE the prompt from memory (Story 4.4), run the turn against
    the broker, send `Result→core`, then exit.

    `prompt` is the current owner message; `assemble` turns it into the Job payload by
    reading DIRECTIVE/about/history read-only and composing them in the AD-6 order
    (default: `build_prompt`, bound to `memory_root`/`history_path`). The seam lets
    turn-lifecycle tests inject an identity assembler so they stay about fencing/
    coalescing, not prompt content.

    Path selection: `resume` (Story 9.3) → continue a paused RISKY turn from the restored
    state (no assembly); else `tool_registry` (Story 9.1) None/empty → the pre-9.1 single
    round-trip (AC5), non-empty → the bounded function-calling loop (AC2). The sole production
    caller (`forkserver`) passes `build_tool_registry()` (and `resume` for an approval tap)."""
    reader, writer = await connect(socket_path, Actor.WORKER)
    # Story 9.5: collect the self-coded tools that fail this turn (import skips seeded here +
    # run-failures added in the loop) → Result.tool_failures → core's quarantine ledger (AD-8).
    failures: set[str] = set(import_failures)
    try:
        if resume is not None:
            result = await _resume_loop(reader, writer, turn_id, tool_registry, resume, failures)
        elif not tool_registry:
            if assemble is None:
                def assemble(message):
                    return build_prompt(message, memory_root=memory_root, history_path=history_path)
            result = await _single_round_trip(reader, writer, turn_id, assemble(prompt))
        else:
            if assemble is None:
                def assemble(message):
                    return build_prompt(message, memory_root=memory_root, history_path=history_path)
            messages = (Message(role="user", content=assemble(prompt)),)
            result = await _agentic_loop(reader, writer, turn_id, tool_registry, messages, failures)
        if failures:
            # Attach the turn's failing self-coded tool names (frozen Result → replace).
            result = msgspec.structs.replace(result, tool_failures=tuple(sorted(failures)))
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
