---
baseline_commit: 2bc5ceddb1a3a0d13463f1f1d5a1bc28ff256842
---

# Story 1.4: Capability broker with one provider and basic retry

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want a broker process that is the only holder of credentials and makes the LLM call on the pet's behalf, retrying a transient error once,
so that a single GLM hiccup doesn't kill a turn and no other process ever touches my keys (AD-2).

## Acceptance Criteria

1. **Broker calls the model, injects creds internally:** the broker process holds the GLM credential (GLM-5.2 via the Z.ai **Anthropic-compatible** endpoint — the Anthropic-format adapter is the first one built). When core sends a model `Job` over the bus, the broker injects the credential internally, calls the model, and returns a `Result` — **the credential never appears on the bus or in core**.
2. **Retry once on transient error:** on a transient model error (e.g. a 500 or timeout), the broker retries the call **once** before surfacing a failure `Result`. (Full multi-provider chain/fallback is Epic 2 — not here.)
3. **Credential isolation:** any process other than the broker **cannot** read the credential or call the model directly — the credential is reachable only inside the broker (process separation + the `core/` import-linter forbidding provider SDKs + no creds on the bus).

## Tasks / Subtasks

- [x] **Task 1: Add the `anthropic` provider SDK (broker-only runtime dep)** (AC: 1)
  - [x] Add `anthropic==0.109.2` to `[project].dependencies` (verify it's still latest at dev time; pin exact per project discipline). The `core/` import-linter already forbids `anthropic` — this story makes that guard load-bearing.
  - [x] `uv lock` + `uv sync --locked`; commit the re-locked `uv.lock`. Confirm `lint-imports` stays KEPT (core must not import it).
- [x] **Task 2: Provider abstraction (SDK-agnostic seam)** (AC: 1, 2)
  - [x] New `shelldon/broker/provider.py`: a `LLMProvider` Protocol with `async def complete(self, prompt: str) -> str`, plus `class TransientProviderError(Exception)` (retryable) and `class PermanentProviderError(Exception)` (not retryable). The broker's retry logic keys on these, NOT on SDK exception types — so broker logic stays testable with fakes and SDK-free.
- [x] **Task 3: GLM provider (Anthropic-format, Z.ai endpoint)** (AC: 1)
  - [x] New `shelldon/broker/glm.py`: an `LLMProvider` implementation using `anthropic.AsyncAnthropic(api_key=<from env>, base_url=<Z.ai Anthropic endpoint>)`. `complete()` calls `client.messages.create(model=..., max_tokens=..., messages=[{"role":"user","content":prompt}])` and returns the text.
  - [x] **Credential + config from env, never hardcoded, never on the bus:** read `GLM_API_KEY` (required), `GLM_BASE_URL` (default the Z.ai Anthropic-compatible endpoint), `GLM_MODEL` (the exact GLM-5.2 model id — config, not a spine invariant). Construct the client inside the broker only.
  - [x] **Translate SDK errors to the abstraction:** map `anthropic.APITimeoutError`, `anthropic.RateLimitError`, `anthropic.InternalServerError`, and `APIStatusError` with `status_code >= 500` → `TransientProviderError`; everything else (4xx, auth, bad-request) → `PermanentProviderError`. This is the only place that knows the SDK.
- [x] **Task 4: Broker turn logic — handle a Job → Result with retry-once** (AC: 1, 2)
  - [x] New `shelldon/broker/broker.py`: `async def handle_job(job: Job, provider: LLMProvider) -> Result`.
    - Call `provider.complete(job.payload)` → on success `Result(ok=True, payload=text)`.
    - On `TransientProviderError`: **retry exactly once**. If the retry succeeds → ok Result; if it also fails → `Result(ok=False, error=...)`.
    - On `PermanentProviderError`: **no retry** → `Result(ok=False, error=...)` (provider called exactly once).
    - The error `Result` carries a message string only — **no credential, no raw exception with secrets**. Errors travel as a `Result` error variant, never as an exception across the bus (Consistency Conventions).
- [x] **Task 5: Broker as a bus client (+ bus registration prerequisite)** (AC: 1)
  - [x] **Bus registration (cross-cutting — see Dev Notes):** the broker is the first *receiver-first* actor (it waits for Jobs), so 1.3's lazy-`src` registration can't address it. Add an explicit registration to `core/bus`: a client announces its `Actor` as the first frame on connect; the hub registers it, then routes. Update `connect(path, actor)` and the hub handler; **update the 1.3 bus tests** to register explicitly (this supersedes the lazy-`src` note 1.3 deferred to 1.7).
  - [x] New `shelldon/broker/service.py` (or in `broker.py`): the broker bus-client loop — `connect(socket_path, Actor.BROKER)`, read `Envelope`s, for each `Job` call `handle_job`, wrap the `Result` in an `Envelope(kind=RESULT, src=BROKER, dst=CORE, turn_id=job's turn_id, body=result)` and `write_frame` it back (routes RESULT→CORE). Keep it thin glue; full end-to-end turn wiring is Story 1.8.
  - [x] Preserve `turn_id`: the Result envelope echoes the Job envelope's `turn_id` so core can later fence on it (AD-12).
- [x] **Task 6: Tests — broker logic (isolation, fake provider, no network)** (AC: 1, 2, 3)
  - [x] `tests/test_broker_retry.py` (fake providers, NO real SDK/network):
    - success → `Result(ok=True, payload=...)`, provider called once.
    - transient-then-success → retried once, `ok=True` (AC2).
    - transient-twice → `Result(ok=False, error=...)` (retry exhausted), provider called exactly twice.
    - permanent error → `Result(ok=False)`, provider called exactly **once** (no retry).
  - [x] `tests/test_broker_creds.py` (AC3): the error/success `Result` contains no credential substring; the GLM provider reads its key from env (assert it raises a clear error when `GLM_API_KEY` is unset, and that the key is not stored on any envelope). Re-affirm `Job`/`Result` carry no cred fields (contracts guard from 1.2 still holds).
  - [x] `tests/test_broker_bus.py` (integration, real `BusServer` + fake provider): a worker-role client sends a `JOB`; the registered broker handles it and a `RESULT` lands in `core_inbox` with the echoed `turn_id`. Exercises the new registration + routing + broker glue (a mini-1.8).
- [x] **Task 7: Verify guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → **core stays LLM-free**: `core/` (incl. `core/bus`) imports no provider SDK; `broker/` may import `anthropic`. Confirm KEPT.
  - [x] `uv run pytest -q` → all green (prior suites + new broker tests; the updated 1.3 bus tests still pass).

## Dev Notes

### Architecture compliance (binding)

- **AD-2 — Broker is the sole trust boundary:** the broker is a **separate process** and the **only** holder of credentials and the **only** egress to models. It owns retry/fallback (here: ONE provider GLM + retry-once; the ordered chain is Epic 2). `Job` envelopes carry **no creds**; the broker injects them internally. No other process holds creds or calls a model directly. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-1 — LLM-free core:** the broker lives in `broker/`, **not** `core/`. `core/` (including `core/bus`) must never import a provider SDK — the import-linter enforces it and this story makes the `anthropic` entry in the forbidden list real. [Source: ARCHITECTURE-SPINE.md#AD-1]
- **AD-4 / AD-11 — bus + envelopes:** the broker is a bus client; it receives `Job` envelopes (routed JOB→BROKER) and returns `Result` envelopes (routed RESULT→CORE) over the 1.3 UDS bus. [Source: ARCHITECTURE-SPINE.md#AD-4, #AD-11]
- **AD-12 — turn identity:** the broker echoes the Job's `turn_id` onto its Result so core can fence later. The broker itself does no fencing. [Source: ARCHITECTURE-SPINE.md#AD-12]
- **NFR5 — pluggable brain, default GLM:** default provider GLM-5.2 via Z.ai; the **Anthropic-format adapter is the first built** (it also serves native Claude later, Epic 2). [Source: epics.md#NFR5; Story 2.1]
- **Consistency Conventions:** config + secrets resolve **only inside the broker**; errors surface as a `Result` error variant, never an exception across the bus; no credentials ever on the bus. [Source: ARCHITECTURE-SPINE.md#Consistency Conventions]

### ⚠️ Cross-cutting decision: bus registration (touches Story 1.3 code)

Story 1.3 used **lazy-`src` registration** (the hub learns a connection's identity from the `src` of frames it *sends*) and explicitly deferred a real handshake "to Story 1.7 (display)". **That deferral is actually due now:** the broker is the first *receiver-first* actor — it connects and waits for `Job`s, sending nothing first, so lazy-`src` can never register it as a routable `BROKER`. 

**Resolution (recommended):** add explicit registration to `core/bus` — the client sends its `Actor` as a mandatory first frame on connect; the hub registers `actor→writer` before the Envelope loop, then routes. This is cleaner than lazy-`src` and is the foundation display (1.7) needs too. It **modifies 1.3's `core/bus` + bus tests** — a justified change (a required capability the next actor needs, replacing an explicitly-deferred placeholder), not gratuitous refactoring. Keep it minimal: one small registration frame (the `Actor` value, length-prefixed via the existing framing), mandatory for all clients, no new `MsgKind`/contract surface.

### Scope boundary (prevent scope creep)

**IN scope (1.4):** the provider abstraction + one GLM (Anthropic-format/Z.ai) provider, broker handle-Job logic with retry-**once**, cred-from-env injection, the broker bus-client glue, the bus registration handshake (prerequisite), isolation + one integration test.

**OUT of scope (later stories, do NOT build):**
- Multi-provider **ordered chain + fallback** through providers → **Epic 2 (2.1/2.2)**
- **Degrade-to-reflex** on total failure → **Epic 2 (2.3)**; here a failed turn just returns a failure `Result`.
- The **fork-server / worker** that assembles prompts and proxies the call → **Story 1.5**. In 1.4 the `Job.payload` is treated as the ready prompt; core/a-test sends the Job.
- **Tool execution + safety policy** → SPEC non-goal (broker is their future home, not built now).
- **Vault/uid isolation** of the worker → **Epic 4 (4.3)**. AC3 here is process separation + import-linter + no-creds-on-bus, not OS uid isolation.
- Real network calls in tests — all broker tests use **fake providers**; the real SDK path is config-driven and exercised manually/in 1.8.

### Library / provider notes

- **`anthropic` SDK** (`AsyncAnthropic`): point `base_url` at the Z.ai Anthropic-compatible endpoint and pass the GLM key. `client.messages.create(model, max_tokens, messages=[...])`; the text is in `response.content[0].text` (guard for empty content). Pin exact (`==0.109.2` at time of writing — re-verify latest in dev).
- **Exact model id / base_url are broker CONFIG, not spine** — read from env (`GLM_API_KEY`, `GLM_BASE_URL`, `GLM_MODEL`). Do not hardcode the model string in code; default `GLM_BASE_URL` to the Z.ai Anthropic endpoint and require `GLM_API_KEY`. [Source: ARCHITECTURE-SPINE.md#Deferred — "Exact LLM model id + per-provider config — broker config"]
- **Error taxonomy:** anthropic raises `APITimeoutError`, `RateLimitError`, `APIStatusError` (has `.status_code`), `InternalServerError`. Map 5xx/timeout/rate-limit → transient (retry once); 4xx/auth → permanent (no retry).
- **No new runtime dep in core** — `anthropic` is imported only under `broker/`. Tests that use fake providers never import `glm.py`, so they don't import `anthropic`.

### Previous story intelligence (1.1–1.3)

- **Bus API (1.3):** `from shelldon.core.bus import BusServer, connect, read_frame, write_frame`. The hub routes by `contracts.ROUTING_TABLE` (`JOB→BROKER`, `RESULT→CORE`); `BusServer.core_inbox` is the CORE-local queue. **Registration is the piece you're extending** (see cross-cutting note). [Source: 1.3]
- **contracts (1.2):** `Job(payload: str)`, `Result(ok: bool, payload="", error: str|None)`, `Envelope(id, kind, src, dst, body, v, turn_id)`. `Result.error` is the error variant to use for failures. Envelopes are frozen, value-equal. Build Results with these — don't invent new shapes. [Source: 1.2]
- **Packaging/pins:** `uv` + `hatchling`; `uv lock`/`uv sync --locked`/`uv run`; CI runs `uv sync --locked` → `lint-imports` → `pytest`. Pin `anthropic` exactly, commit `uv.lock`. [Source: 1.1/1.2/1.3]
- **Async tests:** `pytest-asyncio==1.4.0` with `asyncio_mode="auto"` is already configured — write `async def test_...` directly. UDS sockets in tests use the `sock_path` fixture (`tests/conftest.py`) to stay under the macOS AF_UNIX path cap. [Source: 1.3]
- **import-linter is the AC3 guard:** `core is LLM-free (AD-1)` already lists `anthropic` as forbidden. After this story core genuinely depends on that rule — confirm KEPT. [Source: 1.1 `pyproject.toml`]

### Testing standards

- `pytest` + `pytest-asyncio` (auto mode), mirroring package layout. Broker logic tests use **fake providers** (a class implementing `complete()` that returns canned text or raises `TransientProviderError`/`PermanentProviderError`) — **no network, no real key**. Assert call counts to prove retry-once vs no-retry.
- The integration test uses the real `BusServer` + a fake-provider broker client over a `sock_path` socket; assert the `Result` reaches `core_inbox` with the echoed `turn_id`.
- Run `uv run lint-imports` (KEPT) and `uv run pytest -q` (green) before marking tasks done.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.4; #NFR5]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-2, #AD-1, #AD-4, #AD-11, #AD-12, #Consistency Conventions, #Deferred]
- [Source: _bmad-output/implementation-artifacts/1-3-...md (bus API, ROUTING_TABLE, registration deferral now due)]
- [Source: _bmad-output/implementation-artifacts/1-2-...md (Job/Result/Envelope contracts)]
- anthropic Python SDK — `AsyncAnthropic`, `messages.create`, error types (https://github.com/anthropics/anthropic-sdk-python); Z.ai GLM Anthropic-compatible endpoint (broker config)

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story)

### Debug Log References

- `uv lock` + `uv sync --locked` → `+ anthropic==0.109.2` (runtime); verified `import anthropic` → 0.109.2.
- `uv run pytest tests/test_broker_*.py -q` → 8 passed (retry ×4, creds ×3, bus integration ×1).
- `uv run lint-imports` → "core is LLM-free (AD-1) KEPT" over 24 files / 27 deps — now load-bearing (anthropic is a real dep core must not touch).
- `uv run pytest -q` → 38 passed (30 prior + 8 broker; updated 1.3 bus tests still green after the registration change).

### Completion Notes List

- ✅ Resolved review finding [Patch]: `handle_job` catch-all → unexpected errors become a failure Result, never an exception across the bus.
- ✅ Resolved review finding [Patch]: empty GLM completion now raises `PermanentProviderError` instead of a silent empty-success.
- ✅ Resolved review finding [Patch]: extracted `_serve_connection` with read-error handling (invalid→continue, framing→break) mirroring the hub.
- ✅ Resolved review finding [Patch]: broker write failure (hub gone) ends the connection cleanly rather than crashing.
- ✅ Resolved review finding [Patch]: closed the worker writer in `test_broker_bus.py`.
- Review fixes verified: +6 tests (unexpected-exception, glm text/empty, serve happy/write-fail/framing-error); 44 tests pass, import-linter KEPT. The 8 [Defer] items left per their scope notes. Residual ResourceWarnings are unclosed client writers in the 1.3 bus tests (pre-existing, same class as finding 5, not flagged here).
- All 3 ACs satisfied. Built the broker as the sole trust boundary: provider seam (`broker/provider.py`), GLM Anthropic-format/Z.ai provider (`broker/glm.py`), retry-once turn logic (`broker/broker.py`), and the bus-client loop (`broker/service.py`).
- **AC1 (model call + cred injection):** `GLMProvider` builds `AsyncAnthropic` with the key + base URL read **only from env** inside the broker; `handle_job` calls it and returns a `Result`. The integration test proves a `Job` over the bus yields a `Result` in `core_inbox` with the echoed `turn_id` (AD-12). Credential never on a Job/Result/Envelope.
- **AC2 (retry-once):** `handle_job` keys on `TransientProviderError` (retry exactly once, 2 attempts max) vs `PermanentProviderError` (no retry). Tests assert exact provider call counts for success / transient-then-ok / transient-twice / permanent.
- **AC3 (cred isolation):** key required from env (provider raises without it), key not exposed on the provider's public API, and `Job`/`Result` structurally carry no cred field. The mechanical backstop is the `core/` import-linter forbidding `anthropic` — KEPT, and now real.
- **SDK error taxonomy lives only in `glm.py`:** timeout/connection/rate-limit/5xx → transient; other 4xx → permanent. Broker logic stays SDK-free and fake-testable.
- **⚠️ Cross-cutting (touched done 1.3 code):** added **explicit bus registration** to `core/bus` — a client announces its `Actor` as a mandatory first frame on connect (`connect(path, actor)`), the hub registers then routes. This replaces 1.3's lazy-`src` (which can't address a receiver-first actor like the broker) and supersedes the handshake 1.3 deferred to 1.7. Updated the four 1.3 bus tests to register explicitly; all still pass.
- **Config deferred per spine:** exact GLM-5.2 model id + base URL are env config (`GLM_MODEL`/`GLM_BASE_URL`), not hardcoded; only sensible overridable defaults shipped.
- **Note (not acted on):** `broker/` imports `shelldon.core.bus` for the client framing helpers — consistent with 1.3's placement of `connect`/`read_frame`/`write_frame` under `core/bus`. If "edges never import core" tightens later, those wire helpers could move to a neutral module. Flagged, not changed (out of scope).

### File List

- `pyproject.toml` (modified — `anthropic==0.109.2` runtime dep)
- `uv.lock` (modified — re-locked with anthropic + transitive deps)
- `shelldon/core/bus/frame.py` (modified — registration framing; `connect(path, actor)` now registers)
- `shelldon/core/bus/server.py` (modified — explicit registration replaces lazy-src in `_handle`)
- `shelldon/core/bus/__init__.py` (modified — export `read_registration`/`write_registration`)
- `shelldon/broker/provider.py` (new — `LLMProvider` Protocol + Transient/Permanent errors)
- `shelldon/broker/glm.py` (new — GLM Anthropic-format/Z.ai provider + SDK error translation)
- `shelldon/broker/broker.py` (new — `handle_job` retry-once logic)
- `shelldon/broker/service.py` (new — broker bus-client loop)
- `tests/test_broker_retry.py` (new — retry/no-retry call-count tests)
- `tests/test_broker_creds.py` (new — credential isolation)
- `tests/test_broker_bus.py` (new — Job→Result over the real bus; worker writer closed)
- `tests/test_broker_glm.py` (new — GLM text vs empty-completion handling; review fix)
- `tests/test_broker_service.py` (new — `_serve_connection` resilience: write-fail + framing-error; review fixes)
- `tests/test_bus_routing.py` `tests/test_bus_disconnect.py` `tests/test_bus_errors.py` (modified — register explicitly via `connect(path, actor)`)

### Change Log

- 2026-06-16: Implemented Story 1.4 — capability broker (AD-2): SDK-agnostic provider seam, GLM Anthropic-format/Z.ai provider (cred from env, never on the bus), retry-once turn logic (transient vs permanent), and the broker bus-client loop returning Results to core. Added explicit bus registration to `core/bus` (the broker is the first receiver-first actor; replaces 1.3 lazy-src) and updated the 1.3 bus tests. `anthropic==0.109.2` pinned. 38 tests pass, core-LLM-free import-linter KEPT (now load-bearing). Status → review.
- 2026-06-16: Addressed code review — 5 [Patch] findings resolved: `handle_job` catch-all (no exception across the bus), empty-completion → failure, broker loop read/write resilience (`_serve_connection` mirrors the hub), and the test socket-close fix. +6 tests; 44 pass, import-linter KEPT. Status → review (re-review).

### Review Findings

- [x] [Review][Patch] Unhandled exceptions escape `handle_job` [`broker.py`] — added a catch-all `except Exception` returning `Result(ok=False, error="unexpected provider error: <Type>")` (non-retryable). Tested: `test_unexpected_exception_becomes_failure_result`.
- [x] [Review][Patch] Empty `resp.content` → silent `ok=True` [`glm.py`] — `complete()` now raises `PermanentProviderError("provider returned no text")` on empty text. Tested: `test_empty_content_raises_permanent` (+ text-response happy path).
- [x] [Review][Patch] `read_frame` exceptions uncaught in `service.py` — extracted `_serve_connection` mirrors `server.py`: `ValidationError` → log+continue, `ValueError` (framing) → break. Tested: `test_serve_survives_framing_error`.
- [x] [Review][Patch] `write_frame` OSError kills broker [`service.py`] — the reply write is wrapped in `try/except OSError` → log + end the connection cleanly. Tested: `test_serve_survives_write_failure`.
- [x] [Review][Patch] WORKER writer never closed in `test_broker_bus.py` — close + `wait_closed()` the worker connection. (Note: the same unclosed-client-writer pattern remains in the 1.3 bus tests — pre-existing, not in this review's [Patch] scope; left for a later hygiene sweep.)
- [x] [Review][Defer] `Result.error` could theoretically leak credential via `str(sdk_exc)` in TransientProviderError chain [`shelldon/broker/broker.py`, `shelldon/broker/glm.py`] — deferred, speculative; SDK error messages don't typically include keys
- [x] [Review][Defer] Tests use `asyncio.sleep(0.05)` for broker registration sync — timing-based, flakiness risk on slow CI [`tests/test_broker_bus.py`] — deferred, pre-existing pattern from 1.3
- [x] [Review][Defer] No backoff between transient retries — immediate retry into a rate-limited endpoint wastes the only retry budget [`shelldon/broker/broker.py`] — deferred, beyond "basic retry" spec scope
- [x] [Review][Defer] `connect()` has no timeout — hung server blocks broker indefinitely [`shelldon/core/bus/frame.py`] — deferred, resilience is later scope
- [x] [Review][Defer] Sequential job processing — `await handle_job` blocks the read loop; slow model call stalls all incoming Jobs [`shelldon/broker/service.py`] — deferred, concurrent handling is Epic 2 scope
- [x] [Review][Defer] No test for non-Job envelope path (`log.warning + continue` branch in `run_broker`) [`tests/test_broker_bus.py`] — deferred, low-priority coverage gap
- [x] [Review][Defer] No test for hub-disconnect path in broker (`env is None: break`) [`tests/test_broker_bus.py`] — deferred, low-priority coverage gap
- [x] [Review][Defer] `run_broker` has no reconnect logic — a transient hub restart kills the broker permanently [`shelldon/broker/service.py`] — deferred, resilience / supervisor loop is later scope
