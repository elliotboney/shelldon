"""Shared test fixtures."""

import shutil
import tempfile
from pathlib import Path

import pytest


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
