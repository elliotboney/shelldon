# Epic 10 — Persona-as-Files (the soul lives in the worktree, never hardcoded) — Design

- **Status:** draft (design approved with Elliot 2026-06-25; pending BMAD epic/story breakdown)
- **Date:** 2026-06-25
- **Author:** Elliot (designed with Claude)
- **Origin:** Elliot caught the miss directly — *"in v1 and openclaw, I wanted a core directory where the bot markdowns lived, and that's what gets passed along with prompts, never hardcoded."*
- **Planning home:** BMAD (`epics.md` + per-story dev loop) — mirrors the Epic 9 flow

---

## 1. Context & problem

shelldon's character is a **hardcoded Python constant**, `SYSTEM_INSTRUCTION` (`worker/prompt.py:45`). The line *"You are shelldon, a small AI pet…"* is welded into source alongside the machine protocol (the `THOUGHT:`/`FACE:` lines, the `ops` JSON formats, tool-use copy). Changing who shelldon **is** means editing code and redeploying.

This is the exact thing the v1/openclawgotchi design avoided. In v1:

- A repo `templates/` dir shipped the bot's markdown soul: `BOT_INSTRUCTIONS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`, `HEARTBEAT.md`, plus reference (`TOOLS.md`, `ARCHITECTURE.md`, `BOOT.md`, `BOOTSTRAP.md`, `VAULT.md`).
- On first run, `shutil.copytree(templates_dir, WORKSPACE_DIR)` seeded a **writable worktree** (`.workspace/`).
- The prompt loader (`src/llm/prompts.py`) read the worktree (falling back to `templates/`) and composed every prompt from those files.
- The bot **evolved its own soul** at runtime via `write_file()` on `SOUL.md`/`IDENTITY.md`/`USER.md`.

shelldon already has *part* of this pattern: `gather_context` reads `DIRECTIVE.md` + `about.md` from `~/.shelldon/memory/` and injects them (`worker/prompt.py:131-134`), and the bot rewrites `about.md` via the `rewrite_about` memory-op. The miss is that the **core character never got a file**, and there is no `SOUL`/`IDENTITY`/`USER` layer, no seed-on-boot of persona templates, and the proactive/dream prompts are *also* hardcoded (`core/proactive.py`).

This epic moves the full persona into the writable worktree, spine-correctly.

### Decisions made (2026-06-25)

| Decision | Choice |
|---|---|
| What moves out of code | **Everything** — character AND machine protocol go to markdown files; nothing persona-shaped stays hardcoded |
| Where files live | The **deployed read/write area** (the v1 "worktree") — shelldon's `~/.shelldon/memory/` tree |
| Who can write them | **The bot**, via the spine's op path (NOT raw `write_file` — see §3 tension). **Decision 2026-06-25: ALL persona files bot-writable, incl. `BOT_INSTRUCTIONS.md`** (its own machine contract) — guarded, see §3. |
| File set | **Mirror v1** — `BOT_INSTRUCTIONS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`; map the rest onto existing constructs; **do NOT port `VAULT.md`** (name collision) |
| `DIRECTIVE.md` writability | **Bot-editable, owner-approval-gated** (decision 2026-06-25) — bot proposes `rewrite_directive`, owner Approves/Denies via the **Epic 9.3 Telegram flow** before core applies. Not autonomous, not locked off. |
| Onboarding | **Required** — a first-run conversation populates `SOUL`/`IDENTITY`/`USER` (the v1 `BOOTSTRAP.md` flow). This is the mechanism that creates the "USER" part. |
| Planning home | **BMAD**, 5 stories |

---

## 2. Goals / non-goals

**Goals**
- The system instruction, soul, identity, and owner-profile are **markdown files in the worktree**, injected into prompts — never hardcoded.
- Repo ships **pristine seed templates**; first boot copies any missing file into the writable runtime tree (the v1 `copytree` pattern, made idempotent / copy-if-absent).
- The bot can **evolve its own persona** (`SOUL.md`/`IDENTITY.md`/`USER.md`) at runtime — through core's single-writer op path, preserving AD-5.
- The bot is **aware of its self-knowledge files** (the prompt explains what they are) and **edits them autonomously — no chat instruction required** — when it learns something durable, primarily on the dream cycle. (Closes a latent gap: the current prompt doesn't even advertise the existing `rewrite_about` op.)
- The bot can edit **`DIRECTIVE.md` too, but only with owner approval** — proposed via `rewrite_directive`, confirmed through the Epic 9.3 Telegram Approve/Deny flow. Not locked off, not autonomous.
- A **first-run onboarding** conversation fills `IDENTITY`/`SOUL`/`USER` from the owner, then steps aside.
- The proactive/dream prompt copy moves to files too (no hardcoded LLM-facing prose left).
- **Persona is re-sent every turn (forced by stateless LLM + ephemeral fork worker) but cheaply** — investigate provider prompt-caching of the stable persona prefix so the repeated tokens aren't re-paid each turn.
- Every spine invariant holds (LLM-free core, single-writer, disjoint writers, fail-soft, atomic writes, OS-enforced `vault/` exclusion).

**Non-goals**
- Porting v1's `VAULT.md`. shelldon's `vault/` is the **OS-enforced secret store the worker can never read** (AD-6) — the opposite of v1's knowledge-capture vault. The knowledge-capture concern is already covered by `facts/`/`people/` + the dream cycle.
- A general templating engine. v1 used `{{BOT_NAME}}`-style placeholders; shelldon fills persona by **onboarding conversation + bot self-rewrite**, not string substitution.
- Multi-user owner profiles. Single-owner behind `ALLOWED_USERS` (AD-13 keeps the schema add non-breaking for later).
- Letting the LLM `write_file()` directly. That is a v1 affordance the spine deliberately replaced with the op path.

---

## 3. Architecture

### The file model (mapped onto the spine, AD-6)

The worktree = the existing curated markdown tree at `~/.shelldon/memory/`. Persona files slot into AD-6's **three writer categories**, so single-writer (AD-5) holds because writer sets stay disjoint:

| File | Writer category (AD-6) | Writer | Injected in prompt? |
|---|---|---|---|
| `BOT_INSTRUCTIONS.md` | bot-owned curated (seeded once) | **bot** via `rewrite_instructions` op (guarded, §3) + owner may hand-edit on disk | always (replaces `SYSTEM_INSTRUCTION`) |
| `SOUL.md` | bot-owned curated | **bot** via `rewrite_soul` op | always |
| `IDENTITY.md` | bot-owned curated | **bot** via `rewrite_identity` op | always |
| `USER.md` | bot-owned curated | **bot** via `rewrite_user` op (filled at onboarding) | always |
| `about.md` | bot-owned curated *(exists)* | bot via `rewrite_about` | always *(exists)* |
| `DIRECTIVE.md` | owner-owned *(exists)* | owner (direct) **+ bot via `rewrite_directive`, owner-approval-gated** (§3) | always, first/authoritative *(exists)* |
| `facts/`,`people/` | bot-owned curated *(exists)* | bot via `remember` | bounded surface *(exists)* |

`MEMORY.md` and `USER.md` from v1's set are **not duplicated wholesale**: v1's `MEMORY.md` (curated long-term) is already `about.md` + `facts/`; only the **owner-profile** slice (`USER.md`) is a genuine gap, so it becomes a new bot-owned file.

### Seam changes (bounded)

- **`worker/prompt.py`** — `SYSTEM_INSTRUCTION` constant is **deleted**; `gather_context` reads `BOT_INSTRUCTIONS.md` (system slot) + `SOUL.md` + `IDENTITY.md` + `USER.md` from the memory root via `CuratedMemory`, fails-soft exactly like the current `about.md`/`DIRECTIVE.md` reads (a missing/corrupt file degrades, never raises). `assemble_prompt` gains the new sections in a defined AD-6 order (see below). Char-budgeted like the existing `knowledge` surface so a runaway file can't blow the 416MB context.
- **`core/memory.py` (`CuratedMemory`)** — gains `read_soul/read_identity/read_user` accessors (mirror `read_about`) and `_apply_rewrite_soul/identity/user` (mirror `_apply_rewrite_about`, atomic temp+rename). Gains a **seed-on-init** step: copy any missing persona file from the shipped templates into the root (mirrors `faces._seed_document`'s absent→seed behavior).
- **`contracts/__init__.py`** — three new `MemoryOp` variants: `RewriteSoul`, `RewriteIdentity`, `RewriteUser` (each frozen, tagged, mirror `RewriteAbout`; join the `MemoryOp` union so they route through `apply_memory_op`). No new op *machinery* — same dispatch path.
- **`core/proactive.py`** — the hardcoded `_PROACTIVE_PROMPT` / dream prompt copy moves to seed files (`HEARTBEAT.md` / `DREAM.md`) read at build time; the `{feeling}` weave stays a pure fill over file-loaded text.
- **Repo** — new `shelldon/persona/` dir holds the shipped seed `.md` templates. **`deploy/setup-pi.sh`** ships them; runtime copy-if-absent covers the already-deployed Pi without a manual migration.
- **Worker system prompt** — the protocol copy (THOUGHT/FACE/ops/tool formats) now lives in `BOT_INSTRUCTIONS.md`. `parse_reply` is unchanged; the contract it depends on is now seeded-from-file instead of constant-from-code.

### The central tension (writable-by-bot vs read-only worker)

Elliot's requirement: persona files **writable by the bot**. But the spine forbids the v1 mechanism: **the worker is read-only to memory; core is the sole writer** (AD-5/AD-6). v1 let the LLM call `write_file()` directly — shelldon replaced that with the **broker-gated op path** (the bot already rewrites `about.md` only by *proposing* a `rewrite_about` op that core validates and applies atomically).

**Resolution:** "bot writes its own soul" = the bot emits `rewrite_soul`/`rewrite_identity`/`rewrite_user` ops; **core applies them**. Same felt outcome (the bot evolves its persona at runtime), zero spine violation. This is a pure extension of the existing `rewrite_about` pattern — the lowest-risk part of the epic.

**`BOT_INSTRUCTIONS.md` is fully bot-writable (decision 2026-06-25), with a guardrail.** Elliot chose to let the bot evolve its *entire* prompt, including the machine protocol. The risk: a self-edit that drops the `FACE:`/`ops` contract could break `parse_reply` until a fix. Two-layer mitigation:
1. **Validate-on-apply:** `_apply_rewrite_instructions` rejects (logs, no-op) a rewrite that drops the required protocol markers (the `THOUGHT:`/`FACE:` directives + the `ops`-fence instruction). The bot can rephrase its character freely but cannot delete the contract tokens `parse_reply` needs.
2. **Always-available recovery:** the pristine seed lives in the repo (`shelldon/persona/BOT_INSTRUCTIONS.md`); `parse_reply` already fails-soft (missing `FACE:` → default face), so even a degraded instructions file never crashes a turn, and re-seeding restores the contract.

### `DIRECTIVE.md` — bot-editable, owner-approval-gated (decision 2026-06-25)

The owner's constitution is no longer owner-only: the bot MAY propose changes to it, but **every bot write requires owner approval** — not autonomous like the other persona files. Mechanism: `rewrite_directive` is modeled as a **RISKY-tier action**, so it rides the **Epic 9.3 approval flow verbatim** (the bot proposes → core emits `RequestToolApproval` with a human-readable summary → Telegram inline **Approve / Deny** keyboard → on Approve, a resumed turn applies the rewrite; on Deny, skip). No new approval plumbing — 9.3 already ships the two-phase resumable flow + pending-approvals table.

Two consequences for the spine: (1) DIRECTIVE now has **two writers** (owner-direct on disk + core-on-approval), but the gate keeps the owner authoritative on every write — single-*authority* holds even though single-*writer* relaxes; an unapproved directive change can never land. (2) DIRECTIVE edits are offered on **chat turns only** (where the owner is present to approve), **never in the unattended dream** — the dream may autonomously edit SOUL/IDENTITY/USER/about but is structurally barred from proposing a directive change. This keeps the constitution from drifting while you're not looking.

### Prompt assembly order (AD-6 is binding)

Current: `system → directive → about → knowledge → summary → recent → recall → owner_message`.

Proposed: `BOT_INSTRUCTIONS (system) → DIRECTIVE (authoritative) → IDENTITY → SOUL → USER → about → knowledge → summary → recent → recall → owner_message`.

Rationale: identity/soul/user are stable self-and-owner context, placed right after the owner's authoritative directive and before the volatile memory/recall layers, so persona shapes every reply. All sections still omit-if-empty (no empty headers), owner message always last.

---

## 4. The five stories (detailed)

### 10.1 — Persona files + seed-on-boot + prompt reads them (the foundation)

Create `shelldon/persona/` seed templates (`BOT_INSTRUCTIONS.md` carrying today's `SYSTEM_INSTRUCTION` text verbatim, plus starter `SOUL.md`/`IDENTITY.md`/`USER.md` ported from v1). `CuratedMemory` seeds any missing file into the memory root on init. `gather_context` reads them; `assemble_prompt` injects them in the new order; **delete the `SYSTEM_INSTRUCTION` constant**. Fail-soft: any missing/corrupt file degrades (worst case → owner message only), logged, never raised.

**Testing:** unit-test seed-on-absent (empty root → files appear), seed-skip-on-present (existing file untouched), assembly order with all sections, degrade path (corrupt `SOUL.md` → omitted + logged). Golden-test that the assembled prompt with seed files equals the prior hardcoded prompt (no behavior change on day one).

### 10.2 — Bot-writable persona via memory-ops

Add `RewriteSoul`/`RewriteIdentity`/`RewriteUser`/`RewriteInstructions` to `contracts/` (mirror `RewriteAbout`), wire `_apply_rewrite_*` in `CuratedMemory`, and add the read accessors. `_apply_rewrite_instructions` carries the **validate-on-apply guardrail** (§3): reject + log a rewrite that drops the required protocol markers, so the bot can re-voice but not break its own parse contract. These apply **autonomously** (core sole writer, atomic temp+rename).

**`rewrite_directive` is the gated exception** (§3): it does NOT apply autonomously — it's a RISKY-tier action routed through the **Epic 9.3 approval flow** (propose → `RequestToolApproval` → Telegram Approve/Deny → resumed turn applies on Approve). `_apply_rewrite_directive` exists in `CuratedMemory` but is reachable only from the approved-resume path, never the autonomous op-apply path. This is the only story-10.2 piece that depends on Epic 9 (done) rather than being self-contained.

**Awareness is part of this story (the bot must KNOW the files exist and that it may edit them — Elliot's requirement).** `BOT_INSTRUCTIONS.md` gains an explicit **"Your self-knowledge files"** section (v1 parity — v1 had it, shelldon dropped it) that: (a) names each file and explains what it is (`SOUL` = voice/values, `IDENTITY` = who/hardware/mission, `USER` = what you know about your owner, `about` = your running self-summary); (b) states the bot MAY rewrite them via the ops, **with no chat instruction required**, when it learns something durable about itself or its owner. This also closes a **latent gap found 2026-06-25**: the current prompt never advertises even the existing `rewrite_about` op (`prompt.py:81` is a code comment, not prompt text), so today's about.md self-rewrite is unreachable by the model. The bot does **not** need a read tool for these — they are injected into every prompt (10.1), so it always sees their current content in-context; the file-tool jail (`~/.shelldon/workspace/`) is deliberately NOT widened to the memory root, keeping `vault/` and the curated tree off the tool surface.

**Testing:** apply each op → file written atomically; malformed op rejected; round-trip via `parse_reply` → core apply → `read_soul` returns new content. Crash-safety test (interrupted rename leaves prior file intact, per M0). **Guardrail tests:** a `rewrite_instructions` dropping the `FACE:`/`ops` tokens is rejected (file unchanged, logged); a valid re-voice is applied. **Awareness test:** the assembled `BOT_INSTRUCTIONS.md` advertises all rewrite ops incl. `rewrite_about` (golden-string assertion so the copy can't silently regress). **Directive-gate tests:** a `rewrite_directive` proposal does NOT apply autonomously — it emits `RequestToolApproval`; Approve → `read_directive` returns new content; Deny → file unchanged (drive with the fake transport from 9.3, no live LLM).

### 10.3 — Proactive & dream prompts move to files

Port `core/proactive.py`'s hardcoded prompt copy into seed templates (`HEARTBEAT.md` for the proactive check-in, `DREAM.md` for the dream cycle), read at build time. The `{feeling}` weave and pending-learnings injection stay pure fills over file-loaded text. No hardcoded LLM-facing prose left anywhere.

**This story carries the autonomous-edit trigger (Elliot's "without direct instruction from chat").** The dream cycle already promotes facts and (per 10.2) can rewrite about.md; extend the `DREAM.md` prompt so that, while reflecting, the bot also **reviews and updates SOUL / IDENTITY / USER when it has learned something durable** — e.g. a stable owner preference → `rewrite_user`, an evolved trait → `rewrite_soul`. These fire as ordinary proposed ops that core applies (AD-5), on the scheduled dream turn, with no chat prompt. The dream is the right home (introspective, low-frequency, already the place self-knowledge is consolidated) rather than the hot path, so the persona doesn't churn every turn.

**Testing:** proactive prompt built from file == prior constant; missing file → safe fallback (degrade to a minimal built-in, logged); feeling-weave still correct with file source. **Autonomous-edit test:** a scripted dream turn that surfaces a durable owner preference emits a `rewrite_user` op and core applies it (fake provider, no live LLM) — proves the no-chat-instruction path end to end.

### 10.4 — First-run onboarding (creates SOUL/IDENTITY/USER, incl. the "USER" part)

A first-boot conversational flow (v1 `BOOTSTRAP.md`): when the persona files are still at their seed defaults, the bot runs a short warm onboarding (2-3 turns) asking the owner who they are and who shelldon should be, then emits `rewrite_identity`/`rewrite_soul`/`rewrite_user` ops to populate them. A sentinel (e.g. a seeded `BOOTSTRAP.md` or an `onboarded` flag in personality-state) marks completion so it never re-runs. This is the mechanism that creates the **USER** profile.

**Testing:** fresh worktree → onboarding triggers; after the ops apply, `USER.md`/`SOUL.md`/`IDENTITY.md` hold owner answers and the sentinel flips; second boot → no onboarding. Drive with a scripted fake provider (no live LLM).

### 10.5 — Cost (prompt caching + lazy-load), reference files, and Pi migration (woven)

**Why we re-read + re-send every turn (the cost premise).** The LLM is stateless (each `provider.complete` is a one-shot; nothing persists model-side) and the worker is an **ephemeral fork that dies after each turn** (AD-3 — the choice that killed v1's OOM). So the prompt is rebuilt from scratch every turn by design; there is no in-process cache to lean on, and persona text MUST be in every request or the model loses character + the parse contract. The disk read is trivial (KB); the real cost is re-sending ~3K persona tokens per turn under the credit budget (Story 5.2). The fix is not persistence (impossible) but **caching the stable prefix**:

- **Prompt caching (the cost lever).** The persona block (`BOT_INSTRUCTIONS`+`IDENTITY`+`SOUL`+`USER`+`DIRECTIVE`) is a stable prompt **prefix** — the ideal cache candidate. shelldon has only **two provider adapters** (`broker/anthropic_provider.py`, `broker/openai_provider.py`), so caching is a **2-surface problem, not per-preset**:

  | Surface | Presets it drives | Caching mechanism | shelldon action |
  |---|---|---|---|
  | **Anthropic** (`anthropic_provider.py`) | `claude`, **`glm`** (z.ai `/api/anthropic` endpoint) | **Explicit** `cache_control: {type:"ephemeral"}` breakpoints (max 4, prefix-match, 5-min TTL default / 1h opt-in; write 1.25×, read ~0.1×; min cacheable 2048–4096 tok by model) | Set ONE `cache_control` breakpoint on the last stable persona block. Native Claude honors it. **z.ai/GLM passthrough is the spike** — GLM/Zhipu has native context caching, but whether the Anthropic-compat proxy forwards `cache_control` is unverified. |
  | **OpenAI** (`openai_provider.py`) | `openai`, `gemini`, `groq`, `cerebras`, `mistral`, `openrouter`, `nvidia`, `github`, `ollama` | Mostly **automatic** prefix caching (OpenAI ≥1024 tok, ~50% read discount; Gemini 2.5 implicit). No request field. | **Nothing to send** — just keep the persona a real, byte-stable prefix (the assembly-order rule). Free where supported, no-op where not (`ollama` local = free anyway). |

  **The single highest-leverage move is the prefix-ordering itself** (persona first, byte-stable, volatile content last) — it makes the OpenAI surface cache for free AND positions the Anthropic surface for the breakpoint. The breakpoint + z.ai spike is the incremental Anthropic-surface win. Both live in the broker (egress boundary, AD-2). Verify each with the provider's cache-hit signal (`usage.cache_read_input_tokens` / `prompt_tokens_details.cached_tokens`); log the per-provider result (no silent assumption).
- **Lazy-load the heavy reference docs.** Port v1's `TOOLS.md`/`ARCHITECTURE.md` as seed files, loaded **only on keyword** (v1's `needs_extra_context`), so they cost tokens only when the owner asks about hardware/internals. The small core persona stays always-on.
- **Pi migration.** Update `deploy/setup-pi.sh` to ship `shelldon/persona/`. Confirm the live Pi picks up new files via copy-if-absent on next boot (existing `about.md`/`facts/` untouched). **Do NOT port `VAULT.md`.**

**Testing:** keyword triggers lazy-load; non-matching message omits the heavy docs; the persona prefix is positioned for caching (and, if GLM supports it, a cache-hit is exercised against a recorded provider response — no live LLM); setup-pi.sh dry-run ships the dir; copy-if-absent leaves a populated root intact.

---

## 5. Invariants preserved

| Invariant | How it holds |
|---|---|
| Core is LLM-free (AD-1, import-linter) | Persona is data read by the worker; core only applies ops + seeds files. No model code in core. |
| Single-writer per resource (AD-5) | All file writes go through **core** applying an op — autonomously for the bot-owned files, on owner-Approve for `DIRECTIVE`. The owner's optional hand-edit is out-of-band. Core remains the sole runtime writer. |
| Disjoint-writer memory model (AD-6) | `SOUL`/`IDENTITY`/`USER`/`about`/`BOT_INSTRUCTIONS` = bot-owned (core applies autonomously); `DIRECTIVE` = owner-authoritative, bot writes only via the approval gate (§3) — single-*authority* preserved (no unapproved bot write lands) even though single-*writer* relaxes. |
| Worker reads memory read-only (AD-6) | The worker only *reads* persona files for prompt assembly; it *proposes* ops, never writes. |
| Atomic markdown writes / crash-safe (AD-6, M0) | `_apply_rewrite_*` use temp+rename, same as `rewrite_about`. |
| `vault/` OS-unreadable to worker (AD-6) | Unchanged. `VAULT.md` is **not** ported; persona files live in the worker-readable tree only. |
| Fail-soft (4.1/parse_reply discipline) | Missing/corrupt persona file → section omitted + logged, turn proceeds. Seed-on-absent never overwrites. |
| Fork = no accumulation (AD-3/AD-9) | Files are read fresh each fork; no new resident state. |

---

## 6. Risks & open questions

- **`BOT_INSTRUCTIONS.md` bot-writability — RESOLVED 2026-06-25: fully bot-writable** via `rewrite_instructions`, guarded by validate-on-apply (§3) + the pristine repo seed as recovery. Residual risk: a *valid-but-bad* re-voice (keeps the contract tokens but degrades reply quality) is possible; acceptable for single-owner — re-seed or hand-edit recovers.
- **Prompt bloat / per-turn token cost on a 416MB Pi.** Always-injecting the persona grows every prompt, and it's re-sent every turn (stateless LLM + ephemeral worker — §10.5). Mitigations, in order: (1) **prompt caching** of the stable persona prefix — near-zero repeat cost if GLM supports it (10.5 spike); (2) char-budget each file (reuse `KNOWLEDGE_CHAR_BUDGET`); (3) lazy-load the heavy reference docs; (4) lazy-load even SOUL/IDENTITY on keyword (v1's fallback) only if cost still bites.
- **Caching is two surfaces, not one.** Anthropic-shape (`claude`/`glm`) needs an explicit `cache_control` breakpoint; OpenAI-shape (everything else) caches automatically on a stable prefix. Mitigation: the prefix-ordering rule serves both; the explicit breakpoint is added in `anthropic_provider.py` only. **z.ai/GLM `cache_control` passthrough is the one unknown** — 10.5 timeboxes that spike; if z.ai drops it, the OpenAI-surface providers and native Claude still cache, and GLM falls back to lazy-load + budget (logged, no silent cap).
- **Cache-prefix byte-stability (silent invalidator).** Any per-request byte change *inside* the persona prefix (a timestamp, a UUID, an unsorted dict) invalidates the cache for every provider. Mitigation: keep the persona prefix frozen — never interpolate `now()`/IDs into `BOT_INSTRUCTIONS`/`SOUL`/`IDENTITY`/`USER`; put volatile content (recent window, owner message) strictly after it (the assembly order already does this). Verify with the cache-hit counter in the 10.5 spike.
- **Onboarding without a live LLM in tests (10.4).** Same constraint as Epics 6/9 — the *flow/apply mechanism* is testable with a fake provider; real-model onboarding quality is unverifiable in CI, verified only in the live smoke on the Pi.
- **Live-Pi migration.** The deployed Pi already has a populated `~/.shelldon/memory/`. Copy-if-absent is safe, but confirm seed files don't shadow an owner who already hand-wrote a `DIRECTIVE.md`. (They won't — disjoint names — but verify in 10.5.)
- **Behavior drift on day one.** Moving the constant to a file must be a *no-op* for replies. 10.1's golden test (assembled prompt == prior prompt) is the guard.

---

## 7. Sequencing

Build order: **10.1 → 10.2 → 10.3 → 10.4**, with **10.5 woven through** (reference files land with 10.1's seed mechanism; the prompt-caching spike runs once 10.1 fixes the persona prefix shape; setup-pi.sh + migration verified last). 10.1 is the actual fix (persona in files, constant deleted) and the first felt win. 10.2 makes the soul evolvable. 10.3 finishes the "no hardcoded prose" goal. 10.4 delivers the owner-profile (USER) via onboarding — the part Elliot called out.

**Next step:** hand this design to the BMAD epic/story flow to produce Epic 10 in `epics.md` + the five story specs, then run the per-story dev loop ([[shelldon-dev-conventions]]).
