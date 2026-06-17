---
baseline_commit: 5f221f01d8c66aa03de5af256d3920afb5d49269
---

# Story 2.1: Provider abstraction and an ordered chain

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As the owner,
I want the broker to drive LLM calls through a provider-agnostic interface with a configurable ordered chain,
so that GLM is just the first choice and alternates are one config line away — never a code change (AD-2, AD-1).

## Acceptance Criteria

1. **Common adapter interface + config-driven ordered chain:** Given the broker, when providers are configured, then each provider sits behind one common adapter interface, and the broker reads an **ordered chain (GLM first, then alternates) from config**. Two adapters by wire format: the **Anthropic-format** adapter (GLM-5.2 via Z.ai **and** native Claude) and a single **OpenAI-compatible** adapter. The OpenAI-compatible adapter serves a data-driven preset table — Ollama-LAN, OpenAI, OpenRouter, **Groq, Cerebras, NVIDIA, Mistral, GitHub Models, and Gemini** (via its OpenAI-compatible endpoint). *(Gemini was tried as a native adapter during dev, then simplified to its OpenAI-compatible endpoint — see Change Log — so no third SDK is needed.)*
2. **Reorder/extend with no code change:** Given a reordered or extended provider chain in config, when the broker restarts, then the new order takes effect with **no change outside the broker** and the `core/` import-linter still passes.

> **Scope seam (binding):** 2.1 builds the **abstraction + the adapters + the config-driven chain assembly**. It does NOT build the automatic fallback/advance-through-the-chain behavior — **that is Story 2.2.** In 2.1 the broker holds the ordered chain but a turn still executes against the **primary (chain[0])** with the existing single-retry; 2.2 makes a failed call advance through the chain.

## Tasks / Subtasks

> **What already exists (reuse, do NOT reinvent):** `shelldon/broker/provider.py` already defines the common interface `LLMProvider` (Protocol: `async complete(prompt) -> str`) plus `TransientProviderError` / `PermanentProviderError`. `shelldon/broker/glm.py::GLMProvider` is already an **Anthropic-format adapter** (it uses `anthropic.AsyncAnthropic` with a configurable `base_url`/`model`/`api_key`) — it is GLM-specific only in its *defaults*. `broker/broker.py::handle_job` already does the single transient-retry. `broker/service.py::run_broker` is the bus loop. The live path is proven: `tests/test_glm_live_smoke.py` made a real `glm-4.7` call via Z.ai. **2.1 generalizes naming + adds one adapter + a config chain builder — it is not a rewrite.**

- [x] **Task 1: Add the `openai` SDK dependency (broker-only)** (AC: 1)
  - [x] Add `openai` (latest stable `1.x`, pin the exact version like the others) to `[project].dependencies` in `pyproject.toml`, with a broker-only comment mirroring the `anthropic` line. **`openai` is already in the `core` import-linter forbidden list** (`[tool.importlinter]` "core is LLM-free"), so AD-1 stays mechanically green — confirm with `uv run lint-imports`.
  - [x] `uv lock` / sync so the pinned version is locked. **No other new dependency** (Gemini's SDK is deferred with the Gemini adapter).

- [x] **Task 2: Generalize the Anthropic-format adapter (GLM + native Claude)** (AC: 1)
  - [x] Rename `shelldon/broker/glm.py` → `shelldon/broker/anthropic_provider.py` and `GLMProvider` → `AnthropicProvider`: the **wire-format** adapter, not a GLM-specific one. Keep the implementation (it already takes `api_key`/`base_url`/`model`); drop the GLM-flavored *class name*, keep GLM-flavored *defaults* available via the chain presets (Task 4), and ensure it serves **native Claude** too (point `base_url` at Anthropic + a Claude model + an Anthropic key — no code difference, just config).
  - [x] Preserve the existing error mapping verbatim (it's the template for the OpenAI adapter): `APITimeoutError`/`APIConnectionError`/`RateLimitError`/`InternalServerError` → `TransientProviderError`; `APIStatusError` with `status >= 500` → transient, else `PermanentProviderError`; empty/no-text response → `PermanentProviderError`.
  - [x] Update the two importers of the old name: `tests/test_broker_glm.py` and `tests/test_glm_live_smoke.py` (the live smoke). Keep their behavior identical — just the import + construction site change. (Optionally rename `test_broker_glm.py` → `test_anthropic_provider.py`; keep the live smoke filename or rename to `test_provider_live_smoke.py` — your call, keep the `live` marker + key-gated skip.)

- [x] **Task 3: Build the OpenAI-compatible adapter (Ollama-LAN / OpenAI / OpenRouter)** (AC: 1)
  - [x] New `shelldon/broker/openai_provider.py`: `OpenAIProvider` implementing `LLMProvider`, backed by `openai.AsyncOpenAI(api_key=..., base_url=...)` calling `client.chat.completions.create(model=..., max_tokens=..., messages=[{"role":"user","content":prompt}])`. Resolve `api_key`/`base_url`/`model` from constructor args → env, **only inside the broker** (AD-2) — never on a Job/Result/Envelope.
  - [x] **Mirror the Anthropic adapter's error mapping** so retry/fallback keys on the same two exception types (never raw SDK exceptions): `openai.APITimeoutError`/`APIConnectionError`/`RateLimitError`/`InternalServerError` → `TransientProviderError`; `openai.APIStatusError` (`.status_code >= 500` → transient, else `PermanentProviderError`); extract `resp.choices[0].message.content` and treat empty/missing as `PermanentProviderError("provider returned no text")` (same "no-text is a failed turn" rule as the Anthropic adapter).
  - [x] **Ollama-over-LAN note:** base_url is `http://<host>:11434/v1` (your `.env` `OLLAMA_API_BASE=http://192.168.0.25:11434` — append `/v1` in the preset), model from `OLLAMA_MODEL`; Ollama ignores the api_key, so pass any non-empty placeholder. One adapter, three endpoints — the only difference is `base_url`/`model`/key source.
  - [x] Unit-test with a fake (monkeypatch `client.chat.completions.create`, mirror `tests/test_broker_glm.py`): a text response returns the text; an empty/`None` content raises `PermanentProviderError`; a transient SDK error maps to `TransientProviderError`.

- [x] **Task 4: Config-driven ordered chain builder** (AC: 1, 2)
  - [x] New `shelldon/broker/chain.py`: a `build_chain(env=os.environ) -> list[LLMProvider]` that reads an **ordered, comma-separated preset list** from one config var (e.g. `PROVIDER_CHAIN="glm,ollama"`; default `"glm"` to preserve today's single-provider behavior) and constructs the ordered provider instances. Map each **preset name → (adapter class, base_url, model, api_key env)**:
    - `glm` → `AnthropicProvider`, Z.ai base url, `GLM_MODEL`/`ANTHROPIC_MODEL`, key from `GLM_API_KEY`/`ANTHROPIC_API_KEY`.
    - `claude` → `AnthropicProvider`, Anthropic base url (SDK default), a Claude model, `ANTHROPIC_API_KEY` (native).
    - `ollama` → `OpenAIProvider`, `OLLAMA_API_BASE` + `/v1`, `OLLAMA_MODEL`, placeholder key.
    - `openai` → `OpenAIProvider`, OpenAI default base url, `OPENAI_MODEL`, `OPENAI_API_KEY`.
    - `openrouter` → `OpenAIProvider`, OpenRouter base url, `OPENROUTER_MODEL`, `OPENROUTER_API_KEY`.
  - [x] Keep the preset table small and data-driven (a dict), so **adding/reordering is a config line, not code** (AC2). An unknown preset name is a clear startup error (fail fast, not a silent skip). A preset whose required key env is missing is also a clear error at build time.
  - [x] Unit-test (no network): `PROVIDER_CHAIN="glm,ollama"` builds a 2-element list of the right adapter types in order; reordering the env var reorders the list; an unknown preset raises; a missing-key preset raises. Use a fake/empty env dict — do not touch real creds.

- [x] **Task 5: Broker holds the ordered chain; primary-only execution (fallback deferred to 2.2)** (AC: 1, 2)
  - [x] Wire the chain into the bus loop. **Recommended (zero ripple):** `run_broker(socket_path, chain)` accepts the ordered `list[LLMProvider]` (or a tiny `ProviderChain` wrapper holding `.providers`); internally it calls the **existing, unchanged** `handle_job(job, chain[0])` — so the tested single-retry behavior on the primary is preserved and `handle_job` is not modified in 2.1. Document inline that **2.2 changes this to iterate the chain** (the single explicit seam for fallback).
  - [x] Update existing `run_broker` callers to pass a chain: `tests/test_broker_bus.py`, `tests/test_broker_service.py`, `tests/test_end_to_end_turn.py`, and `tests/test_endurance_soak.py` (they currently pass a single provider → pass `[provider]`). Keep their assertions identical.
  - [x] A production composition note (do NOT build the process yet): the broker process builds its chain via `build_chain()` at startup; reorder = edit `PROVIDER_CHAIN` + restart (AC2). The literal broker-as-its-own-process launcher is a later deployment story.

- [x] **Task 6: Credential-hygiene test (deferred-fold from 1.4 review)** (AC: 1)
  - [x] Add a test proving **no credential material reaches `Result.error`** via `str(sdk_exc)`: construct an adapter, inject a fake SDK exception whose message embeds a fake key sentinel (e.g. `"sk-SECRET-do-not-leak"`), run it through `handle_job`, and assert the sentinel does **not** appear in the returned `Result.error`. (Resolves the 1.4 deferred "potential credential leak via `str(sdk_exc)`" item per the Epic 1 retro.) If the assertion fails, **redact** in the adapter's error mapping (don't echo raw SDK text that could contain request headers/keys) and note it.

- [x] **Task 7: Verify guard + full suite (+ optional live smoke)** (AC: 1, 2)
  - [x] `uv run lint-imports` → both contracts KEPT; **`core/` still imports no provider lib** (the new `openai` import lives only in `broker/`). Confirm `openai` is covered by the core forbidden list.
  - [x] `uv run pytest -q` → green (existing suites updated for the chain signature + the new adapter/chain unit tests). Default run does not hit the network.
  - [x] **Optional live smoke (gated, your call):** extend the `live`-marked smoke to also exercise the OpenAI-compatible adapter against your Ollama LAN box (`OLLAMA_API_BASE`/`OLLAMA_MODEL`) — skipped unless those env vars are set, same pattern as the GLM smoke. Documents that the second wire format really works.

## Dev Notes

### Architecture compliance (binding)

- **AD-2 — Broker is the sole trust boundary:** "the broker … owns the ordered **provider chain with retry/fallback** (default GLM; alternates Ollama-LAN/Gemini/OpenAI/OpenRouter). No other process holds creds or calls a model/tool directly. `Job` envelopes carry no creds; the broker injects them internally." 2.1 builds the **chain + adapters** half (config-driven ordering); the retry/**fallback** half is 2.2. Creds resolve only inside the adapters, from the broker's env — never on the bus. [Source: ARCHITECTURE-SPINE.md#AD-2]
- **AD-1 — LLM-free core:** the new `openai` SDK and all adapters live in `broker/`; `core/` imports none of them. The import-linter forbidden list for core already includes `openai`, `anthropic`, `google`, `litellm`, `zhipuai`, `ollama` — so this stays mechanically green. [Source: ARCHITECTURE-SPINE.md#AD-1, pyproject.toml [tool.importlinter]]
- **CAP-8 (LLM fallback on error) = broker provider chain + arbiter degradation (AD-2, AD-9):** 2.1 lays the chain; 2.2 adds fallback-through-chain; 2.3 wires whole-chain-exhaustion to the arbiter's reflex degrade. [Source: ARCHITECTURE-SPINE.md#CAP-8]
- **Not a spine invariant (config, not code):** "Exact LLM model id + per-provider config — broker config, not spine (default GLM-5.2 via Z.ai compatible endpoint)." So the preset table + `PROVIDER_CHAIN` are config, free to evolve. [Source: ARCHITECTURE-SPINE.md#Out-of-scope/config]

### Wire formats (the grouping the AC requires)

- **Anthropic-format** (`anthropic` SDK): GLM-5.2 via Z.ai's Anthropic-compatible endpoint **and** native Claude. Already built (generalize `GLMProvider` → `AnthropicProvider`). Proven live (`glm-4.7` returned text).
- **OpenAI-compatible** (`openai` SDK): one adapter, `base_url` selects **Ollama-LAN / OpenAI / OpenRouter**. New in 2.1.
- **Gemini**: reached via its **OpenAI-compatible endpoint** (`.../v1beta/openai/`) as an `_OPENAI_COMPAT` preset — no native SDK/adapter. (A native `google-genai` adapter was built then removed once it was clear Gemini fits the OpenAI adapter.) Other free-tier OpenAI-compatible providers (Groq/Cerebras/NVIDIA/Mistral/GitHub) are preset rows too.

### Error taxonomy must stay uniform (this is what makes 2.2 possible)

Every adapter maps SDK errors to exactly `TransientProviderError` (retryable: timeout, connection, rate-limit, 5xx) or `PermanentProviderError` (4xx, no-text, anything non-retryable) — **never a raw SDK exception across `handle_job`**. 2.2's fallback logic keys on these two types, so a new adapter that leaks raw SDK exceptions would break fallback. The `anthropic` mapping in the existing adapter is the canonical template; the `openai` SDK exposes the same shape (`APITimeoutError`, `APIConnectionError`, `RateLimitError`, `InternalServerError`, `APIStatusError.status_code`). [Source: shelldon/broker/glm.py, broker/provider.py]

### Reuse / preserve (from Story 1.4)

- `LLMProvider` Protocol + `TransientProviderError`/`PermanentProviderError` in `provider.py` — the common interface AC1 asks for **already exists**; reuse it, don't define a new one. [Source: 1.4 provider.py]
- `handle_job(job, provider)` single transient-retry (`_MAX_ATTEMPTS=2`) — **unchanged in 2.1** (it runs against the primary). [Source: 1.4 broker.py]
- `run_broker` bus loop (read Job → `handle_job` → Result→CORE, per-frame resilience) — only its **wiring** changes (hold a chain, use the primary). [Source: 1.4 service.py]
- The live-smoke pattern (`live` marker + key-gated skip) — extend, don't reinvent. [Source: tests/test_glm_live_smoke.py]

### What 2.1 deliberately does NOT do

- **No automatic fallback / advance-through-chain** → Story 2.2 (the chain is held but only the primary executes).
- **No reflex degrade on chain exhaustion** → Story 2.3 (and the 1.8 degrade path already exists in core).
- ~~No Gemini adapter~~ → **Gemini folded into 2.1** via its OpenAI-compatible endpoint (a preset, no separate SDK).
- **No backoff between retries, no broker reconnect/supervisor, no `connect()` timeout** → these deferred 1.4 items are folded into **Story 2.2** per the Epic 1 retro, not here. (2.1 folds only the **credential-hygiene** item, Task 6.)
- **No literal broker-as-its-own-process launcher / IPC** → deployment-hardening story.

### Project Structure Notes

- New: `shelldon/broker/openai_provider.py`, `shelldon/broker/chain.py`. Renamed: `shelldon/broker/glm.py` → `anthropic_provider.py` (`GLMProvider` → `AnthropicProvider`). Modified: `pyproject.toml` (+`openai` dep), `broker/service.py` (`run_broker` holds the chain), and the test call-sites that construct `run_broker`/`GLMProvider`. `core/` untouched — import-linter stays KEPT. [Source: ARCHITECTURE-SPINE.md#Structural-Seed]

### Testing standards

- `pytest` + `pytest-asyncio` (auto). Unit-test adapters with monkeypatched SDK clients (no network), mirroring `tests/test_broker_glm.py`. Unit-test the chain builder with a fake env dict (no creds). Keep the network behind the `live` marker + key-gated skip. Before done: `uv run lint-imports` (KEPT) and `uv run pytest -q` (green). [Source: 1.4 tests; Epic 1 retro]

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Epic 2 / Story 2.1; #Story 2.2 (fallback — the next seam); #Story 2.3 (degrade)]
- [Source: ARCHITECTURE-SPINE.md#AD-2, #AD-1, #CAP-8, #Out-of-scope (model id/provider config is config not spine)]
- [Source: shelldon/broker/provider.py (LLMProvider + error types), broker/glm.py (Anthropic-format adapter to generalize), broker/broker.py (handle_job retry), broker/service.py (run_broker loop)]
- [Source: tests/test_broker_glm.py (adapter fake-test pattern), tests/test_glm_live_smoke.py (live marker + key-gated skip)]
- [Source: _bmad-output/implementation-artifacts/epic-1-retro-2026-06-17.md (decisions: Anthropic+OpenAI-compatible now / Gemini later; fold credential-hygiene into 2.1; backoff/reconnect/timeout → 2.2)]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (1M context)

### Debug Log References

- `uv run pytest -q` → 114 passed, 2 skipped (2 Linux-fork), 3 deselected (live, opt-in), 0 failed.
- `uv run lint-imports` → 2 contracts kept, 0 broken (`core` LLM-free; the only provider SDK is `openai`, isolated to `broker/`).
- **Live smokes (real network, `-m live` with `.env`):** Anthropic-format `glm-4.7` via Z.ai → `'Hi!'`; OpenAI-compatible Ollama-LAN `gemma4:26b` → `'Hi!'`; Gemini `gemini-2.5-flash` via its OpenAI-compatible endpoint → `'Hello'`.

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- **Scope decision (Epic 1 retro):** built **Anthropic-format + OpenAI-compatible** adapters. Only one new broker dep, `openai==2.42.0` (already in core's import-linter forbidden list, so AD-1 stays green).
- **Gemini (evolved during dev):** first added as a native `GeminiProvider` (`google-genai`), then — on the owner's observation that Gemini fits the OpenAI adapter — **simplified to its OpenAI-compatible endpoint** (`.../v1beta/openai/`) as a plain preset. The native adapter + `google-genai` dep were removed. One less SDK to maintain; full chat path verified live.
- **Owner-requested free-tier providers folded in (all OpenAI-compatible → config, not code):** `groq`, `cerebras`, `nvidia`, `mistral`, `github` (GitHub Models, uses `GITHUB_TOKEN`), plus `gemini`. Added as rows in a data-driven `_OPENAI_COMPAT` table in `chain.py`; each reads `{NAME}_API_KEY`/`{NAME}_MODEL` (+ optional `{NAME}_BASE_URL`) from env and strips a LiteLLM-style `name/` model prefix. No new code per provider.
- **Live-test infra fix:** live smokes are now **opt-in** via `addopts = "-m 'not live'"` in pyproject — a default `pytest` never hits the network, even when a provider key is in the ambient shell env. (Surfaced because `GEMINI_API_KEY` was exported in the dev shell and the key-gated `live` test ran during the normal suite.) Run live with `uv run pytest -m live`.
- **Anthropic adapter generalized:** `broker/glm.py::GLMProvider` → `broker/anthropic_provider.py::AnthropicProvider` — a pure config-in wire-format adapter (no env reads; serves GLM via Z.ai AND native Claude by config). Env resolution moved to the chain builder. Renamed `test_broker_glm.py` → `test_anthropic_provider.py` and `test_glm_live_smoke.py` → `test_provider_live_smoke.py`; updated `test_broker_creds.py`.
- **OpenAI-compatible adapter:** new `broker/openai_provider.py` — one adapter, `base_url` selects Ollama-LAN / OpenAI / OpenRouter; same error→Transient/Permanent mapping as the Anthropic adapter so 2.2's fallback keys on uniform types.
- **Config-driven chain:** new `broker/chain.py::build_chain(env)` reads `PROVIDER_CHAIN` (default `"glm"`) → ordered provider list via a data-driven preset table (glm/claude/ollama/openai/openrouter). Reorder/extend = config line (AC2); unknown preset or missing credential fails fast at build time.
- **Fallback seam (scope):** `run_broker(socket_path, chain)` now holds the ordered chain but executes the **primary (`chain[0]`)** via the unchanged `handle_job` single-retry. The one place Story 2.2 adds chain iteration is documented inline. Updated the 3 `run_broker` callers to pass a one-element chain.
- **Credential hygiene (1.4 deferral resolved):** found a real leak vector — adapters echoed `str(sdk_exc)` into `TransientProviderError`, which crosses the bus in `Result.error`. **Redacted to the exception TYPE name** (`type(exc).__name__`) in both adapters; full detail stays in the chained `__cause__` for broker-side logs. New tests (`test_broker_creds.py`) inject an SDK error carrying a `sk-SECRET-do-not-leak` sentinel and assert it never reaches `Result.error` (both adapters).
- **AD-1 held:** the new `openai` import lives only in `broker/`; import-linter "core is LLM-free" KEPT.

### File List

- `pyproject.toml` (modified — +`openai==2.42.0` broker dep; `addopts = "-m 'not live'"`; `live` marker; `uv.lock` updated)
- `shelldon/broker/anthropic_provider.py` (new — renamed from `glm.py`; `GLMProvider`→`AnthropicProvider`, pure config-in, error text redacted)
- `shelldon/broker/glm.py` (deleted — superseded by `anthropic_provider.py`)
- `shelldon/broker/openai_provider.py` (new — OpenAI-compatible adapter: Ollama/OpenAI/OpenRouter/Groq/Cerebras/NVIDIA/Mistral/GitHub/Gemini)
- `shelldon/broker/chain.py` (new — config-driven ordered chain builder; `_OPENAI_COMPAT` preset table + Anthropic-format/Ollama specials)
- `shelldon/broker/service.py` (modified — `run_broker` holds the chain, runs the primary; 2.2 fallback seam documented)
- `tests/test_anthropic_provider.py` (renamed from `test_broker_glm.py`; updated for `AnthropicProvider` + transient-mapping case)
- `tests/test_openai_provider.py` (new — OpenAI adapter unit tests)
- `tests/test_chain.py` (new — ordering/reorder/unknown-preset/missing-cred + per-preset build + LiteLLM-prefix strip)
- `tests/test_broker_creds.py` (modified — adapter rename + credential-hygiene leak tests for both adapters)
- `tests/test_provider_live_smoke.py` (renamed from `test_glm_live_smoke.py`; per-test gating + Ollama + Gemini-via-OpenAI live smokes)
- `tests/test_broker_bus.py`, `tests/test_end_to_end_turn.py`, `tests/test_endurance_soak.py` (modified — pass a one-element chain to `run_broker`)

### Review Findings

- [x] [Review][Decision] GLM default model `"glm-4.6"` vs live-tested `"glm-4.7"` [chain.py:27] — **RESOLVED:** `_glm()` fallback default bumped to `"glm-4.7"`.
- [x] [Review][Patch] `_ollama` URL corruption — double `/v1` append [chain.py] — **RESOLVED:** append is now idempotent (skips if path ends with `/v1` OR contains `/v1/`); regression test `test_ollama_base_with_v1_midpath_not_double_appended`.
- [x] [Review][Patch] `run_broker` NameError in `finally` [service.py:64-70] — **REJECTED (false positive):** `connect()` (line 66) is OUTSIDE the `try` (line 67). If it raises, the `try/finally` is never entered, so `writer.close()` never runs and `writer` is never referenced — no NameError is possible. Verified against the current code; no change made.
- [x] [Review][Patch] `AnthropicProvider.complete()` AttributeError on `b.text` [anthropic_provider.py] — **RESOLVED:** now `getattr(b, "text", "")`.
- [x] [Review][Patch] `build_chain` case-sensitive preset lookup [chain.py] — **RESOLVED:** preset names normalized with `.strip().lower()`; test `test_preset_names_are_case_insensitive`.
- [x] [Review][Patch] No test for `_claude` preset — **RESOLVED:** `test_claude_preset_builds_with_key` + `test_claude_preset_missing_key_raises`.
- [x] [Review][Patch] No test for `anthropic.APIStatusError` 4xx/5xx — **RESOLVED:** `test_status_5xx_is_transient` + `test_status_4xx_is_permanent` (anthropic).
- [x] [Review][Patch] No test for `openai.APIStatusError` 4xx/5xx — **RESOLVED:** `test_status_5xx_is_transient` + `test_status_4xx_is_permanent` (openai).
- [x] [Review][Defer] `OpenAIProvider.complete()` — `resp.choices[0].message = None` → AttributeError (SDK shouldn't return this; guard is theoretical) [openai_provider.py:52] — deferred, pre-existing SDK contract
- [x] [Review][Defer] `build_chain` catches only `RuntimeError` — other exception types from builders lose the preset-name context prefix [chain.py:106-109] — deferred, all known builder errors are RuntimeErrors
- [x] [Review][Defer] Missing model env var error doesn't name which env var to set (usability gap, not a functional bug) — deferred, usability improvement
- [x] [Review][Defer] Duplicate preset names in `PROVIDER_CHAIN` silently builds duplicate providers — `"glm,glm"` wastes a retry slot in Story 2.2 [chain.py:101-109] — deferred, Story 2.2 concern

## Change Log

- 2026-06-17 — Story 2.1 implemented: provider abstraction + config-driven ordered chain. Generalized the Anthropic-format adapter (GLM+Claude), added the OpenAI-compatible adapter (Ollama/OpenAI/OpenRouter) and `build_chain` (`PROVIDER_CHAIN`, reorder=config). Broker holds the chain, runs the primary (fallback seam → 2.2). Resolved the 1.4 credential-leak deferral by redacting SDK error text to the type name. Status → review.
- 2026-06-17 — **Gemini folded into 2.1** (owner request; was deferred): first as a native `GeminiProvider` (`google-genai`), then **simplified to its OpenAI-compatible endpoint** as a preset (native adapter + `google-genai` dep removed) — no third SDK. Also made live smokes opt-in (`addopts = "-m 'not live'"`) after a key in the ambient shell caused a live test to run during the normal suite. Verified live (GLM, Ollama, Gemini-via-OpenAI). Suite 112 passed / 2 skipped / 3 deselected; contracts kept.
- 2026-06-17 — **Added free-tier OpenAI-compatible presets** (owner request): `groq`, `cerebras`, `nvidia`, `mistral`, `github`, `gemini` — data-driven rows in `chain.py`, no new code/SDK per provider. Set `{NAME}_API_KEY`/`{NAME}_MODEL` in `.env` and add to `PROVIDER_CHAIN`. Suite 114 passed / 2 skipped / 3 deselected; contracts kept.
- 2026-06-17 — **Addressed code review:** 7 patches resolved (idempotent Ollama `/v1`, `getattr` guard on Anthropic `b.text`, case-insensitive presets, glm-4.7 default, + `_claude`/anthropic-status/openai-status tests) and 1 finding rejected as a false positive (`run_broker` finally — `connect()` is outside the `try`). 4 lower-priority items deferred. Suite 122 passed / 2 skipped / 3 deselected; contracts kept. Status → review.
