"""Epic 9 retro action #1 — LIVE self-coding smoke against the real GLM brain.

Every Epic 9 story (9.1–9.6) was tested with FAKE providers — the self-coding loop has never run
against a live LLM. This is the validation gap that mirrors the pre-8.0 risk: the MECHANISM is
proven (stage/gate/promote/discover have fake-provider unit tests), but whether a real model can
emit a `propose_tool` op carrying tool code + a pytest test that actually PASSES the gate is
unverified. This smoke drives the WHOLE wire against GLM:

    owner turn → worker (real `run_worker` + `assemble_prompt`, SYSTEM_INSTRUCTION's propose_tool
    clause) → broker → GLM → reply with a ```ops `propose_tool` block → core stages + runs the
    REAL pytest gate (a subprocess) → on PASS parks a promotion → owner Approve → promote to the
    live dir → a fresh `build_tool_registry` discovers it FREE.

`propose_tool` rides the ops-block wire (Story 9.4), so the pre-9.1 single-round-trip worker path
is enough to elicit it — no function-call loop needed. (Quarantine + the FREE call-loop are
mechanism-tested in test_self_coded_discovery.py / test_selfcode_flow.py; this smoke covers the
live model's ability to author a gate-passing tool, which is the only live-unproven link.)

Opt-in, network-gated (skips without a key), NOT in CI — costs real tokens. Run on the Pi:

    set -a; . ./.env; set +a   # or: source the systemd EnvironmentFile
    uv run pytest -m live -s -k self_coding

A green run = the live model self-coded a tool that passed its own test and went live end-to-end.
A red run (degrade / no parked promotion) IS the finding action #1 asked us to surface.
"""

import os

import pytest

from shelldon.broker.chain import build_chain
from shelldon.contracts import ToolTier
from shelldon.core import selfcode
from shelldon.core.runtime import DEGRADE_TEXT
from shelldon.worker.tools import build_tool_registry
from shelldon.worker.worker import run_worker
from test_end_to_end_turn import Spawns, _await, build_harness

pytestmark = pytest.mark.live

_GLM_KEY = os.environ.get("GLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")

#: A propose turn includes a real pytest gate SUBPROCESS (~1–3s) on top of network latency —
#: generous so a slow round-trip + gate never degrades the turn under test.
_LIVE_TURN_TIMEOUT = 90.0

_PROMPT = (
    "Please write yourself a brand-new tool, right now, using a propose_tool op. "
    "The tool must be named `add_numbers`: it takes two integers `a` and `b` and returns their "
    "sum as a string. Emit ONE ```ops block containing a single propose_tool op whose `code` "
    "defines `run(a, b)`, `DESCRIPTION`, and `PARAMS_SCHEMA` at module level (no imports of any "
    "AI library), and whose `test` is a pytest module that does `import add_numbers` and asserts "
    "`add_numbers.run(2, 3) == '5'`."
)


@pytest.mark.skipif(not _GLM_KEY, reason="no GLM_API_KEY/ANTHROPIC_API_KEY")
async def test_live_self_coding_propose_gate_promote_discover(sock_path, tmp_path):
    """A real owner turn asks GLM to write a tool; core stages + runs the REAL gate; on PASS it
    parks a promotion; the owner approves; it promotes; a fresh registry discovers it FREE — the
    whole self-coding loop against a live brain."""
    ws = tmp_path / "workspace"
    h = await build_harness(
        sock_path,
        chain=build_chain(os.environ),
        spawns=Spawns(worker=run_worker),
        turn_timeout=_LIVE_TURN_TIMEOUT,
    )
    h.core.workspace_root = ws  # stage/gate/promote under tmp, never real $HOME
    try:
        h.source.feed(_PROMPT)
        # TWO replies land: outbound[0] = the model's spoken reply; outbound[1] = the propose
        # verdict ("…passed its test — add it?" / "…failed its check, tossed it"), sent AFTER the
        # gate (a real pytest subprocess) runs inline in _handle_result (Story 9.4). Wait for the
        # verdict so the gate has finished + the promotion is parked-or-discarded before we read.
        await _await(lambda: len(h.outbound) >= 2, timeout=_LIVE_TURN_TIMEOUT)
        reply, verdict = h.outbound[0], h.outbound[1]

        row = h.core.history._conn.execute(
            "SELECT turn_id, tool_name FROM pending_promotions"
        ).fetchone()
        staging = selfcode.staging_dir(ws)
        staged = sorted(p.name for p in staging.glob("*.py")) if staging.exists() else []
        print(f"\n[propose] reply={reply!r}\n[gate verdict] {verdict!r}\n"
              f"parked={dict(row) if row else None}\nstaged={staged}")

        assert reply and reply != DEGRADE_TEXT, "the turn degraded — the live chain failed (FINDING)"
        assert row is not None, (
            "no promotion parked — the live model emitted a propose_tool but the gate FAILED "
            f"(its pytest test didn't pass / import-check rejected). Gate verdict: {verdict!r} (FINDING)"
        )
        turn_id, stem = row["turn_id"], row["tool_name"]

        # Owner taps Approve → core promotes the staged module to the live dir.
        await h.core._handle_approval_decision(turn_id, True)
        live = selfcode.live_tools_dir(ws) / f"{stem}.py"
        assert live.exists(), "approved tool did not promote to the live dir (FINDING)"
        print(f"[promote] live tool {stem}.py:\n{live.read_text()}")

        # The next fresh worker's registry discovers it FREE — no restart (the fork-reimport property).
        reg = build_tool_registry(workspace_root=ws, memory_root=tmp_path / "memory")
        assert stem in reg, f"promoted tool {stem!r} not discovered by build_tool_registry (FINDING)"
        assert reg[stem].tier is ToolTier.FREE, "promoted tool not registered FREE (FINDING)"
        print(f"[discover] {stem!r} registered FREE — description={reg[stem].description!r}")
    finally:
        await h.teardown()
