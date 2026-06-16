---
baseline_commit: 4c6dff5293bdbfb136834c7a5746714a7a4c89ec
---

# Story 1.3: Envelope bus over Unix domain sockets, hub-routed through core

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer building shelldon,
I want core to host a UDS message bus that routes typed envelopes by kind,
so that independent processes communicate through one seam (AD-4) instead of ad-hoc channels.

## Acceptance Criteria

1. **Length-prefixed UDS frames:** with core running, a client process connecting over the Unix domain socket can send and receive **4-byte big-endian length-prefixed** msgspec `Envelope` frames.
2. **Hub routing by table:** core routes each envelope by a **static `kind`→destination table defined in `contracts/`** (AD-11 point-to-point mode). A `CORE`-destined envelope is delivered to core's own in-process handler; an envelope bound for another actor is forwarded to that actor's connection.
3. **Disconnect resilience:** when a connected client drops, core handles the disconnect **without crashing**, and a reconnecting client resumes cleanly.

## Tasks / Subtasks

- [x] **Task 1: Add the static routing table to `contracts/`** (AC: 2)
  - [x] Add `ROUTING_TABLE: dict[MsgKind, Actor]` to `shelldon/contracts/` — the AD-11 point-to-point `kind`→destination map. Seed with the kinds that exist today: `MsgKind.JOB: Actor.BROKER`, `MsgKind.RESULT: Actor.CORE`. (Later stories add entries as they introduce their own kinds — message/snapshot/event.)
  - [x] Export `ROUTING_TABLE` from `shelldon.contracts` (`__all__`).
  - [x] Unit test: every `MsgKind` member has a `ROUTING_TABLE` entry (guards against a future kind added without a route).
- [x] **Task 2: Length-prefixed frame codec** (AC: 1)
  - [x] New `shelldon/core/bus/frame.py`: `async write_frame(writer, env: Envelope)` → `contracts.encode(env)`, prefix with `len.to_bytes(4, "big")`, `writer.write(...)` + `await writer.drain()`.
  - [x] `async read_frame(reader) -> Envelope | None`: `await reader.readexactly(4)` → length; `await reader.readexactly(length)` → payload → `contracts.decode(...)`. Return `None` on clean EOF (`asyncio.IncompleteReadError`). **Use `readexactly`** — it reassembles a frame split across multiple reads, so partial-read handling is free.
  - [x] Guard against absurd frame sizes (reject a length over a sane cap, e.g. a few MB) so a corrupt prefix can't allocate unboundedly.
- [x] **Task 3: The hub (`BusServer`)** (AC: 2, 3)
  - [x] New `shelldon/core/bus/server.py`: an asyncio UDS hub using `asyncio.start_unix_server(handler, path=socket_path)`.
  - [x] Maintain a registry `dict[Actor, StreamWriter]` of connected remote actors, plus an in-process sink (callback or `asyncio.Queue`) for `CORE`-bound envelopes — **core is the hub AND a destination**, so `Actor.CORE` is delivered locally, never over a socket.
  - [x] Per-connection handler loop: read frames via `read_frame`; for each, resolve destination = `ROUTING_TABLE[env.kind]`; if `CORE` → hand to the local sink; else → look up the destination actor's writer and `write_frame` to it. If the destination actor isn't connected, **drop and log** (do not crash, do not block).
  - [x] **Registration:** learn a connection's identity from the `src` of the frames it sends (lazy registration — zero new contract surface). On the first frame from a connection, record `registry[env.src] = writer`. This covers the JOB/RESULT senders in scope. (A pure-receiver registration handshake — needed once a receive-only actor like display arrives in Story 1.7 — is **deferred**; note it in Dev Notes, don't build it now.)
  - [x] Socket lifecycle: **unlink any stale socket file before bind**; create the parent dir at runtime (not in source); close the server and clean up the socket on shutdown.
- [x] **Task 4: Socket path helper** (AC: 1)
  - [x] A `bus_socket_path()` helper resolving the runtime default `~/.shelldon/bus.sock` (expanduser), creating `~/.shelldon/` at runtime if missing. The `BusServer` takes the path as a parameter (default = this helper) so **tests pass a `tmp_path` socket** and never touch `~/.shelldon/`.
- [x] **Task 5: A minimal bus client helper for tests/edges** (AC: 1, 2, 3)
  - [x] A thin `connect(socket_path) -> (reader, writer)` (or small `BusClient`) wrapping `asyncio.open_unix_connection`, reusing `read_frame`/`write_frame`. Keep it minimal — it exists so tests (and later real adapters) speak the bus without re-implementing framing. Decide its home: `core/bus/` (shared) is fine since framing is core-owned; transports/broker import it.
- [x] **Task 6: Tests — framing, routing, disconnect/reconnect** (AC: 1, 2, 3)
  - [x] `tests/test_bus_frame.py`: `write_frame`→`read_frame` over a connected socket pair yields a value-equal `Envelope`; a frame whose bytes arrive split across reads still decodes (proves length-prefix reassembly); clean EOF returns `None`.
  - [x] `tests/test_bus_routing.py`: start a `BusServer` on a `tmp_path` socket; connect a `BROKER` client; send a `JOB` envelope (`src=WORKER`/`CORE`) — assert the broker client receives it (JOB→BROKER). Send a `RESULT` envelope — assert core's local sink receives it (RESULT→CORE).
  - [x] `tests/test_bus_disconnect.py`: a connected client drops mid-session — assert the server keeps running (other clients unaffected) and a freshly reconnecting client can send/receive again.
  - [x] Tests are `async` (use `pytest`'s asyncio support — see Library Notes for the runner decision); every test uses a `tmp_path` socket and tears the server down.
- [x] **Task 7: Verify the guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → `core/bus/` imports only stdlib `asyncio` + `shelldon.contracts`; **core stays LLM-free (AD-1)** — confirm import-linter still KEPT.
  - [x] `uv run pytest -q` → all green (1.1 + 1.2 suites + the new bus tests; no regressions).

## Dev Notes

### Architecture compliance (binding)

- **AD-4 — Envelope bus is the only seam:** all cross-process comms are versioned msgspec `Envelope`/`Job`/`Result` over **Unix domain sockets**, **length-prefixed**, **hub-routed through core**. Components address each other through the bus only; workers are connect-do-die clients. This story builds that hub. [Source: ARCHITECTURE-SPINE.md#AD-4]
- **AD-11 — Closed header + two routing modes:** the hub supports exactly two routing modes, **both declared in `contracts/`**: (1) **point-to-point** — a static `kind`→destination table (the default, built here); (2) broadcast/subscription — fan-out to N subscribers from a manifest-built registry (**deferred to Story 7.2**, do NOT build it). 1.2 defined the closed `MsgKind`/`Actor` enums; this story adds the point-to-point table that keys on them. [Source: ARCHITECTURE-SPINE.md#AD-11]
- **AD-1 — LLM-free core:** the bus lives in `core/` and must import only stdlib (`asyncio`) + `shelldon.contracts`. No provider SDKs. The import-linter enforces it — keep it KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **Consistency Conventions — Data & formats:** UDS frames are **4-byte big-endian length + msgspec bytes** (msgpack — reuse `contracts.encode/decode`); closed header `id/v/kind/src/dst/turn_id`; **no credentials ever on the bus**; errors surface as a `Result` error variant, never an exception across the bus. [Source: ARCHITECTURE-SPINE.md#Consistency Conventions]
- **AD-12 — Turn identity (forward-looking):** envelopes carry `turn_id`; core will fence on it. This story routes frames; **turn fencing / late-Result discard is NOT in scope** (that lands with the worker/arbiter, Stories 1.5/1.8). Just route — don't add turn logic yet. [Source: ARCHITECTURE-SPINE.md#AD-12]

### Key design decision: routing + registration

The AC says route by the **`kind`→dst table**, not by the envelope's `dst` field. So:
- The hub looks up `ROUTING_TABLE[env.kind]` → a destination `Actor`. The header's `dst` field stays (AD-11 mandates it in the closed header) and is available for reply/broadcast routing later, but **1.3 routes by the table** as the AC requires. Do not add a `dst == ROUTING_TABLE[kind]` validation — replies and broadcast will legitimately diverge (YAGNI / over-constraint).
- **Core is the hub AND `Actor.CORE`.** A `CORE`-destined envelope (e.g. `RESULT→CORE`) is delivered to an **in-process sink** (callback or `asyncio.Queue`), never over a socket. Only non-`CORE` actors are remote socket connections in the registry. Make this split explicit — it's the easy thing to get wrong.
- **Registration is lazy by `src`** (recommended, minimal): the hub records `registry[env.src] = writer` from the frames a connection sends. Sufficient for JOB/RESULT (both have senders). A registration handshake for pure-receiver actors (display, Story 1.7) is a **known deferral** — note it, don't build it. If during dev this proves too limiting for a clean routing test, the smallest escalation is an explicit first-frame hello; prefer lazy-`src` unless a test genuinely can't be written without the handshake.

### Scope boundary (prevent scope creep)

**IN scope (1.3):** the UDS hub, 4-byte length-prefix framing, the point-to-point `ROUTING_TABLE` in contracts, lazy-`src` registration, route-to-CORE-local + route-to-remote-actor, graceful disconnect/reconnect, a minimal client helper, async tests.

**OUT of scope (later stories, do NOT build):**
- Broadcast/subscription fan-out + manifest-built subscription registry → **Story 7.2**
- Turn fencing / late-or-zombie `Result` discard (`turn_id` logic) → arbiter, **Stories 1.5/1.8**
- The actual broker/worker/transport/display processes that connect as clients → **1.4 / 1.5 / 1.6 / 1.7**
- Pure-receiver registration handshake → when display arrives, **1.7**
- Reconnect/replay *delivery guarantees* (replaying missed frames) → AD-13 says these are adapter-specific and **deferred** to the adapter's story
- Auth/uid isolation on the socket → vault/uid work is **Epic 4 (4.3)**; the bus itself carries no creds (AD-2)

Keep the hub a dumb, correct router. It moves frames by table lookup and survives disconnects — nothing more.

### Source tree

- New package `shelldon/core/bus/` with `__init__.py`, `frame.py`, `server.py` (client helper in `frame.py` or a small `client.py` — dev's call). This matches the spine seed: `core/ # ... bus/ arbiter/ scheduler/ ...`. [Source: ARCHITECTURE-SPINE.md#Structural Seed]
- `core/__init__.py` already documents core as the "bus hub" owner — populate it here. Keep public bus names importable (e.g. `from shelldon.core.bus import BusServer`).
- Runtime socket (`~/.shelldon/bus.sock`) lives **outside** source and is gitignored; never scaffold `~/.shelldon/` into the repo. [Source: ARCHITECTURE-SPINE.md#Structural Seed; 1.1 — `.shelldon/` gitignored]

### Library / framework notes

- **asyncio UDS:** `asyncio.start_unix_server(client_connected_cb, path=...)` for the hub; `asyncio.open_unix_connection(path=...)` for clients. Both return `StreamReader`/`StreamWriter`. `StreamReader.readexactly(n)` reassembles split reads and raises `IncompleteReadError` at EOF — use it for the length and payload reads. Always `await writer.drain()` after `write`.
- **Stale socket:** `start_unix_server` fails if the socket path already exists — `os.unlink(path)` (ignore `FileNotFoundError`) before binding.
- **Async test runner — pick one and pin it:** the project has only `pytest` so far. `async def` tests need a runner. **Recommend adding `pytest-asyncio` (latest, pinned, dev group)** and `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`. Alternative (zero new dep): wrap each test body in `asyncio.run(...)` from a sync test. Recommend `pytest-asyncio` for readability since the bus and everything above it (broker, transport) is async-heavy — this runner pays off across Epic 1. Pin it exactly and re-lock `uv.lock` (per 1.1/1.2 pin discipline). Confirm it pulls no provider SDKs (it won't) so import-linter stays KEPT.
- **No new runtime deps:** the bus is stdlib `asyncio` + `shelldon.contracts` only. `pytest-asyncio` is **dev-group**, not runtime.

### Previous story intelligence (Stories 1.1, 1.2)

- **contracts API is ready:** `from shelldon.contracts import Envelope, Job, Result, MsgKind, Actor, encode, decode, SCHEMA_VERSION`. `encode`/`decode` already do msgpack + the closed-header/version enforcement — the frame codec just adds the 4-byte length prefix around them. **Do not re-encode by hand; reuse `encode`/`decode`.** [Source: 1.2 `shelldon/contracts/__init__.py`]
- **`decode()` rejects bad versions and unknown fields** and `__post_init__` rejects kind↔body drift — so a malformed frame raises `msgspec.ValidationError` at `read_frame`. Decide hub policy: **log-and-drop the offending frame, keep the connection alive** (a bad frame shouldn't kill the bus). Don't let the exception escape the handler loop.
- **Packaging:** `uv` + `hatchling`; `uv lock` / `uv sync --locked` / `uv run`. CI = `uv sync --locked` → `lint-imports` → `pytest`. Pin any new dep exactly and commit the re-locked `uv.lock` — a loose pin reads as a regression. [Source: 1.1/1.2]
- **Python `>=3.13,<3.14`** (`.python-version` 3.13). asyncio UDS APIs are POSIX — fine on the macOS dev box and the Pi target; they do **not** exist on Windows, which is irrelevant here. [Source: 1.1]
- **Test layout:** `tests/` mirrors packages; isolation tests per story (Epic 1 cross-cutting note — every story 1.1–1.7 ships its own fakes/stubs so 1.8 confirms wiring rather than running things for the first time). The bus tests are that isolation layer for the seam. [Source: epics.md#Epic 1 cross-cutting]

### Testing standards

- `pytest` (+ `pytest-asyncio` if adopted), mirroring package layout. Cover all three ACs: framing (incl. split-read reassembly + clean EOF), table routing (route-to-remote + route-to-CORE-local), and disconnect/reconnect resilience.
- Every test binds a **`tmp_path` socket** and tears down the server (no leaked sockets, no `~/.shelldon/` writes).
- Run `uv run lint-imports` (must stay KEPT — core LLM-free) and `uv run pytest -q` (all green) before marking tasks done.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.3]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-4, #AD-11, #AD-1, #AD-12, #Consistency Conventions, #Structural Seed]
- [Source: _bmad-output/implementation-artifacts/1-2-versioned-message-contracts.md (contracts API: encode/decode, MsgKind/Actor, closed-header enforcement)]
- [Source: _bmad-output/implementation-artifacts/1-1-...md (packaging, pin discipline, `.shelldon/` gitignored, test layout)]
- Python asyncio streams — `start_unix_server` / `open_unix_connection` / `StreamReader.readexactly` (https://docs.python.org/3.13/library/asyncio-stream.html)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story)

### Debug Log References

- `uv lock` + `uv sync --locked` → `+ pytest-asyncio==1.4.0` (dev group); `asyncio_mode = "auto"`.
- `uv run pytest tests/test_bus_*.py -q` → 6 passed (frame ×3, routing ×2, disconnect ×1).
- `uv run lint-imports` → "core is LLM-free (AD-1) KEPT" — `core/bus` imports only `asyncio` + `shelldon.contracts`.
- `uv run pytest -q` → 26 passed (19 prior + 6 bus + 1 routing-table test; no regressions).

### Completion Notes List

- ✅ Resolved review finding [Patch]: oversized frame now closes the connection (framing-error vs invalid-message split in `_handle`).
- ✅ Resolved review finding [Patch]: `_route` write failures are caught — dead target dropped + deregistered, source survives.
- ✅ Resolved review finding [Patch]: `stop()` awaits each `writer.wait_closed()` before `server.wait_closed()`.
- ✅ Resolved review finding [Patch]: mid-payload truncation returns `None` (clean disconnect), no misleading log.
- Review fixes verified: +4 tests (oversized raise/close, mid-payload EOF, dead-target survival); 30 tests pass, import-linter KEPT. The 3 [Defer] items left as-is per their scope notes.
- All 3 ACs satisfied. Built the core-resident UDS hub: `shelldon/core/bus/` (`frame.py` codec, `server.py` hub, `__init__.py` exports), plus `ROUTING_TABLE` in `contracts/`.
- **AC1 (framing):** `write_frame`/`read_frame` = 4-byte big-endian length + `contracts.encode/decode` (msgpack). `read_frame` uses `StreamReader.readexactly`, so a frame split across reads reassembles for free and a clean EOF returns `None`. 8 MiB frame cap rejects a corrupt prefix before allocating.
- **AC2 (routing):** hub routes by `ROUTING_TABLE[env.kind]` (AD-11 point-to-point). `CORE`-bound envelopes go to an in-process `core_inbox` queue (core is hub AND destination); other actors are forwarded over their socket. Unconnected destination → log-and-drop, never crash.
- **AC3 (disconnect):** per-connection handler exits cleanly on EOF, deregisters, and survives a peer drop; a reconnecting client routes again. `stop()` force-closes tracked connections before `Server.wait_closed()`.
- **Registration:** lazy by `src` (zero new contract surface) — `registry[env.src] = writer` on each frame. Pure-receiver handshake (display, 1.7) deferred per scope.
- **Design choices held:** routed by the `kind`→dst table (not the `dst` field) per the AC; no `dst==table[kind]` validation (replies/broadcast will diverge — YAGNI); no turn-fencing (AD-12 is the arbiter's job, 1.5/1.8); no broadcast mode (7.2).
- **Test-env note (macOS):** AF_UNIX caps the socket path at ~104 chars and pytest's `tmp_path` exceeds it — added a `sock_path` fixture (`tests/conftest.py`) that mints a short `/tmp` socket. Behavior is identical on the Pi; this is purely a test-harness path-length workaround.
- **Async runner:** adopted `pytest-asyncio==1.4.0` (dev, pinned) with `asyncio_mode="auto"` — the bus and everything above it (broker/transport) is async-heavy, so this pays off across Epic 1. No new *runtime* dep; the bus is stdlib `asyncio` + `contracts` only. import-linter stays KEPT.

### File List

- `shelldon/contracts/__init__.py` (modified — added `ROUTING_TABLE` + export)
- `shelldon/core/bus/__init__.py` (new — package exports)
- `shelldon/core/bus/frame.py` (new — length-prefixed codec + `connect` client helper)
- `shelldon/core/bus/server.py` (new — `BusServer` hub + `bus_socket_path`)
- `tests/conftest.py` (new — short-path `sock_path` fixture for AF_UNIX)
- `tests/test_bus_frame.py` (new — framing + split-read + EOF)
- `tests/test_bus_routing.py` (new — JOB→BROKER, RESULT→CORE)
- `tests/test_bus_disconnect.py` (new — disconnect/reconnect resilience)
- `tests/test_bus_errors.py` (new — oversized-frame close + dead-target survival; review fixes)
- `tests/test_contracts_roundtrip.py` (modified — every-kind-has-a-route test)
- `pyproject.toml` (modified — `pytest-asyncio==1.4.0` dev dep + `asyncio_mode="auto"`)
- `uv.lock` (modified — re-locked with pytest-asyncio)

### Change Log

- 2026-06-16: Implemented Story 1.3 — core-resident UDS Envelope bus (AD-4): 4-byte length-prefixed framing, hub routing by the static `kind`→dst `ROUTING_TABLE` (AD-11 point-to-point), CORE-local inbox vs remote-actor forwarding, lazy-`src` registration, graceful disconnect/reconnect. Adopted `pytest-asyncio` for async tests. 26 tests pass, import-linter KEPT. Status → review.
- 2026-06-16: Addressed code review — 4 [Patch] findings resolved in the hub/codec error paths: framing-error vs invalid-message handling (close vs continue), caught `_route` write failures (dead target deregistered, source survives), `stop()` awaits `writer.wait_closed()`, and mid-payload truncation treated as clean EOF. +4 tests; 30 pass, import-linter KEPT. Status → review (re-review).

### Review Findings

- [x] [Review][Patch] Oversized frame does not close the connection [`server.py`, `frame.py`] — `_handle` now distinguishes a `ValueError` framing error (stream offset lost → **break/close the connection**) from a `msgspec.ValidationError` (stream still aligned → log + continue). Tested: `test_oversized_frame_closes_connection_but_hub_survives` (+ frame-level `test_oversized_length_raises_before_allocating`).
- [x] [Review][Patch] `_route` write exceptions propagate uncaught [`server.py`] — `write_frame` to a target is wrapped in `try/except OSError`; a dead target is dropped + deregistered, never killing the source handler. Tested: `test_write_to_dead_target_keeps_source_alive`.
- [x] [Review][Patch] `stop()` missing `await writer.wait_closed()` [`server.py`] — `stop()` now `await`s each writer's `wait_closed()` (errors suppressed) after `close()` so transports drain before `server.wait_closed()`.
- [x] [Review][Patch] Mid-payload `IncompleteReadError` → spurious malformed-frame log [`frame.py`] — `read_frame` wraps both the header and payload `readexactly` in one `IncompleteReadError` handler returning `None`; a peer dying mid-frame is now a clean disconnect, not a logged "malformed frame". Tested: `test_mid_payload_truncation_is_eof_not_malformed`.
- [x] [Review][Defer] Tests synchronize via `asyncio.sleep(0.05)` — timing-based sync is fragile on slow CI; no event-based mechanism to know when the hub has processed a disconnect or registered an actor [`tests/test_bus_routing.py`, `tests/test_bus_disconnect.py`] — deferred, pre-existing test style acceptable for current scope
- [x] [Review][Defer] No test for oversized-frame `ValueError` path — the 8 MiB cap in `read_frame` is untested; the fix for finding #1 above should include a test [`shelldon/core/bus/frame.py:35`] — deferred, pre-existing
- [x] [Review][Defer] `conftest.py` hardcodes `/tmp` — `tempfile.gettempdir()` would respect `TMPDIR` and work in constrained CI environments [`tests/conftest.py:17`] — deferred, pre-existing
