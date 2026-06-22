# Epic 9 — Self-Coding (Live, Tiered) — Design

- **Status:** draft (design approved in brainstorm 2026-06-21; pending BMAD epic/story breakdown)
- **Date:** 2026-06-21
- **Author:** Elliot (brainstormed with Claude)
- **Origin:** field-note item 3 (`field-notes-2026-06-21.md`) + memory `shelldon-self-coding-tools`
- **Planning home:** BMAD (`epics.md` + per-story dev loop) — not superpowers writing-plans

---

## 1. Context & problem

A real-usage field note reported: *"shelldon says it can't code, but a coding tool should be available."* Investigation of both codebases resolved the premise:

- **v2 (shelldon) has no tool capability at all.** The brain is pure text-in/text-out: `provider.complete(prompt) -> str` (`broker/provider.py:17`), broker passes only `model`/`max_tokens`/`messages` (`broker/anthropic_provider.py:36`, `broker/openai_provider.py:34`), and the system prompt advertises only memory ops (`worker/prompt.py:45-69`). The closed op set is `MemoryOp | AddFace | CaptureLearning | ResolveLearning` — zero code ops. **The bot is telling the truth.**
- **v1 (openclawgotchi) had full agentic self-coding** — native litellm function-calling, on by default, 26+ tools incl. `execute_bash`, `write_file`, `check_syntax`, `git_command`, `safe_restart` (`src/llm/litellm_connector.py:881-1119`), in a 100-turn agent loop. It really could write/run/commit its own code.

So this is **not a wiring bug** — it is the deliberately-deferred self-coding feature ([[shelldon-self-coding-tools]]), with the A-vs-B design decision previously left open (`epics.md:765-772`). This design closes it.

### Decisions made (brainstorm 2026-06-21)

| Decision | Choice |
|---|---|
| Scope | **Full v1-parity self-coding, live** — both inline per-turn execution AND promotable persistent named tools |
| Safety posture | **Tiered allowlist** — safe tools run free; risky tools gate via Telegram approval |
| Tool model | **Named tools, each tagged with a safety tier** (not sandboxing raw in-process Python) |
| Call mechanism | **Native function-calling**; the broker normalizes provider tool-call formats; the agentic loop runs inside the fork worker |
| Decomposition | One epic, **five stories**, built sequentially |
| Planning home | **BMAD** |

---

## 2. Goals / non-goals

**Goals**
- Give the brain the ability to call real tools (native function-calling) through the broker.
- Inline, in-the-moment execution for **safe** tasks with zero friction.
- Tiered approval: **risky** ops (writes, shell, network, git) require owner approval over Telegram.
- The bot can **author new persistent tools** (code + test), reviewed by the owner, that join its toolbox and are live on the next turn via fork-reimport.
- Preserve every spine invariant (LLM-free core, broker sole egress, fork = no accumulation, single-writer, fail-soft).

**Non-goals**
- Multi-user. shelldon is single-owner behind the `ALLOWED_USERS` gate; the threat model is "don't let the bot wedge/brick its own Pi," not adversarial isolation.
- A hardened OS-level sandbox for arbitrary Python. Tiering is enforced **at the tool boundary**, not by sandboxing the interpreter (impractical on a 416MB Pi and unnecessary for a single-owner device).
- Credentialed external-API tools in the first pass (broker-side execution per NFR9 is noted as future work).

---

## 3. Architecture

**The agentic loop lives in the worker.** Core stays LLM-free (import-linter enforced); the broker stays a pure egress boundary. The worker already owns both ends of the LLM exchange (`worker/worker.py`) — it gains a bounded loop instead of a single round-trip.

```
core --fork--> worker
                 │  assemble prompt + tool schemas
                 ▼
        ┌──────► Job(prompt, tools) ──► broker ──► provider.complete_with_tools
        │                                              │
        │        Completion(text | tool_calls) ◄───────┘
        │                 │
        │         tool_call? ──no──► parse ops ──► Result ──► core (applies + replies)
        │                 │ yes
        │           tier check
        │            ├─ FREE  ─► execute in worker ─┐
        │            └─ RISKY ─► emit RequestToolApproval, end turn (2-phase)
        └──────────────── feed ToolResult back ──────┘   (loop, still < 25s)
```

### Seam changes (bounded)

- **`broker/provider.py`** — add `complete_with_tools(messages, tools) -> Completion` returning either text or structured tool-calls. Existing `complete()` untouched. The broker normalizes each provider's native tool-call format to/from shelldon contracts, so the worker loop stays provider-agnostic (AD-2). `name` audit label preserved.
- **`broker/broker.py`** — thread `tools` through `handle_job`/`handle_job_chain`, keep retry/chain semantics. **The broker never executes tools** — it only shuttles call requests/results (stays "no pet-domain parsing").
- **`worker/worker.py`** — single round-trip becomes a bounded agentic loop. **FREE-tier tools execute in the worker** (no creds needed). Credentialed tools (future) would execute in the broker per NFR9 — deferred.
- **`worker/prompt.py`** — system instruction gains a short "you have tools" section; tool schemas are passed structurally (not prose), so the brittle text-parsing of v1 is avoided.

### The central tension (why B was deferred)

The worker is `fork → one turn → die in < 25s` (`_COMPLETION_TIMEOUT_S = 25.0`, coherent-timeout invariant W < R < T). **FREE-tier** tools fit inside the loop synchronously. But a **RISKY-tier** tool needs *human approval over Telegram* — async, potentially minutes. **The worker cannot block on it.** Therefore risky ops are a **two-phase resumable flow** (§4, story 9.3), not an in-loop await. This split is the core of the decomposition: free-tier inline is easy and high-value; risky-tier approval is the hard, separable part.

---

## 4. The five stories (detailed)

### 9.1 — Function-calling foundation (the spine)

Extend the provider/broker/worker seam to carry native tool-calls and run the bounded agentic loop. Ships with **one trivial FREE tool** (`get_time`) purely to prove the loop end-to-end.

**New contracts** (`contracts/__init__.py`):
- `ToolSpec(name, description, params_schema, tier, fn)` — frozen, mirrors `PluginManifest` style. `tier ∈ {FREE, RISKY}` (closed enum). `params_schema` is a JSON-schema dict. `fn` is the worker-side callable (not serialized across the bus).
- `ToolCall(id, name, args)` and `ToolResult(id, ok, content)` — the SDK-agnostic tool-call vocabulary the broker normalizes to/from.
- `Completion` gains an optional `tool_calls: tuple[ToolCall, ...]` alongside `payload` (text). A completion is either text (final) or tool-calls (continue the loop).

**Loop** (`worker/worker.py`): assemble prompt + tool schemas → Job → broker → Completion. If `tool_calls`: for each call, look up the `ToolSpec`, run FREE tools, append `ToolResult` to the running message list, loop. If text: parse ops as today, emit `Result`. Bounds: **max 6 iterations** AND the 25s budget is the hard ceiling. Exhaustion/timeout → best-effort reply + logged note (fail-soft, same discipline as `parse_reply`).

**Error handling:** a tool raising is caught and fed back as `ToolResult(ok=False, content=<error>)` — the model recovers, the turn never crashes. A malformed/unknown tool-call name → `ToolResult(ok=False)` "unknown tool", logged.

**Testing:** fake provider scripted to emit a `get_time` tool-call then a text reply; assert the loop executes the tool, feeds the result back, and returns the final text. Unit-test the normalizer with recorded Anthropic + GLM tool-call shapes. No live LLM.

### 9.2 — Free-tier tool pack (inline magic)

The read-only / pure-compute tools that make "it can code" true for safe tasks, all FREE-tier (no approval):
- `read_file(path)` — read within an allowed workspace root (path-jailed; never reads `vault/`, never `.env`).
- `list_dir(path)` — directory listing, same jail.
- `python_eval(code)` — evaluate a pure-compute snippet in a restricted namespace (no `open`/`os`/`subprocess`/`import` of side-effecting modules; CPU/time-bounded). This is the "compute now" path; anything that needs the filesystem/network/shell is a RISKY tool, not this.

**Path jail:** a single `WORKSPACE_ROOT` (e.g. `~/.shelldon/workspace`); all FREE file tools resolve+confirm the real path stays under it (reject symlink escapes). `vault/` and credential files are always denied regardless of tier.

**Testing:** each tool unit-tested for happy path + jail escape rejection + the eval restriction (assert `open`/`import os` inside `python_eval` fail closed).

### 9.3 — Risky-tier + Telegram approval (the hard one)

RISKY-tier tools: `write_file`, `run_shell`, `http_get`, `git`. Each requires owner approval before executing.

**Two-phase resumable flow:**
- New proposed-op kind `RequestToolApproval(call: ToolCall, summary: str)`.
- **Phase 1:** the loop hits a RISKY call → loop pauses, the worker ends the turn emitting `RequestToolApproval` + the user-facing reply ("I want to run X — ok?"). Core persists the **pending agent state** (the message list so far + the pending call) in sqlite, keyed by turn id.
- **Surface:** Telegram transport sends the summary with an **inline keyboard** (Approve / Deny callback buttons).
- **Phase 2:** your tap arrives as a Telegram `callback_query` → routed as an approval signal → a fresh worker is forked that **resumes** from the persisted state: it executes (or skips) the call, appends the `ToolResult`, and continues the loop to a final reply.

**Telegram transport additions** (`transport/telegram.py`) — also clears field-note items 4 & 5:
- `parse_mode` on `sendMessage` (HTML + `<pre>` for tool output) — fixes raw-backtick rendering (item 4).
- Inline keyboards + `answerCallbackQuery` + `callback_query` handling in the long-poll.
- `setMyCommands` on startup + `/cmd` routing (item 5) — the approval callback rides this surface.

**State model:** pending approvals table `(turn_id, created_at, agent_state_blob, expires_at)`. A stale/expired pending approval (e.g. >1h) is dropped with a logged note — never executes. Single-owner, so no per-user scoping needed beyond the existing `ALLOWED_USERS` gate.

**Testing:** drive the state machine with a fake transport — assert a RISKY call parks state + emits the approval op; assert Approve resumes + executes; assert Deny resumes + skips; assert expiry drops. Telegram client faked (as today).

### 9.4 — Persistent self-coded tools (the actual self-coding)

The bot authors a new named tool that joins its permanent toolbox.

**Flow:**
- New proposed-op kind `ProposeTool(name, code, test)` — the model emits a tool implementation **and** its test together (the memory-note requirement under A, kept under B).
- Staged to a **staging dir** (`workspace/tools-staging/`), not live.
- Local CI-style gate runs on the staged pair: `pytest` on the test file (and, for the LLM-free-core contract, `lint-imports` so a tool importing an LLM lib into `core/` is auto-rejected — reuses the existing CI gate shape).
- On pass → Telegram approval (reuse 9.3's inline keyboard) → on Approve, the tool is promoted to the **live tools dir** (`workspace/tools/`).
- **Discovery:** the worker-side tool registry discovers live tools the same way the plugin host discovers plugins (`pkgutil.iter_modules` over the tools dir, manifest/`ToolSpec` convention). Because every turn forks a fresh worker that re-imports, **the next turn picks up the new tool automatically** — no restart (the Python fork-reimport property that motivated the language choice).

**Framing:** self-coded tools are **portable data** (identity-as-data) — they live in the workspace/vault, version with memory, travel across bodies. Core stays pure; the worker is the mutable surface.

**Testing:** propose a trivial tool + passing test → assert it stages, passes the gate, and after approval is discoverable + callable on the next (simulated) fork. Propose a tool whose test fails → assert it's rejected, never promoted.

### 9.5 — Safety hardening (woven through)

- **Quarantine** (faces-registry pattern): a live tool that errors on import or raises on run is **skipped + logged**, never wedges the turn or kills the worker. A repeatedly-bad tool is moved to a `tools-quarantine/` dir.
- **Resource caps:** `python_eval` and `run_shell` get CPU/time/memory bounds (the 416MB Pi must not OOM; reuse the worker's existing RAM discipline).
- **Cost/credit-tier gating:** the agentic loop can make multiple model calls per turn → reuse Story 5.2 cost-tier gating + credit budget so a runaway loop can't burn the budget.
- **Loop ceiling** (from 9.1) is also a safety control (no infinite tool↔model ping-pong).

---

## 5. Invariants preserved

| Invariant | How it holds |
|---|---|
| Core is LLM-free (AD-1, import-linter) | Loop + tools live in the worker, never core. Self-coded tools auto-rejected by `lint-imports` if they touch core. |
| Broker = sole model/tool-cred egress (NFR9) | FREE tools need no creds (run in worker). Credentialed tools = broker-side, deferred. Broker still the only model egress. |
| Broker does no pet-domain parsing (AD-2) | Broker only normalizes provider tool-call format; it executes nothing and parses no ops. |
| Fork = no memory accumulation (AD-3/AD-9) | Worker still forks per turn and dies; the loop runs inside one fork. Resumed turns (9.3) are *new* forks from persisted state. |
| Single-writer per resource (AD-5) | Core remains sole writer of memory/state; tool file-writes are jailed to the workspace, never memory/vault. |
| Fail-soft (4.1/parse_reply discipline) | Tool errors → `ToolResult(ok=False)`; bad tools quarantined; loop exhaustion → best-effort reply. |
| Coherent timeout W < R < T (5.0) | Loop hard-ceiled at 25s; risky ops don't block (2-phase), so the worker never exceeds its window waiting on a human. |

---

## 6. Risks & open questions

- **Resumable agent state (9.3)** is the hardest part — serializing the message list + pending call across a fork boundary. Mitigation: persist a compact JSON blob in sqlite; the resumed worker rebuilds the loop from it. Validate the blob size stays small.
- **GLM native function-calling fidelity** — GLM-4.x supports tool-use, but reliability of multi-step loops on the live endpoint is unverified. Mitigation: 9.1's loop is provider-agnostic; if GLM underperforms, the normalizer can target a different chain provider without touching the loop.
- **`python_eval` containment** — restricted namespaces are not a true sandbox. Accepted for single-owner; anything risky is a gated RISKY tool, and `python_eval` is FREE only because it's blocked from side effects. Revisit if the threat model changes.
- **Loop cost** — multiple model calls per turn × live GLM. 9.5 credit gating is the backstop; set a conservative default loop ceiling.
- **Approval UX latency** — a 2-phase risky op feels slower than v1's free-run. This is the intended safety trade (your tiered-allowlist choice); FREE tools keep the magic for the common case.

---

## 7. Sequencing

Build order: **9.1 → 9.2 → 9.3 → 9.4**, with **9.5 hardening woven through each** (quarantine lands with 9.4, caps with 9.2/9.3, credit gating with 9.1's loop). 9.1+9.2 is the first felt win (inline coding for safe tasks); 9.3 is the risk-bearing milestone; 9.4 is the headline self-coding capability.

**Next step:** hand this design to the BMAD epic/story flow to produce Epic 9 in `epics.md` + the five story specs, then run the per-story dev loop ([[shelldon-dev-conventions]]).
