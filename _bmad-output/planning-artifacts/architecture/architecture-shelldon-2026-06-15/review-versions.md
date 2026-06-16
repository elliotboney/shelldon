# Spine Review — Version / Reality Lens

**Target:** `ARCHITECTURE-SPINE.md` (shelldon / OpenClawGotchi v2)
**Reviewed:** 2026-06-15
**Lens:** Verify every committed technology decision against the live web — current versions, existence, role-fit. File not modified.

**Verdict:** Spine is technically sound and current. One stale model-name reference (`GLM-5.x` / `GLM-5.0`) is the only material defect; everything else is confirmed-correct or a known-and-acceptable risk.

---

## Confirmed against live web

| Claim in spine | Status | Evidence (verified 2026-06-15) |
| --- | --- | --- |
| Python 3.14.6 is current upstream | **TRUE** | python.org/downloads/latest = 3.14.6, released 2026-06-10. 3.14.0 GA'd 2025-10-07. |
| Choosing Python 3.13.x for Pi OS lib compat | **Reasonable** | 3.13 is the conservative, widely-packaged choice; 3.14 is fine but newer. Defensible call, not a defect. |
| CPython `os.fork()` COW is load-bearing for pre-warming | **TRUE but incomplete** | COW works, *but* CPython refcounts defeat naive COW. See HIGH finding below. |
| `msgspec@0.21.1` is the current official jcrist/msgspec release | **TRUE** | PyPI latest = 0.21.1. Note: repo moved to `github.com/msgspec/msgspec` org (was `jcrist/msgspec`); same project. |
| msgspec does msgpack + structs | **TRUE** | PyPI classifiers + docs: MessagePack encoder/decoder + `Struct` type confirmed. |
| msgspec 0.21.1 supports Python 3.14 | **TRUE** | PyPI classifiers now list Python 3.10–3.14. (The Oct-2025 "when 3.14 wheels?" issue #891 is resolved.) |
| `bleak@3.0.1` is current; BLE on Linux/BlueZ | **TRUE** | bleak 3.0.1, docs dated 2026-03-25. Supports BlueZ >= 5.55. |
| bleak does passive scanning on BlueZ | **TRUE, with a caveat** | `scanning_mode="passive"` supported on BlueZ (not macOS). Caveat: service-UUID filtering not implemented for passive — must use BlueZ `or_patterns`. See MEDIUM. |
| Z.ai exposes OpenAI- AND Anthropic-compatible endpoints | **TRUE** | OpenAI-compat: `https://api.z.ai/api/openai/v1` (or `/api/paas/v4/chat/completions`); Anthropic-compat: `https://api.z.ai/api/anthropic`. Z.ai is the notable Anthropic-compatible drop-in. |
| Provider chain (Ollama / Gemini / OpenAI / OpenRouter) reachable via a common adapter | **TRUE** | Ollama, Gemini (has an OpenAI-compat surface), OpenAI, and OpenRouter all expose OpenAI-style chat-completions; a single OpenAI-compatible adapter covers all four. Z.ai also speaks OpenAI-compat, so the default can ride the same adapter. |
| Waveshare V4 E-Ink driver options (vendored module or omni-epd) + spidev | **TRUE** | Waveshare ships `waveshare_epd` Python lib + V4 demos; `omni-epd` (robweber) abstracts it and auto-installs `waveshare-epd`. spidev is the standard SPI path; SPI must be enabled. |
| PiSugar2 local API | **TRUE** | pisugar-server exposes UDS `/tmp/pisugar-server.sock`, TCP `:8423`, websocket `:8422`, HTTP `:8421`. Commands incl. `get battery`, `set_button_enable`, `set_button_shell`. |

---

## Findings

### F1 — `GLM-5.x` / `GLM-5.0` is a stale / nonexistent model name  `[MEDIUM]`
The spine repeatedly names the default model **`GLM-5.x`** (frontmatter, AD-2 commentary, Structural Seed line 133/158, Deferred line 179). There is **no GLM-5.0**. As of 2026-06-15 the live lineage is **GLM-4.6 (Oct 2025) → GLM-4.7 → GLM-5.1 → GLM-5.2 (released 2026-06-13, two days before this spine)**. "GLM-5.x" as a *family glob* is loosely defensible, but readers will reach for a literal "GLM-5.0" that doesn't exist.
**Fix:** the spine itself wisely defers the exact model id to broker config (Deferred + line 158), so this is cosmetic in the invariants. Change the illustrative default to **`GLM-5.2`** (current flagship) or write it as "latest GLM Coding model" to avoid implying a specific nonexistent version. Severity is MEDIUM only because the binding decision (Z.ai Anthropic/OpenAI-compatible endpoint) is correct and the model id is explicitly non-spine.

### F2 — COW pre-warming needs `gc.freeze()` or the RAM win evaporates  `[HIGH]`
AD-3's value proposition ("fork-server parent pre-imports LLM libs... worker assembles with warm libs... RAM reclaimed") rests on COW sharing the pre-imported libs across forks. **CPython defeats this by default:** every object header carries `ob_refcnt`, so merely *reading* a shared object on the child increments the refcount → dirties the page → kernel copies it. The GC compounds it. On a 512MB box this silently erases the memory benefit the whole fork-server exists for.
**Fix (well-established recipe, available since 3.7):** in the fork-server parent, `gc.disable()` early, `gc.freeze()` immediately before `os.fork()`, `gc.enable()` early in the child. Do **not** `gc.collect()` right before freezing (creates COW "holes"). This belongs as a one-line note in AD-3 or the structural seed so it isn't lost at build time. Not a spine-invariant change — an implementation constraint the spine should flag.

### F3 — 5+ Python processes on 512MB is tight but fits — if workers stay lean  `[MEDIUM]`
The spine runs core + broker + fork-server + display + peripheral-host + (≤1 ephemeral worker) = ~6 processes. Real-world budget: headless Pi OS Lite uses ~85–100MB, leaving **~400MB usable**. A bare CPython process is ~10–30MB; heavy libs multiply *per process* because each interpreter has its own memory space. Five lean processes (~15–25MB each) ≈ 100–150MB — comfortable. **Risk concentrates in the worker:** if LLM-client/prompt-assembly libs are heavy and COW fails (see F2), the forked worker plus warm libs can spike. The spine's own mitigations are correct and load-bearing here: **≤1 worker in flight (AD-9)** and **markdown memory, no vectors/sqlite (AD-6)** keep the footprint sane.
**Fix:** none to the spine. Validate with `free -h`/`htop` at M0; the ≤1-worker invariant is the right guardrail. Add a heat-sink note for sustained multi-core load (operational, not architectural).

### F4 — bleak passive scanning can't filter by service UUID  `[LOW]`
AD-8/CAP-3 use bleak for "BLE presence." bleak's BlueZ passive mode works but **does not implement service-UUID filtering** — you filter via BlueZ `or_patterns` (match on data type/bytes, e.g. the LE device-address field) instead. For presence detection (watching for a known device address / manufacturer data) this is the *expected* path and is fine. It only bites if a plugin assumed `service_uuids=[...]` filtering in passive mode.
**Fix:** none to the spine; flag at the BLE-presence plugin's story time that passive filtering uses `or_patterns`, not service UUIDs.

### F5 — Repo/import path note for msgspec  `[LOW]`
Project canonically lives at `github.com/msgspec/msgspec` now (the `jcrist/msgspec` references in the prompt redirect — same author/project). Import name and PyPI name are unchanged (`pip install msgspec`, `import msgspec`). No action; noted so a future reader doesn't think the package was forked/renamed.

---

## Net assessment
Every bindable technology in the spine **exists, is current, and fits its stated role.** The only literal inaccuracy is the `GLM-5.x` model name (F1, cosmetic given the spine defers model id to config). The one finding that could undermine a stated architectural goal is **F2 (gc.freeze for COW)** — high-impact, trivially fixable, and an implementation note rather than an invariant change. 512MB fit (F3) is real but the spine's own invariants (≤1 worker, no vector DB, RAM-checkpoint) already defend it.

### Sources
- Python: python.org/downloads/latest (3.14.6, 2026-06-10)
- msgspec: pypi.org/project/msgspec (0.21.1, classifiers 3.10–3.14, msgpack+structs)
- bleak: bleak.readthedocs.io (3.0.1, 2026-03-25; passive BlueZ via or_patterns)
- Z.ai: docs.z.ai (OpenAI-compat `/api/openai/v1`, Anthropic-compat `/api/anthropic`); GLM-5.2 launch 2026-06-13
- COW/gc.freeze: docs.python.org/3/library/gc.html (gc.freeze since 3.7); Instagram/Luis-Sena writeups
- Waveshare/omni-epd: waveshare.com/wiki + github.com/robweber/omni-epd
- PiSugar2: github.com/PiSugar/pisugar-power-manager-rs (UDS/TCP/ws/HTTP ports)
- Pi Zero 2W RAM: 512MB LPDDR2; headless Lite ~85–100MB baseline
