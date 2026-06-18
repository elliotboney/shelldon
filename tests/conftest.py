"""Shared test fixtures and helpers."""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

import shelldon.broker.broker as _broker
import shelldon.broker.service as _service
import shelldon.core.memory as _memory
import shelldon.core.runtime as _runtime


@pytest.fixture(autouse=True)
def _isolate_state_checkpoint(tmp_path, monkeypatch):
    """Never let a test write real ~/.shelldon files.

    A Core constructed without explicit paths falls back to DEFAULT_CHECKPOINT_PATH
    (~/.shelldon/state.json) and DEFAULT_FACES_PATH (~/.shelldon/faces.toml). Story
    3.2 dirties state on every inbound message; Story 3.3 seeds faces.toml on load —
    both would hit real $HOME. Redirect the defaults at tmp files for every test
    (Story 3.1/3.2/3.3: "no test may write real $HOME").
    """
    monkeypatch.setattr(_runtime, "DEFAULT_CHECKPOINT_PATH", tmp_path / "state.json")
    monkeypatch.setattr(_runtime, "DEFAULT_FACES_PATH", tmp_path / "faces.toml")
    monkeypatch.setattr(_runtime, "DEFAULT_HISTORY_PATH", tmp_path / "history.db")
    # Story 4.2/4.5: a CuratedMemory built without an explicit root falls back to
    # DEFAULT_MEMORY_ROOT (~/.shelldon/memory). Story 4.5 has Core construct one, so
    # redirect the name runtime resolves AND the memory module's own (Epic 3 retro #3 —
    # isolate $HOME in the same change).
    monkeypatch.setattr(_memory, "DEFAULT_MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(_runtime, "DEFAULT_MEMORY_ROOT", tmp_path / "memory")


@pytest.fixture(autouse=True)
def _no_broker_backoff(monkeypatch):
    """Zero the broker's retry + reconnect backoffs so the suite never sleeps for real.

    The backoffs (Story 2.2) are exercised explicitly in test_broker_chain_fallback.py
    and test_broker_reconnect.py; everywhere else they would only add wall-clock time.
    """
    monkeypatch.setattr(_broker, "_RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(_service, "_RECONNECT_BACKOFF_S", 0)


async def await_true(predicate, timeout=2.0):
    """Poll a state predicate to a bounded deadline (no fixed-sleep anchors — Epic 2
    retro #1). Shared by the state/reflex suites so an interface change fails in one
    place."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


class DummySpawner:
    """A spawner whose `ready()` is a no-op; used where a Core is constructed but its
    turn loop is never driven (state/reflex unit tests)."""

    async def ready(self):  # pragma: no cover - never run in these tests
        pass


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
