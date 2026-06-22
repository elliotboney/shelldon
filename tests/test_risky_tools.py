"""Story 9.3: the four RISKY-tier tools (write_file / run_shell / http_get / git).

Unit tests over a tmp_path workspace — no bus, no LLM, no approval flow (that's
test_risky_approval.py). Just the tool functions: happy path + the safety guards.
"""

import shutil

import pytest

from shelldon.contracts import ToolTier
from shelldon.worker.tools import (
    _git,
    _http_get,
    _run_shell,
    _write_file,
    build_tool_registry,
)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    mem = tmp_path / "memory"
    (mem / "vault").mkdir(parents=True)
    ws.mkdir()
    return ws, mem


# --- registry wiring ---


def test_risky_tools_registered_as_risky():
    reg = build_tool_registry()
    for name in ("write_file", "run_shell", "http_get", "git"):
        assert reg[name].tier is ToolTier.RISKY


# --- write_file (the first writer tool) ---


def test_write_file_writes_within_jail(workspace):
    ws, mem = workspace
    out = _write_file("notes/todo.txt", "buy milk", workspace_root=ws, memory_root=mem)
    assert (ws / "notes" / "todo.txt").read_text() == "buy milk"
    assert "wrote" in out


def test_write_file_rejects_escape(workspace, tmp_path):
    ws, mem = workspace
    with pytest.raises(ValueError):
        _write_file("../escape.txt", "x", workspace_root=ws, memory_root=mem)
    assert not (tmp_path / "escape.txt").exists()


def test_write_file_rejects_dotenv(workspace):
    ws, mem = workspace
    with pytest.raises(ValueError):
        _write_file(".env", "API_KEY=leak", workspace_root=ws, memory_root=mem)


# --- run_shell ---


def test_run_shell_runs_in_workspace_cwd(workspace):
    ws, mem = workspace
    (ws / "marker.txt").write_text("x")
    out = _run_shell("ls", workspace_root=ws)
    assert "marker.txt" in out and "exit 0" in out


def test_run_shell_nonzero_exit_is_output_not_error(workspace):
    ws, mem = workspace
    out = _run_shell("exit 3", workspace_root=ws)
    assert "exit 3" in out  # a non-zero exit is normal tool output, not a raised error


# --- http_get ---


def test_http_get_returns_status_and_body(monkeypatch):
    import httpx

    from shelldon.worker import tools

    # No real network/DNS: stub the SSRF resolve to a public IP + inject a MockTransport client.
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="hello world")),
                          follow_redirects=False)
    out = _http_get("https://example.com", client=client)
    assert "200" in out and "hello world" in out


def test_http_get_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        _http_get("file:///etc/passwd")


def test_http_get_rejects_url_embedded_credentials():
    """Review #4 (NFR9): credentials in the URL would be sent worker-side — reject."""
    with pytest.raises(ValueError):
        _http_get("https://user:pass@example.com/x")


def test_write_file_rejects_oversized_content(workspace):
    """Review #5: cap write size to bound Pi disk use (reject, never a partial write)."""
    from shelldon.worker.tools import _MAX_READ_BYTES

    ws, mem = workspace
    with pytest.raises(ValueError):
        _write_file("big.txt", "x" * (_MAX_READ_BYTES + 1), workspace_root=ws, memory_root=mem)
    assert not (ws / "big.txt").exists()  # nothing written


# --- git ---


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_runs_in_workspace(workspace):
    ws, mem = workspace
    out = _git("--version", workspace_root=ws)
    assert "git version" in out and "exit 0" in out
