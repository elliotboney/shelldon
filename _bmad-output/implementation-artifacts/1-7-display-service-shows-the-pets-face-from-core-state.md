---
baseline_commit: bd64006bb65f9077e8891e7de5f10db33d666271
---

# Story 1.7: Display service shows the pet's face from core state

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want a long-lived display service that renders the pet's face from core's pushed state,
so that there's a creature on the screen, not a terminal (AD-5, NFR3).

## Acceptance Criteria

1. **Latest-wins per-region render:** Given the long-lived display process holding the Waveshare V4 (real driver behind an interface; **stub renderer for tests**), when core pushes a state snapshot carrying a monotonic `seq`, then the display renders the corresponding face and applies **latest-wins**, dropping any stale (lower-or-equal-`seq`) snapshot.
2. **Coalesce under E-Ink's slow refresh:** Given rapid successive snapshots under E-Ink's seconds-scale refresh, when they arrive faster than the panel can draw, then the display **coalesces to the latest** without flicker or backlog (renders the newest pending, never a queued backlog of intermediate frames — NFR3).

## Tasks / Subtasks

> **Test seam (read first — it shapes everything):** the Waveshare panel is injected behind a `Renderer` interface. ALL display logic (latest-wins drop, per-region tracking, burst coalescing, frame resilience) is tested cross-platform with a **fake renderer** whose `render()` can be gated to simulate the slow E-Ink draw — **no real hardware, no `spidev`**. The real Waveshare V4 driver is the production `Renderer` impl, added on the Pi (component-local dep, out of scope here — the same "ship the mechanism + seam, gate the hardware" discipline as Story 1.5's `skipif(darwin)` real-fork test).
>
> **Zero `core/` edits expected (mirror 1.6):** the snapshot type + its region-id type + the routing row land in `contracts/`; the service lands in `display/`. The hub (`core/bus/server.py`) already routes generically via `ROUTING_TABLE[env.kind]` and already registers any actor (incl. a pure receiver) from its first frame — so the new kind routes to DISPLAY with **no hub change**. The actual core-side *push* of face state is Story 1.8 / Epic 3; 1.7 ships the display + contract and tests against a **stand-in core** (like 1.6's stub core). If you find yourself editing `shelldon/core/`, stop.

- [x] **Task 1: Region-id + state-snapshot contract in `contracts/` (AD-5, AD-11)** (AC: 1)
  - [x] Add a **closed/registered region-id type** `Region(StrEnum)` with one member for now: `FACE = "face"` (core owns the `face` region; plugin-claimed widget regions are Epic 7). A typo must not be able to mint a new region — that's why it's an enum, not a free string (AD-5).
  - [x] Add `MsgKind.STATE_SNAPSHOT = "state-snapshot"`.
  - [x] Add a frozen msgspec body mirroring the `Job`/`Result`/`InboundMessage` style (`frozen=True`, `tag="state-snapshot"`, `forbid_unknown_fields=True`): `StateSnapshot(region: Region, seq: int, face: str)`. `seq` is the per-region monotonic sequence (AD-5); `face` is a **minimal placeholder expression token** for the skeleton (the real starter emotion set — content/sleepy/curious/grumpy/excited/low-battery — and the mood→face mapping are **Story 3.3**, do NOT build them here).
  - [x] Extend the `Envelope.body` union to include `StateSnapshot`; add it to `_KIND_FOR_BODY` (so the `__post_init__` kind↔body guard covers it).
  - [x] Add the routing row `MsgKind.STATE_SNAPSHOT: Actor.DISPLAY` to `ROUTING_TABLE`. (`Actor.DISPLAY` already exists from 1.2.)
  - [x] Export `Region` and `StateSnapshot` in `__all__`.

- [x] **Task 2: The `Renderer` interface + a stub (`display/renderer.py`)** (AC: 1, 2)
  - [x] New `shelldon/display/renderer.py`: a `Renderer` interface — a `typing.Protocol` (or ABC) with `async def render(self, snapshot: StateSnapshot) -> None`. Async because the real E-Ink draw is a slow (seconds-scale) I/O operation the event loop must await, not block on.
  - [x] A `StubRenderer(Renderer)` that records each rendered snapshot (e.g. appends to a public `rendered: list[StateSnapshot]`) — the default for tests and for a headless laptop run. Keep it tiny.
  - [x] Document (one block) that the **real Waveshare V4 driver** is the production `Renderer`, living component-locally (`spidev` + the vendored Waveshare module / `omni-epd`) and added when the hardware is in hand — NOT a dependency of this story. Note partial-refresh/layered-sprite techniques are a 3.3 concern, not 1.7.

- [x] **Task 3: The display service (`display/service.py`)** (AC: 1, 2)
  - [x] New `shelldon/display/service.py`: `async def run_display(socket_path, renderer: Renderer)` — connect as `Actor.DISPLAY` via `connect(socket_path, Actor.DISPLAY)` (the display is the first **pure-receiver** actor; registration already makes it addressable — see Dev Notes). Hold BOTH stream ends alive (dropping the writer closes the connection — the gotcha caught in 1.6's routing test).
  - [x] Run an **intake loop** + a **render loop** (two concurrent tasks under one supervising `await`, sibling cancelled when either ends):
    - **Intake loop:** `read_frame`; for each `STATE_SNAPSHOT`/`StateSnapshot`, apply **latest-wins by seq, per region**: track `latest_seq[region]` (max seq ever accepted, rendered OR pending). If `snapshot.seq <= latest_seq.get(region, -1)` → **drop** (stale). Else set `latest_seq[region] = seq`, stash `pending[region] = snapshot`, and signal the render loop (an `asyncio.Event`). Mirror the broker/CLI per-frame resilience: skip a `msgspec.ValidationError`, end cleanly on a framing `ValueError` or `read_frame()→None` (hub gone). A non-snapshot kind is logged and ignored.
    - **Render loop:** await the signal; atomically take + clear all `pending` regions (snapshot the dict, reset it), then `await renderer.render(...)` each. **This is the coalescing seam:** while a slow `render()` is in flight, intake overwrites `pending[region]` with newer snapshots; when render finishes it picks up only the **latest** per region — intermediate frames never draw (no backlog, NFR3).
  - [x] **Credential/LLM discipline:** `display/` imports no provider SDK and no broker cred path — it's a pure compositor. (No new import-linter contract is required by an AC, but keep it clean; see Dev Notes for the optional hardening.)

- [x] **Task 4: Contract round-trip test — extend M0 (AD-10)** (AC: 1)
  - [x] Extend `tests/test_contracts_roundtrip.py`: encode→decode an `Envelope` carrying a `StateSnapshot` (region=`Region.FACE`, a `seq`, a `face` token); assert lossless round-trip and the decoded body resolves to `StateSnapshot`. Add a kind↔body mismatch case (`kind=STATE_SNAPSHOT` with a non-snapshot body) asserting `__post_init__` raises — proving the closed header bites for the new type. (`test_every_kind_has_a_route` already covers that the new kind has a routing entry — confirm it stays green.)

- [x] **Task 5: Routing test (data-driven hub, no core change)** (AC: 1)
  - [x] New `tests/test_display_routing.py` (mirror `tests/test_transport_routing.py`): with a real `BusServer`, a `DISPLAY` client connects (pure receiver) and a stand-in `CORE` client sends a `STATE_SNAPSHOT` (`dst=DISPLAY`); assert it is delivered to the display client's reader. Proves the new row routes on an **unmodified hub** and that a pure-receiver actor is addressable. Hold all four stream ends alive.

- [x] **Task 6: Display service isolation tests (latest-wins + coalescing, AC1/AC2)** (AC: 1, 2)
  - [x] New `tests/test_display_service.py`: drive `run_display` against a **real `BusServer`** with a **stand-in core** (the test pushes `STATE_SNAPSHOT` envelopes to the display's registered connection) and an injected **gateable fake renderer** (its `render()` awaits a controllable `asyncio.Event` so the test can simulate a slow draw and inspect ordering).
    - (a) **Renders a snapshot:** push one snapshot → the renderer records a face with the expected `seq`/`face`.
    - (b) **Drops stale lower/equal seq (AC1):** push `seq=5`, let it render; then push `seq=3` (and `seq=5` again) → neither draws (both `<= latest_seq`); only `seq=5` ever rendered. Also assert an equal-seq duplicate is dropped (latest-wins is `<=`, not `<`).
    - (c) **Coalesces a burst (AC2):** gate the renderer closed mid-draw of `seq=1`; push `seq=2,3,4,5` rapidly while it's busy; open the gate → the next render is `seq=5`, and `2/3/4` **never** render. Assert the rendered seq sequence is `[1, 5]` (intermediate frames coalesced away — no backlog).
    - (d) **Clean teardown:** hub disconnect (`await srv.stop()`) → `run_display` returns cleanly within a timeout.
  - [x] These are the **isolation tests the Epic-1 cross-cutting note requires** — Story 1.8 then *confirms* the display against real core-pushed state, rather than meeting it for the first time.

- [x] **Task 7: Verify guard + full suite** (AC: 1, 2)
  - [x] `uv run lint-imports` → both existing contracts still KEPT (core LLM-free; transport no-creds); `display/` introduces no provider import. **No `shelldon/core/**` file in the diff** (`git diff --name-only`).
  - [x] `uv run pytest -q` → all green (prior suites + new). No new runtime dependency (asyncio/enum stdlib; msgspec already pinned) — the stub renderer keeps 1.7 hardware-free; so **no `pyproject`/`uv.lock` dependency change**.

## Dev Notes

### Architecture compliance (binding)

- **AD-5 — Core is sole writer; display is a latest-wins region compositor:** "Display never reads shared memory; **core pushes a state snapshot** carrying a **monotonic `seq`**, and display **renders latest-wins**, dropping stale snapshots (tolerates slow E-Ink under reflex churn). The display is a **compositor of REGIONS**: **core owns the `face` region**… Region ids are a **closed/registered type in `contracts/`** (not free strings — a typo can't silently mint a new region). … each region keeps its own latest-wins snapshot stream." 1.7 implements exactly this for the FACE region; plugin-claimed widget regions + conflict-rejection-at-load are **Epic 7** (AD-8). [Source: ARCHITECTURE-SPINE.md#AD-5]
- **AD-11 — Closed envelope header + routing in `contracts/`:** the `STATE_SNAPSHOT→DISPLAY` row and the `Region` closed type live in `contracts/`; the header stays closed (`forbid_unknown_fields` + the `__post_init__` kind↔body check) and the new body inherits that guard. [Source: ARCHITECTURE-SPINE.md#AD-11]
- **AD-4 — The Envelope bus is the only seam:** the display is a bus client (peer to broker/transport); it reaches core only through the bus, never core internals. It is **receive-only** in 1.7 (it consumes snapshots, emits nothing). [Source: ARCHITECTURE-SPINE.md#AD-4]
- **AD-13 / Consistency Conventions — graceful degradation:** a display crash kills the screen, not the soul; the long-lived service should not die on one malformed frame (mirror the broker/CLI per-frame resilience). Full supervision/auto-restart is the end-to-end concern (1.8); 1.7 delivers clean per-frame + disconnect handling. [Source: ARCHITECTURE-SPINE.md#Consistency Conventions]
- **NFR3 — E-Ink seconds-scale refresh:** behaviors/animations tolerate refresh latency measured in seconds; the **coalescing** design (render the latest pending, drop the backlog) is the mechanism that makes this safe under rapid snapshots. [Source: epics.md#NFR3]
- **NFR1 — 512MB ceiling:** coalescing also bounds memory — a single pending slot per region, never an unbounded queue of frames. [Source: epics.md#NFR1]

### Why a stub renderer (not the real Waveshare driver)

The Waveshare V4 driver needs `spidev` + a vendored Waveshare module (or `omni-epd`) and **real hardware** — a component-local, install-time dependency resolved on the Pi, explicitly **not** a spine dependency (Structural Seed). Story 1.7 ships the `Renderer` *interface* + a recording stub so the whole display behaves and is fully tested on Elliot's laptop, dependency-free; the real driver is the production impl swapped in behind the interface on the Pi. Same discipline as 1.5's Linux-gated real-fork test: ship the mechanism + the seam, gate the hardware. [Source: ARCHITECTURE-SPINE.md#Structural Seed, #Component-local deps]

### The pure-receiver actor (first one — the "deferred to 1.7" note)

`core/bus/server.py`'s docstring says "a registration handshake for pure-receiver actors is deferred to Story 1.7." **Registration already handles this** — `connect()` sends the `Actor.DISPLAY` registration as the mandatory first frame and the hub learns the connection from it (`read_registration` in `frame.py`), so a `STATE_SNAPSHOT` routed to DISPLAY finds the display's writer with **no hub change**. The only thing that made the display "special" was that it never *sends* — and the existing `_handle` loop simply blocks on `read_frame` (EOF) for a silent client, which is correct. So 1.7 *exercises and confirms* pure-receiver registration (Task 5) rather than building it. (The stale docstring line can be left or trimmed — but trimming it touches `core/`, which AC-wise we avoid; leave it.) [Source: core/bus/server.py, core/bus/frame.py]

### Coalescing design (the key decision — AC2/NFR3)

The display is **not** a simple read→render loop (that would queue a backlog under E-Ink's seconds-scale draw). It is **intake → single-slot-per-region → render-latest**:

1. **Intake** overwrites `pending[region]` with the newest accepted snapshot and bumps `latest_seq[region]`; a stale (`seq <= latest_seq`) snapshot is dropped at the door.
2. **Render** takes the current pending set, clears it, and awaits the slow `renderer.render()` for each. New arrivals during that await overwrite the (now-cleared) pending slot, so when render loops back it draws only the **latest** — intermediate frames are coalesced away.

This gives both ACs from one structure: latest-wins (intake drop + single slot) and burst-coalescing (render-the-latest, never a backlog). The fake renderer's gate (an `asyncio.Event` awaited inside `render()`) is what makes the coalescing deterministically testable. [Source: AD-5 latest-wins + NFR3 seconds-scale]

### Previous story intelligence (1.1–1.6)

- **Bus client API:** `from shelldon.core.bus import connect, read_frame, write_frame`. `connect(socket_path, Actor.DISPLAY)` registers on connect (1.4). The display only `read_frame`s. [Source: 1.3/1.4]
- **Edge-actor loop to copy:** `broker/service.py` and `transport/cli.py` — connect, loop `read_frame`, skip `ValidationError`, end on framing `ValueError`/`None`, never die on one bad frame. The display's intake loop is the same shape. [Source: 1.4/1.6]
- **Two-task supervise/teardown pattern:** `transport/cli.py run_cli_transport` runs two concurrent loops under `asyncio.wait(FIRST_COMPLETED)`, cancels the sibling, re-raises a genuine failure. Reuse this exact shape for intake+render. [Source: 1.6 transport/cli.py]
- **Contract style:** add `StateSnapshot` exactly like `InboundMessage`/`OutboundMessage` were added in 1.6 — frozen tagged struct, body-union + `_KIND_FOR_BODY` + `ROUTING_TABLE` rows + `__all__`. The 1.6 contract edit is the template. [Source: 1.6 contracts/__init__.py]
- **Test gotcha (from 1.6):** holding only one stream end (`_, writer = await connect(...)` then rebinding `_`) lets the connection close and **deregisters the actor** — multi-connection bus tests must keep BOTH ends of every connection alive. The display routing/service tests will have several connections; name every stream. [Source: 1.6 review / test_transport_routing.py fix]
- **Test harness:** `pytest` + `pytest-asyncio` (auto). UDS uses the `sock_path` fixture (`tests/conftest.py`, macOS AF_UNIX path cap). Stand-in-core/registration sync uses `await asyncio.sleep(0.05)` after connect, or poll `srv._registry.get(Actor.DISPLAY)` like `test_cli_transport.py::_transport_writer`. [Source: 1.3 conftest, 1.6 tests]
- **No deps:** asyncio/enum/typing are stdlib; msgspec pinned. The stub renderer keeps 1.7 hardware-free. If (unexpectedly) a dep is added, pin exact + commit `uv.lock`. [Source: 1.1–1.6 pin/lock discipline]
- **`display/__init__.py`** is a one-line stub — add `renderer.py` + `service.py` beside it; optionally re-export `run_display`/`StubRenderer`. [Source: scaffold 1.1]

### Project Structure Notes

- All edits in **`shelldon/contracts/__init__.py`** (region type + snapshot body + routing) and new files under **`shelldon/display/`** (`renderer.py`, `service.py`) + `display/__init__.py` (optional re-export); tests under `tests/`. **No `shelldon/core/` file is touched** — the hub routes the new kind generically (verify with `git diff --name-only`). Aligns with the Structural Seed: `display/` = region compositor; `contracts/` = shared msgspec types incl. region ids. [Source: ARCHITECTURE-SPINE.md#Structural Seed]

### Optional hardening (not required by an AC)

- A sibling import-linter `forbidden` contract over `shelldon.display` (forbidding provider SDKs) would mirror the `transport/` guard added in 1.6 and cheaply prove the compositor stays LLM-free. It's not mandated by a 1.7 AC; add it if you want the symmetry, otherwise note it as a candidate. Keep the decision visible (don't silently skip).

### Scope boundary (prevent scope creep)

**IN scope (1.7):** the `Region` closed type + `StateSnapshot` contract + routing row; the `Renderer` interface + recording stub; the long-lived display service (intake + coalescing render loop, latest-wins per region); contract round-trip + routing + display-service isolation tests.

**OUT of scope (later, do NOT build):**
- **Real core-side push of face state** (arbiter/reflexes producing snapshots) → **Story 1.8 / Epic 3**. 1.7 tests against a stand-in core.
- **The real Waveshare V4 driver** (`spidev`, partial-refresh/layered-sprite, on-Pi) → component-local, added with hardware; 1.7 ships the interface + stub only.
- **The real expression vocabulary + mood→face mapping** (content/sleepy/curious/grumpy/excited/low-battery) → **Story 3.3**. 1.7's `face` is a placeholder token.
- **Plugin-claimed widget regions + conflict-rejection-at-load** → **Epic 7** (AD-8). 1.7 has one region (FACE); design per-region so widgets slot in, but don't build claiming.
- **Display supervision / auto-restart** → **1.8 / Epic 2**.
- **Personality-state struct / reflex loop** (the source of face changes) → **Epic 3**.

### Testing standards

- `pytest` + `pytest-asyncio` (auto), mirroring package layout. Display logic is tested via the **injected fake renderer** (gateable to simulate slow draw) against a real `BusServer` + stand-in core — deterministic, cross-platform, no hardware. The coalescing test gates `render()` mid-draw to prove intermediate frames never draw. The contract round-trip (encode/decode `StateSnapshot`) is the M0 test (AD-10).
- Before marking tasks done: `uv run lint-imports` (KEPT), `uv run pytest -q` (green), and `git diff --name-only` shows **no `shelldon/core/**`** change.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.7; #NFR3, #NFR1; #Epic 1 cross-cutting (isolation tests)]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-5, #AD-11, #AD-4, #AD-13, #Structural Seed, #Consistency Conventions]
- [Source: shelldon/contracts/__init__.py (Job/Result/InboundMessage/OutboundMessage pattern, _KIND_FOR_BODY, ROUTING_TABLE, __post_init__ guard); shelldon/core/bus/server.py + frame.py (data-driven routing, pure-receiver registration already implemented)]
- [Source: _bmad-output/implementation-artifacts/1-6-...md (transport adapter: two-task supervise/teardown, contract-extension template, the keep-both-stream-ends test gotcha); 1-5 (injectable-seam + hardware-gate discipline); 1-4 (edge-actor connect+serve loop); 1-3 (bus, conftest sock_path)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story). Context carried from the 1.6 session: epics.md, ARCHITECTURE-SPINE.md (AD-5/AD-11/AD-4/AD-13), and the live codebase (contracts, bus hub, the broker/CLI edge-actor patterns).

### Debug Log References

- `uv run pytest tests/test_contracts_roundtrip.py -q` → green (StateSnapshot round-trip + kind↔body mismatch added).
- `uv run pytest tests/test_display_routing.py tests/test_display_service.py -q` → 5 passed (routing + 4 service: render, latest-wins drop, burst-coalesce, clean teardown).
- `uv run pytest -q` → **76 passed, 1 skipped** (+7 vs the 69 after the 1.6 sweep; the 1 skip is the pre-existing darwin-gated fork test). No regressions.
- `uv run lint-imports` → 2 contracts KEPT (core LLM-free; transport no-creds); `display/` introduces no provider import.
- `git status --porcelain | grep shelldon/core/` → **empty** (AC: zero core edits).
- No dependency change — asyncio/enum/typing stdlib, msgspec already pinned; the stub renderer keeps 1.7 hardware-free.

### Completion Notes List

- Both ACs satisfied. There's a creature on the screen (a recording stub today; the Waveshare panel swaps in behind the same interface on the Pi).
- **AC1 (latest-wins per region):** new `StateSnapshot(region, seq, face)` body + `Region(StrEnum){FACE}` + `STATE_SNAPSHOT→DISPLAY` routing in `contracts/`. The intake loop drops any snapshot whose `seq` is not **strictly greater** than the latest accepted for its region — covering both stale (lower) and duplicate (equal) seqs. Proven by `test_drops_stale_and_duplicate_seq`.
- **AC2 (coalesce under slow refresh):** the display is intake→single-slot-per-region→render-latest. A gated fake renderer holds a draw open while a burst (seq 2–5) arrives; when the draw finishes the render loop picks up only the newest (seq 5) — 2/3/4 never draw. Proven by `test_coalesces_burst_to_latest` (rendered sequence `[1, 5]`).
- **AC-clean (zero `core/` edits):** the hub routes the new kind generically and already registers pure-receiver actors from their first frame, so the **first pure-receiver actor** (display) needed no hub change — the stale "deferred to 1.7" docstring note in `server.py` is now satisfied by existing registration (left untouched to keep `core/` out of the diff). `git diff` confirms no `core/` file changed.
- **Design decisions:** (a) `Renderer` is an async `Protocol` + a recording `StubRenderer`; the real Waveshare/`spidev` driver is a Pi-install swap-in (hardware-gated, like 1.5's fork test). (b) Two-task supervise/teardown (`asyncio.wait(FIRST_COMPLETED)`) reused verbatim from 1.6's transport; the intake loop owns termination. (c) Per-region `latest_seq` + `pending` dicts so Epic 7 widget regions slot in without rework (1.7 exercises only FACE).
- **Test-design fix during dev:** the first cut of the service tests opened an idle stand-in `CORE` *client* connection, which made `BusServer.stop()` hang (a pre-existing `stop()` quirk: it waits on a handler whose client never disconnects). Rather than touch `core/`, I switched the tests to push snapshots via the display's **registered server-side writer** (the same pattern 1.6's CLI test used for outbound) — routing is covered separately in `test_display_routing.py`. Recorded as a latent `stop()` robustness item for the deferred ledger.
- **Scope held:** no real core-side push of face state (1.8/Epic 3), no real Waveshare driver (Pi/hardware), no expression vocabulary or mood→face mapping (3.3), no plugin-claimed widget regions (Epic 7), no display supervision/auto-restart (1.8/Epic 2).
- **Optional hardening not taken:** a `display/` import-linter contract (mirroring `transport/`) — not required by a 1.7 AC; noted as a candidate rather than silently skipped.

### File List

- `shelldon/contracts/__init__.py` (modified — `Region` enum, `StateSnapshot` body, `STATE_SNAPSHOT` kind, body-union + `_KIND_FOR_BODY` + `ROUTING_TABLE` row, `__all__`)
- `shelldon/display/renderer.py` (new — `Renderer` Protocol + recording `StubRenderer`; real Waveshare driver documented as the Pi swap-in, AD-5)
- `shelldon/display/service.py` (new — `run_display`: pure-receiver bus client, intake + coalescing render loop, latest-wins per region, AD-5/NFR3)
- `shelldon/display/__init__.py` (modified — re-export `Renderer`/`StubRenderer`/`run_display`)
- `tests/test_contracts_roundtrip.py` (modified — round-trip `StateSnapshot` + a kind↔body mismatch case, M0)
- `tests/test_display_routing.py` (new — `STATE_SNAPSHOT→DISPLAY` on an unmodified hub; pure-receiver addressability)
- `tests/test_display_service.py` (new — render, latest-wins drop of stale+duplicate seq, burst coalescing, clean teardown)

## Change Log

- 2026-06-16: Implemented Story 1.7 — display service renders the pet's face from core-pushed state (AD-5). Added `Region`/`StateSnapshot` to `contracts/` with `STATE_SNAPSHOT→DISPLAY` routing; built `display/` (a `Renderer` interface + stub, and `run_display`: a pure-receiver bus client with an intake + coalescing render loop — latest-wins per region by strict-greater `seq`, burst coalescing under E-Ink's slow refresh). Stub renderer keeps 1.7 hardware-free; the real Waveshare driver is a Pi swap-in behind the interface. **Zero `core/` changes** — the hub routes the new kind generically and already registers pure-receiver actors. 76 pass / 1 skipped, both import-linter contracts KEPT, no dep change. Status → review.
