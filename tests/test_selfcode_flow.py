"""Story 9.4: the full self-coding flow at the CORE level (a fake spawner, like test_risky_approval).

A `ProposeTool` op on a Result → core stages + gates the tool; on PASS it parks a pending
promotion and tags the reply with the 9.3 approval keyboard; an Approve tap promotes the staged
module to the live dir; a Deny discards it; a FAILED gate replies + parks nothing; an
expired/unknown promotion decision is dropped (never promoted). The gate runs a REAL pytest
subprocess, so each test stages a tiny tool and uses a `tmp_path` workspace (never real $HOME).
"""

from datetime import UTC, datetime

from shelldon.contracts import Actor, Envelope, MsgKind, ProposeTool, Result
from shelldon.core.runtime import Core
from shelldon.core.selfcode import live_tools_dir, staging_dir

_GOOD_CODE = (
    "DESCRIPTION = 'add two ints'\n"
    "PARAMS_SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}}\n"
    "def run(a=0, b=0):\n"
    "    return str(int(a) + int(b))\n"
)
_GOOD_TEST = "import adder\ndef test_adds():\n    assert adder.run(2, 3) == '5'\n"
_FAILING_TEST = "import adder\ndef test_adds():\n    assert adder.run(2, 3) == '6'\n"


class _FakeSpawner:
    async def ready(self):  # pragma: no cover
        pass

    async def spawn_turn(self, turn_id, prompt):  # pragma: no cover
        pass

    async def spawn_resume(self, turn_id, messages, call, approved):  # pragma: no cover
        pass

    async def reap_current(self):
        pass


def _core(sock_path, tmp_path):
    return Core(sock_path, _FakeSpawner(), memory_root=tmp_path / "memory",
                history_path=tmp_path / "history.db", checkpoint_path=tmp_path / "s.json",
                workspace_root=tmp_path / "workspace")


def _capture_replies(core):
    sent = []

    async def _rec(text, *, approval_turn_id=None):
        sent.append((text, approval_turn_id))

    core._send_reply = _rec
    return sent


def _open_turn(core, turn_id):
    core.arbiter.submit("write me a tool")
    core._current_prompt = "write me a tool"
    core._current_turn_id = turn_id
    core.fence.open(turn_id)


def _result_env(turn_id, ops):
    return Envelope(id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
                    body=Result(ok=True, payload="wrote a tool", proposed_ops=ops), turn_id=turn_id)


async def test_propose_passes_gate_parks_and_tags_reply(sock_path, tmp_path):
    """AC1/AC2: a ProposeTool op → staged + gated; PASS → promotion parked + reply tagged."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        _open_turn(core, "t1")
        op = ProposeTool(name="adder", code=_GOOD_CODE, test=_GOOD_TEST)
        await core._handle_result(_result_env("t1", [op]))

        # Reply tagged with the turn id (the 9.3 Approve/Deny keyboard surface).
        assert sent[-1][1] == "t1" and "add it" in sent[-1][0].lower()
        # Parked (peek without consuming) and staged on disk, not yet live.
        row = core.history._conn.execute(
            "SELECT tool_name FROM pending_promotions WHERE turn_id = 't1'").fetchone()
        assert row["tool_name"] == "adder"
        assert (staging_dir(tmp_path / "workspace") / "adder.py").exists()
        assert not (live_tools_dir(tmp_path / "workspace") / "adder.py").exists()
    finally:
        core._cleanup()


async def test_approve_promotes_to_live(sock_path, tmp_path):
    """AC3: an Approve tap moves the staged module to the live dir and confirms."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        _open_turn(core, "t2")
        await core._handle_result(_result_env("t2", [ProposeTool(name="adder", code=_GOOD_CODE, test=_GOOD_TEST)]))

        await core._handle_approval_decision("t2", True)

        assert (live_tools_dir(tmp_path / "workspace") / "adder.py").exists()
        assert "live" in sent[-1][0].lower()
        # Consumed: a second decision finds nothing parked.
        assert core.history.take_promotion("t2", datetime.now(UTC)) is None
    finally:
        core._cleanup()


async def test_deny_discards(sock_path, tmp_path):
    """AC3: a Deny tap discards the staged files and confirms."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        _open_turn(core, "t3")
        await core._handle_result(_result_env("t3", [ProposeTool(name="adder", code=_GOOD_CODE, test=_GOOD_TEST)]))

        await core._handle_approval_decision("t3", False)

        assert not (staging_dir(tmp_path / "workspace") / "adder.py").exists()
        assert not (live_tools_dir(tmp_path / "workspace") / "adder.py").exists()
        assert "discard" in sent[-1][0].lower()
    finally:
        core._cleanup()


async def test_failed_gate_replies_and_parks_nothing(sock_path, tmp_path):
    """AC2: a failing gate → the staged files are discarded, the owner gets a note, nothing parked."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        _open_turn(core, "t4")
        await core._handle_result(_result_env("t4", [ProposeTool(name="adder", code=_GOOD_CODE, test=_FAILING_TEST)]))

        assert "failed" in sent[-1][0].lower() and sent[-1][1] is None  # not an approval prompt
        assert core.history.take_promotion("t4", datetime.now(UTC)) is None  # nothing parked
        assert not (staging_dir(tmp_path / "workspace") / "adder.py").exists()  # discarded
    finally:
        core._cleanup()


async def test_failed_promote_discards_staged_files(sock_path, tmp_path, monkeypatch):
    """Review fix: if promote() fails (e.g. shutil.move errors), the staged pair must NOT be left
    orphaned on disk — core discards it and tells the owner it didn't promote."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        _open_turn(core, "t6")
        await core._handle_result(_result_env("t6", [ProposeTool(name="adder", code=_GOOD_CODE, test=_GOOD_TEST)]))
        # Force a promote failure (parked + staged, but the move "fails").
        monkeypatch.setattr("shelldon.core.selfcode.promote", lambda *a, **k: False)

        await core._handle_approval_decision("t6", True)

        assert "couldn't add" in sent[-1][0].lower()
        assert not (staging_dir(tmp_path / "workspace") / "adder.py").exists()  # discarded, not orphaned
        assert not (live_tools_dir(tmp_path / "workspace") / "adder.py").exists()
    finally:
        core._cleanup()


async def test_expired_promotion_decision_is_dropped(sock_path, tmp_path):
    """AC3: an expired/unknown promotion decision NEVER promotes — it's dropped with a note."""
    core = _core(sock_path, tmp_path)
    try:
        sent = _capture_replies(core)
        # Park with an expiry already in the past (stamped from a long-ago `now`).
        core.history.park_promotion("t5", "adder", datetime(2020, 1, 1, tzinfo=UTC), ttl_seconds=60)

        await core._handle_approval_decision("t5", True)

        assert not (live_tools_dir(tmp_path / "workspace") / "adder.py").exists()  # never promoted
        assert sent and ("expired" in sent[-1][0].lower() or "pending" in sent[-1][0].lower())
    finally:
        core._cleanup()
