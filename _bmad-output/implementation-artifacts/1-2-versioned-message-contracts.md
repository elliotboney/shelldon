---
baseline_commit: aa970b33a689119b9d3b63171dc9b440794acbf2
---

# Story 1.2: Versioned message contracts

Status: done

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As a developer building shelldon,
I want the `Envelope`/`Job`/`Result` types defined once as versioned msgspec structs with a closed header,
so that every process shares one wire vocabulary (AD-4) and contract drift is caught by tests from M0 (AD-10).

## Acceptance Criteria

1. **Closed header + versioned structs:** the `contracts/` package defines `Envelope` carrying a closed header — `id`, `v`, `kind`, `src`, `dst`, `turn_id` — and `Job`/`Result` as versioned msgspec structs. `kind`, `src`, `dst` are **closed types** (enums), not free strings.
2. **M0 round-trip test:** a test encodes and decodes **every** envelope type without loss (decoded value equals the original). This is an AD-10 M0 required test.
3. **No creds on the bus:** a `Job` envelope contains **no credential fields** — verified by a test, not just by inspection (AD-2 / NFR9).

## Tasks / Subtasks

- [x] **Task 1: Add the pinned msgspec runtime dependency** (AC: 1)
  - [x] Add `msgspec==0.21.1` to `[project].dependencies` in `pyproject.toml` (runtime dep — contracts are imported by core; **not** the dev group)
  - [x] `uv lock` then `uv sync --locked`; confirm `uv.lock` updated and committed
  - [x] Sanity: `uv run python -c "import msgspec; print(msgspec.__version__)"` → `0.21.1`
- [x] **Task 2: Define the closed header vocabulary** (AC: 1)
  - [x] In `shelldon/contracts/`, define `Actor` (StrEnum): `CORE`, `BROKER`, `WORKER`, `CHAT_TRANSPORT`, `DISPLAY`, `PLUGIN_HOST` — the addressable processes (`src`/`dst` domain)
  - [x] Define `MsgKind` (StrEnum) with the kinds Story 1.2 actually constructs: `JOB`, `RESULT`. (Later stories extend this closed enum as they introduce their own envelope kinds — message/snapshot/event — do **not** add them speculatively now.)
  - [x] Define module constant `SCHEMA_VERSION: int = 1` (the value of `Envelope.v`)
- [x] **Task 3: Define `Job` and `Result` payload structs** (AC: 1, 3)
  - [x] `Job` = `msgspec.Struct` (frozen, `tag=True` or tagged via `kind`) — fields are the request payload **only**; **NO** credential fields of any kind (no `token`/`key`/`secret`/`password`/`api_key`/`authorization`/`credential`). Broker injects creds internally (AD-2). Keep fields minimal — this is the contract shell; broker/worker stories (1.4/1.5) flesh out payload semantics. A minimal Job (e.g. `prompt: str` or an opaque `payload` field) is sufficient to satisfy round-trip now.
  - [x] `Result` = `msgspec.Struct` (frozen, tagged) — carries the outcome, including an **error variant** (errors surface as a `Result`, never an exception across the bus — Consistency Conventions). Minimal shape now; worker/broker stories extend it.
  - [x] Both versioned: rely on the `Envelope.v` schema version (single source of truth); do not duplicate a per-struct version field.
- [x] **Task 4: Define `Envelope` with the closed header** (AC: 1)
  - [x] `Envelope` = `msgspec.Struct` with exactly the closed header `id: str`, `v: int = SCHEMA_VERSION`, `kind: MsgKind`, `src: Actor`, `dst: Actor | None`, `turn_id: str | None = None`, plus the typed body
  - [x] Body: `body: Job | Result` as a **msgspec tagged union** so the hub (Story 1.3) can decode polymorphically by tag. `dst = None` is allowed (reserved for the broadcast/subscription mode declared by AD-11 — used later; point-to-point is the only mode 1.2 needs)
  - [x] `turn_id` is `str | None` — not every envelope belongs to a turn; the field exists now so AD-12 turn-fencing has its slot from M0
- [x] **Task 5: Encoder/decoder helpers** (AC: 2)
  - [x] Provide thin `encode(envelope) -> bytes` / `decode(bytes) -> Envelope` helpers using **`msgspec.msgpack`** (the spine's wire format: "4-byte big-endian length + msgspec bytes" — msgpack, not JSON). Construct a module-level `msgspec.msgpack.Decoder(Envelope)` for typed decoding of the tagged union.
  - [x] Do **NOT** implement the 4-byte length-prefix framing or any UDS/socket code here — that is Story 1.3. Story 1.2 stops at typed encode/decode of a single `Envelope`.
- [x] **Task 6: M0 round-trip test (AD-10)** (AC: 2)
  - [x] In `tests/`, add `test_contracts_roundtrip.py`: build one `Envelope` for **each** body type (`Job`, `Result`), `encode` then `decode`, assert `decoded == original` (msgspec structs compare by value). Assert the decoded `body` is the correct concrete type.
  - [x] Cover the header edges: a `turn_id=None` case and a populated `turn_id` case; a `dst=None` case.
- [x] **Task 7: No-creds structural test** (AC: 3)
  - [x] In `tests/`, add a test that introspects `Job`'s fields (`msgspec.structs.fields(Job)` / annotations) and asserts no field name matches the forbidden credential set (`token`, `key`, `secret`, `password`, `api_key`, `authorization`, `credential` — case-insensitive substring match). This guards the invariant structurally so a future careless field addition fails CI.
- [x] **Task 8: Verify the guard + full suite** (AC: 1, 2, 3)
  - [x] `uv run lint-imports` → contracts must remain LLM-free-core-compatible (contracts is imported by core; it must not pull provider SDKs). Confirm import-linter still passes.
  - [x] `uv run pytest -q` → all green (1.1's tests + the new contract tests)

### Review Findings

- [x] [Review][Patch] Closed header accepts unknown wire fields [shelldon/contracts/__init__.py:55] — `forbid_unknown_fields=True` on `Envelope`/`Job`/`Result`; an unknown field now raises `ValidationError` at decode instead of being silently dropped (AD-11 closedness).
- [x] [Review][Patch] Envelope kind can drift from tagged body [shelldon/contracts/__init__.py:65] — `Envelope.__post_init__` rejects a `kind` that disagrees with the body type (`_KIND_FOR_BODY` map); enforced on both construction and decode, so the routed `kind` can't contradict the body tag.
- [x] [Review][Patch] Unsupported schema versions decode as valid envelopes [shelldon/contracts/__init__.py:81] — `decode()` rejects any `v != SCHEMA_VERSION` with `ValidationError`, making AD-10 versioning enforced rather than decorative.

## Dev Notes

### Architecture compliance (binding)

- **AD-4 — Envelope bus is the only seam:** all cross-process comms are versioned msgspec `Envelope`/`Job`/`Result`. Story 1.2 defines **the types**; the UDS transport + hub routing is Story 1.3. [Source: ARCHITECTURE-SPINE.md#AD-4]
- **AD-10 — Versioned typed contracts + tests from M0:** `Envelope`/`Job`/`Result` are versioned msgspec structs in `contracts/`; **contract round-trip (encode/decode every envelope) is a required M0 test.** This story delivers that test. [Source: ARCHITECTURE-SPINE.md#AD-10]
- **AD-11 — Closed envelope header + two routing modes:** every `Envelope` has the closed header `id`, `v`, `kind`, `src`, `dst`, `turn_id`. The two routing modes (point-to-point `kind`→dst table; broadcast/subscription) are **declared in `contracts/`** but **consumed in later stories** — 1.2 defines the closed `MsgKind`/`Actor` enums the modes will key on; the actual `kind`→destination routing table is **Story 1.3** (do not build it here). [Source: ARCHITECTURE-SPINE.md#AD-11]
- **AD-2 / NFR9 — No creds on the bus:** `Job` envelopes carry **no** credentials; the broker injects them internally. Enforced here structurally by Task 7's test. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-12 — Turn identity:** `turn_id` lives in the closed header so core can fence on it and discard late/zombie Results later. Field present from M0; fencing logic is core's job (Stories 1.3/1.5/1.8). [Source: ARCHITECTURE-SPINE.md#AD-12]
- **Consistency Conventions — Data & formats:** msgspec structs; UDS frames are 4-byte big-endian length + msgspec bytes (msgpack); closed header `id/v/kind/src/dst/turn_id`; timestamps ISO-8601 UTC; **no credentials ever on the bus**; errors surface as a `Result` error variant, never an exception across the bus. [Source: ARCHITECTURE-SPINE.md#Consistency Conventions]

### Scope boundary (prevent scope creep)

**IN scope (1.2):** the three struct types, the closed `Actor`/`MsgKind` enums, `SCHEMA_VERSION`, typed `msgspec.msgpack` encode/decode of a single Envelope, the M0 round-trip test, the no-creds test, the msgspec pin.

**OUT of scope (later stories, do NOT build):**
- 4-byte length-prefix framing + UDS sockets + the hub → **Story 1.3**
- The static `kind`→destination routing table → **Story 1.3**
- Job/Result payload *semantics* (what a model Job actually contains, retry/error detail) → broker **1.4** / worker **1.5**
- Inbound/outbound transport-agnostic **message** contract → **Story 1.6**
- Display **state-snapshot** envelope (`seq`, face region) → **Story 1.7**
- Broadcast **event** kinds (`message-answered`, `tool-used`, `day-alive`) + subscription registry → **Story 7.2**

Keep `Job`/`Result` minimal — a contract shell that round-trips. Resist fleshing out fields for stories that own them. Per Elliot's standing rule: minimum code that satisfies the ACs, nothing speculative.

### Recommended design (dev's call on exact msgspec mechanics)

```python
# shelldon/contracts/__init__.py  (or split into _enums.py / _envelope.py — dev's call)
from enum import StrEnum
import msgspec

SCHEMA_VERSION = 1

class Actor(StrEnum):
    CORE = "core"; BROKER = "broker"; WORKER = "worker"
    CHAT_TRANSPORT = "chat-transport"; DISPLAY = "display"; PLUGIN_HOST = "plugin-host"

class MsgKind(StrEnum):
    JOB = "job"; RESULT = "result"

class Job(msgspec.Struct, frozen=True, tag="job"):
    payload: str            # minimal shell — broker/worker stories define real fields. NO creds.

class Result(msgspec.Struct, frozen=True, tag="result"):
    ok: bool
    payload: str = ""
    error: str | None = None   # error variant — errors travel as Result, not exceptions (AD convention)

class Envelope(msgspec.Struct, frozen=True):
    id: str
    kind: MsgKind
    src: Actor
    dst: Actor | None
    body: Job | Result         # tagged union → polymorphic decode by `tag`
    v: int = SCHEMA_VERSION
    turn_id: str | None = None

_decoder = msgspec.msgpack.Decoder(Envelope)
def encode(env: Envelope) -> bytes: return msgspec.msgpack.encode(env)
def decode(raw: bytes) -> Envelope: return _decoder.decode(raw)
```

This is a recommendation, not a mandate. If a cleaner msgspec idiom (e.g. struct inheritance, a `Raw` body) serves the ACs better, use it — but keep: closed header fields exactly as named, closed enums for `kind`/`src`/`dst`, tagged-union body so 1.3's hub can decode by kind, and `msgspec.msgpack` as the wire codec.

### Library notes (msgspec 0.21.1 — verified latest on PyPI)

- `msgspec.Struct` with `frozen=True` gives value `__eq__` (round-trip equality "for free") and hashability; `tag=...`/`tag=True` enables tagged unions for polymorphic decode. `gc=False` is an option for the hot path but **not needed in 1.2** — don't add it speculatively.
- Use `msgspec.msgpack.Decoder(Envelope)` (reusable, typed) — it resolves the `Job | Result` tagged union automatically. `msgspec.msgpack.encode(...)` for the encode side.
- `msgspec.structs.fields(Job)` returns the field metadata for the Task 7 introspection test.
- msgspec is a compiled C extension with zero required runtime deps — adding it won't pull provider SDKs, so AD-1's import-linter stays green. It is a **runtime** dependency (contracts is imported by `core`), so it goes in `[project].dependencies`, not the dev group.

### Project Structure Notes

- All new types live under `shelldon/contracts/` (the spine's shared-types package). Story 1.1 left `shelldon/contracts/__init__.py` as a docstring-only stub explicitly marked "populated in Story 1.2" — this is that population. Splitting into submodules (`_enums.py`, `_envelope.py`) is fine; keep public names importable from `shelldon.contracts`. [Source: ARCHITECTURE-SPINE.md#Structural Seed]
- Tests go in `tests/` mirroring package layout (`test_contracts_roundtrip.py`, and the no-creds test — combine or split, dev's call). [Source: 1.1 Dev Notes — "keep `tests/` mirroring the package layout"]
- Runtime data dirs (`~/.shelldon/`) are NOT scaffolded — irrelevant to this story.

### Previous story intelligence (Story 1.1)

- **Packaging is `uv` + `hatchling`**, project installed editable. Dependency commands: `uv lock` / `uv sync --locked` / `uv run <cmd>`. CI runs `uv sync --locked` then `lint-imports` then `pytest` and fails on any. [Source: 1.1 File List, `.github/workflows/ci.yml`]
- **Pins are enforced:** Story 1.1's review hardened the project to pin versions and run CI against the committed lockfile. Follow suit — pin `msgspec==0.21.1` (exact, matching the spine), and commit the re-locked `uv.lock`. A loose pin will (correctly) read as a regression. [Source: 1.1 Change Log — review fixes]
- **import-linter forbidden list already exists** in `pyproject.toml` (`[tool.importlinter]`, contract "core is LLM-free (AD-1)"). Don't touch it; just confirm it still passes after adding msgspec.
- **Python pinned `>=3.13,<3.14`** (`.python-version` = 3.13). msgspec 0.21.1 ships 3.13 wheels — no build-from-source concern.
- No data dirs, no provider SDKs (those are Story 1.4). Deps stay minimal: this story adds exactly one runtime dep (`msgspec`).

### Testing standards

- `pytest`, mirroring package layout. The round-trip test **is** the AD-10 M0 deliverable — make it cover every envelope body type and the header edge cases (`turn_id` present/absent, `dst=None`), not just one happy path.
- The no-creds test is a structural invariant guard (like 1.1's LLM-free-core guard) — it must fail if someone later adds a credential-named field to `Job`.
- Run `uv run lint-imports` and `uv run pytest -q`; both must be green before marking tasks done.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 1 / Story 1.2]
- [Source: _bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md#AD-4, #AD-10, #AD-11, #AD-12, #AD-2, #Consistency Conventions, #Structural Seed]
- [Source: _bmad-output/implementation-artifacts/1-1-greenfield-scaffold-with-an-enforced-llm-free-core-and-m0-tests.md (packaging, pin discipline, import-linter, test layout)]
- msgspec 0.21.1 — Structs, tagged unions, msgpack codec (https://jcristharif.com/msgspec/) — verified latest on PyPI 2026-06-16

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (dev-story)

### Debug Log References

- `uv lock` + `uv sync --locked` → `+ msgspec==0.21.1`; `uv run python -c "import msgspec; print(msgspec.__version__)"` → `0.21.1`
- `uv run pytest tests/test_contracts_roundtrip.py -q` → 6 passed
- `uv run lint-imports` → "core is LLM-free (AD-1) KEPT. Contracts: 1 kept, 0 broken." (msgspec adds no provider-SDK edge into core)
- `uv run pytest -q` → 15 passed (9 from Story 1.1 + 6 new contract tests; no regressions)

### Completion Notes List

- ✅ Resolved review finding [Patch]: closed header accepts unknown wire fields — added `forbid_unknown_fields=True` to `Envelope`/`Job`/`Result`.
- ✅ Resolved review finding [Patch]: Envelope kind can drift from tagged body — added `__post_init__` kind↔body agreement check.
- ✅ Resolved review finding [Patch]: unsupported schema versions decode as valid — added `v == SCHEMA_VERSION` gate in `decode()`.
- Review fixes verified: 4 new hardening tests (unknown-field reject, kind-drift reject, consistent-kind accept, bad-version reject); full suite 19 passed, import-linter KEPT.
- All 3 ACs satisfied. Defined `Envelope` (closed header `id/v/kind/src/dst/turn_id` + typed body), `Job`, and `Result` as versioned `msgspec.Struct`s in `shelldon/contracts/`, with closed `Actor`/`MsgKind` StrEnums and `SCHEMA_VERSION = 1`.
- **AC2 (M0 round-trip, AD-10):** `test_contracts_roundtrip.py` round-trips an Envelope for every body type via `msgspec.msgpack`, asserting value-equality and correct concrete body type; covers header edges (`turn_id` present/absent, `dst=None`) and the closed-header field set.
- **AC3 (no creds, AD-2/NFR9):** structural test introspects `Job` fields and fails on any credential-shaped name — guards the invariant in CI against future field additions.
- **Design:** `body: Job | Result` is a **msgspec tagged union** (`tag="job"`/`tag="result"`), so Story 1.3's hub can decode polymorphically by tag. Structs are `frozen=True` → value `__eq__` makes round-trip equality exact and the types hashable.
- **Scope held:** no UDS/length-prefix framing, no kind→dst routing table, no message/snapshot/event kinds — those belong to Stories 1.3 / 1.6 / 1.7 / 7.2 per the spec boundary. `Job`/`Result` are minimal contract shells; broker/worker stories flesh out their payloads.
- **Pin discipline (per 1.1 review):** `msgspec==0.21.1` (exact, matching the spine) added to `[project].dependencies` (runtime, not dev group); `uv.lock` re-locked and updated. msgspec ships cpython-3.13 wheels — no source build.

### File List

- `shelldon/contracts/__init__.py` (modified — replaced the stub with the Envelope/Job/Result contracts, enums, codec helpers)
- `tests/test_contracts_roundtrip.py` (new — M0 round-trip + closed-header + no-creds tests)
- `pyproject.toml` (modified — added `msgspec==0.21.1` runtime dependency)
- `uv.lock` (modified — re-locked with msgspec)

### Change Log

- 2026-06-16: Implemented Story 1.2 — versioned msgspec `Envelope`/`Job`/`Result` contracts with a closed header (AD-11), tagged-union body, and `msgspec.msgpack` codec in `contracts/`; M0 round-trip test (AD-10) + no-creds-on-the-bus guard (AD-2). msgspec pinned `==0.21.1`. 15 tests pass, import-linter KEPT. Status → review.
- 2026-06-16: Addressed code review — 3 [Patch] findings resolved: closed the header against unknown fields (`forbid_unknown_fields`), enforced `kind`↔body agreement (`__post_init__`), and rejected unsupported schema versions at decode. +4 hardening tests; 19 tests pass, import-linter KEPT. Status → review (re-review).
