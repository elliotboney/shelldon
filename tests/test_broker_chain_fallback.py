"""Story 2.2 AC1/AC3: a failed model call falls through to the next provider.

`handle_job_chain` iterates the ordered chain, calling the unchanged per-provider
`handle_job` for each; the first success wins, any failure advances, and an
exhausted chain returns the last failure Result. Fakes only — no SDK/network/key.
"""

import logging

import pytest

import asyncio

import shelldon.broker.broker as broker_mod
from shelldon.broker.broker import handle_job, handle_job_chain
from shelldon.broker.provider import PermanentProviderError, TransientProviderError
from shelldon.broker.service import run_broker
from shelldon.contracts import Actor, Envelope, Job, MsgKind, Result
from shelldon.core.bus import BusServer, connect, write_frame


class _Provider:
    """Named fake: succeeds with `text`, or raises `exc` every call."""

    def __init__(self, name, *, text="ok", exc=None):
        self.name = name
        self.text = text
        self.exc = exc
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.text


async def test_first_success_wins_and_tail_not_called():
    primary = _Provider("glm", text="from-glm")
    secondary = _Provider("ollama", text="from-ollama")
    res = await handle_job_chain(Job(payload="ping"), [primary, secondary])
    assert res.ok and res.payload == "from-glm"
    assert primary.calls == 1
    assert secondary.calls == 0  # never reached — first provider answered


async def test_transient_primary_falls_through_to_secondary():
    primary = _Provider("glm", exc=TransientProviderError("boom"))
    secondary = _Provider("ollama", text="recovered")
    res = await handle_job_chain(Job(payload="ping"), [primary, secondary])
    assert res.ok and res.payload == "recovered"
    assert primary.calls == 2  # one failure + the single in-provider retry
    assert secondary.calls == 1


async def test_permanent_primary_also_falls_through():
    """A permanent 4xx on the primary still advances — provider B may answer."""
    primary = _Provider("glm", exc=PermanentProviderError("bad request"))
    secondary = _Provider("ollama", text="recovered")
    res = await handle_job_chain(Job(payload="ping"), [primary, secondary])
    assert res.ok and res.payload == "recovered"
    assert primary.calls == 1  # permanent → no retry, but still advances
    assert secondary.calls == 1


async def test_chain_exhausted_returns_last_failure():
    primary = _Provider("glm", exc=TransientProviderError("boom"))
    secondary = _Provider("ollama", exc=PermanentProviderError("nope"))
    res = await handle_job_chain(Job(payload="ping"), [primary, secondary])
    assert not res.ok and res.error
    assert "nope" in res.error  # the LAST provider's failure is surfaced


async def test_single_element_chain_regression():
    only = _Provider("glm", text="solo")
    res = await handle_job_chain(Job(payload="ping"), [only])
    assert res.ok and res.payload == "solo"
    assert only.calls == 1


async def test_audit_logs_winning_provider_and_fallback_count(caplog):
    primary = _Provider("glm", exc=TransientProviderError("boom"))
    secondary = _Provider("ollama", text="recovered")
    with caplog.at_level(logging.INFO, logger="shelldon.broker"):
        await handle_job_chain(Job(payload="ping"), [primary, secondary])
    audit = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("ollama" in m for m in audit)   # the winner is recorded (AC3)
    assert all("glm" not in m for m in audit)  # the failed provider is not the answerer
    assert any("1 fallback" in m for m in audit)  # fallback count recorded


async def test_audit_record_carries_no_credential(caplog):
    """The audit name is the preset label, never a key/base_url (AC3)."""
    primary = _Provider("glm", text="hi")
    with caplog.at_level(logging.INFO, logger="shelldon.broker"):
        await handle_job_chain(Job(payload="ping"), [primary])
    record = " ".join(r.getMessage() for r in caplog.records)
    assert "glm" in record
    assert "sk-" not in record and "api_key" not in record


async def test_transient_retry_backs_off_before_retrying(monkeypatch):
    """1.4 deferral: a transient retry waits a backoff before re-hitting the endpoint."""
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(broker_mod, "_RETRY_BACKOFF_S", 0.5)
    monkeypatch.setattr(broker_mod.asyncio, "sleep", _fake_sleep)
    p = _Provider("glm", text="recovered")
    # Fail once (transient), then succeed → exactly one backoff before the retry.
    calls = {"n": 0}

    async def _complete(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientProviderError("boom")
        return "recovered"

    p.complete = _complete
    res = await handle_job(Job(payload="ping"), p)
    assert res.ok and res.payload == "recovered"
    assert slept == [0.5]  # backed off once, before the single retry


async def test_fallback_completes_a_turn_end_to_end_over_the_bus(sock_path):
    """AC2: a forced primary failure → the turn still completes via the fallback,
    proven through run_broker/_serve_connection over a real UDS bus."""
    srv = BusServer(socket_path=sock_path)
    await srv.start()
    primary = _Provider("glm", exc=TransientProviderError("injected 500"))
    secondary = _Provider("ollama", text="from-fallback")
    broker_task = asyncio.create_task(run_broker(sock_path, [primary, secondary]))
    try:
        await asyncio.sleep(0.05)  # let the broker register as BROKER
        reader, w = await connect(sock_path, Actor.WORKER)
        job = Envelope(
            id="j1", kind=MsgKind.JOB, src=Actor.WORKER, dst=Actor.BROKER,
            body=Job(payload="ping"), turn_id="turn-2-2",
        )
        await write_frame(w, job)

        res_env = await asyncio.wait_for(srv.core_inbox.get(), timeout=2.0)
        assert isinstance(res_env.body, Result)
        assert res_env.body.ok and res_env.body.payload == "from-fallback"
        assert res_env.turn_id == "turn-2-2"  # echoed for core's fencing (AD-12)
        assert primary.calls == 2 and secondary.calls == 1  # primary tried+retried, then fell through
        w.close()
        await w.wait_closed()
    finally:
        broker_task.cancel()
        await asyncio.gather(broker_task, return_exceptions=True)
        await srv.stop()
