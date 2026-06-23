"""Story 9.6: tool-policy hardening — defense-in-depth on the RISKY network/shell tools.

All worker-side unit tests; no real network/DNS (MockTransport + stubbed `getaddrinfo`), no
real daemons (mechanism inspection). The owner-approval flow is unchanged (tested in 9.3) — these
cover only what runs AFTER approval.
"""

import subprocess

import httpx
import pytest

from shelldon.worker import tools
from shelldon.worker.tools import _assert_host_allowed, _deny_sensitive, _git, _http_get


def _stub_dns(monkeypatch, ip: str):
    """Resolve any host to `ip` (no real DNS)."""
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 0))])


# ============================ AC1: http_get SSRF + streaming ============================


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
def test_host_allowed_blocks_loopback_linklocal_every_hop(monkeypatch, ip):
    _stub_dns(monkeypatch, ip)
    with pytest.raises(ValueError):
        _assert_host_allowed("http://anything/", is_redirect=False)  # blocked even on the initial hop


def test_host_allowed_permits_private_on_initial_but_blocks_on_redirect(monkeypatch):
    _stub_dns(monkeypatch, "10.0.0.5")  # private range
    # Initial owner-approved URL to a LAN host is allowed (the owner typed it)...
    _assert_host_allowed("http://nas.lan/", is_redirect=False)
    # ...but a REDIRECT into private space is the attack — blocked.
    with pytest.raises(ValueError):
        _assert_host_allowed("http://nas.lan/", is_redirect=True)


def test_host_allowed_permits_public(monkeypatch):
    _stub_dns(monkeypatch, "93.184.216.34")
    _assert_host_allowed("https://example.com/", is_redirect=False)
    _assert_host_allowed("https://example.com/", is_redirect=True)


def test_http_get_rejects_redirect_to_internal(monkeypatch):
    # example.com → public; the redirect target → cloud-metadata IP (link-local) → blocked mid-chain.
    def _resolve(host, *a, **k):
        ip = "169.254.169.254" if host == "metadata.internal" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(tools.socket, "getaddrinfo", _resolve)

    def handler(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://metadata.internal/latest/meta-data/"})
        return httpx.Response(200, text="SECRETS")  # must never be reached

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(ValueError, match="internal"):
        _http_get("http://example.com/", client=client)


def test_http_get_follows_safe_redirect(monkeypatch):
    _stub_dns(monkeypatch, "93.184.216.34")  # both hops public

    def handler(req):
        if req.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, text="arrived")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    out = _http_get("https://example.com/start", client=client)
    assert "200" in out and "arrived" in out


def test_http_get_streams_with_byte_cap(monkeypatch):
    from shelldon.worker.tools import _MAX_TOOL_OUTPUT_CHARS

    _stub_dns(monkeypatch, "93.184.216.34")
    big = "x" * (_MAX_TOOL_OUTPUT_CHARS * 4)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=big)),
                          follow_redirects=False)
    out = _http_get("https://example.com/", client=client)
    assert "truncated" in out
    assert len(out) < len(big)  # capped, not the full multi-MB body


def test_http_get_rejects_too_many_redirects(monkeypatch):
    _stub_dns(monkeypatch, "93.184.216.34")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "https://example.com/x"})),
        follow_redirects=False,
    )
    with pytest.raises(ValueError, match="too many redirects"):
        _http_get("https://example.com/", client=client)


# ============================ AC2: run_shell process group ============================


def test_run_subprocess_starts_new_session(monkeypatch, tmp_path):
    captured = {}
    real_popen = subprocess.Popen

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, argv, **kw):
            captured.update(kw)

        def communicate(self, timeout=None):
            return ("out", "")

    monkeypatch.setattr(tools.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools.os, "killpg", lambda pgid, sig: None)
    tools._run_subprocess(["echo", "hi"], cwd=tmp_path)
    assert captured.get("start_new_session") is True
    assert callable(captured.get("preexec_fn"))  # 9.5 RLIMIT preexec preserved


def test_run_subprocess_kills_group_on_timeout(monkeypatch, tmp_path):
    killed = []

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kw):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        def kill(self):
            pass

        def wait(self, timeout=None):
            return -9

    monkeypatch.setattr(tools.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(tools.os, "killpg", lambda pgid, sig: killed.append(pgid))
    with pytest.raises(subprocess.TimeoutExpired):
        tools._run_subprocess("sleep 99", cwd=tmp_path, shell=True)
    assert 999 in killed  # the whole process group was signalled


# ============================ AC3: git subcommand allowlist ============================


@pytest.mark.parametrize("args", ["status", "log -1", "diff", "commit -m hi", "commit -c HEAD",
                                  "log -C", "--version", "--no-pager status"])
def test_git_allows_safe_commands(monkeypatch, tmp_path, args):
    seen = {}

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        return "ok"

    monkeypatch.setattr(tools, "_run_subprocess", _fake_run)
    assert _git(args, workspace_root=tmp_path) == "ok"
    assert seen["argv"][0] == "git"


@pytest.mark.parametrize("args", [
    "clone https://evil/x",
    "-c core.sshCommand=bash status",
    "-C /etc status",
    "fetch --upload-pack=/bin/sh",
    "submodule add x",
    "daemon",
    "config core.sshCommand=bash",        # review: config writes can persist a code-exec hook
    "config user.name x",
    "status --git-dir=/etc",              # review: repo-redirect rejected post-subcommand too
    "status --work-tree=/etc",
])
def test_git_rejects_dangerous_commands(monkeypatch, tmp_path, args):
    monkeypatch.setattr(tools, "_run_subprocess", lambda *a, **k: "SHOULD-NOT-RUN")
    with pytest.raises(ValueError):
        _git(args, workspace_root=tmp_path)


# ============================ AC4: credential blocklist ============================


@pytest.mark.parametrize("name", ["key.pem", "server.key", "id_rsa", "id_ed25519", ".env.bak",
                                   "cert.p12", "site.crt", "ID_RSA", "Server.KEY",
                                   "id_ecdsa_sk", "id_ed448", "id_custom"])  # review: id_* wildcard
def test_deny_sensitive_blocks_credentials(tmp_path, name):
    with pytest.raises(ValueError):
        _deny_sensitive(tmp_path / name, tmp_path / "memory")


@pytest.mark.parametrize("name", ["notes.txt", "tool.py", "data.json", "readme.md"])
def test_deny_sensitive_allows_normal_files(tmp_path, name):
    _deny_sensitive(tmp_path / name, tmp_path / "memory")  # no raise
