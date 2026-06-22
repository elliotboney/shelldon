# Epic 9 live self-coding smoke — findings (2026-06-22)

Closes Epic 9 retro **action #1**: the whole self-coding line (9.1–9.6) was fake-provider tested;
this is the first run against a **live LLM on the real Pi**.

## Setup

- **Where:** `gotchi` (Pi Zero 2W, 416MB, aarch64, Debian 13, Python 3.13.5 — exact pin match). ~160MB available at run time.
- **Brain:** live GLM (GLM-4.7 via Z.ai), `GLM_API_KEY` from the Pi's `.env`; `build_chain` defaults (Z.ai base URL, glm-4.7).
- **Test:** `tests/test_self_coding_live_smoke.py` — opt-in `-m live`, in-process harness (`Spawns(worker=run_worker)`, no real fork), `core.workspace_root` pointed at `tmp_path`. The gate runs a REAL `pytest` subprocess.
- **Command:** `set -a; . ./.env; set +a; uv run pytest -m live -s -k self_coding`

## Result: 🟢 GREEN (1 passed, 15.4s)

The live model drove the WHOLE self-coding loop end-to-end:

1. Owner: *"write yourself an `add_numbers` tool…"* → GLM spoken reply: `"I'll create that tool for you right away!"`
2. GLM emitted a `propose_tool` op (ops-block wire) → core **staged** `add_numbers.py` + `test_add_numbers.py`.
3. Core ran the **real pytest gate** (subprocess) → **PASSED** → verdict reply: `"I wrote a tool `add_numbers` and it passed its test — add it?"`, promotion parked (`turn_id=05c1ef1e…`, `tool_name=add_numbers`).
4. Owner Approve → core **promoted** the module to the live dir. The model's code was clean and correct:
   ```python
   DESCRIPTION = "Adds two integers together and returns the result as a string."
   PARAMS_SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}
   def run(a: int, b: int) -> str:
       return str(a + b)
   ```
5. A fresh `build_tool_registry` **discovered it FREE** — callable on the next turn, no restart.

## What this retires

- **The live-LLM gap (the dominant Epic 9 unknown):** a real model CAN emit a well-formed `propose_tool` (decodable op + module defining `run`/`DESCRIPTION`/`PARAMS_SCHEMA` + a pytest test) that PASSES the gate, first try, with no special coaxing beyond the SYSTEM_INSTRUCTION clause + a clear owner ask.
- **RLIMIT on the Pi (retro action #1 folded-in check):** the gate's `pytest` subprocess ran under the 9.5/9.6 `RLIMIT_AS=1GiB` + `RLIMIT_CPU` preexec cap WITHOUT a false-kill, on the 416MB box. The 1GiB default is safe for the real gate; no OOM.
- **End-to-end on real hardware:** stage → real gate → park → approve → promote → discover, against GLM, on the deployed Pi.

## Findings / notes

- **Test timing bug found + fixed (not a product defect):** `_handle_propose_tool` (stage→gate→park) runs INLINE in `_handle_result` AFTER the model's spoken reply is sent. So `outbound[0]` is the chat reply and the gate verdict is `outbound[1]`; the first run read `pending_promotions` mid-gate (`parked=None`, staged files present) and failed. Fixed by waiting for the 2nd reply (the verdict) before reading. The PRODUCT behavior was correct throughout.
- **Not covered live (acceptable):** the model then CALLING its promoted tool through the function-call loop, and tripping a quarantine — both need the worker spawned WITH a tool registry (the in-process smoke harness uses the single-round-trip path). Both are mechanism-tested with fake providers (`test_self_coded_discovery.py`, `test_selfcode_flow.py`); the live-unproven link (authoring a gate-passing tool) is now proven.
- **No degrade, no OOM, 15.4s** including a live GLM round-trip + a real pytest gate subprocess on the Pi.

## Verdict

Epic 9 is not just mechanism-complete — **the pet genuinely self-codes against its real brain on its real hardware.** Retro action #1 is closed.
