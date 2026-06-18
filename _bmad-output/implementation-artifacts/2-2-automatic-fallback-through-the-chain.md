---
baseline_commit: 11a46563bedd05583dd23c81cfc7d862fa124eca
---

# Story 2.2: Automatic fallback through the chain

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want a failed model call to fall through to the next provider automatically,
so that a GLM 500 or timeout doesn't kill my turn — exactly the v1 pain we're fixing (AD-2, CAP-8).

## Acceptance Criteria

1. **Advance through the chain on failure:** Given an ordered provider chain, when the current provider returns an error, times out, or rate-limits, then the broker advances to the next provider and retries the call until one succeeds or the chain is exhausted, returning the **first successful** `Result`.
2. **Demonstrable end-to-end fallback:** Given a forced failure on the primary provider (injected GLM 500/timeout), when a turn runs, then the turn completes via the fallback provider — proven end-to-end through `_serve_connection`/`run_broker`, not just `handle_job` in isolation.
3. **Audit which provider answered:** Given a fallback occurred, when the turn completes, then which provider answered is recorded for audit — **with no credential material in the record**.
4. **Holds under sustained fault injection:** Given provider faults injected under sustained load (provider raising mid-call, transient errors flapping repeatedly across the chain), when turns run continuously, then fallback holds — turns keep completing via the chain, or the chain exhausts and a terminal failure `Result` is returned for the arbiter to degrade (Story 2.3) — with **no crash, no hang, and no memory growth** across the run.

> **Scope seam (binding):** 2.1 built the abstraction + adapters + config-driven chain assembly; the broker held the chain but ran only the **primary** (`chain[0]`). **2.2 is the single change that makes a failed call advance through the chain.** 2.2 returns a **terminal failure `Result`** when the chain is exhausted — it does **NOT** wire the arbiter's reflex-degrade on exhaustion; that is **Story 2.3**. `core/` is **untouched** (import-linter stays KEPT).

## Tasks / Subtasks

> **What already exists (reuse, do NOT reinvent):**
> - `broker/broker.py::handle_job(job, provider)` — per-provider call with the **single transient-retry** (`_MAX_ATTEMPTS=2`). **Keep it unchanged as the per-provider unit; the chain loop calls it once per provider.** Do NOT inline retry logic into a new chain function.
> - `broker/service.py::_serve_connection(reader, writer, provider)` and `run_broker(socket_path, chain)` — `run_broker` already accepts and validates the `list[LLMProvider]` chain; it currently calls `_serve_connection(..., chain[0])`. **This is the documented 2.2 seam** (`service.py:62`, `service.py:68`).
> - `broker/provider.py` — `LLMProvider` Protocol (`async complete(prompt) -> str`) + `TransientProviderError` / `PermanentProviderError`. Fallback keys on the `Result.ok` flag from `handle_job`, never on raw SDK exceptions (2.1 guaranteed uniform error mapping in every adapter).
> - `broker/chain.py::build_chain(env)` — constructs the ordered provider list from `PROVIDER_CHAIN`. The builders are where provider **identity** is known (preset name), so name-tagging belongs here.
> - `tests/test_broker_retry.py` — the fake-provider test pattern (`_OK`, `_TransientThen`, `_Permanent`, `_Unexpected`). **Extend this pattern for chain tests; do not invent a new fixture style.**
> - `tests/test_endurance_soak.py` — the NFR2 soak harness (resident-memory-flat assertion). **Mirror it for AC4's fault-injection soak; do not write a new memory-measurement harness.**

- [x] **Task 1: Chain-iterating fallback in `broker.py`** (AC: 1, 3)
  - [x] Add `async def handle_job_chain(job: Job, chain: list[LLMProvider]) -> Result` to `broker/broker.py`. Iterate providers **in order**; for each, call the **existing unchanged** `handle_job(job, provider)`. On `result.ok is True` → return it immediately (first success wins, AC1). On `result.ok is False` → log a fallback at WARNING (`provider %s failed, advancing`) and continue to the next provider.
  - [x] **Advance on ANY failed `Result`** — transient-exhausted **and** permanent (a 4xx on provider A, e.g. a bad key/model, should still try provider B). The loop keys only on `result.ok`; it never inspects exception types (those are already collapsed inside `handle_job`). *(Binding decision — see Dev Notes "Fallback triggers on any failure".)*
  - [x] When the chain is **exhausted** (every provider returned `ok=False`), return the **last** failure `Result` (preserve its `error`). This terminal failure is what Story 2.3's arbiter will later turn into a reflex-degrade — 2.2 stops at returning it.
  - [x] **Record which provider answered (AC3):** on success, log at INFO `turn answered by provider %r (after %d fallback(s))` using the provider's `name` (Task 2). The preset name (`"glm"`, `"ollama"`, …) carries **no credential** — never log `api_key`/`base_url`. This log line IS the audit record (see Dev Notes "Audit record"). Do **not** add a field to the `Result`/`Job` bus contract in this story unless the owner chose that option (saved question Q1).
  - [x] Unit-test in `tests/test_broker_chain_fallback.py` with fakes (extend the `test_broker_retry.py` style): primary fails (transient-exhausted) → secondary succeeds → returns secondary's text; primary permanent-fails → secondary succeeds; **all** providers fail → returns a failure `Result` (the last error); first provider succeeds → later providers are **never called** (assert `.calls == 0` on the tail); single-element chain still works (regression with today's behavior).

- [x] **Task 2: Provider identity for the audit record** (AC: 3)
  - [x] Add a `name: str` member to the `LLMProvider` Protocol in `broker/provider.py` (a plain attribute, e.g. `name: str`). This is the **broker-internal provider seam**, not the bus contract — no `contracts/` change.
  - [x] Give `AnthropicProvider` and `OpenAIProvider` a `name` constructor arg (store on `self.name`). Default it sensibly (e.g. `"anthropic"`/`"openai"`) so direct construction in existing tests doesn't break.
  - [x] In `broker/chain.py`, set each built provider's `name` to its **preset name** (`"glm"`, `"claude"`, `"ollama"`, `"openai"`, …) inside the builders / `_make_openai_compat`. The preset name is the audit identity.
  - [x] Confirm `name` carries no secret (preset key, not the credential). Add/extend a unit test asserting `build_chain({"PROVIDER_CHAIN": "glm,ollama", ...}).` providers expose the expected `.name` in order.

- [x] **Task 3: Wire the chain into the bus loop** (AC: 1, 2)
  - [x] In `broker/service.py`, change `_serve_connection` to take the **full chain** (`chain: list[LLMProvider]`) and call `await handle_job_chain(env.body, chain)` instead of `handle_job(env.body, provider)`. Update `run_broker` to pass `chain` (not `chain[0]`). Remove the now-stale "executes the primary / 2.2 changes this" inline comment and replace it with a one-line "iterates the chain on failure (Story 2.2)".
  - [x] Keep every other line of the read/write loop identical (per-frame resilience, `turn_id` echo, RESULT→CORE routing). The only behavioral change is primary-only → chain-iteration.
  - [x] Update callers that construct `_serve_connection` directly: `tests/test_broker_service_branches.py` drives `_serve_connection` with a fed `StreamReader` — update its signature to pass a one-element chain `[provider]` and keep assertions identical. (`run_broker` callers already pass a chain from 2.1 — no change.)

- [x] **Task 4: End-to-end fallback proof** (AC: 2)
  - [x] Add a test (in `tests/test_broker_chain_fallback.py` or alongside the existing `tests/test_broker_service*.py`) that drives `_serve_connection` (or `run_broker` over a real UDS pair, mirroring `tests/test_broker_bus.py`) with a **2-element chain**: a primary fake that raises a transient/`InternalServerError`-style failure every call, and a secondary fake that succeeds. Feed a `Job` envelope; assert the returned `Result` envelope is `ok=True`, carries the secondary's text, and echoes the original `turn_id`. This is AC2's "demonstrable end-to-end."

- [x] **Task 5: Sustained fault-injection soak** (AC: 4)
  - [x] Mirror `tests/test_endurance_soak.py` (the NFR2 flat-memory harness) into a fault-injection variant: a chain whose providers fail **intermittently/randomly** across a long run of turns (e.g. 200+), so fallback is exercised continuously — some turns succeed via primary, some via fallback, some exhaust the chain (terminal failure `Result`). Assert: **no exception escapes** the loop, **no hang** (each turn completes or fails within a bound), and **resident memory stays flat** (reuse the soak's measurement; do not write a new one). This corroborates AC4 under load.
  - [x] Keep it fast and deterministic-enough for CI (seeded failure pattern, not real network, no real sleeps — see Task 6 backoff note). Mark `slow` if it materially lengthens the suite, following the soak test's existing marking.

- [x] **Task 6: Fold the 1.4 resilience deferrals routed to 2.2** (AC: 1, 4)
  > Per the Epic 1 retro + Story 2.1 "deliberately does NOT do," these three 1.4 deferrals were explicitly routed to **Story 2.2**. They directly support AC4 (survive sustained faults without hang/crash). See saved question Q2 if scope needs splitting.
  - [x] **Backoff between retries/attempts:** add a small bounded backoff (e.g. a short `asyncio.sleep`) before a transient **retry** in `handle_job` and/or between provider **advances** in `handle_job_chain`, so the pet doesn't hammer a rate-limited/flapping endpoint and burn the budget instantly. Keep it tiny and **injectable** (a module-level constant or arg) so tests can monkeypatch sleep to `0` — **never a real wall-clock wait in the suite**. (Resolves 1.4 "No backoff between transient retries.")
  - [x] **`connect()` timeout:** wrap the `connect()` call in `run_broker` with `asyncio.wait_for(...)` so a hung hub doesn't block the broker indefinitely. On timeout, fail/retry cleanly (tie into reconnect below). (Resolves 1.4 "`connect()` has no timeout.")
  - [x] **Reconnect/supervisor loop:** wrap `run_broker`'s connect→serve in a bounded reconnect loop so a transient hub drop/restart does **not** kill the broker permanently — on a clean disconnect or connect-timeout it backs off and reconnects rather than returning. Preserve the existing clean-shutdown path (don't loop forever on a deliberate stop — honor cancellation). (Resolves 1.4 "`run_broker` has no reconnect logic.") Unit-test the reconnect with a fake `connect` that fails once then succeeds (monkeypatched sleep).

- [x] **Task 7: Dedup duplicate preset names in the chain** (AC: 1)
  - [x] In `build_chain` (`chain.py`), **dedup** consecutive/duplicate preset names (e.g. `PROVIDER_CHAIN="glm,glm"`) — a duplicate wastes a fallback slot on the same failing provider. De-duplicate preserving first-occurrence order, and `log.warning` the dropped duplicate (don't silently build it). (Resolves the 2.1 deferral "Duplicate preset names … silently builds duplicate providers.") Add a unit test: `"glm,ollama,glm"` → 2 providers `["glm","ollama"]`.

- [x] **Task 8: Verify guard + full suite (+ optional live smoke)** (AC: 1, 2, 3, 4)
  - [x] `uv run lint-imports` → both contracts KEPT (`core/` still LLM-free; 2.2 touches only `broker/` + `tests/`).
  - [x] `uv run pytest -q` → green (new chain/fallback/reconnect/dedup tests + soak). Default run hits **no network** (`addopts = "-m 'not live'"` from 2.1).
  - [x] **Optional live smoke (gated, owner's call):** extend the `live`-marked smoke with a 2-provider chain (e.g. a deliberately bad primary key + a working secondary like Ollama-LAN or Gemini) to watch a real fallback land. Skipped unless the relevant env vars are set — same pattern as 2.1's smokes.

## Dev Notes

### Architecture compliance (binding)

- **AD-2 — Broker owns the provider chain WITH retry/fallback:** "It owns the ordered **provider chain with retry/fallback** (default GLM; alternates Ollama-LAN/Gemini/OpenAI/OpenRouter)." 2.1 built the chain; **2.2 builds the fallback half.** All fallback logic lives in `broker/`; creds stay inside the adapters; nothing credential-shaped touches a `Job`/`Result`/`Envelope`. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **CAP-8 — LLM fallback on error = broker provider chain + arbiter degradation:** 2.1 laid the chain, **2.2 adds fallback-through-chain**, 2.3 wires whole-chain-exhaustion to the arbiter's reflex degrade. 2.2's terminal failure `Result` is the handoff point to 2.3. [Source: ARCHITECTURE-SPINE.md#CAP-8 (line 220)]
- **AD-9 — The arbiter degrades on chain exhaustion (NOT this story):** "on provider-chain exhaustion the arbiter **falls back to a reflex behavior** so the pet never freezes." That arbiter wiring is **Story 2.3**. 2.2 only returns the terminal failure `Result`; do not modify `core/` arbiter logic. (The 1.8 graceful "can't think right now" path already exists as the interim degrade.) [Source: ARCHITECTURE-SPINE.md#AD-9]
- **AD-1 — LLM-free core:** 2.2 changes only `broker/` + `tests/`. `core/` imports no provider lib; import-linter forbidden list already covers the SDKs. Stays mechanically KEPT. [Source: ARCHITECTURE-SPINE.md#AD-1, pyproject.toml [tool.importlinter]]
- **AD-4 / Consistency Conventions — failures are Results, never exceptions across the bus:** `handle_job` already collapses everything to a `Result`. `handle_job_chain` must preserve this — it returns a `Result` in every path (success, fallback-success, full-exhaustion). No exception may escape into `_serve_connection`. [Source: broker/broker.py, ARCHITECTURE-SPINE.md#AD-4]

### The single seam 2.2 changes

`broker/service.py:62` and `:68` carry 2.1's inline note: *"Story 2.2 changes `_serve_connection` to advance through the chain on failure (the single fallback seam)."* That is literally this story. Before: `handle_job(env.body, chain[0])`. After: `handle_job_chain(env.body, chain)`. Everything else in the loop is unchanged. [Source: shelldon/broker/service.py:57-70]

### Fallback triggers on any failure (binding decision)

`handle_job` already maps a provider's outcome to one of: success `Result`, transient-exhausted failure `Result`, permanent failure `Result`, or unexpected→failure `Result`. **The chain advances on every `ok=False`**, regardless of whether the underlying cause was transient or permanent. Rationale: the goal is a completed turn — if provider A returns a permanent 4xx (bad key, wrong model id, content-policy 400), provider B may still answer. Keying on `result.ok` (not exception type) also keeps the chain loop trivially testable with the existing fakes and avoids re-classifying errors a second time. *(If the owner wants permanent errors to short-circuit instead, see saved question Q3 — but the resilient default is advance-on-any-failure.)*

### Audit record (AC3) — log-based, no contract change

AC3 says *which provider answered is recorded for audit, no credentials in the record.* The minimal, surgical satisfaction is a **broker-side INFO log line** keyed on the provider's preset `name` (`"glm"`, `"ollama"`, …) — which is a config label, never a credential. A test asserts it via `caplog`. **Do not** add a `provider` field to the `Result` bus struct in this story: `core/` consumes no provider identity today, and the bus contract is versioned — a wire add is heavier and speculative here. (If the owner chooses the contract-field option in Q1, add it as an OPTIONAL field with a default — a non-breaking versioned add, the same pattern noted for `InboundMessage.chat_id` in `contracts/__init__.py:73` — and populate it in `handle_job_chain`.) [Source: shelldon/contracts/__init__.py:58-65]

### Folded 1.4 deferrals (routed to 2.2 by the Epic 1 retro)

Story 2.1's "What 2.1 deliberately does NOT do" states: *"No backoff between retries, no broker reconnect/supervisor, no `connect()` timeout → these deferred 1.4 items are folded into **Story 2.2** per the Epic 1 retro."* They are AC4-enabling (survive sustained faults without hang/crash), so they belong with fallback. Each has a concrete entry in `deferred-work.md` (1.4 review section):
- "No backoff between transient retries" → Task 6 backoff.
- "`connect()` has no timeout" → Task 6 timeout.
- "`run_broker` has no reconnect logic" → Task 6 reconnect/supervisor.
[Source: _bmad-output/implementation-artifacts/deferred-work.md (1.4 review); 2-1 story "What 2.1 deliberately does NOT do"]

### Reuse / preserve (from Stories 1.4 + 2.1)

- `handle_job(job, provider)` single transient-retry — **unchanged**; it's the per-provider unit `handle_job_chain` composes. [Source: broker/broker.py]
- Uniform error taxonomy: every adapter maps SDK errors to `TransientProviderError`/`PermanentProviderError`, **never raw SDK exceptions across `handle_job`** — and 2.1 redacted SDK error text to the **type name** so no credential leaks into `Result.error`. Fallback relies on this uniformity; don't reintroduce raw-exception leakage. [Source: 2-1 story "Error taxonomy"; broker/anthropic_provider.py, broker/openai_provider.py]
- `build_chain` fail-fast on unknown/missing-cred presets — keep; 2.2 adds dedup on top. [Source: broker/chain.py:93-112]
- The `live` marker + key-gated skip + `addopts = "-m 'not live'"` — extend, don't reinvent. A default `pytest` must never hit the network. [Source: 2-1 story; pyproject.toml]

### What 2.2 deliberately does NOT do

- **No arbiter reflex-degrade on chain exhaustion** → Story 2.3 (2.2 returns the terminal failure `Result`; the 1.8 "can't think right now" path is the interim degrade).
- **No `core/` changes** — fallback is wholly inside `broker/`; import-linter stays KEPT.
- **No `provider` field on the bus `Result`** unless the owner picks Q1's contract option — log-based audit by default.
- **No concurrent/parallel provider racing** — fallback is strictly **sequential** through the ordered chain (try A, then B, …). Parallel "first-to-answer" racing is out of scope (and would multiply spend). The "Sequential job processing in `run_broker`" 1.4 deferral (task-pool throughput) also remains deferred — not an AC here.

### Project Structure Notes

- Modified: `shelldon/broker/broker.py` (+`handle_job_chain`, optional backoff), `shelldon/broker/service.py` (`_serve_connection` takes the chain; `run_broker` reconnect+timeout), `shelldon/broker/provider.py` (+`name` on the Protocol), `shelldon/broker/anthropic_provider.py` + `openai_provider.py` (+`name` ctor arg), `shelldon/broker/chain.py` (name-tag providers + dedup).
- New tests: `tests/test_broker_chain_fallback.py` (unit + end-to-end fallback), a fault-injection soak (mirror `tests/test_endurance_soak.py`), reconnect/timeout/dedup unit tests.
- Modified tests: `tests/test_broker_service_branches.py` (`_serve_connection` now takes a chain). `core/` untouched. [Source: ARCHITECTURE-SPINE.md#Structural-Seed; 2-1 File List]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Unit-test the chain loop with monkeypatched fake providers (extend `test_broker_retry.py`'s `_OK`/`_TransientThen`/`_Permanent`). Monkeypatch any backoff sleep to 0 — **no real waits**. End-to-end fallback through `_serve_connection`/`run_broker` over a UDS pair (mirror `test_broker_bus.py`). Soak mirrors `test_endurance_soak.py`'s flat-memory assertion. Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: 1.4/2.1 tests; Epic 1 retro]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2 / Story 2.2 (this story); #Story 2.1 (chain assembly — done); #Story 2.3 (degrade — next)]
- [Source: ARCHITECTURE-SPINE.md#AD-2 (chain+retry/fallback), #AD-9 (arbiter degrades on exhaustion → 2.3), #AD-1 (LLM-free core), #AD-4 (failures are Results), #CAP-8 (line 220)]
- [Source: shelldon/broker/service.py:57-70 (the documented 2.2 seam), broker/broker.py (handle_job to compose), broker/chain.py (provider identity + dedup), broker/provider.py (LLMProvider + error types), contracts/__init__.py:58-65 (Result), :73 (optional-field non-breaking-add pattern)]
- [Source: _bmad-output/implementation-artifacts/2-1-provider-abstraction-and-an-ordered-chain.md ("What 2.1 deliberately does NOT do"; "Error taxonomy"; File List)]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md (1.4 review: backoff / connect-timeout / reconnect; 2.1 review: duplicate-preset dedup)]
- [Source: tests/test_broker_retry.py (fake-provider pattern), tests/test_endurance_soak.py (NFR2 flat-memory soak), tests/test_broker_bus.py (UDS end-to-end), tests/test_broker_service_branches.py (_serve_connection caller)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (1M context)

### Debug Log References

- `uv run pytest -q` → **136 passed, 2 skipped** (Linux-fork, macOS-gated), **3 deselected** (live, opt-in), 0 failed. (Up from 2.1's 122; +14 new fallback/reconnect/identity/dedup/soak tests.)
- `uv run lint-imports` → 2 contracts KEPT (`core` LLM-free; `transport` holds no model/tool creds). 2.2 touched only `broker/` + `tests/`.
- Fallback soak (`tests/test_broker_fallback_soak.py`, runs by default): turns=300 → primary=140, fallback=112, exhausted=28, heap_delta=416 B (bound 250 KB) — all three outcomes exercised, heap flat.

### Completion Notes List

- **Fallback (AC1, the single seam):** added `handle_job_chain(job, chain)` to `broker/broker.py` — iterates the ordered chain, calls the **unchanged** per-provider `handle_job` for each, returns the **first success**, advances on **ANY** `ok=False` (transient-exhausted AND permanent 4xx — owner-confirmed "advance on any failure"), and returns the **last failure Result** on exhaustion (the terminal failure the arbiter degrades on in Story 2.3). `service.py::_serve_connection` now takes the full `chain` and calls `handle_job_chain` instead of `handle_job(chain[0])` — the exact seam 2.1 documented.
- **Audit record (AC3) — log-based (owner pick Q1):** each provider carries a `name` (the chain preset label, e.g. `"glm"`/`"ollama"`); on success `handle_job_chain` logs `turn answered by provider 'X' (after N fallback(s))` at INFO. The name is a config label, never a credential — verified by `test_audit_record_carries_no_credential`. No bus-contract (`Result`) change.
- **Provider identity:** added `name: str` to the `LLMProvider` Protocol and a `name=` ctor arg to both adapters (defaults `"anthropic"`/`"openai"`); `chain.py` tags each built provider with its preset name.
- **Folded 1.4 resilience deferrals (owner pick Q2 — all three in 2.2):** (1) **backoff** before the in-provider transient retry in `handle_job` (`_RETRY_BACKOFF_S`, module-level/injectable; tests run it at 0 via an autouse conftest fixture); (2) **connect() timeout** — `run_broker` wraps `connect()` in `asyncio.wait_for(_CONNECT_TIMEOUT_S)`; (3) **reconnect/supervisor loop** — `run_broker(reconnect=True)` (default) backs off and reconnects on a dropped/refused/timed-out hub instead of dying; cancellation still exits cleanly. Resolves the three 1.4 deferred-work items.
- **Dedup (2.1 deferral):** `build_chain` now drops duplicate preset names (first-occurrence wins) and warns, so `PROVIDER_CHAIN="glm,glm"` doesn't waste a fallback slot.
- **AD-1 held / core untouched:** all changes are in `broker/` + `tests/`; import-linter KEPT.
- **Live smoke:** the optional gated multi-provider live fallback smoke was **not** run (network/keys, owner's call) — deferred to manual `-m live`.
- **Test fakes updated for the `name` contract:** `_OK`/`OkProvider`/`GatedProvider`/`AlwaysTransientProvider` across `test_broker_bus.py`, `test_broker_service.py`, `test_end_to_end_turn.py` gained a `name` attribute, and `test_broker_service*.py` now pass a one-element chain to `_serve_connection` (signature changed from `provider` → `chain`).

### File List

- `shelldon/broker/broker.py` (modified — +`handle_job_chain`; +`_RETRY_BACKOFF_S` backoff in `handle_job`)
- `shelldon/broker/service.py` (modified — `_serve_connection` takes the chain + calls `handle_job_chain`; `run_broker` connect-timeout + reconnect loop)
- `shelldon/broker/provider.py` (modified — +`name: str` on the `LLMProvider` Protocol)
- `shelldon/broker/anthropic_provider.py` (modified — +`name` ctor arg)
- `shelldon/broker/openai_provider.py` (modified — +`name` ctor arg)
- `shelldon/broker/chain.py` (modified — name-tag each preset provider; dedup duplicate presets + warn)
- `tests/conftest.py` (modified — autouse fixture zeroes retry + reconnect backoffs)
- `tests/test_broker_chain_fallback.py` (new — fallback unit tests, audit-log, backoff, end-to-end-over-bus)
- `tests/test_broker_reconnect.py` (new — reconnect-after-failure + connect-timeout)
- `tests/test_broker_fallback_soak.py` (new — AC4 sustained fault-injection soak; flat heap)
- `tests/test_chain.py` (modified — provider `.name` by preset + duplicate-preset dedup)
- `tests/test_broker_service.py` (modified — `_OK` +`name`; `_serve_connection` one-element chain)
- `tests/test_broker_service_branches.py` (modified — `_serve_connection` one-element chain)
- `tests/test_broker_bus.py` (modified — `_OK` +`name`)
- `tests/test_end_to_end_turn.py` (modified — `OkProvider`/`GatedProvider`/`AlwaysTransientProvider` +`name`)

## Change Log

- 2026-06-17 — Story 2.2 implemented: automatic fallback through the provider chain. `handle_job_chain` advances on any failure, returns first success, records the answering provider (log-based audit, no creds), and returns the terminal failure on exhaustion (→ arbiter degrade, Story 2.3). Wired into `_serve_connection`/`run_broker`. Folded the three 1.4 resilience deferrals (retry backoff, connect timeout, reconnect loop) and the 2.1 duplicate-preset dedup. `core/` untouched; import-linter KEPT. Suite 136 passed / 2 skipped / 3 deselected. Status → review.
- 2026-06-17 — Addressed code review: 6 findings resolved (writer `wait_closed` on reconnect, `_RecordingProvider` name, blank-chain-entry warning, dead-`continue` removal + backoff falsy-flag restructure), 1 pushed back with evidence (#2 — permanent errors are already logged at WARNING), 2 deferred to retro per the reviewer's "design notes" categorization (#7 empty-chain guard, #8 `name` Protocol coupling). Suite 137 passed / 2 skipped / 3 deselected; contracts KEPT.

## Code Review

*Reviewed 2026-06-17 — 8-angle high-effort scan, 1-vote verify, 17 candidates → 8 confirmed/plausible findings.*

### Fix before merge

**1. `writer.close()` without `await writer.wait_closed()`** — `service.py:95`
`StreamWriter.close()` initiates shutdown but does not block until the transport is released. On a rapid hub restart, the reconnect loop can call `connect()` on the same socket path while the previous transport is still draining — causing dropped final write frames or `Event loop is closed` errors. Fix: `writer.close(); await writer.wait_closed()` (guard `wait_closed` in `try/except OSError` in case the peer already closed first).

**2. Permanent errors advance the chain with no audit trail** — `broker.py:69`
`handle_job_chain` advances on `any not result.ok`, including `PermanentProviderError` (bad API key, 4xx). A bad key exhausts the entire chain before returning failure, with no indication the error was unrecoverable. The Dev Notes ("Fallback triggers on any failure") document this as a deliberate design choice — the fix is not to change the advance-on-any logic, but to log the permanent error at WARNING before discarding it, so the audit trail doesn't silently eat the root cause.

**3. `_RecordingProvider` missing `name: str`** — `tests/test_broker_service_branches.py:37`
`LLMProvider` Protocol now requires `name: str`. `_RecordingProvider` has none. Existing tests pass only because the non-Job and EOF branches never reach `handle_job_chain`. Route a Job through this stub and `provider.name` raises `AttributeError`. One-line fix: add `name = "test"` to the class.

### Fix soon

**4. Blank `PROVIDER_CHAIN` entries silently dropped** — `chain.py:107`
`[n.strip().lower() for n in … if n.strip()]` discards blank entries (e.g. `"glm,,openai"`) with no log output. The duplicate-preset path added in the same story explicitly warns. Inconsistent behavior introduced together — add a `log.warning` for blank entries parallel to the duplicate warning.

**5. `continue` in `TransientProviderError` handler is dead code** — `broker.py:48`
The `continue` is the last statement in the `except TransientProviderError` block, which is the last clause in the `for` loop body. The loop advances regardless — `continue` is a no-op that misleads readers into thinking there is an alternative non-continuing path. Delete it.

**6. `and _RETRY_BACKOFF_S` is an implicit test/prod contract** — `broker.py:46`
`if attempt < _MAX_ATTEMPTS and _RETRY_BACKOFF_S:` uses `0` as a falsy flag to skip sleep entirely. The conftest fixture sets `_RETRY_BACKOFF_S = 0` for this reason. But `asyncio.sleep(0)` is a valid no-op that yields the loop — the two meanings (skip-sleep vs. zero-duration-sleep) are conflated. Cleaner: always enter the `if` block, put `if _RETRY_BACKOFF_S: await asyncio.sleep(…)` inside it, so `0` means "no delay" without the reader having to know the falsy convention.

### Design notes for retrospective

**7. Empty chain guard only in `run_broker`** — `service.py:34`
`_serve_connection` accepts `list[LLMProvider]` with no guard. If called directly with `[]`, `handle_job_chain` silently returns `Result(ok=False, error='empty provider chain')` with no log at the service layer. Currently no caller bypasses `run_broker`, so this is hypothetical — but worth an `assert chain` at `_serve_connection` entry to make the invariant explicit.

**8. `name: str` on `LLMProvider` Protocol is metadata coupling** — `provider.py:15`
`name` is used only for audit logging in `handle_job_chain` — it is not a behavioral capability. The Protocol now rejects any structurally valid provider that lacks a display label. The story explicitly chose this approach (Task 2 / Dev Notes "Audit record"), so this is a known tradeoff rather than a bug. Worth revisiting if a third-party or test provider is added that doesn't naturally carry a name — a `(name, provider)` NamedTuple at the chain layer would be a cleaner seam.

### Review Resolution (2026-06-17)

Suite after fixes: **137 passed / 2 skipped / 3 deselected**; import-linter 2 contracts KEPT.

- **[x] #1 `writer.close()` without `wait_closed()`** — FIXED (`service.py`): the reconnect loop now `await writer.wait_closed()` (guarded `except OSError`) so a fast reconnect can't re-open the socket mid-drain.
- **[x] #2 Permanent errors advance with no audit** — NO CHANGE (premise inaccurate). `handle_job_chain` already logs **every** failure — permanent included — at WARNING with its `error` text (`broker.py:72`: `provider %r failed, advancing: %s`). A permanent 4xx surfaces as `"provider error: status 401"` and a transient-exhausted as `"transient provider error: …"`, so the root cause is in the audit trail, not silently eaten. If you want the *permanence* called out distinctly (vs. inferring from the error string), say so and I'll tag it — but nothing is currently lost.
- **[x] #3 `_RecordingProvider` missing `name`** — FIXED (`test_broker_service_branches.py`): added `name = "test"`.
- **[x] #4 Blank `PROVIDER_CHAIN` entries silently dropped** — FIXED (`chain.py`): blanks are now warned (parallel to the duplicate warning); test `test_blank_chain_entries_dropped_with_warning`.
- **[x] #5 Dead `continue` in transient handler** — FIXED (`broker.py`): removed as part of the #6 restructure.
- **[x] #6 `and _RETRY_BACKOFF_S` falsy-flag conflation** — FIXED (`broker.py`): split into `if attempt < _MAX_ATTEMPTS:` then an inner `if _RETRY_BACKOFF_S:` — `0` now plainly means "no delay" with no falsy convention to decode.
- **[~] #7 Empty-chain guard only in `run_broker`** — DEFERRED to retro (reviewer-categorized "design note"; hypothetical — no caller bypasses `run_broker`). Logged in `deferred-work.md`.
- **[~] #8 `name` Protocol metadata coupling** — DEFERRED to retro (reviewer-categorized known tradeoff). Logged in `deferred-work.md`.
