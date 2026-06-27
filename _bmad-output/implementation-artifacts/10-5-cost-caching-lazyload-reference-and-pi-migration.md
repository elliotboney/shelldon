---
baseline_commit: 683e2a93273aa606af2147b3dea3e40e55293720
---

# Story 10.5: Cost (prompt caching + lazy-load), reference files, and Pi migration

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the always-injected persona to be cheap to re-send every turn, the heavy reference docs loaded only when relevant, and the live Pi to pick up the new files safely,
so that running shelldon on a metered GLM budget and a 416MB Pi stays affordable — and Epic 10 lands non-destructively on the already-deployed device.

## Acceptance Criteria

**AC1 — The persona prefix is byte-stable (the free cache lever, both surfaces)**
**Given** the persona is a stable prompt prefix re-sent every turn (stateless LLM + ephemeral fork worker — no model-side persistence is possible, AD-3)
**When** the prompt is assembled twice for the same memory root
**Then** the persona-prefix bytes (`BOT_INSTRUCTIONS`→`DIRECTIVE`→`IDENTITY`→`SOUL`→`USER`→`about`) are IDENTICAL across assemblies — no `now()`/UUID/unsorted-dict/hostname interpolated anywhere inside the prefix
**And** all volatile content (knowledge surface, summary, recent window, recall, owner message) stays strictly AFTER the prefix (the existing AD-6 assembly order) — so the OpenAI-surface providers (`openai`/`gemini`/`groq`/… via `openai_provider.py`) cache the prefix automatically with no request field
**And** a guard test asserts prefix byte-stability so a future change that interpolates a per-request value into the persona is caught (the silent-cache-invalidator from design §6)

**AC2 — Anthropic `cache_control` breakpoint (timeboxed spike, defer-allowed, no silent cap)**
**Given** the Anthropic-shape adapter (`anthropic_provider.py`) serves both `claude` and `glm` (z.ai `/api/anthropic`)
**When** a turn is sent
**Then** ONE `cache_control: {type: "ephemeral"}` breakpoint is placed on the stable persona prefix block, AND the provider's cache signal (`usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens`) is LOGGED per turn (no usage logging exists today — this story adds it)
**And** native-Claude honoring of the breakpoint is proven against a FAKED SDK response carrying `usage.cache_read_input_tokens` (no live LLM in CI), exercised as a unit test
**And** the z.ai/GLM `cache_control` passthrough is UNVERIFIABLE in CI → the per-provider cache signal is logged so the owner's live check (out-of-CI, like the 8.0 live smokes) reveals it; the result is captured in a short findings note
**And** DEFER-ALLOWED: if structuring the request to carry a mid-prompt breakpoint exceeds the timebox (today both `complete(prompt:str)` and `complete_with_tools(messages,…)` embed the persona INSIDE a single content string — there is no breakpoint site without a worker→broker contract change), the explicit breakpoint is deferred WITH a logged findings note, the byte-stable prefix (AC1) still gives free OpenAI-surface + native-Claude-auto caching, and GLM falls back to lazy-load + the Story 5.2 budget — never a silent token cap

**AC3 — Heavy reference docs are lazy-loaded by keyword (v1's `needs_extra_context`)**
**Given** v1's reference docs `TOOLS.md` (what tools the bot has) and `ARCHITECTURE.md` (its hardware/internals)
**When** they are ported as seed files into `shelldon/persona/` and seeded copy-if-absent like the other prompt templates
**Then** they inject into the prompt ONLY when the owner message matches their trigger keywords (e.g. tools/capability/command → `TOOLS`; hardware/internals/architecture/"how do you work" → `ARCHITECTURE`), costing tokens only when relevant — a non-matching message OMITS them entirely
**And** they inject AFTER the stable persona prefix (with the volatile layers) so they never break the cache prefix; each is char-budgeted; `VAULT.md` is NOT ported
**And** `read_tools()`/`read_architecture()` accessors mirror `read_heartbeat` (fail-soft `OSError/UnicodeDecodeError → None`); no `rewrite_*` op targets them (owner-hand-editable prompt policy, like HEARTBEAT/DREAM)

**AC4 — Pi migration: persona ships in the wheel + copy-if-absent is non-destructive**
**Given** `deploy/setup-pi.sh` git-clones the repo and `uv sync`s, and the wheel is `packages = ["shelldon"]` (hatchling)
**When** the package is built
**Then** the built wheel CONTAINS every `shelldon/persona/*.md` seed (`BOT_INSTRUCTIONS`/`SOUL`/`IDENTITY`/`USER`/`HEARTBEAT`/`DREAM`/`BOOTSTRAP`/`TOOLS`/`ARCHITECTURE`) — verified by a test that inspects the build artifact (the same failure class as a `.md` never reaching prod: seeding then fail-soft-skips it and the file is silently absent)
**And** a populated memory root (existing `about.md`/`facts/`/an owner-written `DIRECTIVE.md`) + a fresh `CuratedMemory` construction adds ONLY the absent seed files and leaves every existing file byte-for-byte untouched (copy-if-absent never shadows an owner edit)

**AC5 — Invariants hold**
**Given** the change set
**When** the suite + import-linter run
**Then** core stays LLM-free (AD-1 — caching lives in the broker egress boundary AD-2; lazy-load keyword logic + accessors are pure reads in the worker/core-memory, no model code), the worker stays read-only to memory (AD-6 — it READS the reference files, never writes), single-writer holds (AD-5 — the reference docs have no write path), and the fork accumulates no new resident state (AD-3 — reference files read fresh per fork, the persona prefix is rebuilt per turn by design)

## Tasks / Subtasks

- [x] **Task 1 — Byte-stable persona prefix: audit + guard test** (AC: 1)
  - [x] AUDIT `assemble_prompt` (`worker/prompt.py`) and every accessor feeding the persona prefix (`read_instructions`/`read_directive`/`read_identity`/`read_soul`/`read_user`/`read_about`) for ANY per-request byte variation: `datetime.now()`, `uuid`, unsorted `dict`/`set` iteration, hostnames, PIDs. Today's assembly composes from static file text — CONFIRM and document there is none (this is the verification AC, not a refactor). If any is found, move it OUT of the prefix into the volatile suffix.
  - [x] Add a guard test in `tests/test_prompt_assembly.py`: assemble the same seeded root twice → the prefix substring (system through `# About you`, i.e. everything before the first volatile section) is byte-identical; and a regex assert that the persona sections contain no ISO-timestamp / UUID pattern. This catches the silent-cache-invalidator regression (design §6).
  - [x] Confirm (test or assertion) the existing AD-6 order keeps volatile content (knowledge/summary/recent/recall/owner message) strictly after the prefix — the single highest-leverage caching move, already true; lock it with the test so a reorder can't regress it.

- [x] **Task 2 — Anthropic cache signal logging + `cache_control` spike** (AC: 2)
  - [x] In `anthropic_provider.py`, after each `messages.create`, LOG the response `usage` cache fields (`cache_creation_input_tokens`, `cache_read_input_tokens` when present) at INFO under the broker logger — no usage logging exists today. Guard with `getattr` (GLM/z.ai may omit the fields). This is the per-provider cache-hit signal the design mandates ("no silent assumption").
  - [x] SPIKE (timeboxed): place ONE `cache_control: {type:"ephemeral"}` breakpoint on the stable persona prefix. NOTE the obstacle up front: both `complete(prompt:str)` and `complete_with_tools(messages, tools)` send the persona embedded INSIDE a single content string (`messages=[{"role":"user","content":prompt}]`) — there is no mid-string breakpoint site. Decide the MINIMAL seam within the timebox: either (a) the Anthropic adapter splits the leading persona block from the volatile remainder into two content blocks and sets `cache_control` on the persona block (needs a stable boundary marker the worker emits — e.g. a sentinel line between prefix and volatile that the adapter splits on, stripped before send), or (b) defer. Do NOT widen the worker→broker contract beyond the minimal marker.
  - [x] Prove native-Claude honors the breakpoint with a UNIT test against a faked Anthropic SDK response object exposing `usage.cache_read_input_tokens` (mirror the existing provider unit-test style; no live LLM, no network). Assert the adapter sends the `cache_control` block AND logs the cache-read signal.
  - [x] If the spike exceeds the timebox (obstacle (a) too invasive), DEFER the explicit breakpoint: keep the usage-logging + the byte-stable prefix, write a findings note (see Task 5), and record the defer in `deferred-work.md`. The AC explicitly allows this — the fallback is free OpenAI-surface + native-Claude-auto caching + GLM lazy-load/budget, never a silent cap.

- [x] **Task 3 — Lazy-load reference docs (`TOOLS.md` / `ARCHITECTURE.md`)** (AC: 3)
  - [x] Create `shelldon/persona/TOOLS.md` (concise: the tool surface the bot has — `get_time`, `read_file`/`list_dir`, `python_eval`, self-coded tools via `propose_tool`; keep it short, owner-editable) and `shelldon/persona/ARCHITECTURE.md` (concise: Pi Zero 2W, E-Ink face, fork-worker/core split, memory tree — the "how do you work" answer). Port from v1's intent, NOT verbatim — match shelldon's real architecture. **`git add` both** (the untracked-seed failure class — see 10.4 review).
  - [x] In `core/memory.py`: add `TOOLS.md`/`ARCHITECTURE.md` to `_PROMPT_TEMPLATE_SEED_FILES` (copy-if-absent, owner-editable, NOT rewrite targets); add `read_tools()`/`read_architecture()` accessors mirroring `read_heartbeat`/`read_bootstrap` (`is_file` guard + `except (OSError, UnicodeDecodeError): None`).
  - [x] In `worker/prompt.py`: add a pure keyword matcher (e.g. `_needs_reference(message) -> set[str]`) — a small frozenset of trigger words per doc (tools/tool/command/capability → tools; hardware/architecture/internals/"how do you work"/Pi/screen → architecture). In `gather_context`, when the owner message matches, read the doc fail-soft + char-budgeted (`_bounded_text(_safe_read(...), …)`) and add it to the returned dict (`None` when no match). In `assemble_prompt`, add `tools=None`/`architecture=None` kwargs, injected as distinct sections placed WITH the volatile layers (after `about`, before/around `knowledge`) so they never sit inside the cached prefix. Omit-empty like every section.
  - [x] Tests (`tests/test_prompt_assembly.py`): a matching message injects the doc (section header present, after the persona prefix); a non-matching message omits it; the accessor returns `None` when the file is absent; seed copy-if-absent + skip-present (in `tests/test_memory.py`, mirror `test_seed_bootstrap_*`).

- [x] **Task 4 — Pi migration: packaging verify + non-destructive copy-if-absent** (AC: 4)
  - [x] Add a test asserting the BUILD ARTIFACT contains the persona seeds: build the wheel (`uv build` / `python -m build`) into a temp dir and assert every `shelldon/persona/*.md` is present inside it — OR, if a full build is too heavy for the unit suite, assert via `importlib.resources.files("shelldon.persona").joinpath(name).is_file()` for every seed name (the same API `_seed_persona` uses, so it proves the runtime resolution path). If the wheel omits `*.md`, add `[tool.hatch.build.targets.wheel.force-include]` / `artifacts` in `pyproject.toml` so the seeds ship.
  - [x] Add a non-destructive-migration test: pre-populate a root with `about.md`, `facts/x.md`, and a hand-written `DIRECTIVE.md`; construct `CuratedMemory(root)`; assert the existing three are byte-unchanged AND the new seeds (incl. `TOOLS`/`ARCHITECTURE`) were added. (Extends the existing seed-skip-present tests.)
  - [x] `deploy/setup-pi.sh`: the repo clone + `uv sync` already brings `shelldon/persona/` along, and `CuratedMemory` seeds copy-if-absent at boot — so the script likely needs NO change. CONFIRM that's true (the persona dir is part of the package, not a separate data path the script must copy). If a gap exists (e.g. the service runs from an installed wheel that drops `*.md`), fix it in Task 4's packaging step, not by adding a copy step to the script. Document the conclusion.

- [x] **Task 5 — Findings note + invariants + suite** (AC: 2, 5)
  - [x] Write `_bmad-output/implementation-artifacts/epic-10-caching-findings-2026-06-25.md` (short): what byte-stable prefix buys on each surface; whether the Anthropic breakpoint shipped or deferred (and why); the GLM/z.ai passthrough result (or "owner live-check pending"); the recommended budget posture if GLM doesn't cache.
  - [x] Run `uv run python -m pytest -q` (prior count + new, all green) and `uv run lint-imports` (3 contracts KEPT — caching in the broker AD-2 boundary, lazy-load is pure worker reads, core stays LLM-free).

## Dev Notes

### What this story is (and is NOT)
- **IS:** the COST + DEPLOYMENT close-out of Epic 10 — (A) make the always-on persona cheap to re-send by keeping it a byte-stable prefix (free caching on the OpenAI surface, positions the Anthropic surface), (B) a timeboxed spike for an explicit Anthropic `cache_control` breakpoint + cache-signal logging, (C) lazy-load heavy reference docs by keyword, (D) verify the persona seeds ship in the wheel and land non-destructively on the live Pi. (Design §4 story 10.5, §6 risks.)
- **IS NOT:** a new persona file the bot rewrites (TOOLS/ARCHITECTURE are owner-editable prompt policy, no `rewrite_*` op — like HEARTBEAT/DREAM/BOOTSTRAP); a general templating engine; multi-user profiles; porting `VAULT.md` (design non-goal — shelldon's `vault/` is the OS-isolated secret store, the opposite of v1's capture vault); a live-LLM test (CI uses fakes; the GLM cache check is owner-run out-of-CI).

### KEY OBSTACLE — the single-string prompt (read before scoping the caching spike)
The worker assembles ONE prompt string (`build_prompt` → `assemble_prompt` returns `"\n\n".join(parts)`), and the broker sends it as a SINGLE content block:
- `complete(prompt:str)` → `messages=[{"role":"user","content":prompt}]` (`anthropic_provider.py:91-97`).
- `complete_with_tools(messages, tools)` (the 9.1 native-function-calling path, the DEFAULT for owner turns when `job.tools` is set — `broker.py:48`) → the persona is inside the first `Message.content`.
So there is **no system block and no mid-string breakpoint site today.** Anthropic prompt caching attaches `cache_control` to a *content block*; to cache the persona prefix you must make it a SEPARATE block from the volatile remainder. That is the crux of the AC2 spike, and the reason the design timeboxes it and allows a defer. The CHEAP, certain win is AC1 (byte-stable prefix) — OpenAI-surface prefix caching and native-Claude automatic caching both key on a stable token prefix of the single string, no structure needed. **Do AC1 first; treat AC2 as the bounded spike.**

### Caching is TWO surfaces, not per-preset (design §4 table)
- **Anthropic-shape** (`anthropic_provider.py` — drives `claude` + `glm`): EXPLICIT `cache_control` breakpoints (max 4, prefix-match, 5-min TTL; write 1.25×, read ~0.1×; min cacheable ~2048–4096 tok by model). One breakpoint on the last stable persona block. **z.ai/GLM passthrough of `cache_control` is the one unknown** — Zhipu has native context caching but whether the Anthropic-compat proxy forwards the field is unverified → log the signal, owner verifies live.
- **OpenAI-shape** (`openai_provider.py` — drives `openai`/`gemini`/`groq`/`cerebras`/`mistral`/`openrouter`/`nvidia`/`github`/`ollama`): mostly AUTOMATIC prefix caching (OpenAI ≥1024 tok ~50% read discount; Gemini 2.5 implicit). No request field — just keep the persona a real byte-stable prefix. `ollama` local = free anyway. **Nothing to send here; AC1 covers it.**

### Existing patterns to MIRROR (do not reinvent)
- **Seed copy-if-absent + read accessor:** `core/memory.py` `_PROMPT_TEMPLATE_SEED_FILES` + `_seed_persona` + `read_heartbeat`/`read_dream`/`read_bootstrap` (10.3/10.4). `TOOLS.md`/`ARCHITECTURE.md` are two more entries + two more accessors — IDENTICAL shape (fail-soft `OSError/UnicodeDecodeError → None`, no write path).
- **Fail-soft + char-budgeted read + omit-empty section:** `worker/prompt.py` `_safe_read`/`_bounded_text`/`PERSONA_CHAR_BUDGET`; `assemble_prompt` `if x and x.strip(): parts.append(...)`. Read the reference docs and the (deferred) prefix split the SAME way.
- **Provider unit tests with a faked SDK response:** the broker provider tests construct fake response objects (text/tool_use blocks). Add a fake carrying a `usage` object with `cache_read_input_tokens` to prove the breakpoint+logging without a live call.
- **Out-of-CI live verification:** the 8.0 live smokes (`-m live`, network-gated, default suite stays green) — the GLM cache-hit check follows that model; the owner runs the paid call, dev ships the scaffold + findings doc.
- **Lazy-import discipline (analogous intent):** `transport/telegram.py:258` / `app.py:86` lazy-import heavy deps only when used — the same "don't pay until needed" idea applied to reference DOC tokens via keyword gating (v1's `needs_extra_context`). NOTE: shelldon has NO existing lazy-load-by-keyword for prompt content — this is a NEW (small, pure) addition, not a port of existing code.

### Current state of the files being modified (read before editing)
- **`shelldon/worker/prompt.py`** — `assemble_prompt` (pure compose, AD-6 order, now incl. `bootstrap` from 10.4), `gather_context` (read-only opens, fail-soft), `build_prompt`, `_safe_read`/`_bounded_text`/`PERSONA_CHAR_BUDGET`. **Must preserve:** omit-empty sections, owner message last, per-file fail-soft, the char budget, the 10.4 onboarding section after `system`. Reference docs go in the VOLATILE region (after `about`), NOT the cached prefix.
- **`shelldon/core/memory.py`** — `_PROMPT_TEMPLATE_SEED_FILES` (now `HEARTBEAT`/`DREAM`/`BOOTSTRAP`), `_seed_persona` (copy-if-absent, never raises, never overwrites), the `read_*` accessors. **Must preserve:** copy-if-absent non-destructive, construction never raises, core sole writer, fail-soft reads.
- **`shelldon/broker/anthropic_provider.py`** — `complete`/`complete_with_tools` (the only egress that could carry `cache_control`); pure (config-in, no env reads, AD-2). **Must preserve:** the credential never leaves the broker; error-type mapping to `Transient/PermanentProviderError`; no pet-domain parsing. The `name` audit label is never a credential.
- **`pyproject.toml`** — `[tool.hatch.build.targets.wheel] packages = ["shelldon"]`. Hatchling SHOULD include non-`.py` package data under that tree, but the 10.4 review found a seed that was simply never `git add`ed — so VERIFY the dist actually contains `persona/*.md`, don't assume.
- **`deploy/setup-pi.sh`** — git-clone + `uv sync --locked` + systemd unit (MemoryMax=400M). It runs `uv run python -m shelldon` from the repo (`WorkingDirectory=$REPO`), so the persona dir is present as repo files at runtime; likely no script change needed — CONFIRM.

### Invariants that MUST hold (design §5)
- **AD-1 core LLM-free / AD-2 broker egress:** caching lives in `anthropic_provider.py` (the egress boundary); lazy-load keyword logic is a pure string match in the worker; accessors are file reads in core-memory. No model code added to core. import-linter stays green.
- **AD-6 worker read-only / AD-5 single-writer:** the worker READS `TOOLS`/`ARCHITECTURE`; they have NO write path (no `rewrite_*` op). The owner may hand-edit on disk (out-of-band). Core stays sole runtime writer.
- **AD-3 fork = no accumulation:** reference files read fresh per fork; the persona prefix is rebuilt per turn by design (the cost premise — caching is the mitigation, not in-process persistence which is impossible with the ephemeral worker).
- **Fail-soft:** missing/corrupt reference file → section omitted, turn proceeds. Copy-if-absent never overwrites a populated root.
- **No silent cap (AC2):** if GLM/z.ai doesn't honor `cache_control`, that's LOGGED and the budget/lazy-load fallback engages — never a quiet token truncation.

### Project Structure Notes
- New files: `shelldon/persona/TOOLS.md`, `shelldon/persona/ARCHITECTURE.md` (both `git add`ed). Edits: `core/memory.py` (2 seed entries + 2 accessors), `worker/prompt.py` (keyword matcher + 2 assembly kwargs + reads), `broker/anthropic_provider.py` (usage logging + the spike breakpoint), possibly `pyproject.toml` (force-include if the wheel drops `*.md`), possibly `deploy/setup-pi.sh` (likely none). Test additions: `tests/test_memory.py` (seed/accessor/non-destructive), `tests/test_prompt_assembly.py` (byte-stable prefix, lazy-load match/omit), `tests/test_*provider*` or a new broker test (cache logging + breakpoint via fake SDK response). Findings doc: `epic-10-caching-findings-2026-06-25.md`. No new op, no `SCHEMA_VERSION` bump, no `runtime.py`/`state.py`/`contracts` change.
- **Scope guard (CLAUDE.md "Simplicity First"):** this is the LAST Epic 10 story and is deliberately woven/spiky. Keep AC1 + AC3 + AC4 tight and certain; treat AC2 as a bounded spike with the explicit defer path — do NOT over-build a request-restructuring framework if the timebox says defer. No speculative caching abstraction across both providers (OpenAI surface needs nothing).

### References
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#4 — Story 10.5 (cost premise, 2-surface caching table, lazy-load, Pi migration)]
- [Source: _bmad-output/planning-artifacts/epic-10-persona-files-design-2026-06-25.md#6 — risks: prompt bloat, two-surface caching, byte-stability silent invalidator, live-Pi migration, day-one drift]
- [Source: _bmad-output/planning-artifacts/epics.md#Story-10.5 — the 3 AC blocks (byte-stable prefix + Anthropic breakpoint; lazy-load by keyword; non-destructive Pi migration)]
- [Source: shelldon/broker/anthropic_provider.py#91-144 — complete/complete_with_tools: persona sent as a single content string; where cache_control + usage logging attach]
- [Source: shelldon/broker/broker.py#36-49 — handle_job: tools→complete_with_tools, else complete; the two egress paths]
- [Source: shelldon/worker/prompt.py — assemble_prompt/gather_context/build_prompt/_safe_read/_bounded_text/PERSONA_CHAR_BUDGET (prefix + where lazy-load injects)]
- [Source: shelldon/core/memory.py#62 (_PROMPT_TEMPLATE_SEED_FILES), #126 (_seed_persona), #278-310 (read_heartbeat/read_dream/read_bootstrap) — the seed+accessor pattern to mirror]
- [Source: pyproject.toml#27-28 — hatchling wheel packages=["shelldon"]; verify persona/*.md ships]
- [Source: deploy/setup-pi.sh — clone + uv sync + systemd; persona dir present as repo files at runtime]
- [Source: 10-4 story + code review — the untracked-seed failure class (BOOTSTRAP.md was `??`); git add every new seed and verify it lands in the dist]
- [Source: _bmad-output/implementation-artifacts/deferred-work.md — 10.3/10.4 deferrals; record any 10.5 spike defer here]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- `uv run python -m pytest -q` → 838 passed, 3 skipped (pre-existing live/soak), 8 deselected (~17s).
- `uv run lint-imports` → 3 contracts KEPT (core LLM-free AD-1 / transport-creds AD-13 / plugins-never-import-core AD-8).
- `uv build --wheel` → 0.6s; the wheel carries all 9 `shelldon/persona/*.md` seeds; all 9 git-tracked.

### Completion Notes List

- **AC1 (byte-stable prefix) — DONE, verification not refactor.** Audited `assemble_prompt` + every prefix accessor (`read_instructions/directive/identity/soul/user/about`): each is a plain `path.read_text()` of static file text; no `now()`/UUID/unsorted-dict/hostname/PID anywhere in the prefix. Added 3 guard tests: prefix byte-identical across two assemblies of the same root; no ISO-timestamp/UUID regex match in the prefix (the silent-cache-invalidator guard); volatile content strictly after the prefix (locks AD-6 order).
- **AC2 (Anthropic caching) — logging SHIPPED, explicit breakpoint DEFERRED (spec-allowed).** Added `_log_cache_usage` to `anthropic_provider.py`, called on both egress paths (`complete` + `complete_with_tools`); logs `usage.cache_creation_input_tokens`/`cache_read_input_tokens` at INFO under `shelldon.broker`, `getattr`-guarded (GLM/z.ai may omit → nothing logged, no crash). Unit tests (faked SDK response): cache-read signal logged; absent fields don't crash or log. The explicit `cache_control` breakpoint is deferred — the persona is sent inside a single content string, so a breakpoint needs a worker-emitted boundary marker that EVERY provider surface would have to strip (cross-cutting contract change beyond the timebox). Recorded in `deferred-work.md` + `epic-10-caching-findings-2026-06-25.md`. No silent cap: byte-stable prefix gives free OpenAI + native-Claude-auto caching; GLM falls back to lazy-load + the 5.2 budget.
- **AC3 (lazy-load reference docs) — DONE.** Created `TOOLS.md`/`ARCHITECTURE.md` seeds (git-added, owner-editable, NOT rewrite targets), added to `_PROMPT_TEMPLATE_SEED_FILES` + `read_tools()`/`read_architecture()` accessors (fail-soft `OSError/UnicodeDecodeError → None`, mirrors `read_heartbeat`). Pure `_needs_reference(message)` keyword matcher in the worker injects each doc only on a matching message, AFTER the cached prefix (with the volatile layers), char-budgeted; an ordinary message reads neither (the cost win). `VAULT.md` not ported.
- **AC4 (Pi migration) — DONE, no script change.** `test_packaging.py`: every seed resolves via `importlib.resources` (the `_seed_persona` runtime path), every seed is git-tracked (the 10.4 untracked-seed lesson — the Pi deploys via `git clone`), and the built wheel contains every seed. Non-destructive-migration test in `test_memory.py`: a populated root (owner `DIRECTIVE.md` + `about.md`/`facts/`) survives a fresh `CuratedMemory` byte-for-byte while the absent seeds are added. `deploy/setup-pi.sh` confirmed needs NO change (runs `python -m shelldon` from `WorkingDirectory=$REPO`, the clone; seeds copy-if-absent at boot). No `pyproject.toml` change (hatchling already ships package data).
- **AC5 (invariants) — HELD.** Core stays LLM-free (caching is in `anthropic_provider.py`, the AD-2 egress; lazy-load is a pure worker string match; accessors are core-memory file reads). Worker reads the reference docs, never writes (no `rewrite_*` op). Single-writer + fork-no-accumulation hold (docs read fresh per fork). import-linter 3 KEPT. No new op, no new dep, no `SCHEMA_VERSION`/runtime/state/contracts change.

### File List

- `shelldon/persona/TOOLS.md` (new, git-added) — lazy-load reference seed: the bot's tool surface.
- `shelldon/persona/ARCHITECTURE.md` (new, git-added) — lazy-load reference seed: hardware/internals.
- `shelldon/core/memory.py` — `TOOLS.md`/`ARCHITECTURE.md` added to `_PROMPT_TEMPLATE_SEED_FILES`; `read_tools()`/`read_architecture()` accessors.
- `shelldon/worker/prompt.py` — `_needs_reference` keyword matcher; `tools`/`architecture` gather reads (keyword-gated, fail-soft, char-budgeted) + assemble kwargs/sections (after `about`, in the volatile region).
- `shelldon/broker/anthropic_provider.py` — module logger + `_log_cache_usage` (per-turn cache-signal logging on both egress paths).
- `tests/test_prompt_assembly.py` — lazy-load match/omit/fail-soft + byte-stable-prefix guard tests.
- `tests/test_memory.py` — reference-doc seed/accessor tests + non-destructive migration test.
- `tests/test_anthropic_provider.py` — cache-signal logging tests (hit + GLM-omits).
- `tests/test_packaging.py` (new) — seeds resolve via importlib.resources / are git-tracked / ship in the wheel.
- `_bmad-output/implementation-artifacts/epic-10-caching-findings-2026-06-25.md` (new) — caching findings + defer rationale.
- `_bmad-output/implementation-artifacts/deferred-work.md` — 2 Story 10.5 defer entries (explicit breakpoint, GLM passthrough).

### Review Findings

- [x] [Review][Patch] `_ARCH_KEYWORDS` false-positive bare words trigger ARCHITECTURE.md on unrelated messages [`shelldon/worker/prompt.py`] — RESOLVED: dropped bare `pi`/`ram`/`screen`/`cpu`/`raspberry` from `_ARCH_KEYWORDS` (now `{hardware,architecture,internals,eink,systemd}`); moved the real signal to a `"raspberry pi"` phrase in `_ARCH_PHRASES`; added `test_needs_reference_no_false_positive_on_common_words` (screen/ram/pi/cpu/raspberry-jam → `set()`). Confirmed by 2nd review (Blind+Edge+Auditor all converged).
- [x] [Review][Patch] Migration test only checks 4 of 9 seeds in the "new seeds added" assertion [`tests/test_memory.py`] — RESOLVED: `test_migration_is_non_destructive_on_populated_root` now loops over `_PERSONA_SEED_FILES + _PROMPT_TEMPLATE_SEED_FILES` (all 9 seeds), not the named 4.
- [x] [Review][Patch] `_prefix_of` test helper could silently return the whole prompt if `_VOLATILE_HEADERS` drifts [`tests/test_prompt_assembly.py`] — RESOLVED (2nd review, Edge Hunter): `_prefix_of` now asserts a volatile header is present, so the byte-stable guard can't pass while no longer isolating a prefix.
- [x] [Review][Defer] The git-tracked packaging test is the SOLE catcher of an untracked seed; it skips on a no-git runner [`tests/test_packaging.py`] — DEFERRED (2nd review, Edge Hunter, medium): the importlib/wheel tests read/bundle present-but-untracked files, so only `test_persona_seeds_are_git_tracked` catches the 10.4 untracked-seed class — and it `pytest.skip`s where `git` is absent (installed sdist / some sandboxes). The project's CI has git and the Pi deploys via `git clone`, so the guard holds in the environments that matter; hardening the importlib/wheel tests to catch untracked-without-git is non-trivial. Recorded in `deferred-work.md`.
- [x] [Review][Dismiss] `"e-ink"` phrase entry claimed dead/redundant — FALSE POSITIVE (2nd review, Blind Hunter): `_WORD_RE` (`\w+`) tokenizes `"e-ink"` → `["e","ink"]`, NOT `"eink"`, so the `"e-ink"` phrase is the ONLY catcher for hyphenated input and is complementary to the `eink` keyword. Kept.
- [x] [Review][Dismiss] `_needs_reference(None)` would `AttributeError` (not fail-soft) — DISMISSED: `gather_context` defaults `owner_message=""` and the worker always passes a real string; per CLAUDE.md no error handling for impossible inputs.
- [x] [Review][Defer] `_log_cache_usage` docstring is a 90-word design diary — style debt [`shelldon/broker/anthropic_provider.py`] — deferred, pre-existing style pattern
- [x] [Review][Defer] `read_tools`/`read_architecture` are copy-paste — project pattern (mirrors `read_heartbeat`/`read_dream`); extract if a 4th reader is added [`shelldon/core/memory.py`] — deferred, pre-existing
- [x] [Review][Defer] `_TOOLS_PHRASES`/`_ARCH_PHRASES` tuples vs `_TOOLS_KEYWORDS`/`_ARCH_KEYWORDS` frozensets — minor type inconsistency for parallel data [`shelldon/worker/prompt.py:65-73`] — deferred, pre-existing
- [x] [Review][Defer] AC2 cache-signal test only covers `complete()` path, not `complete_with_tools()` — same `_log_cache_usage` method; not a spec requirement but leaves 2nd egress path test-uncovered [`tests/test_anthropic_provider.py`] — deferred, pre-existing
- [x] [Review][Defer] AC3 ordering test doesn't assert reference docs come after the full persona prefix (directive/identity/soul/user) — byte-stable prefix test covers that lock [`tests/test_prompt_assembly.py:414-429`] — deferred, pre-existing
- [x] [Review][Defer] Pre-existing `read_about`/`read_summary` UnicodeDecodeError uncaught at method level — caught by `gather_context` outer handler; pre-existing in untouched methods [`shelldon/core/memory.py:251,257`] — deferred, pre-existing
- [x] [Review][Defer] Pre-existing `resp.content is None` guard missing in `complete()`/`normalize_anthropic_response()` — SDK contract trusted [`shelldon/broker/anthropic_provider.py`] — deferred, pre-existing

## Change Log

- 2026-06-26 — Code review (3 parallel adversarial layers: Blind Hunter, Edge Case Hunter, Acceptance Auditor) — no hard AC violations. 3 patches applied: (1) dropped ambiguous bare `_ARCH_KEYWORDS` (`pi`/`ram`/`screen`/`cpu`/`raspberry`) → `"raspberry pi"` phrase + negative test (false-positive lazy-load); (2) migration test now asserts all 9 seeds; (3) `_prefix_of` asserts a volatile header is present. 1 new defer (git-tracked test is sole untracked-seed guard, skips without git → deferred-work). 3 dismissed (`e-ink` "dead code" = false positive; `_needs_reference(None)` impossible input; wheel-build-flake skip). 839 pass (+1), import-linter 3 KEPT.
- 2026-06-25 — Story 10.5 DEV DONE → review: Epic 10 cost + deployment close-out. (A) Byte-stable persona prefix verified (no per-request interpolation) + 3 guard tests (silent-cache-invalidator caught). (B) Per-turn Anthropic cache-signal logging shipped (`_log_cache_usage`, both egress paths, `getattr`-guarded for GLM); explicit `cache_control` breakpoint DEFERRED (single-content-string obstacle = cross-cutting all-surface marker change beyond timebox; spec-allowed) → findings note + deferred-work. (C) Lazy-load `TOOLS.md`/`ARCHITECTURE.md` by keyword (`_needs_reference`, pure, after the cached prefix), seeded copy-if-absent, no rewrite op. (D) Pi migration verified: seeds git-tracked + in the wheel + non-destructive copy-if-absent; `setup-pi.sh` + `pyproject.toml` need no change. 838 pass (+18), import-linter 3 KEPT, 0 new ops/deps, no runtime/state/contracts change. `VAULT.md` not ported. GLM passthrough owner-verified out-of-CI. (Opus 4.8)
- 2026-06-25 — Story 10.5 drafted (ready-for-dev): Epic 10 cost + deployment close-out. (A) Persona prefix kept BYTE-STABLE (free OpenAI-surface caching + positions Anthropic) with a guard test against the silent-cache-invalidator; (B) a TIMEBOXED, DEFER-ALLOWED spike for an explicit `cache_control` breakpoint in `anthropic_provider.py` + per-turn cache-signal logging (the obstacle: the persona is sent as a single content string, so there's no breakpoint site without a minimal worker→broker marker — defer keeps free caching + GLM budget fallback, never a silent cap); (C) lazy-load `TOOLS.md`/`ARCHITECTURE.md` by keyword (v1's `needs_extra_context`, a new small pure addition), seeded copy-if-absent, no `rewrite_*` op; (D) verify the persona seeds ship in the wheel + copy-if-absent is non-destructive on the live Pi. No new op, no deps, no runtime/state/contracts change. `VAULT.md` not ported. GLM cache passthrough is owner-verified out-of-CI. (Opus 4.8)
