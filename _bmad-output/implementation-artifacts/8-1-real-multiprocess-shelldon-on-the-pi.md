---
baseline_commit: 8a9ae7e
---
# Story 8.1: shelldon on the real Pi — headless, live GLM, the fork/OOM proof

Status: done

<!-- Epic 8 (Verify & Deploy), story 2. Done HANDS-ON then documented (deployment is interactive ops, not TDD). The Pi (host `gotchi`, a 416MB Pi Zero 2W running openclawgotchi v1) was available, so we deployed shelldon to it and ran a real turn against live GLM. v1 left stopped during testing (owner's call — restarting it triggers Telegram traffic). -->

## Story

As the owner,
I want shelldon running on the actual Raspberry Pi Zero 2W (416MB) against the live GLM brain — headless (no E-Ink yet), without disturbing the running v1 — and to watch its memory stay bounded,
so that the project's last big unknown after live-LLM verification (does the real fork/process model survive 512MB-class hardware?) is answered on the real target, not just in macOS tests.

**Why this matters:** v1 (openclawgotchi) accumulated RAM until it OOM-crashed on the Pi Zero's tiny memory — the single documented pain shelldon's whole architecture (ephemeral fork-server workers, RAM-resident state) was designed to engineer out. Story 8.0 proved the brain works against a live LLM in macOS tests; this proves the runtime survives on the real hardware.

## Acceptance Criteria (what was proven)

### AC1 — shelldon deploys + runs on the real Pi (aarch64) ✅

**Given** `gotchi` = aarch64, Debian 13, Python 3.13.5 (exact `.python-version` match), git; `uv` absent
**When** deployed: `uv` installed (0.11.23), repo cloned from GitHub (HEAD `8a9ae7e`), `.env` (GLM key) copied over, `uv sync --locked`
**Then** the deps resolved from aarch64 wheels in ~29s (0 compilation), and the suite ran on the real arch: **536 passed / 1 skipped** (minus the slow soak/endurance, macOS-proven) in 52s — including the **real Linux `os.fork()` tests that can only skip on macOS**. The fork-server path works on the target.

### AC2 — A real owner turn against live GLM applies a memory-op ON THE PI ✅

**Given** the live GLM-4.7 chain + the real fork worker, run headless (CLI transport reading stdin, `StubRenderer` — no E-Ink), `gotchi-bot.service` (v1) stopped to free RAM + release hardware
**When** an owner message is fed: *"Hey shelldon! Please remember that my favorite database is BigQuery."*
**Then** a real reply came back over the real wire — **"Got it, noted! BigQuery it is."** — and **core applied the `remember` end-to-end: `~/.shelldon/memory/facts/favorite-db.md` was written = "BigQuery"** on the Pi. Live brain → fork worker → broker → GLM → Result → applied op, on 416MB hardware.

### AC3 — RAM stays bounded (the OOM proof) ✅

**Given** the Pi Zero 2W's 416MB (v1 idle ≈ 169MB used baseline)
**When** the turn runs through the real fork worker
**Then** RAM **peaked at 244MB used** (~80MB over baseline) and settled flat at ~233MB — the ephemeral fork worker does NOT accumulate across the turn. v1's OOM-crash pain does not reproduce. (The full 5-process `launch_multiprocess` app also came up — peaked 348MB, no OOM — but could not be driven by stdin; see Finding 1.)

### Out of scope (explicit — these are follow-on stories, NOT done here)

- **Full multiprocess separation end-to-end** — blocked on a cross-process transport (Finding 1); only the in-process launcher was driven through a turn (real fork worker either way).
- **Real uid-drop / vault isolation** — ran as `eboney` (uid-drop is a logged no-op without root); needs a systemd unit with the worker uid configured.
- **E-Ink display, sensors (PiSugar2/BLE), systemd service, provisioning-as-code** — later Epic 8 stories.

## What was done (the run book)

1. `curl -LsSf https://astral.sh/uv/install.sh | sh` → `uv 0.11.23` on `gotchi`.
2. `git clone https://github.com/elliotboney/shelldon.git ~/shelldon` (HEAD `8a9ae7e`).
3. `scp .env gotchi:~/shelldon/.env` (carries `GLM_API_KEY` — renamed from v1's `ANTHROPIC_API_KEY`, which *was* the GLM key; GLM speaks the Anthropic API).
4. `uv sync --locked` (~29s, aarch64 wheels) → `uv run pytest -q -k "not soak and not endurance"` → **536 passed / 1 skipped** in 52s.
5. `sudo systemctl stop gotchi-bot.service` (v1; freed ~80MB, released the hardware). **Left stopped** per owner.
6. Headless turn via the in-process launcher (transport-as-task reads the real stdin):
   `... | uv run python -c "import asyncio; from shelldon.app import run_app, launch_in_process; asyncio.run(run_app(launch_actors=launch_in_process))"` with the `.env` sourced + `GLM_MODEL=glm-4.7`. → reply + `facts/favorite-db.md` written; RAM sampled (peak 244MB).

## Findings (logged as follow-ons in deferred-work.md)

1. **`python -m shelldon` (multiprocess+`spawn`) — the CLI transport child can't read the parent's stdin.** The transport runs as a `spawn` child (`_transport_proc`), which does not inherit the launching process's stdin pipe, so a piped owner message never reaches it (no turn fires; the 5 processes still spin up). The CLI-over-stdin transport only works in the **in-process** launcher. Real deployment needs a non-stdin transport (a Telegram/socket adapter) — **only the CLI transport is built today.** This also blocks proving the full multiprocess path end-to-end. → the natural next story (8.2: a real transport).
2. **Forked-worker history read hit a sqlite "locking protocol" error and degraded.** On the Pi, the worker's read-only history open during prompt assembly logged `history read failed during assembly (locking protocol); degrading` — the 4.4 fail-soft path caught it (the turn still replied + applied the `remember`), so no user-visible failure, but the recent-history/recall context was dropped for that turn. Likely a WAL/read-only-handle interaction on first turn / fresh db on the Pi's filesystem. Worth root-causing before relying on recall in production.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Completion Notes List

- **The headline proof landed: shelldon's real fork/OOM model survives the 416MB Pi Zero 2W.** A real owner turn against live GLM-4.7 replied and applied a memory-op (`facts/favorite-db.md` written) with RAM peaking at 244MB and staying flat — the ephemeral fork worker means no accumulation, which is the exact failure mode that OOM-crashed v1. After live-LLM verification (8.0), this answers the project's last big unknown for the headless path.
- **Cross-arch is clean** — 536 tests pass on aarch64 (incl. the real `os.fork()` tests that only skip on macOS); aarch64 wheels for every dep (0 compilation, 0 new deps); Python 3.13.5 on the Pi matches the pin exactly.
- **Two real findings, neither fatal:** the multiprocess transport-stdin gap (only the CLI transport exists; a real transport is the next story) and a fail-soft sqlite history-read degrade. Both logged in `deferred-work.md`. The first scopes Story 8.2 (a real chat transport, needed for the full multiprocess path + actual desk usability); the second is a root-cause-before-prod item.
- **v1 untouched + left stopped** per owner (restarting `gotchi-bot.service` triggers Telegram traffic during testing). shelldon lives in its own `~/shelldon` + `.venv`; nothing of v1's was modified.

### File List

- `_bmad-output/implementation-artifacts/8-1-real-multiprocess-shelldon-on-the-pi.md` — NEW (this doc).
- `_bmad-output/implementation-artifacts/deferred-work.md` — MODIFIED (2 findings).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED (`8-1` added = done).
- (No `shelldon/` change — this is a deployment/run story; the only repo edit was the local `.env` key rename, gitignored.)

### Change Log

- 2026-06-20 — Story 8.1 done (hands-on, then documented): deployed shelldon to the real Pi Zero 2W (`gotchi`, 416MB aarch64) and ran a real owner turn against live GLM-4.7 headless — reply returned, `remember` applied (`facts/favorite-db.md` written), RAM bounded at 244MB peak (no fork-worker accumulation = v1's OOM pain engineered out). 536 tests pass on aarch64. 2 findings logged (multiprocess transport-stdin gap → only CLI transport exists; fail-soft sqlite history-read degrade). v1 left stopped per owner. Status → done.
