---
baseline_commit: 6eb5e35
---
# Story 8.4: systemd service + one-shot Pi setup script

Status: done

<!-- Epic 8, the "make it permanent + installable" story ([B]). Built + verified on `gotchi`: shelldon now runs as a systemd service (autostart, auto-restart, memory-capped) installable with one script, like v1. -->

## Story

As the owner,
I want shelldon to run as a service that starts on boot, restarts if it crashes, and is installable with one script,
so that it's a permanent thing on the desk (not a manual `python` invocation) and anyone can stand it up like v1.

## Acceptance Criteria (proven on hardware)

### AC1 — A one-shot, idempotent Pi setup script ✅

**Given** a fresh clone on the Pi
**When** `./deploy/setup-pi.sh` runs
**Then** it: installs `uv` (if missing), `uv sync --locked`, detects a Pi by `/dev/spidev0.0` and installs the E-Ink deps (`swig` + `liblgpio-dev` + `fonts-unifont` via apt; `pillow`/`spidev`/`gpiozero`/`lgpio`/`rpi-lgpio` via `uv pip`) — or skips them on a headless box — copies `.env` from `.env.example` if absent, and installs + enables the systemd unit. Re-running is safe. `.env.example` documents the config (`GLM_API_KEY`, `SHELLDON_TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`).

### AC2 — A systemd service: autostart, auto-restart, memory-capped ✅

**Given** the unit the script writes (mirrors v1's `gotchi-bot.service`)
**Then** `shelldon.service` runs `uv run python -m shelldon` as the user, `EnvironmentFile=.env` (secrets) + `Environment=SHELLDON_TRANSPORT=telegram` (+ `SHELLDON_DISPLAY=waveshare`/`GPIOZERO_PIN_FACTORY=lgpio` when a panel is present), `Restart=always`, `WantedBy=multi-user.target` (boot autostart), and the **512MB-Pi guardrail `MemoryMax=400M` / `MemoryHigh=350M`**.

### AC3 — Verified handling a real turn under the cap ✅

**Given** the service started (`systemctl start shelldon`)
**When** the owner messages the v2 bot from a phone
**Then** the SERVICE (not a manual run) replied with a live GLM response ("Hi Elliot! *beep boop* Nice to meet you too!"), drove the face on the panel, and **applied a memory-op** (`facts/owner-name.md` written — it now knows the owner's name). Crucially it stayed within the cgroup memory cap: **`NRestarts=0`** (no OOM-kill/crash-loop), RAM spiked for the ephemeral fork worker then **settled to 279MB** (the fork-server design holding on the real 416MB Pi — the whole-service cgroup cap was the worry; it held).

### Out of scope / follow-ons

- **Real uid-drop** still a no-op (service runs as `eboney`, no `SHELLDON_WORKER_USER`); wiring a dedicated worker uid for real vault isolation is a hardening follow-on.
- **The sqlite history-read degrade** (top open bug) still recurs every turn — orthogonal to this story.
- **`epd.sleep()` on `systemctl stop`** — the panel holds the last face after a stop (E-Ink persists); a clean-shutdown sleep is the 8.3 follow-on.

## Dev Agent Record

### Completion Notes List

- **shelldon is now a real, permanent desk pet.** It autostarts on boot, auto-restarts on failure, is memory-capped for the Pi Zero, and installs with one script — the v1-parity "run it like a product" experience the owner asked for. With 8.0–8.3 it's the whole stack (memory + autonomy + live brain + face) running as a service you text from your phone.
- **The memory cap held through a real turn** — the genuine risk (a 5-process app + a forked worker under one `MemoryMax=400M` cgroup on a 416MB box) did NOT OOM: `NRestarts=0`, RAM settled to 279MB. The fork-server's "ephemeral worker, no accumulation" thesis held on hardware, under a hard cap, end-to-end.
- **The script is Pi-aware** — `/dev/spidev0.0` detection means the same script works headless (skips E-Ink, display off) or on the Pi (full panel), generating the right `Environment=` lines in the unit.

### File List

- `deploy/setup-pi.sh` — NEW. The idempotent installer (uv, deps, Pi-detected E-Ink deps, .env, systemd unit install+enable). Executable.
- `.env.example` — NEW. Documented config template (the real `.env` is gitignored).
- `_bmad-output/implementation-artifacts/{8-4-...md, sprint-status.yaml}` — tracking.

### Change Log

- 2026-06-20 — Story 8.4 done (built + verified on `gotchi`): `deploy/setup-pi.sh` (one-shot idempotent installer) + a systemd service (autostart, `Restart=always`, `MemoryMax=400M`). shelldon now runs as a service; the owner texted the v2 bot → the SERVICE replied (live GLM) + drove the panel + wrote `facts/owner-name.md`, staying under the memory cap (NRestarts=0, settled 279MB). `.env.example` documents config. Status → done.
