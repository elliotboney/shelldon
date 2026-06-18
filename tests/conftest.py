"""Shared test fixtures."""

import shutil
import tempfile
from pathlib import Path

import pytest

import shelldon.broker.broker as _broker
import shelldon.broker.service as _service


@pytest.fixture(autouse=True)
def _no_broker_backoff(monkeypatch):
    """Zero the broker's retry + reconnect backoffs so the suite never sleeps for real.

    The backoffs (Story 2.2) are exercised explicitly in test_broker_chain_fallback.py
    and test_broker_reconnect.py; everywhere else they would only add wall-clock time.
    """
    monkeypatch.setattr(_broker, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(_service, "_RECONNECT_BACKOFF_S", 0)


@pytest.fixture
def sock_path():
    """A short-lived UDS path under /tmp.

    pytest's `tmp_path` is too long for AF_UNIX (macOS caps the path at ~104
    chars); a short /tmp dir keeps the socket name within the limit.
    """
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="shd-"))
    try:
        yield str(d / "bus.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)
