# Epic 10 — persona caching findings (Story 10.5, 2026-06-25)

Short field notes on making the always-injected persona cheap to re-send every turn. The persona
is a stable prompt PREFIX re-sent on every turn (stateless LLM + ephemeral fork worker — no
model-side persistence is possible, AD-3), so prompt caching is the only lever to make it cheap.

## What the byte-stable prefix (AC1) buys, per surface

The persona prefix is `BOT_INSTRUCTIONS → DIRECTIVE → IDENTITY → SOUL → USER → about` (plus the
blank-USER onboarding block), composed purely from static file text — no `now()`/UUID/unsorted-dict/
hostname/PID interpolated anywhere. All volatile content (lazy-load reference docs, knowledge,
summary, recent window, recall, owner message) stays strictly AFTER it (AD-6 order). A guard test
(`test_persona_prefix_has_no_timestamp_or_uuid`) now fails if a future change drops a per-request
value into the prefix — the "silent cache invalidator" from design §6.

- **OpenAI-shape surface** (`openai_provider.py` — drives `openai`/`gemini`/`groq`/`cerebras`/
  `mistral`/`openrouter`/`nvidia`/`github`/`ollama`): mostly AUTOMATIC prefix caching (OpenAI ≥1024
  tok ~50% read discount; Gemini 2.5 implicit). **No request field — AC1 fully covers it.** `ollama`
  local is free anyway. Nothing was sent here and nothing needs to be.
- **Anthropic-shape surface** (`anthropic_provider.py` — drives `claude` + `glm`): native Claude
  also AUTO-caches a stable token prefix to a degree, but the explicit `cache_control` breakpoint is
  what guarantees the 5-min-TTL prefix cache (write 1.25×, read ~0.1×). See AC2 below.

## Anthropic `cache_control` breakpoint (AC2) — DEFERRED, with the cheap win shipped

**Shipped:** per-turn cache-signal logging in `anthropic_provider.py` (`_log_cache_usage`, both the
`complete` and `complete_with_tools` paths) — logs `usage.cache_creation_input_tokens` /
`usage.cache_read_input_tokens` at INFO under `shelldon.broker`, `getattr`-guarded so GLM/z.ai
omitting the fields logs nothing and never crashes. This is the only way to SEE whether the prefix
is caching, and the signal the owner's live check reads.

**Deferred:** the explicit `cache_control: {type:"ephemeral"}` breakpoint. The obstacle (design §
"KEY OBSTACLE"): both egress paths send the persona embedded INSIDE a single content string
(`messages=[{"role":"user","content":prompt}]` / the first `Message.content`). Anthropic attaches
`cache_control` to a content BLOCK, so caching the prefix requires splitting it into a separate
block from the volatile remainder — which needs a stable boundary marker the worker emits between
prefix and volatile. That marker would have to be **stripped by every provider surface** (an
OpenAI-shape adapter would otherwise send the literal sentinel to the model), so it is a
cross-cutting worker→broker→all-adapters contract change, not a local Anthropic tweak. That exceeds
the story's timebox and the design explicitly allows the defer.

**Why the defer is safe (no silent cap):** the byte-stable prefix already gives free OpenAI-surface
prefix caching AND native-Claude automatic prefix caching. For GLM specifically, if it does not
cache, the fallback is lazy-load (AC3, heavy reference docs only when relevant) + the Story 5.2
credit budget — never a silent token truncation. Recorded in `deferred-work.md`.

## GLM / z.ai `cache_control` passthrough — owner live-check pending

z.ai exposes an Anthropic-compatible proxy; Zhipu has native context caching, but whether the proxy
forwards `cache_control` (and surfaces `usage.cache_*` fields) is **unverifiable in CI** (no live
LLM). The per-turn logging above means the owner's next live turn against GLM will reveal it: if the
log shows non-zero `cache_read_input_tokens`, GLM caches; if the fields are absent, it does not and
the budget/lazy-load posture stands. Follow the 8.0 live-smoke model — the owner runs the paid call.

## Lazy-load reference docs (AC3)

`TOOLS.md` / `ARCHITECTURE.md` ported as owner-editable seeds (NOT `rewrite_*` targets). A pure
keyword matcher (`_needs_reference`) injects each only when the owner message is about the bot's
tools/internals, AFTER the cached prefix. An ordinary message reads neither — tokens spent only when
relevant. `VAULT.md` deliberately NOT ported (shelldon's `vault/` is the OS-isolated secret store).

## Pi migration (AC4) — no script change needed

`deploy/setup-pi.sh` git-clones the repo and runs `uv run python -m shelldon` from
`WorkingDirectory=$REPO`, so the persona dir is present as repo files at runtime; `CuratedMemory`
seeds copy-if-absent at boot, leaving any existing owner files byte-untouched. Verified: all 9 seeds
are git-tracked (so the clone carries them — the 10.4 untracked-seed lesson) AND present in the
built wheel (`test_packaging.py`). No `pyproject.toml` force-include needed — hatchling already ships
non-`.py` data under `packages=["shelldon"]`.

## Recommended budget posture

Keep AC1 (byte-stable prefix) + AC3 (lazy-load) as the cost floor regardless of provider. After the
owner's live GLM check: if GLM caches → no action; if it does not → lean on lazy-load + the 5.2
budget and consider revisiting the explicit breakpoint only if persona-prefix tokens dominate the
GLM bill in practice.
