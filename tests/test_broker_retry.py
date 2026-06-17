"""AC1/AC2: broker turns a Job into a Result, retrying a transient error once.

Fake providers only — no SDK, no network, no key.
"""

import pytest

from shelldon.broker.broker import handle_job
from shelldon.broker.provider import PermanentProviderError, TransientProviderError
from shelldon.contracts import Job


class _OK:
    def __init__(self, text="hi"):
        self.text = text
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        return self.text


class _TransientThen:
    """Raises TransientProviderError for the first `fail_times` calls, then succeeds."""

    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientProviderError("transient")
        return "recovered"


class _Permanent:
    def __init__(self):
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        raise PermanentProviderError("bad request")


async def test_success_calls_provider_once():
    p = _OK("pong")
    res = await handle_job(Job(payload="ping"), p)
    assert res.ok and res.payload == "pong" and res.error is None
    assert p.calls == 1


async def test_transient_then_success_retries_once():
    p = _TransientThen(fail_times=1)
    res = await handle_job(Job(payload="ping"), p)
    assert res.ok and res.payload == "recovered"
    assert p.calls == 2  # one failure + one retry


async def test_transient_twice_surfaces_failure():
    p = _TransientThen(fail_times=2)  # fails original + the single retry
    res = await handle_job(Job(payload="ping"), p)
    assert not res.ok and res.error
    assert p.calls == 2  # retried exactly once, not endlessly


async def test_permanent_error_not_retried():
    p = _Permanent()
    res = await handle_job(Job(payload="ping"), p)
    assert not res.ok and res.error
    assert p.calls == 1  # no retry on a permanent error


class _Unexpected:
    """Raises an error the provider never mapped to Transient/Permanent."""

    def __init__(self):
        self.calls = 0

    async def complete(self, prompt):
        self.calls += 1
        raise RuntimeError("boom")


async def test_unexpected_exception_becomes_failure_result():
    """Any unmapped exception must surface as a failure Result, not crash the loop
    (errors never cross the bus as exceptions)."""
    p = _Unexpected()
    res = await handle_job(Job(payload="ping"), p)
    assert not res.ok and res.error
    assert p.calls == 1  # treated as non-retryable
