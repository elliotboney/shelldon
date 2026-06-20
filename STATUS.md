# Status & Roadmap

🟢 **Deployed and running on real hardware.** shelldon lives on a Raspberry Pi Zero 2W as a systemd service — you text it from your phone (Telegram), it thinks with a live LLM brain, replies, shows its face on the E-Ink panel, remembers you across the conversation, and drifts in mood between chats.

39 stories shipped, **550 tests passing** (plus opt-in live-provider smokes), zero external runtime deps beyond the LLM SDKs, the LLM-free-core import contract held throughout. **It's not a demo — it runs end-to-end on a 416MB Pi:** a real Telegram message → the fork-server worker assembles a memory-shaped prompt → GLM (via Z.ai) replies → core applies the model's ops (facts written, learnings classified/resolved in sqlite) → the expressive face updates on the panel — with RAM staying flat (the ephemeral fork worker is the whole reason it survives the Pi Zero's memory, the box that OOM-killed its predecessor). It autostarts on boot, restarts on failure, and installs with one script (`deploy/setup-pi.sh`). Verification, hardware bring-up (E-Ink + the real `os.fork()` worker), and the production deployment were all proven on-device. **Remaining work is polish, not core function.**

## Roadmap

**Epics 1–8 are done — shelldon is verified, deployed, and running on a real Pi as a service.** Epics 1–4 are the daily-driver core; 5–6 add autonomy + learning; 7 is the optional plugin layer; 8 verified + deployed it.

- [x] **Epic 1 — Talking Pet** — the full walking skeleton end-to-end: chat turn (message in → reply out), the face reacts, and an endurance soak proved flat memory over sustained turns. (9 stories)
- [x] **Epic 2 — Resilient Brain** — an ordered provider chain with automatic fallback, degrading gracefully to reflex-only when the whole chain fails. (3 stories) ⭐ daily-driver
- [x] **Epic 3 — A Pet That Feels Alive** — persistent personality state, a resident reflex loop (blink/idle/mood drift), and a self-modifiable expressive-face registry. (3 stories) ⭐ daily-driver
- [x] **Epic 4 — Memory & Continuity** — sqlite conversation history (WAL/FTS5) + a curated markdown memory tree (sole-writer core, worker proposes), an OS-isolated vault, and memory injected into every prompt so the past shapes the reply. ⭐ daily-driver
- [x] **Epic 5 — Autonomous Life** — a core-resident multi-cadence scheduler, a daily credit budget + cooldown, battery-aware backoff, and proactive action — the pet acts on its own, bounded.
- [x] **Epic 6 — Dreaming & Learning** — cheap hot-path learning capture + a scheduled dream cycle that classifies, promotes the durable learnings into memory, and prunes the rest. (Confirmed live: a real GLM dream classified seeded learnings and core applied the promote/prune ops.)
- [x] **Epic 7 — Extensibility & Optional Embodiment** — a generalized plugin model: plugins emit/subscribe events, own private state, and claim display regions, *never importing core* (enforced by import-linter). Exercised by an optional XP/leveling widget, optional physical sensing (PiSugar2 button + BLE pair-first presence), and a bounded plugin→core channel that lets plugin events nudge the pet's mood. (6 stories — optional; the core pet is complete without it.)
- [x] **Epic 8 — Verify & Deploy** — live-LLM verification (a real turn + a real dream against GLM, core applies the ops), then real-hardware deployment: the fork/OOM model proven on the 416MB Pi, a Telegram chat transport, the Waveshare 2.13" E-Ink renderer, and a systemd service + one-shot `setup-pi.sh`. Includes the fix for the fork-not-fork-safe SQLite bug that had cost the pet its short-term memory on-device.
- [ ] **Polish** *(none load-bearing)* — real worker privilege-drop on the Pi (vault isolation), physical sensors (button/BLE) wired to real hardware, partial-refresh face animations, Telegram niceties.

## Planning artifacts

The design docs behind the build:

| Artifact | Path |
|---|---|
| Spec (11 capabilities) | [`SPEC.md`](_bmad-output/specs/spec-openclawgotchi-v2/SPEC.md) |
| Architecture spine (15 decisions) | [`ARCHITECTURE-SPINE.md`](_bmad-output/planning-artifacts/architecture/architecture-shelldon-2026-06-15/ARCHITECTURE-SPINE.md) |
| Epics & stories | [`epics.md`](_bmad-output/planning-artifacts/epics.md) |
| Per-epic retrospectives | `_bmad-output/implementation-artifacts/epic-*-retro-*.md` |
