---
baseline_commit: bd64006bb65f9077e8891e7de5f10db33d666271
---

# Story 1.6: One chat-transport adapter over a transport-agnostic contract

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want to message shelldon through one real chat transport that plugs in behind a generic message contract,
so that I can talk to my pet today without Telegram being welded into its core (AD-13, FR9/CAP-9).

## Acceptance Criteria

1. **Transport-agnostic round-trip:** Given a transport-agnostic inbound/outbound message contract in `contracts/`, when the initial chat adapter — a **local CLI** (chosen first so the end-to-end turn is demoable on a laptop, before any hardware or bot token) — runs as a bus client, then an owner message arrives at core as an **inbound-message** envelope and a pet reply leaves core as an **outbound-message** envelope over that adapter. A Telegram (or other service) adapter is explicitly a *later* adapter, added without touching core.
2. **Credential split:** Given the chat adapter, when it connects to its service, then it holds its **own connection credential** (e.g. a bot token) and **never touches model/tool credentials** (the broker remains the sole holder of those — AD-2/NFR9). The CLI adapter needs no secret, so this is satisfied by the adapter importing **no provider SDK and no broker credential path** — proven mechanically, not by convention.
3. **Core untouched / linter green:** Given `core/`, when the adapter is built or swapped, then **nothing under `core/` changes** and the import-linter still passes (CAP-9 success — the conversation surface is pluggable, not welded in).

## Tasks / Subtasks

> **Test seam (read first — it shapes everything):** the CLI's stdin/stdout are injected behind two seams — an **inbound source** (an async iterator of text lines) and an **outbound sink** (an async callable taking text). ALL adapter logic (read→inbound envelope, outbound envelope→render, clean shutdown, frame resilience) is tested cross-platform by driving those seams with an in-test queue + list — **no real TTY, no `sys.stdin`**. The default seams wrap stdin/stdout for real use; mirror Story 1.5's injectable-`spawn` pattern.
>
> **Zero `core/` edits is an AC, not a side effect (AC3):** every change lands in `contracts/` (the message types + routing rows) and `transport/` (the adapter). The bus hub (`core/bus/server.py`) already routes generically via `ROUTING_TABLE[env.kind]` and already registers any actor from its first frame — so new kinds route automatically with **no hub change**. If you find yourself editing anything under `shelldon/core/`, stop — you've taken a wrong turn.

- [x] **Task 1: Transport-agnostic message contract in `contracts/` (AD-13)** (AC: 1)
  - [x] In `shelldon/contracts/__init__.py` add two `MsgKind` members: `INBOUND_MSG = "inbound-message"` and `OUTBOUND_MSG = "outbound-message"`.
  - [x] Add two frozen msgspec structs mirroring the existing `Job`/`Result` style (`frozen=True`, `tag=...`, `forbid_unknown_fields=True`): `InboundMessage(text: str)` (owner→core) tagged `"inbound-message"`, and `OutboundMessage(text: str)` (core→adapter) tagged `"outbound-message"`. Keep them **minimal** — just `text` for now.
  - [x] **Single-owner, non-breaking-later (AD-13/AD-6):** do NOT add a `chat_id`/`user_id` field now. A later multi-user adapter adds it as an **optional field with a default**, which msgspec accepts without breaking the wire — note this in a docstring comment so the next dev doesn't reach for it early. (Architected-for, not implemented — per AD-13 Deferred.)
  - [x] Extend the `Envelope.body` union to `Job | Result | InboundMessage | OutboundMessage`, and add both new types to `_KIND_FOR_BODY` so the closed-header kind↔body check (`__post_init__`) covers them (a typo'd kind must still raise).
  - [x] Add the two routing rows to `ROUTING_TABLE`: `MsgKind.INBOUND_MSG: Actor.CORE` and `MsgKind.OUTBOUND_MSG: Actor.CHAT_TRANSPORT`. (`Actor.CHAT_TRANSPORT` already exists from 1.2.)
  - [x] Export `InboundMessage` and `OutboundMessage` in `__all__`.

- [x] **Task 2: The CLI chat adapter (`transport/cli.py`)** (AC: 1, 2)
  - [x] New `shelldon/transport/cli.py`: `async def run_cli_transport(socket_path, *, inbound, outbound)` where `inbound` is an `AsyncIterator[str]` (lines the owner types) and `outbound` is an `async callable (str) -> None` (renders a pet reply). Connect as `Actor.CHAT_TRANSPORT` via the existing `connect(socket_path, Actor.CHAT_TRANSPORT)`.
  - [x] Run **two concurrent loops** under one `asyncio.gather` (or task group), cancelling the sibling when either ends:
    - **Inbound loop:** for each line from `inbound`, send `Envelope(id=uuid4().hex, kind=MsgKind.INBOUND_MSG, src=CHAT_TRANSPORT, dst=CORE, body=InboundMessage(text=line))`. When the source is exhausted (owner EOF / Ctrl-D), stop and tear down.
    - **Outbound loop:** `read_frame` from the bus; for each envelope with `kind is MsgKind.OUTBOUND_MSG` / `isinstance(body, OutboundMessage)`, call `await outbound(body.text)`. **Mirror the broker's per-frame resilience** (`broker/service.py`): a `msgspec.ValidationError` skips the frame and continues; a framing `ValueError` or `read_frame()→None` (hub gone / EOF) ends the loop cleanly. A non-outbound kind is logged and ignored, never fatal.
  - [x] **Default seams for real use:** provide thin defaults so `run_cli_transport(socket_path)` works against a terminal — an async stdin line-reader (e.g. `loop.run_in_executor(None, sys.stdin.readline)` yielding until empty/EOF) and an outbound sink that prints. Keep these defaults tiny; the **seams are what's tested**, the TTY glue is documented-not-unit-tested (note it in Dev Notes, like 1.5's real-fork gate).
  - [x] **Credential discipline (AC2):** import nothing from `shelldon.broker`, no provider SDK (`anthropic`/`openai`/…), and read no model/tool secret. The CLI's "own connection credential" is *nothing* (a local terminal); the seam where a future Telegram adapter would hold its bot token is the adapter's own construction, never the bus and never the broker's cred path. Document this one line.

- [x] **Task 3: Contract round-trip test — extend M0 (AD-10)** (AC: 1)
  - [x] Extend `tests/test_contracts_roundtrip.py` (or add `tests/test_message_contract_roundtrip.py` matching its style): encode→decode an `Envelope` carrying `InboundMessage` and one carrying `OutboundMessage`, asserting lossless round-trip and that the decoded body resolves to the right type. Add a kind↔body mismatch case (e.g. `kind=INBOUND_MSG` with an `OutboundMessage` body) asserting `__post_init__` raises — proving the closed header still bites for the new types.

- [x] **Task 4: Routing test (data-driven hub, no core change)** (AC: 1, 3)
  - [x] New `tests/test_transport_routing.py` (mirror `tests/test_bus_routing.py`): with a real `BusServer`, assert an `INBOUND_MSG` envelope from a `CHAT_TRANSPORT` client lands in `srv.core_inbox`, and an `OUTBOUND_MSG` envelope (sent by a stand-in core client) is delivered to a connected `CHAT_TRANSPORT` client's reader. This proves the new rows route with an **unmodified hub**.

- [x] **Task 5: CLI adapter isolation test (the round-trip, AC1)** (AC: 1)
  - [x] New `tests/test_cli_transport.py`: drive `run_cli_transport` against a **real `BusServer`** with a **stub core** (the test reads `srv.core_inbox` and pushes outbound frames to the transport's registered connection). Inject an in-test `inbound` (async queue/generator) and an `outbound` (appends to a list).
    - (a) Feed one owner line → assert an `INBOUND_MSG` `Envelope` with `InboundMessage(text=...)`, `src=CHAT_TRANSPORT`, `dst=CORE` arrives in `core_inbox`.
    - (b) Stub core writes an `OUTBOUND_MSG`(`OutboundMessage("hi back")`) routed to `CHAT_TRANSPORT` → assert it renders to the injected `outbound` sink.
    - (c) Exhaust the `inbound` source (EOF) → assert `run_cli_transport` returns cleanly (both loops torn down, no hang) within a timeout.
  - [x] This is the **isolation test the cross-cutting note requires** — Story 1.8 then *confirms* the wiring against the real arbiter/worker, rather than meeting the transport for the first time.

- [x] **Task 6: Credential-split guard for `transport/` (AC2)** (AC: 2)
  - [x] Add an import-linter **forbidden** contract in `pyproject.toml` (sibling to the existing "core is LLM-free (AD-1)") named e.g. `"transport holds no model/tool creds (AD-13/NFR9)"`: `source_modules = ["shelldon.transport"]`, `forbidden_modules = ["anthropic", "openai", "google", "litellm", "zhipuai", "ollama", "shelldon.broker"]`. This mechanically proves AC2's "never touches model/tool credentials" (the broker is the sole cred holder — AD-2), the same way `test_core_llm_free.py` proves AD-1.
  - [x] `tests/test_core_llm_free.py` already runs `lint-imports` over **all** contracts, so it covers this new one automatically — confirm it stays green (no separate test needed). If you prefer an explicit assertion, add a one-liner; not required.

- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → both contracts pass: `core/` stays LLM-free (AD-1) **and** `transport/` holds no model/tool creds (AD-13). **No `shelldon/core/**` file is in the diff** (AC3) — verify with `git diff --name-only`.
  - [x] `uv run pytest -q` → all green (prior suites + new). No new runtime dependency (uuid/asyncio/sys are stdlib; msgspec already pinned) — so **no `pyproject`/`uv.lock` dependency change**; the only `pyproject` edit is the new import-linter contract.

## Dev Notes

### Architecture compliance (binding)

- **AD-13 — Chat transport is a pluggable first-class adapter:** the chat transport is a first-class **edge actor / bus client** (peer to broker and display) — it emits **inbound-message** envelopes to core and consumes **outbound-message** envelopes from core, speaking a **transport-agnostic message contract in `contracts/`**. **One adapter ships now** (this story: local CLI); more (Telegram, group, web) are added as additional `transport/` adapters **without core change**. The adapter holds its **own connection credential** for its own surface; the **broker remains the sole holder of MODEL + TOOL creds** (AD-2 scope). The conversation schema is shaped so `chat_id`/`user_id` is a **non-breaking add** — architected-for, not implemented. *(Supervision / auto-restart / degrade-to-reflex on transport crash is the AD-13 graceful-degradation contract, wired at the end-to-end Story 1.8 — see Scope boundary; 1.6 ships the adapter + contract.)* [Source: ARCHITECTURE-SPINE.md#AD-13]
- **AD-11 — Closed envelope header + routing in `contracts/`:** the two routing modes and the `kind`→destination table live in `contracts/`, not in any component. Adding `INBOUND_MSG→CORE` and `OUTBOUND_MSG→CHAT_TRANSPORT` rows is the *intended* extension point — the hub stays generic. The header is closed (`forbid_unknown_fields` + the `__post_init__` kind↔body check), so the new types inherit the same anti-drift guard. [Source: ARCHITECTURE-SPINE.md#AD-11]
- **AD-4 — The Envelope bus is the only seam:** the adapter reaches core *only* through the bus; no direct call into core internals. It's a connect-and-serve client like the broker. [Source: ARCHITECTURE-SPINE.md#AD-4]
- **AD-2 / NFR9 — Credential split:** model + tool creds live solely in the broker; a chat adapter owns only its *own connection* credential and never touches model/tool creds, never sees them on the bus (`Job`/`Result`/messages carry none). Task 6's import-linter contract is the mechanical proof for `transport/`. [Source: ARCHITECTURE-SPINE.md#AD-2, epics.md#NFR9]
- **AD-1 — LLM-free core / import-linter KEPT:** the adapter lives in `transport/`, not `core/`; AC3 requires zero `core/` edits and a still-green linter. [Source: ARCHITECTURE-SPINE.md#AD-1]

### Why a local CLI first (not Telegram)

The epic explicitly picks a **local CLI as the first adapter** "so the end-to-end turn is demoable on a laptop, before any hardware or bot token." This keeps Story 1.6 dependency-free (no `python-telegram-bot`, no network, no secret) and makes Story 1.8's end-to-end demo runnable on Elliot's Mac. Telegram is a *later* adapter on the same contract, added with zero core change — that's the whole point of AD-13, and the import-linter + routing-table design proves the seam holds. [Source: epics.md#Story 1.6]

### Message-flow design (the key decision)

The adapter is a **bidirectional bus client** with two independent loops — unlike the worker (fire-and-forget, 1.5) or the broker (request→reply, 1.4):

1. **Owner types a line** → inbound loop wraps it as `INBOUND_MSG`(`InboundMessage`) `dst=CORE` → hub routes `INBOUND_MSG→CORE` → `core_inbox`. (In 1.6 a *stub* core consumes it; the real arbiter→fork→worker turn is **1.8**.)
2. **Core emits a reply** → `OUTBOUND_MSG`(`OutboundMessage`) `dst=CHAT_TRANSPORT` → hub routes `OUTBOUND_MSG→CHAT_TRANSPORT` (registry lookup of the adapter's connection) → outbound loop renders it to stdout.

Registration already works: `connect(...)` sends the `Actor.CHAT_TRANSPORT` registration as the mandatory first frame, and `server._handle` learns the connection from it (`read_registration`), so core's `OUTBOUND_MSG` finds the adapter's writer with **no hub change**. (The `server.py` docstring's "registration handshake deferred to 1.7" note is stale — registration is already implemented and is exactly what makes a receiver-capable actor like the transport addressable.) [Source: core/bus/server.py, core/bus/frame.py]

### The injectable IO seam (testability — mirror 1.5)

Real stdin/stdout are not unit-testable cross-platform (TTY, blocking reads). So inject:
- `inbound: AsyncIterator[str]` — tests pass a queue-backed async generator; real use wraps `sys.stdin` via `run_in_executor`.
- `outbound: Callable[[str], Awaitable[None]]` — tests append to a list; real use prints.

This is the same discipline as Story 1.5's injectable `spawn`/`reap` seam: the orchestration is fully deterministic in tests; the thin OS glue (the stdin executor reader) is documented as the production shape and not unit-covered. Don't block the event loop on a raw `input()`. [Source: 1.5 Dev Notes — injectable seam pattern]

### Previous story intelligence (1.1–1.5)

- **Bus client API:** `from shelldon.core.bus import connect, read_frame, write_frame`. `connect(socket_path, Actor.CHAT_TRANSPORT)` registers explicitly on connect (1.4 added registration; 1.3 the bus). [Source: 1.3/1.4]
- **Edge-actor loop pattern to copy:** `broker/service.py` is the closest sibling — connect, loop on `read_frame`, skip `ValidationError`, break on framing `ValueError`/`None`, never die on one bad frame. Reuse this shape for the outbound loop. [Source: 1.4]
- **Contract style:** `Job`/`Result` are `msgspec.Struct(frozen=True, tag=..., forbid_unknown_fields=True)`; the body union + `_KIND_FOR_BODY` + `__post_init__` enforce kind↔body. Add the two message types the same way — don't invent a new pattern. [Source: contracts/__init__.py, 1.2]
- **Routing is data-driven:** `server._route` does `ROUTING_TABLE[env.kind]`; CORE-dest goes to `core_inbox`, every other actor to its registered writer. New kinds need only a table row. [Source: 1.3, core/bus/server.py]
- **Test harness:** `pytest` + `pytest-asyncio` (auto mode). UDS sockets use the `sock_path` fixture (`tests/conftest.py`) for the macOS AF_UNIX path cap. Stub-actor tasks use `await asyncio.sleep(0.05)` after connect to let registration land (existing pattern in `test_broker_bus.py` / `test_bus_routing.py`). [Source: 1.3 conftest]
- **No deps expected:** uuid/asyncio/sys are stdlib; msgspec is pinned. If (unexpectedly) a dep is added, pin exact + commit `uv.lock`. [Source: 1.1–1.5 pin/lock discipline]
- **`transport/__init__.py`** exists as a one-line stub — add `cli.py` beside it; optionally re-export `run_cli_transport` from the package `__init__`. [Source: scaffold 1.1]

### Project Structure Notes

- All edits in **`shelldon/contracts/__init__.py`** (types + routing) and **`shelldon/transport/cli.py`** (new) + `transport/__init__.py` (optional re-export); tests under `tests/`; one new import-linter contract in `pyproject.toml`. **No file under `shelldon/core/` is touched** — this is the explicit AC3 check (`git diff --name-only` shows no `shelldon/core/**`). Aligns with the Structural Seed: `transport/` = chat-transport adapters; `contracts/` = the shared msgspec types incl. the transport-agnostic message contract. [Source: ARCHITECTURE-SPINE.md#Structural Seed]

### Scope boundary (prevent scope creep)

**IN scope (1.6):** the transport-agnostic `InboundMessage`/`OutboundMessage` contract + routing rows; one **local CLI** adapter (bidirectional bus client behind injected IO seams); the credential-split import-linter guard for `transport/`; contract round-trip + routing + adapter isolation tests.

**OUT of scope (later, do NOT build):**
- **Real turn wiring** — arbiter consuming `INBOUND_MSG`, spawning a worker, the reply flowing back as `OUTBOUND_MSG` → **Story 1.8** (1.6 uses a stub core in tests).
- **Telegram / any networked adapter**, bot-token handling, reconnect/missed-update replay → **later `transport/` adapters** (AD-13 Deferred); 1.6 only proves the seam.
- **Transport supervision / auto-restart / degrade-to-reflex on crash** (the AD-13 resilience contract) → **Story 1.8 / Epic 2** (the pet-stays-alive path needs the arbiter).
- **Multi-user / `chat_id`/`user_id` keying** → architected-for only (optional-field-with-default add later); do NOT add the field now (AD-13 / AD-6 Deferred).
- **Display / face reaction** → Story 1.7 / 1.8.
- **Prompt assembly, memory, history** → 1.8 / Epic 4.

### Testing standards

- `pytest` + `pytest-asyncio` (auto), mirroring package layout. Adapter orchestration is tested via the **injected inbound/outbound seams** (deterministic, cross-platform, no real TTY); the stdin/stdout default glue is the documented production shape, not unit-covered (like 1.5's `skipif(darwin)` real-fork gate). The round-trip (encode/decode the two new bodies) is the M0 contract test (AD-10).
- Before marking tasks done: `uv run lint-imports` (both contracts green), `uv run pytest -q` (green), and `git diff --name-only` shows **no `shelldon/core/**`** change (AC3).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.6; #FR9/CAP-9; #NFR9; #Epic 1 cross-cutting (isolation tests)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-13, #AD-11, #AD-4, #AD-2, #AD-1, #AD-10]
- [Source: shelldon/contracts/__init__.py (Job/Result/Envelope pattern, _KIND_FOR_BODY, ROUTING_TABLE, __post_init__ kind↔body guard)]
- [Source: shelldon/broker/service.py (edge-actor connect+serve loop, per-frame resilience to copy); shelldon/core/bus/server.py + frame.py (data-driven routing, registration already implemented)]
- [Source: _bmad-output/implementation-artifacts/1-5-...md (injectable-seam testing pattern); 1-4 (broker bus-client + stub pattern); 1-3 (bus, conftest sock_path)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story). Story context built from epics.md, ARCHITECTURE-SPINE.md (AD-13/AD-11/AD-4/AD-2/AD-1), and the live codebase (contracts, bus hub, broker edge-actor pattern).

### Debug Log References

- `uv run pytest tests/test_contracts_roundtrip.py tests/test_transport_routing.py tests/test_cli_transport.py -q` → all green (contract round-trip incl. the two new bodies + kind↔body mismatch; routing both directions; CLI round-trip).
- `uv run lint-imports` → 2 contracts KEPT: "core is LLM-free (AD-1)" **and** "transport holds no model/tool creds (AD-13/NFR9)".
- `uv run pytest -q` → **64 passed, 1 skipped** (the pre-existing darwin-gated real-fork test); +6 tests vs the 58 after 1.5, no regressions.
- `git status --porcelain | grep shelldon/core/` → **empty** (AC3: zero core edits).
- No dependency change — uuid/asyncio/sys stdlib, msgspec already pinned; the only `pyproject` edit is the new import-linter contract.

### Completion Notes List

- All 3 ACs satisfied. The conversation surface is now pluggable behind a transport-agnostic contract, with one local-CLI adapter shipping.
- **AC1 (transport-agnostic round-trip):** new `InboundMessage`/`OutboundMessage` msgspec bodies + `INBOUND_MSG`/`OUTBOUND_MSG` kinds in `contracts/`, routed `INBOUND_MSG→CORE` / `OUTBOUND_MSG→CHAT_TRANSPORT`. The CLI adapter (`transport/cli.py`) is a bidirectional bus client: owner line → INBOUND_MSG to core; core OUTBOUND_MSG → rendered to the owner. Proven by `test_cli_transport.py` (real bus + stub core) and `test_transport_routing.py`.
- **AC2 (credential split):** the CLI adapter imports no provider SDK and nothing from `shelldon.broker` — enforced mechanically by a new import-linter forbidden contract over `shelldon.transport` (covered by the existing `test_core_llm_free.py`, which runs `lint-imports` over all contracts). The CLI needs no connection secret; the seam where a Telegram adapter would hold a bot token is the adapter's own construction, never the bus/broker.
- **AC3 (core untouched / linter green):** **zero `shelldon/core/` edits** — the hub routes generically via `ROUTING_TABLE[env.kind]` and registers any actor from its first frame, so the two new kinds route with no hub change. `git diff` confirms no `core/` file in the diff; both import-linter contracts KEPT.
- **Key design decisions:** (a) Injectable IO seams (`inbound` async iterator + `outbound` async sink) make the adapter fully testable cross-platform with no real TTY — mirrors Story 1.5's injectable `spawn` seam; the stdin/stdout default glue is the documented production shape, not unit-covered. (b) Two concurrent loops under `asyncio.wait(FIRST_COMPLETED)` — whichever ends first (owner EOF or hub gone) tears down the sibling and returns cleanly. (c) Outbound loop mirrors the broker's per-frame resilience (skip bad message, end on framing error / EOF). (d) `InboundMessage`/`OutboundMessage` kept minimal (`text` only); `chat_id`/`user_id` is a deferred non-breaking optional-field add (AD-13/AD-6), documented in docstrings.
- **Test-bug caught & fixed during dev:** the first draft of `test_transport_routing.py` discarded a `StreamWriter` via `_` rebinding, which let the transport's connection close and deregister CHAT_TRANSPORT (outbound test hung). Fixed by holding both stream ends of each connection — a reusable gotcha for future multi-connection bus tests.
- **Scope held:** no real turn wiring (arbiter consuming INBOUND_MSG → worker → reply is 1.8), no Telegram/networked adapter, no transport supervision/auto-restart (1.8/Epic 2), no multi-user keying (architected-for only), no display.

### File List

- `shelldon/contracts/__init__.py` (modified — `InboundMessage`/`OutboundMessage` bodies, `INBOUND_MSG`/`OUTBOUND_MSG` kinds, body union + `_KIND_FOR_BODY` + `ROUTING_TABLE` rows, `__all__`)
- `shelldon/transport/cli.py` (new — `run_cli_transport`: bidirectional CLI bus-client adapter behind injected IO seams, AD-13)
- `shelldon/transport/__init__.py` (modified — re-export `run_cli_transport`)
- `pyproject.toml` (modified — new import-linter forbidden contract: `transport/` holds no model/tool creds, AD-13/NFR9)
- `tests/test_contracts_roundtrip.py` (modified — round-trip the two new message bodies + a kind↔body mismatch case, M0)
- `tests/test_transport_routing.py` (new — INBOUND_MSG→CORE / OUTBOUND_MSG→CHAT_TRANSPORT on an unmodified hub)
- `tests/test_cli_transport.py` (new — CLI adapter round-trip vs real BusServer + stub core; clean EOF teardown)

## Review Findings (2026-06-16)

Reviewers: Blind Hunter · Edge Case Hunter · Acceptance Auditor

### Patches (left as action items)

- [ ] `[Review][Patch]` **`_outbound_loop` doesn't catch `OSError` from `read_frame`** — `shelldon/transport/cli.py:_outbound_loop` — catches `ValidationError` (skip) and `ValueError` (framing, clean exit) but not `OSError`. A connection reset (`ConnectionResetError` ← `OSError`) propagates as a task exception and surfaces as a crash from `task.result()`. Same pattern as 1.4 `service.py` unhandled OSError. Fix: add `except OSError as exc: log.warning("connection reset, ending: %s", exc); return` alongside the `ValueError` handler.

### Deferred

- `[Review][Defer]` `_default_inbound` executor thread leaks on task cancellation — `sys.stdin.readline` in `run_in_executor` is not interruptible; thread stays blocked until process exit. Default TTY seam is "documented-not-unit-tested" per spec; fix requires custom executor or `aioconsole`-style non-blocking stdin.
- `[Review][Defer]` Hub-disconnect path untested — `_outbound_loop` returning `None` (hub gone) as the first loop to finish, causing it to cancel `_inbound_loop`, is never exercised. Spec's Task 5 calls (a)(b)(c) explicitly; add as a 4th case in 1.8 end-to-end integration.
- `[Review][Defer]` `ValidationError→skip` and `ValueError→clean-exit` resilience branches in `_outbound_loop` have no tests. Implied by "mirror broker resilience" but not listed in Task 5; add when integration testing expands.
- `[Review][Defer]` Both asyncio tasks finish simultaneously → second exception from `for task in done: task.result()` silently lost. Extremely rare; fix when error reporting is hardened.
- `[Review][Defer]` `asyncio.sleep(0.05)` in `test_transport_routing.py` for CHAT_TRANSPORT registration ordering — recurring deferred from 1.3; revisit if tests flake in CI.
- `[Review][Defer]` `outbound()` callable not protected from exceptions in `_outbound_loop` — default `print()` never throws; protect when non-trivial sinks (socket-backed) are wired.

## Change Log

- 2026-06-16: Implemented Story 1.6 — one chat-transport adapter over a transport-agnostic contract (AD-13). Added `InboundMessage`/`OutboundMessage` to `contracts/` with `INBOUND_MSG→CORE` / `OUTBOUND_MSG→CHAT_TRANSPORT` routing; built a local-CLI adapter (`transport/cli.py`) as a bidirectional bus client behind injected stdin/stdout seams; added an import-linter contract proving `transport/` holds no model/tool creds (AD-2/NFR9). **Zero `core/` changes** (AC3) — the hub routes new kinds generically. 64 pass / 1 skipped, both import-linter contracts KEPT, no dep change. Status → review.
