---
baseline_commit: c8c13cf
---
# Story 9.1: Function-calling foundation

Status: done

## Story

As the owner,
I want shelldon's brain to call defined tools through the broker and loop on the results,
so that the pet can take real actions in a turn instead of only emitting text.

## Acceptance Criteria

### AC1 — Provider seam carries native tool-calls, provider-agnostic

**Given** the provider seam and a registered tool registry  
**When** a turn runs with tools available  
**Then** `broker/provider.py::LLMProvider` gains `complete_with_tools(messages, tools) -> Completion`; the broker normalizes each provider's native tool-call format into closed `ToolCall`/`ToolResult` contracts; the worker loop is provider-agnostic (no Anthropic/OpenAI format knowledge leaks into the worker)  
**And** existing `complete()` is UNTOUCHED — non-tool turns remain unchanged  

### AC2 — Worker runs a bounded agentic loop

**Given** the worker receives a `Completion` with `tool_calls`  
**When** the loop runs  
**Then** the worker executes FREE-tier tools synchronously, appends `ToolResult` to the running messages list, and sends another `Job` to the broker (bounded loop)  
**And** the loop is capped at `_MAX_TOOL_ITERATIONS = 6` AND respects the `_COMPLETION_TIMEOUT_S = 25.0` budget (remaining time tracked per iteration, breaks if < 2s left)  
**And** loop exhaustion returns a best-effort text reply + a logged warning — the turn NEVER crashes  

### AC3 — `get_time` tool proves the loop end-to-end

**Given** `get_time` registered as the only FREE-tier tool  
**When** the model requests it  
**Then** the worker executes it (stdlib `datetime`, 0 new deps), feeds the result back, and the model produces a final text reply that reaches core as a `Result` with `proposed_ops`  

### AC4 — Error handling: bad tool calls never crash the turn

**Given** a tool that raises, an unknown tool name, or a malformed tool-call  
**When** the loop encounters it  
**Then** it is caught and fed back as `ToolResult(ok=False, content=<error>)` — the model recovers; the turn never raises  
**And** `core/` still imports no LLM/provider code (import-linter 3 contracts KEPT)  

### AC5 — Backwards compatibility: non-tool turns unchanged

**Given** a turn with no tools (the default before 9.2)  
**When** `run_worker` is called without a tool registry  
**Then** behavior is identical to the pre-9.1 single round-trip: no messages list sent, `Job.tools` is empty, broker calls `complete()` as before  
**And** all 554 existing tests pass; import-linter 3 contracts green; `uv sync --locked` 0 new deps  

---

## Tasks / Subtasks

- [x] **Task 1 — Extend contracts** (AC1, AC2, AC3)
  - [x] Add to `shelldon/contracts/__init__.py`:
    - `ToolTier(StrEnum)`: `FREE = "free"`, `RISKY = "risky"`
    - `ToolDefinition(name, description, params_schema, tier)` — frozen struct, serializable, goes on the bus
    - `ToolCall(id, name, args: dict)` — broker→worker; provider's normalized tool request
    - `ToolResult(id, ok, content)` — worker→broker feed-back in the messages list
    - `Message(role, content, tool_calls, tool_call_id)` — typed multi-turn message (see Dev Notes)
    - Extend `Completion`: add `tool_calls: tuple[ToolCall, ...] = ()` (additive default, backwards-compat)
    - Extend `Job`: add `tools: tuple[ToolDefinition, ...] = ()` and `messages: tuple[Message, ...] = ()` (both additive defaults)
    - Update `__all__` with all new names
  - [x] Verify: existing msgspec round-trip tests still pass (no SCHEMA_VERSION bump — these are additive optional fields)

- [x] **Task 2 — Create `worker/tools.py`** (AC2, AC3, AC4)
  - [x] `ToolSpec(name, description, params_schema, tier, fn)` — worker-only dataclass (NOT a msgspec struct; `fn: Callable` cannot serialize across the bus)
  - [x] `execute_tool(call: ToolCall, registry: dict[str, ToolSpec]) -> ToolResult` — catches ALL exceptions from `fn()`, returns `ToolResult(ok=False, content=repr(exc))` on error; handles unknown tool name gracefully
  - [x] `build_tool_registry() -> dict[str, ToolSpec]` — returns the registered tool set for the current turn
  - [x] `get_time` FREE tool: `fn = _get_time` (`datetime.datetime.now().isoformat()`), params_schema = `{"type": "object", "properties": {}, "required": []}`
  - [x] Verify: no LLM/provider imports (import-linter passes for worker/)

- [x] **Task 3 — Provider seam: add `complete_with_tools`** (AC1)
  - [x] `broker/provider.py`: add `complete_with_tools(messages: list[Message], tools: list[ToolDefinition]) -> Completion` to `LLMProvider` Protocol — separate from `complete()` (text-only path unchanged)
  - [x] `broker/anthropic_provider.py`: implement `complete_with_tools` + pure `normalize_anthropic_response` — tools→`input_schema`, response tool-use blocks → `ToolCall` contracts
  - [x] `broker/openai_provider.py`: implement `complete_with_tools` + pure `normalize_openai_response` — tools→`type:"function"`/`parameters`, response `tool_calls` → `ToolCall`; `arguments` JSON-string parsed to `dict`
  - [x] Both providers: text-only → `Completion(ok=True, payload=text, tool_calls=())`; tool-call → `Completion(ok=True, tool_calls=(...))`; SDK errors RAISE the provider exception types (so the broker's shared retry/fallback keys on them) → broker maps to `Completion(ok=False, error=...)`

- [x] **Task 4 — Broker: thread tools, return `Completion`** (AC1, AC5)
  - [x] `broker/broker.py`: `handle_job` and `handle_job_chain` now return `Completion` (was `Result`)
  - [x] In `handle_job`: `job.tools` non-empty → `complete_with_tools(list(job.messages), list(job.tools))`; otherwise `complete(job.payload)` wrapped in `Completion(ok=True, payload=text)`
  - [x] Retry logic (transient errors) applies to BOTH paths unchanged (shared try/except)
  - [x] `broker/service.py`: `_serve_connection` sends `handle_job_chain(...)`'s `Completion` directly (intermediate `Result` removed); `turn_id` echo unchanged
  - [x] Update broker tests that asserted the return TYPE: `test_broker_fallback_soak.py` (`isinstance` → `Completion`). `test_broker_retry.py`/`test_broker_chain_fallback.py`/`test_broker_creds.py`/`test_broker_service_branches.py` needed NO change — `Completion` shares the `.ok/.payload/.error` shape they assert on (see Completion Notes)

- [x] **Task 5 — Worker: bounded agentic loop** (AC2, AC3, AC4, AC5)
  - [x] `worker/worker.py`: `run_worker` gains an injectable `tool_registry` seam — empty/None → pre-9.1 single round-trip (`_single_round_trip`); non-empty → bounded loop (`_agentic_loop`). `forkserver.py` (sole prod caller) passes `build_tool_registry()` (DESIGN NOTE below)
  - [x] Loop structure: `messages=(Message(role="user", content=job_payload),)` → `Job(payload="", tools=tool_defs, messages=messages)` → `Completion` → tool_calls: execute+append+loop; text: `parse_reply()` → `Result`
  - [x] Time tracking: `loop_start = asyncio.get_event_loop().time()`; per-iteration `remaining = _COMPLETION_TIMEOUT_S - elapsed`; `remaining < 2.0` → break with best-effort reply; `_read_completion(reader, remaining)`
  - [x] `_result_from_broker` refactored to `_read_completion` returning a `Completion` (the loop inspects `tool_calls`); single-trip path converts to `Result`
  - [x] Preserve `parse_reply()` and `_OPS_BLOCK_RE` — the final step on a text response (both paths)
  - [x] `_MAX_TOOL_ITERATIONS = 6` cap → executes 6 tools, bails on the 7th request with a logged warning

- [x] **Task 6 — Update SYSTEM_INSTRUCTION** (AC3)
  - [x] `worker/prompt.py`: one-sentence "you have tools … call `get_time` …" line added, matching the existing tone (no robot voice)

- [x] **Task 7 — Tests** (AC1–AC5)
  - [x] NEW `tests/test_tool_loop.py` (scripted fake broker over the real bus, no live LLM): `test_tool_loop_get_time`, `test_tool_loop_error_recovery`, `test_tool_loop_exhaustion`, `test_no_tools_path_unchanged`
  - [x] NEW `tests/test_tool_normalizer.py`: Anthropic (also GLM) + OpenAI recorded-response → `ToolCall` (JSON args parsed), text-only → no tool_calls, empty reply → `PermanentProviderError`
  - [x] Added `test_broker_retry.py` AC1 tests: a tools Job routes to `complete_with_tools` (not `complete`) and shares the transient retry
  - [x] Updated for the return-type change: `test_broker_fallback_soak.py`, `test_resilience.py` (monkeypatch `_single_round_trip`). `test_broker_service_branches.py`/`test_broker_creds.py` unchanged (Completion shares the shape)
  - [x] Boundary gate: `uv run pytest -q` → **571 passed / 3 skipped / 7 deselected (live)** (0 new live markers, 0 new gate-deselects); `uv run lint-imports` → **3 KEPT**; `uv sync --locked` → **0 new deps**

---

## Dev Notes

### Architecture constraints (mandatory — violating these breaks Epic 9's foundation)

- **Core stays LLM-free.** `worker/tools.py` MUST NOT import from broker/, anthropic, openai, etc. Import-linter enforces this mechanically via 3 existing contracts. NEW `worker/tools.py` is in the `worker/` package — the `core is LLM-free` contract covers `shelldon.core`, NOT `shelldon.worker`, so adding tools.py there is fine.
- **Broker never executes tools.** The broker's `complete_with_tools` normalizes wire formats only — it does NOT call `fn()`. Tool execution happens exclusively in the worker.
- **Broker = sole model egress (AD-2/NFR9).** The broker already holds creds. `complete_with_tools` follows the same AD-2 discipline as `complete()`: creds injected by the provider adapter, never in the Job/Completion/bus.
- **Fork = no accumulation (AD-3).** The agentic loop runs inside ONE fork worker. The loop ends (the worker dies) before the next turn forks. Resumed turns (9.3 RISKY-tier) are a different story — not in scope here.
- **Fail-soft discipline.** Tool errors → `ToolResult(ok=False)`, never raise. Loop exhaustion → best-effort reply + log warning. `parse_reply()` discipline (4.5) unchanged for the final text response.

### Existing code to read before touching (anti-regression guardrails)

**`shelldon/contracts/__init__.py`** (read fully)
- `Completion` (line 211): currently `ok/payload/error` — add `tool_calls: tuple[ToolCall, ...] = ()` as a NEW optional field; `forbid_unknown_fields=True` means a decoder that doesn't know `tool_calls` would reject new payloads — BUT all processes upgrade together (single binary), so this is safe. No `SCHEMA_VERSION` bump (additive-only).
- `Job` (line 185): currently `payload: str` — add optional `tools: tuple[ToolDefinition, ...] = ()` and `messages: tuple[Message, ...] = ()`. When `messages` is non-empty, the broker uses it instead of `payload` (the broker must implement this check).
- `_KIND_FOR_BODY` (line 275) and `ROUTING_TABLE` (line 320): NO changes — we're not adding new MsgKinds for 9.1; `ToolCall`/`ToolResult` are not envelope-level messages, they travel inside `Job.messages` and `Completion.tool_calls`.

**`shelldon/broker/broker.py`** (read fully)
- Current signature: `async def handle_job(job: Job, provider: LLMProvider) -> Result` — change to `-> Completion`
- Current `handle_job_chain` signature: same, change to `-> Completion`
- The "3 inner exceptions → `Result(ok=False)`" pattern maps to `Completion(ok=False, error=...)` in the new form — keep the same error mapping.
- `log.info("turn answered by provider %r ...")` stays unchanged in `handle_job_chain`.

**`shelldon/broker/service.py`** (read fully)
- Lines 56–68: currently builds `Completion` from `Result`. After Task 4, broker functions return `Completion` directly — so `_serve_connection` becomes `completion = await handle_job_chain(env.body, chain)` and `out = Envelope(body=completion, ...)`. Remove the intermediate `Result` variable.
- The `Completion` coming from `handle_job_chain` now carries `tool_calls` correctly — no extra plumbing needed.

**`shelldon/worker/worker.py`** (read fully)
- `_result_from_broker` (line 98): currently awaits a `COMPLETION` envelope and returns `Result` (parsing ops). In 9.1 this needs refactoring: the loop reads raw `Completion` from the broker (without parsing ops yet), checks for `tool_calls`, and only calls `parse_reply()` on the FINAL text response. The helper should return `Completion` so the loop can inspect `tool_calls`.
- `_COMPLETION_TIMEOUT_S = 25.0` (line 59): this is the TOTAL budget for the whole turn (coherent-timeout invariant W < R < T). The loop must track elapsed time against this budget, not reset per-iteration.
- `_RESULT_WRITE_TIMEOUT_S = 5.0` (line 70): unchanged — the final `Result → core` write is still bounded.

### `Message` struct design

The `Message` struct goes in `contracts/__init__.py` (wire vocab shared by worker + broker):

```python
class Message(msgspec.Struct, frozen=True, tag_field=None):
    """A multi-turn conversation message for the tool-calling path.

    One struct covers all roles: user/assistant text, assistant tool-calls,
    and tool results. Fields are optional to avoid per-role sub-types.
    NOT tagged (not a body union member) — travels inside Job.messages.
    """
    role: str  # "user", "assistant", "tool"
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()   # role="assistant" with pending calls
    tool_call_id: str | None = None          # role="tool", correlates with ToolCall.id
```

Note: `Message` is NOT in the `Envelope.body` union and does NOT need a `tag=` (it's not decoded polymorphically at the envelope level). Do NOT add it to `_KIND_FOR_BODY` or `ROUTING_TABLE`.

### Provider tool-call format normalization

**Anthropic SDK** (`complete_with_tools` in `AnthropicProvider`):

```python
# Send tools:
tools=[{"name": t.name, "description": t.description, "input_schema": t.params_schema} for t in tools]

# Receive:
for block in resp.content:
    if block.type == "tool_use":
        # block.id, block.name, block.input (dict)
        ToolCall(id=block.id, name=block.name, args=dict(block.input))
```

**OpenAI SDK** (`complete_with_tools` in `OpenAIProvider`):

```python
# Send tools:
tools=[{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.params_schema}} for t in tools]

# Receive:
for tc in resp.choices[0].message.tool_calls or []:
    # tc.id, tc.function.name, tc.function.arguments (JSON STRING — parse it!)
    ToolCall(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments))
```

Both providers: a text response with no tool_calls → `Completion(ok=True, payload=text, tool_calls=())`. A response that is tool-calls-only (no text) → `Completion(ok=True, payload="", tool_calls=(...))`. On any provider error → `Completion(ok=False, error=...)` (same exception mapping as `complete()`).

**GLM on Z.ai** uses the Anthropic-compatible endpoint, so `AnthropicProvider.complete_with_tools` handles it without changes. The normalizer tests should include a recorded GLM/Anthropic format response to confirm.

### Messages format for broker's `complete_with_tools`

The broker receives `Job.messages: tuple[Message, ...]` and passes them to the provider. The provider adapters convert our `Message` structs to their SDK format:

```python
# AnthropicProvider: messages → Anthropic SDK format
sdk_messages = []
for m in messages:
    if m.role == "assistant" and m.tool_calls:
        sdk_messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args}
            for tc in m.tool_calls
        ]})
    elif m.role == "tool":
        sdk_messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
        ]})
    else:
        sdk_messages.append({"role": m.role, "content": m.content})
```

**Note on Anthropic's system parameter:** The assembled prompt (from `build_prompt`) currently embeds `SYSTEM_INSTRUCTION` at the start of the user message text. For the tool-calling path, keep this approach (system instruction is part of the first user message content). The `system=""` Anthropic param is not set separately — this is consistent with how `complete()` works today (it passes the whole assembled prompt as a single user message).

### Worker loop scaffold (implementation reference)

```python
_MAX_TOOL_ITERATIONS = 6

async def run_worker(...):
    registry = build_tool_registry()  # from worker/tools.py
    tool_defs = [ToolDefinition(name=s.name, description=s.description,
                                params_schema=s.params_schema, tier=s.tier)
                 for s in registry.values()]

    job_payload = assemble(prompt)  # existing assemble path unchanged
    reader, writer = await connect(socket_path, Actor.WORKER)
    try:
        if not tool_defs:
            # Pre-9.1 single round-trip path — UNCHANGED (AC5)
            result = await _single_round_trip(reader, writer, turn_id, job_payload)
        else:
            result = await _agentic_loop(reader, writer, turn_id, job_payload,
                                          tool_defs, registry)
        # ... send result to core (unchanged)
    finally:
        # ... cleanup (unchanged)


async def _agentic_loop(reader, writer, turn_id, job_payload, tool_defs, registry):
    messages = (Message(role="user", content=job_payload),)
    loop = asyncio.get_event_loop()
    loop_start = loop.time()

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        remaining = _COMPLETION_TIMEOUT_S - (loop.time() - loop_start)
        if remaining < 2.0:
            log.warning("worker: tool loop budget exhausted (%.1fs elapsed)", loop.time() - loop_start)
            return Result(ok=True, payload="I'm running short on time — let me answer directly.", proposed_ops=[])

        # Send Job to broker
        await write_frame(writer, Envelope(
            id=turn_id, kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER,
            body=Job(payload="", tools=tuple(tool_defs), messages=messages),
            turn_id=turn_id,
        ))

        # Receive Completion
        try:
            env = await asyncio.wait_for(read_frame(reader), timeout=remaining)
        except asyncio.TimeoutError:
            return Result(ok=False, error="broker did not answer in time")
        if env is None or not isinstance(env.body, Completion):
            return Result(ok=False, error="no completion from broker")
        comp: Completion = env.body
        if not comp.ok:
            return Result(ok=False, error=comp.error)

        if not comp.tool_calls:
            # Text response — done
            payload, ops = parse_reply(comp.payload)
            return Result(ok=True, payload=payload, proposed_ops=ops)

        if iteration >= _MAX_TOOL_ITERATIONS:
            log.warning("worker: tool loop exhausted after %d iterations", iteration)
            return Result(ok=True, payload="I've used too many steps. Let me try a different approach.", proposed_ops=[])

        # Execute FREE tools, extend messages
        tool_result_messages = []
        assistant_msg = Message(role="assistant", tool_calls=comp.tool_calls)
        for tc in comp.tool_calls:
            tr = execute_tool(tc, registry)
            tool_result_messages.append(
                Message(role="tool", content=tr.content, tool_call_id=tc.id)
            )
        messages = messages + (assistant_msg,) + tuple(tool_result_messages)
```

Note: The scaffold above is a GUIDE for the dev agent, not a copy-paste prescription. Adapt to match the actual code style and the refactored `_result_from_broker` helper.

### `execute_tool` safety

In `worker/tools.py`:
```python
def execute_tool(call: ToolCall, registry: dict[str, ToolSpec]) -> ToolResult:
    spec = registry.get(call.name)
    if spec is None:
        log.warning("worker: unknown tool %r requested", call.name)
        return ToolResult(id=call.id, ok=False, content=f"unknown tool: {call.name!r}")
    try:
        result = spec.fn(**call.args)  # call with named args from the model
        return ToolResult(id=call.id, ok=True, content=str(result))
    except Exception as exc:
        log.warning("worker: tool %r raised: %s", call.name, exc)
        return ToolResult(id=call.id, ok=False, content=repr(exc))
```

The `fn(**call.args)` pattern passes the model's args as kwargs. For `get_time` with `args={}`, this is `get_time()` — correct.

### Test fake provider pattern (reference existing tests)

Look at `tests/test_worker_sends_job.py` and `tests/test_end_to_end_turn.py` for the existing fake provider/harness pattern. The new `test_tool_loop.py` should follow the same pattern: a scripted fake provider that returns pre-configured Completions in sequence (use a counter or a queue of responses).

### Coherent-timeout invariant (do NOT break)

Confirmed by `tests/test_resilience.py::test_timeout_chain_is_coherent`. The loop uses the SAME `_COMPLETION_TIMEOUT_S = 25.0` budget (not per-iteration). The `remaining = _COMPLETION_TIMEOUT_S - elapsed` pattern above ensures total elapsed < 25s. The 2s minimum remaining guard prevents sending a Job with no time to receive a reply.

### No new dependencies

- `datetime` is stdlib — `get_time` uses `datetime.datetime.now().isoformat()`
- `json` is stdlib — OpenAI `arguments` string parsing uses `json.loads()`
- `anthropic` and `openai` SDKs already in `pyproject.toml` — no new packages
- `uv sync --locked` must show 0 changes after 9.1

### What 9.2 will build on top of this

Story 9.2 adds `read_file`, `list_dir`, `python_eval` to the tool registry in `worker/tools.py`. The broker seam changes in 9.1 are the permanent foundation — 9.2 only adds tools to `build_tool_registry()` and adds jail/restriction logic. 9.2 makes NO changes to contracts/broker/loop.

### What 9.3 will add for RISKY tier

The `ToolTier.RISKY` enum is defined here but NEVER enforced in 9.1. The loop only knows how to execute FREE tools (all registered tools are FREE in 9.1–9.2). Story 9.3 adds the tier check before `execute_tool()` and the 2-phase `RequestToolApproval` flow.

---

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (dev-story workflow)

### Debug Log References

- Full suite + gates green: `uv run pytest -q` → 571 passed, 3 skipped, 7 deselected (live); `uv run lint-imports` → 3 KEPT, 0 broken; `uv sync --locked` → 0 new deps.

### Completion Notes List

- **DESIGN DECISION (run_worker tools seam).** The Dev Notes scaffold called `build_tool_registry()` *unconditionally* inside `run_worker`, but that breaks existing direct-call tests (`test_worker_sends_job`, `test_end_to_end_turn`): with `get_time` always registered they'd hit the loop path and a differently-shaped Job (`payload=""` + `messages`). AC5 explicitly frames the unchanged path as "when `run_worker` is called **without** a tool registry," so tools are an **injectable seam**: `run_worker(..., tool_registry=None)` defaults to the pre-9.1 single round-trip; `forkserver.py` (the sole production caller) opts in via `tool_registry=build_tool_registry()`. This satisfies AC3 (live get_time on the Pi), AC5, and "all existing tests pass."
- **Provider error mapping.** `complete_with_tools` RAISES `Transient/PermanentProviderError` on SDK errors (mirroring `complete()`) rather than returning `Completion(ok=False)`, so `handle_job`'s shared try/except retries/falls-through on BOTH paths. The broker is what maps the exhausted exception into `Completion(ok=False, error=...)`. Net effect matches the Dev Notes ("on provider error → Completion(ok=False)") while keeping retry/fallback uniform.
- **Testable normalizers.** Response→`Completion` mapping is extracted into pure module-level `normalize_anthropic_response` / `normalize_openai_response`, unit-tested with `SimpleNamespace` SDK-shaped fakes (no live client). GLM is covered by the Anthropic normalizer (Z.ai Anthropic-compatible endpoint).
- **Test-file scope correction.** The story named `test_broker_service_branches.py` and `test_broker_creds.py` for the return-type change, but `Completion` shares the `.ok/.payload/.error` shape those assert on, so they needed no edit. The actual breakage from `Result`→`Completion` was `test_broker_fallback_soak.py` (an `isinstance(res, Result)` assert) and `test_resilience.py` (monkeypatched the renamed `_result_from_broker` helper → now `_single_round_trip`). Both fixed.
- **No new deps:** `get_time` uses stdlib `datetime`; OpenAI arg parsing uses stdlib `json`; `anthropic`/`openai` SDKs already present. `uv sync --locked` clean.
- `RISKY` tier is DEFINED but never enforced in 9.1 (all tools FREE) — that gate + the approval flow are Story 9.3, as designed.

### File List

- shelldon/contracts/__init__.py (UPDATE — ToolTier/ToolCall/ToolResult/ToolDefinition/Message; Job.tools+messages; Completion.tool_calls; __all__)
- shelldon/broker/provider.py (UPDATE — complete_with_tools on the Protocol)
- shelldon/broker/anthropic_provider.py (UPDATE — complete_with_tools + normalize_anthropic_response + helpers)
- shelldon/broker/openai_provider.py (UPDATE — complete_with_tools + normalize_openai_response + helpers)
- shelldon/broker/broker.py (UPDATE — handle_job/handle_job_chain return Completion; tools branch)
- shelldon/broker/service.py (UPDATE — send Completion directly)
- shelldon/worker/tools.py (NEW — ToolSpec, execute_tool, build_tool_registry, get_time)
- shelldon/worker/worker.py (UPDATE — _read_completion, _single_round_trip, _agentic_loop, tool_registry seam)
- shelldon/worker/forkserver.py (UPDATE — pass build_tool_registry() to run_worker)
- shelldon/worker/prompt.py (UPDATE — SYSTEM_INSTRUCTION tools line)
- tests/test_tool_loop.py (NEW)
- tests/test_tool_normalizer.py (NEW)
- tests/test_broker_retry.py (UPDATE — AC1 tools-path tests)
- tests/test_broker_fallback_soak.py (UPDATE — Completion return type)
- tests/test_resilience.py (UPDATE — monkeypatch _single_round_trip)

### Review Findings

- [x] [Review][Decision] `_MAX_TOOL_ITERATIONS = 6` constant name vs. actual behavior. RESOLVED: renamed → `_MAX_TOOL_EXECUTIONS` + docstring clarified. Kept the behavior (6 executions + a final round-trip so the model can answer with text after its last tool result) — tightening to 6 total would cut the model's final-answer turn. [`shelldon/worker/worker.py`]
- [x] [Review][Patch] `asyncio.get_event_loop()` → `asyncio.get_running_loop()` [`shelldon/worker/worker.py`:_agentic_loop]
- [x] [Review][Patch] Assistant text preserved with tool_calls — (1) `_agentic_loop` now builds `Message(role="assistant", content=comp.payload, tool_calls=comp.tool_calls)`; (2) `_messages_to_anthropic` emits a leading `text` block before the `tool_use` blocks when `m.content` is set. Regression test `test_messages_to_anthropic_keeps_assistant_text_with_tool_use`. [`shelldon/broker/anthropic_provider.py`, `shelldon/worker/worker.py`]
- [x] [Review][Patch] `json.JSONDecodeError` now caught in `normalize_openai_response` → raises `PermanentProviderError`. Regression test `test_openai_malformed_arguments_raise_permanent`. [`shelldon/broker/openai_provider.py`]
- [x] [Review][Patch] `dict(block.input or {})` guards a None-input no-arg tool call. Regression test `test_anthropic_none_input_normalizes_to_empty_args`. [`shelldon/broker/anthropic_provider.py`]
- [x] [Review][Patch] `Job.payload: str = ""` default added. [`shelldon/contracts/__init__.py`:Job]
- [x] [Review][Patch] `LLMProvider.complete_with_tools` docstring corrected — it RAISES the provider exception types; the broker maps an exhausted failure to `Completion(ok=False)`. [`shelldon/broker/provider.py`]
- [x] [Review][Patch] `_run_with_scripted_broker` wraps the worker lifecycle in `try/finally: worker.cancel()` (no leaked task). [`tests/test_tool_loop.py`]
- [x] [Review][Patch] `test_tool_loop_error_recovery` now asserts `tool_msgs[0].tool_call_id == "bad1"`. [`tests/test_tool_loop.py`]
- [x] [Review][Defer] `remaining` slightly stale before `_read_completion` — computed before `write_frame`; if socket pressure slows the write, the timeout value passed to `wait_for` is lower than intended; a near-zero value triggers `ValueError` caught as "bad completion frame" (misleading log). 2s guard provides sufficient margin. [`shelldon/worker/worker.py`:_agentic_loop] — deferred, pre-existing pattern
- [x] [Review][Defer] `tool_call_id=None` silently sends `null` to Anthropic SDK — `Message.tool_call_id: str | None` allows None; current code always provides a valid id from `ToolCall.id`, so this is theoretical. [`shelldon/broker/anthropic_provider.py`:_messages_to_anthropic] — deferred, theoretical
- [x] [Review][Defer] Transient retry re-sends same `messages` snapshot — on retry after a provider timeout, the broker replays the same `job.messages` to the provider; could double-execute on stateful providers (not Anthropic/OpenAI). [`shelldon/broker/broker.py`:handle_job] — deferred, theoretical
- [x] [Review][Defer] `test_tool_loop_exhaustion` missing `proposed_ops == []` assertion on the exhaustion fallback Result. [`tests/test_tool_loop.py`:test_tool_loop_exhaustion] — deferred, low priority

### Change Log

- 2026-06-21: Implemented Story 9.1 function-calling foundation — contracts tool vocab, provider `complete_with_tools` seam (Anthropic/GLM + OpenAI normalizers), broker returns `Completion` and threads tools, worker bounded agentic loop with `get_time` FREE tool, SYSTEM_INSTRUCTION tools line. 13 new tests; 571 pass / 3 import-linter contracts KEPT / 0 new deps.
- 2026-06-21: Addressed code review — 9 action items resolved (constant rename `_MAX_TOOL_EXECUTIONS`, `get_running_loop`, preserve assistant text alongside tool_use [worker + Anthropic mapper], catch malformed OpenAI JSON args → PermanentProviderError, `dict(input or {})` None-guard, `Job.payload` default, Protocol docstring, test worker-task cleanup, tool_call_id assertion). 4 added regression tests; 575 pass / 3 contracts KEPT / 0 new deps. 4 review items deferred (theoretical/low-priority, noted in Review Findings).
