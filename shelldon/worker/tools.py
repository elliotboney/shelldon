"""Worker-side tool registry + execution (Epic 9, Story 9.1).

The worker is the SOLE tool executor (AD-2: the broker only normalizes wire formats,
it never calls `fn`). A `ToolSpec` is worker-only — it carries a `Callable` that cannot
serialize across the bus, so it is a plain dataclass, NOT a msgspec struct. Only the
serializable `ToolDefinition` (name/description/schema/tier) ever travels to the broker.

This module lives in `shelldon.worker` — the `core is LLM-free` import-linter contract
covers `shelldon.core`, not `shelldon.worker`, so a tool module here is fine. It imports
NO provider SDK and nothing from `shelldon.broker` (kept SDK-free, AD-1 spirit).

Fail-soft discipline (Story 9.1): `execute_tool` catches EVERY exception from a tool and
an unknown tool name, returning `ToolResult(ok=False, ...)` — a bad tool call is fed back
to the model as an error it can recover from, the turn never raises.
"""

import ast
import builtins
import datetime
import functools
import importlib.util
import ipaddress
import logging
import os
import shlex
import signal
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from shelldon.contracts import ToolCall, ToolResult, ToolTier
from shelldon.core.memory import DEFAULT_MEMORY_ROOT

#: The workspace layout lives in `core.selfcode` (Story 9.4 relocated it there so core owns the
#: tool dirs without a core→worker import — AD-5). Re-exported here so existing
#: `worker.tools.DEFAULT_WORKSPACE_ROOT` callers (and the file-tool jail below) are unchanged.
from shelldon.core.selfcode import DEFAULT_WORKSPACE_ROOT, live_tools_dir
from shelldon.core.limits import resource_cap_preexec

log = logging.getLogger("shelldon.worker.tools")

#: Cap on `read_file` so one huge file can't blow the 416MB Pi. Bytes past this are
#: dropped with an inline truncation marker (never silently). 9.5 deepens resource caps.
_MAX_READ_BYTES = 64 * 1024

#: Default wall-clock bound for `python_eval` (seconds). Tests inject a tiny value.
_EVAL_TIMEOUT_S = 2.0

#: Wall-clock bound for the RISKY subprocess/network tools (`run_shell`/`git`/`http_get`).
#: Best-effort (`subprocess`/`httpx` timeout); hard CPU/mem caps (RLIMIT) are Story 9.5.
_RISKY_TIMEOUT_S = 20.0

#: Cap on any RISKY tool's returned output (stdout/stderr/body) — keep one big result off
#: the bus / out of the message list on the 416MB Pi (mirrors `read_file`/`python_eval`).
_MAX_TOOL_OUTPUT_CHARS = 16 * 1024

#: Max redirect hops `http_get` follows (Story 9.6). Each hop's host is re-validated; the body
#: is never auto-fetched across an unvalidated redirect (the SSRF surface).
_MAX_REDIRECTS = 5

#: Cap on the `python_eval` result string so a big computation (e.g. `'x'*10**7`) can't
#: ship a multi-MB ToolResult across the bus / into the message list on the 416MB Pi
#: (mirrors `read_file`'s byte cap). A true in-eval memory bound is Story 9.5 (RLIMIT).
_MAX_EVAL_OUTPUT_CHARS = 16 * 1024

#: The ONLY builtins a `python_eval` snippet may touch — pure compute, no side effects.
#: Deliberately omits `open`, `__import__`, `eval`, `exec`, `compile`, `globals`,
#: `locals`, `vars`, `getattr`/`setattr`, `input`, `type`, `object` — so a snippet that
#: reaches for the filesystem/network/imports raises `NameError` and fails closed (AC3).
#: `format` is omitted too: `"{0.__class__}".format(obj)` is a getattr-via-format escape.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "chr", "complex",
    "dict", "divmod", "enumerate", "filter", "float", "frozenset", "hex", "int",
    "len", "list", "map", "max", "min", "oct", "ord", "pow", "range", "repr", "reversed",
    "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
)
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES}

#: `str.format`/`format_map` are method-on-literal escapes (`"{0.__class__...}".format(x)`)
#: that the dunder-attribute guard below can't see (the dunders live inside the format
#: string), so reject those method names outright.
_BANNED_ATTRS = frozenset({"format", "format_map"})


def _assert_eval_safe(tree: ast.AST) -> None:
    """Reject the well-known restricted-eval escapes (Story 9.2 review round 2): walking the
    object graph via dunders (`().__class__.__mro__[-1].__subclasses__()`), reaching
    `__globals__`/`__builtins__`, or the `str.format` getattr trick. Block ANY attribute or
    name that starts with `_` (covers all dunders + privates; normal compute never needs one)
    plus the format methods. NOT a true sandbox (design §6) — defense in depth that makes the
    common, model-likely escapes fail closed; hard isolation stays a RISKY-tier/9.5 concern."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # Attribute access into an existing object is the escape surface: block any
            # underscore-prefixed attr (all dunders + privates). Normal compute (`.upper()`,
            # `.items()`) never needs one.
            if node.attr.startswith("_") or node.attr in _BANNED_ATTRS:
                raise ValueError(f"attribute {node.attr!r} is not allowed in python_eval")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            # Only DUNDER names are dangerous (`__builtins__`, `__import__`); a bare `_` or
            # `_x` is a harmless comprehension/local binding, so allow those.
            raise ValueError(f"name {node.id!r} is not allowed in python_eval")


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: its serializable definition fields PLUS the `fn` to run it.

    `fn` is called with the model's args as keyword arguments (`fn(**call.args)`), so a
    no-arg tool like `get_time` is invoked as `get_time()` for `args={}`. Worker-only —
    the `fn` never crosses the bus (only the `ToolDefinition` projection does)."""

    name: str
    description: str
    params_schema: dict
    tier: ToolTier
    fn: Callable
    #: Story 9.5: True for an owner-approved self-coded tool (discovered from the live dir),
    #: False for a built-in. The loop uses it to attribute a run-failure to a self-coded tool
    #: (→ `Result.tool_failures` → core's quarantine ledger). Built-in failures never strike.
    self_coded: bool = False


def execute_tool(call: ToolCall, registry: dict[str, ToolSpec]) -> ToolResult:
    """Run the requested tool, catching ALL failures into a `ToolResult` (never raise).

    Unknown tool name → `ToolResult(ok=False, ...)`; a tool that raises →
    `ToolResult(ok=False, content=repr(exc))`. Either way the model gets an error it can
    recover from and the turn survives (Story 9.1 AC4)."""
    spec = registry.get(call.name)
    if spec is None:
        log.warning("worker: unknown tool %r requested", call.name)
        return ToolResult(id=call.id, ok=False, content=f"unknown tool: {call.name!r}")
    try:
        # Pass ONLY the args the tool's schema declares — the model controls `call.args`,
        # so an undeclared key would let it inject control kwargs the tool binds privately
        # (e.g. a file tool's `workspace_root`/`memory_root` jail roots, or `python_eval`'s
        # `timeout_s`), overriding the safety binding. Schema-filtering closes that (Story 9.2 review).
        allowed = set(spec.params_schema.get("properties", {}))
        safe_args = {k: v for k, v in call.args.items() if k in allowed}
        result = spec.fn(**safe_args)
        return ToolResult(id=call.id, ok=True, content=str(result))
    except Exception as exc:
        log.warning("worker: tool %r raised: %s: %s", call.name, type(exc).__name__, exc)
        return ToolResult(id=call.id, ok=False, content=repr(exc))


def _get_time() -> str:
    """The single FREE-tier tool that proves the loop end-to-end (Story 9.1 AC3).
    Stdlib only (0 new deps) — the current local date/time as an ISO-8601 string."""
    return datetime.datetime.now().isoformat()


# --- Story 9.2: FREE-tier read-only + pure-compute tools ---


def _resolve_in_jail(path: str, workspace_root: Path) -> Path:
    """Resolve `path` (relative→under root, absolute→as-is) to its REAL location and
    confirm it stays inside `workspace_root`. `.resolve()` dereferences symlinks BEFORE
    the containment check, so a symlink inside the workspace pointing out is caught. An
    absolute `path` drops `root` in the `/` join, so it too fails the containment check.
    Raises `ValueError` on escape (caught by `execute_tool` → fail-closed, AC1)."""
    root = workspace_root.resolve()
    candidate = (root / path).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path!r}")
    return candidate


#: Credential-shaped file suffixes refused regardless of tier (Story 9.6 AC4 broadens 9.2's
#: `.env` rule). Case-insensitive.
_CREDENTIAL_SUFFIXES = frozenset({
    ".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".htpasswd", ".keystore", ".jks", ".ppk",
})

#: Private-key file prefix — `id_rsa`/`id_ed25519`/`id_ecdsa_sk`/`id_ed448`/any custom `id_*` key
#: (review fix: the SSH convention is `id_<type>`, so match the whole `id_` prefix, not 4 stems).
_CREDENTIAL_NAME_PREFIXES = ("id_",)


def _deny_sensitive(candidate: Path, memory_root: Path) -> None:
    """Defense in depth (9.2 AC4 + 9.6 AC4): refuse the secrets tree and credential-shaped files
    even if they sit inside the jail (uid-drop is a no-op on the non-root Pi). `vault/` is at
    `<memory_root>/vault`; the credential set covers `.env`/`*.env`/`.env.*`, common key/cert
    suffixes, and `id_rsa`-style private keys (all case-insensitive)."""
    vault = (Path(memory_root) / "vault").resolve()
    if candidate == vault or candidate.is_relative_to(vault):
        raise ValueError(f"access denied: {candidate} is in the vault")
    name = candidate.name.casefold()
    suffix = candidate.suffix.casefold()
    if name == ".env" or suffix == ".env" or name.startswith(".env."):  # .env / x.env / .env.bak/.local
        raise ValueError(f"access denied: {candidate.name} is a credential file")
    if suffix in _CREDENTIAL_SUFFIXES:
        raise ValueError(f"access denied: {candidate.name} is a credential file")
    if any(name.startswith(p) for p in _CREDENTIAL_NAME_PREFIXES):
        raise ValueError(f"access denied: {candidate.name} is a private key file")


def _read_file(path: str, *, workspace_root: Path, memory_root: Path) -> str:
    """FREE: read a text file inside the workspace jail (capped at `_MAX_READ_BYTES`)."""
    candidate = _resolve_in_jail(path, workspace_root)
    _deny_sensitive(candidate, memory_root)
    if not candidate.is_file():
        raise FileNotFoundError(f"no such file in workspace: {path!r}")
    with candidate.open("rb") as f:
        raw = f.read(_MAX_READ_BYTES + 1)
    text = raw[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(raw) > _MAX_READ_BYTES:
        log.warning("worker: read_file truncated %s at %d bytes", path, _MAX_READ_BYTES)
        text += f"\n…[truncated at {_MAX_READ_BYTES} bytes]"
    return text


def _list_dir(path: str, *, workspace_root: Path, memory_root: Path) -> str:
    """FREE: list a directory inside the workspace jail (dirs marked with a trailing /)."""
    candidate = _resolve_in_jail(path, workspace_root)
    _deny_sensitive(candidate, memory_root)
    if not candidate.is_dir():
        raise NotADirectoryError(f"no such directory in workspace: {path!r}")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in candidate.iterdir())
    return "\n".join(entries) if entries else "(empty)"


def _python_eval(code: str, *, timeout_s: float = _EVAL_TIMEOUT_S) -> str:
    """FREE: evaluate a single pure-compute EXPRESSION in a restricted namespace, wall-clock
    bounded (AC2/AC3). No `open`/`os`/`subprocess`/`import` (a statement isn't valid in eval
    mode, and side-effecting builtins are absent → `NameError`). Any failure raises → the
    caller maps it to `ToolResult(ok=False)`. Bound is best-effort for pure-Python loops via
    `SIGALRM` in the worker's main thread (9.5 deepens caps; the 25s loop ceiling backstops)."""
    tree = ast.parse(code, "<python_eval>", "eval")  # SyntaxError on a bad/multi-statement snippet
    _assert_eval_safe(tree)  # block dunder/MRO + format escapes BEFORE compiling/running
    compiled = compile(tree, "<python_eval>", "eval")

    def _on_timeout(signum, frame):
        raise TimeoutError(f"python_eval exceeded {timeout_s}s")

    previous = signal.signal(signal.SIGALRM, _on_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        out = str(eval(compiled, {"__builtins__": _SAFE_BUILTINS}, {}))
    finally:
        # Disarm BEFORE restoring the handler: restoring first would leave the timer armed
        # against `previous` (default SIGALRM action terminates the process). The nested
        # finally guarantees the handler is restored even if a late tick fires here — so no
        # stale `_on_timeout` is ever left installed (Story 9.2 review round 2).
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            signal.signal(signal.SIGALRM, previous)
    if len(out) > _MAX_EVAL_OUTPUT_CHARS:
        out = out[:_MAX_EVAL_OUTPUT_CHARS] + "\n…[output truncated]"
    return out


# --- Story 9.3: RISKY-tier tools (gated by owner approval; run worker-side after a tap) ---


def _cap(text: str) -> str:
    """Truncate any RISKY tool's output to `_MAX_TOOL_OUTPUT_CHARS` (never silently)."""
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        return text[:_MAX_TOOL_OUTPUT_CHARS] + "\n…[output truncated]"
    return text


def _write_file(path: str, content: str, *, workspace_root: Path, memory_root: Path) -> str:
    """RISKY: write a text file inside the workspace jail. The FIRST writer tool — the 9.2
    read-only invariant is lifted ONLY here, and ONLY after owner approval. Reuses the jail +
    sensitive-path denial; creates parent dirs within the jail."""
    if len(content) > _MAX_READ_BYTES:
        # Reject (not truncate — a partial write corrupts the file) to bound Pi disk use.
        raise ValueError(f"content too large ({len(content)} chars > {_MAX_READ_BYTES} cap)")
    candidate = _resolve_in_jail(path, workspace_root)
    _deny_sensitive(candidate, memory_root)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


def _kill_pgroup(pgid) -> None:
    """SIGKILL a whole process group, swallowing the already-gone race (Story 9.6 AC2)."""
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _run_subprocess(argv, *, cwd: Path, shell: bool = False) -> str:
    """Run a subprocess bounded by `_RISKY_TIMEOUT_S` in `cwd`, returning capped combined
    output + exit code. A timeout raises (→ ToolResult(ok=False)); a non-zero exit is NOT an
    error (it's normal tool output the model should see).

    Story 9.5: a `preexec_fn` sets RLIMIT_AS/RLIMIT_CPU in the child so a spawned process can't
    escape the worker's caps. Story 9.6 (AC2): `start_new_session=True` puts the child in its OWN
    process group; on timeout AND on normal exit the WHOLE group is SIGKILLed (`os.killpg`) so a
    backgrounded child (`cmd &`, `disown`, a daemon) can't outlive the turn."""
    proc = subprocess.Popen(
        argv, cwd=str(cwd), shell=shell,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True, preexec_fn=resource_cap_preexec(),
    )
    try:
        pgid = os.getpgid(proc.pid)  # capture while alive (a zombie's pgid is unreadable once reaped)
    except OSError:
        pgid = None
    try:
        out, err = proc.communicate(timeout=_RISKY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # SIGKILL the group, then the leader, then a NO-timeout reap (review fix): a second
        # `communicate(timeout=1)` could raise a fresh TimeoutExpired that masks the real one —
        # after SIGKILL the leader dies promptly, so an unbounded `wait()` returns at once.
        _kill_pgroup(pgid)
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        proc.wait()  # reap the (now-killed) leader
        raise  # the ORIGINAL timeout → ToolResult(ok=False), matching the 9.3 behavior
    finally:
        _kill_pgroup(pgid)  # reap any orphaned backgrounded children, even on a clean exit
    combined = (out or "") + (err or "")
    return _cap(f"exit {proc.returncode}\n{combined}".rstrip())


def _run_shell(command: str, *, workspace_root: Path) -> str:
    """RISKY: run a shell command in the workspace cwd (best-effort time-bounded). Gated by
    approval — the owner sees the exact command before it runs."""
    return _run_subprocess(command, cwd=workspace_root, shell=True)


#: Closed allowlist of git subcommands the pet may run (Story 9.6 AC3) — read + local-history
#: verbs + the obvious sync verbs the owner approves per-call. NOT here (rejected): `clone`,
#: `submodule`, `daemon`, `archive`, `bundle`, `filter-branch`, anything that fetches/executes
#: arbitrary remote content.
#: Closed allowlist of git subcommands the pet may run (Story 9.6 AC3) — read + local-history
#: verbs + the obvious sync verbs the owner approves per-call. `config` is deliberately EXCLUDED
#: (review): `git config core.sshCommand=…` persists a hook that turns a later approved
#: `fetch`/`commit` into code-exec — exactly the escalation AC3 blocks via the `-c` global flag.
_GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "add", "commit", "branch", "checkout", "switch",
    "restore", "stash", "init", "fetch", "pull", "push", "remote", "tag", "rev-parse",
    "reset", "mv", "rm",
})

#: Benign info flags that take NO subcommand (so `git --version` is allowed).
_GIT_BENIGN_NO_SUBCOMMAND = frozenset({"--version", "--help", "-v", "-h"})

#: Flags dangerous ANYWHERE in the args (review fix — not just as global flags): the remote-command
#: / exec specifiers (turn fetch/push into exec) AND the repo-redirect / config-injection long flags
#: (`git status --git-dir=/etc` post-subcommand would otherwise escape the jail). The SHORT `-c`/`-C`
#: forms stay GLOBAL-only (benign post-subcommand: `git commit -c <commit>`, `git log -C`).
_GIT_EXEC_FLAGS = ("--upload-pack", "--receive-pack", "--exec", "--namespace",
                   "--git-dir", "--work-tree", "--exec-path", "--config-env")


def _git(args: str, *, workspace_root: Path) -> str:
    """RISKY: run `git <args>` in the workspace cwd. `args` is split safely (no shell). Story 9.6
    (AC3): the subcommand must be in `_GIT_ALLOWED_SUBCOMMANDS`; the exec/pack/repo-redirect
    specifiers are rejected anywhere; the `-c`/`-C` config-injection/chdir SHORT flags are rejected
    as GLOBAL flags — so an approved git call can't become code-exec or escape the jail."""
    parts = shlex.split(args)
    if not parts:
        raise ValueError("git: no command given")
    subcommand = None
    for tok in parts:
        if any(tok == p or tok.startswith(p + "=") for p in _GIT_EXEC_FLAGS):
            raise ValueError(f"git: flag {tok!r} is not allowed")
        if subcommand is None and tok.startswith("-"):
            # a GLOBAL flag (precedes the subcommand) — block config-injection / chdir short forms
            if tok.startswith(("-c", "-C")):
                raise ValueError(f"git: global flag {tok!r} is not allowed")
            continue  # other benign global flag (--version, --no-pager, …)
        if subcommand is None:
            subcommand = tok
    if subcommand is None:
        # Only flags — allow exclusively the benign info flags (`git --version`).
        if not all(tok in _GIT_BENIGN_NO_SUBCOMMAND for tok in parts):
            raise ValueError(f"git: no allowed subcommand in {args!r}")
    elif subcommand not in _GIT_ALLOWED_SUBCOMMANDS:
        raise ValueError(f"git: subcommand {subcommand!r} is not allowed")
    return _run_subprocess(["git", *parts], cwd=workspace_root, shell=False)


def _assert_url_shape(url: str) -> None:
    """http(s)-only + no URL-embedded credentials (NFR9 — creds are broker-only). Story 9.3 guards,
    re-applied to each redirect hop in 9.6."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"http_get only supports http(s) URLs, got {url!r}")
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ValueError("credentials in the URL are not allowed")


def _assert_host_allowed(url: str, *, is_redirect: bool) -> None:
    """SSRF guard (Story 9.6 AC1): resolve the URL's host to its IP(s) and reject internal targets.
    Loopback / link-local (incl. the `169.254.169.254` cloud-metadata IP) / unspecified are blocked
    on EVERY hop; private + reserved ranges are blocked only on a REDIRECT hop (the owner explicitly
    approved the initial URL's host, so a LAN fetch they typed is allowed — a *redirect* into
    internal space is the attack). Resolving (not string-matching) defeats `evil.com → 10.0.0.1`.
    Fails CLOSED — an unresolvable host raises (this is a security boundary)."""
    host = urlparse(url).hostname
    if not host:
        raise ValueError(f"http_get: no host in {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"http_get: cannot resolve host {host!r} ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"http_get: blocked host {host} ({ip}) — loopback/link-local/metadata")
        if is_redirect and (ip.is_private or ip.is_reserved):
            raise ValueError(f"http_get: blocked redirect to internal host {host} ({ip})")


def _read_capped(resp) -> str:
    """Stream a response body up to `_MAX_TOOL_OUTPUT_CHARS` bytes, then stop (Story 9.6 AC1) —
    so a multi-MB response never fully buffers into RAM on the 416MB Pi."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_TOOL_OUTPUT_CHARS:
            truncated = True
            break
    raw = b"".join(chunks)[:_MAX_TOOL_OUTPUT_CHARS]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += "\n…[output truncated]"
    return text


def _http_get(url: str, *, client=None) -> str:
    """RISKY: plain HTTP(S) GET (no credentials — NFR9). Story 9.6: follows redirects MANUALLY,
    re-validating each hop's host against the SSRF guard, and STREAMS the body with a byte cap.
    `httpx` is lazily imported (transitive dep). `client` is a test seam (inject a MockTransport
    client); production builds its own non-redirecting client."""
    _assert_url_shape(url)
    _assert_host_allowed(url, is_redirect=False)
    import httpx  # lazy: only when this tool actually runs

    own = client is None
    if own:
        client = httpx.Client(follow_redirects=False, timeout=_RISKY_TIMEOUT_S)
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = str(resp.url.join(resp.headers["location"]))
                    _assert_url_shape(url)
                    _assert_host_allowed(url, is_redirect=True)
                    continue
                return _cap(f"HTTP {resp.status_code}\n{_read_capped(resp)}")
        raise ValueError(f"http_get: too many redirects (>{_MAX_REDIRECTS})")
    finally:
        if own:
            client.close()


def summarize_call(call: ToolCall, spec: "ToolSpec") -> str:
    """A faithful one-line human summary of a pending RISKY call for the approval prompt —
    the owner must see what they're approving (the actual command/path/url)."""
    if call.name == "run_shell":
        return f"run_shell: {call.args.get('command', '')}"
    if call.name == "git":
        return f"git {call.args.get('args', '')}"
    if call.name == "http_get":
        return f"http_get: {call.args.get('url', '')}"
    if call.name == "write_file":
        return f"write_file: {call.args.get('path', '')}"
    return f"{call.name}: {call.args}"


#: Tool-module CONVENTION (Story 9.4) — a self-coded tool module defines these at module level,
#: with NO shelldon imports needed (keeps it import-clean + simple for the model to emit).
_TOOL_MODULE_ATTRS = ("run", "DESCRIPTION", "PARAMS_SCHEMA")


def discover_self_coded_tools(workspace_root: Path, *, skipped: list[str] | None = None) -> list[ToolSpec]:
    """Discover the owner-approved self-coded tools in the live dir (Story 9.4, AC4) and build a
    FREE-tier `ToolSpec` for each. Imports each `*.py` module via `importlib.util.spec_from_file_location`
    (the dir is NOT a package on `sys.path`), mirroring the plugin-host's per-module try/except
    skip (AD-8): a module missing the `run`/`DESCRIPTION`/`PARAMS_SCHEMA` convention or raising on
    import is SKIPPED + logged — the turn survives, a self-coded tool never wedges the worker.

    Story 9.5: a non-None `skipped` collects the stems of tools that failed to import/convention so
    the worker can report them on its Result (→ core's quarantine ledger, AD-8)."""
    ld = live_tools_dir(workspace_root)
    specs: list[ToolSpec] = []
    if not ld.is_dir():
        return specs
    for path in sorted(ld.glob("*.py")):
        if path.name.startswith(("_", "test_")):
            continue  # private/test files are not tools
        try:
            mod_spec = importlib.util.spec_from_file_location(f"shelldon_tool_{path.stem}", path)
            module = importlib.util.module_from_spec(mod_spec)
            mod_spec.loader.exec_module(module)
            for attr in _TOOL_MODULE_ATTRS:
                if not hasattr(module, attr):
                    raise AttributeError(f"missing {attr!r}")
            spec = ToolSpec(
                name=path.stem,
                description=module.DESCRIPTION,
                params_schema=module.PARAMS_SCHEMA,
                tier=ToolTier.FREE,
                fn=module.run,
                self_coded=True,
            )
        except Exception:
            log.warning("skipping self-coded tool %r — bad import or convention", path.name, exc_info=True)
            if skipped is not None:
                skipped.append(path.stem)  # report it so a repeatedly-bad tool gets quarantined (9.5)
            continue
        specs.append(spec)
    return specs


def build_tool_registry(
    workspace_root: Path | None = None, memory_root: Path | None = None, *,
    import_failures: list[str] | None = None,
) -> dict[str, ToolSpec]:
    """Return the FREE-tier tools available for the current turn. Story 9.1 shipped
    `get_time`; Story 9.2 adds `read_file`/`list_dir` (jailed to `workspace_root`) and
    `python_eval`. The broker seam and worker loop do not change. The file tools bind
    `workspace_root`/`memory_root` here so `execute_tool`'s `fn(**call.args)` passes only
    the model's `path`."""
    ws = DEFAULT_WORKSPACE_ROOT if workspace_root is None else Path(workspace_root)
    mr = DEFAULT_MEMORY_ROOT if memory_root is None else Path(memory_root)
    _PATH_SCHEMA = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path within the workspace."}},
        "required": ["path"],
    }
    specs = [
        ToolSpec(
            name="get_time",
            description="Get the current local date and time as an ISO-8601 string.",
            params_schema={"type": "object", "properties": {}, "required": []},
            tier=ToolTier.FREE,
            fn=_get_time,
        ),
        ToolSpec(
            name="read_file",
            description="Read a text file from your workspace. Returns the file contents.",
            params_schema=_PATH_SCHEMA,
            tier=ToolTier.FREE,
            fn=functools.partial(_read_file, workspace_root=ws, memory_root=mr),
        ),
        ToolSpec(
            name="list_dir",
            description="List the entries of a directory in your workspace.",
            params_schema=_PATH_SCHEMA,
            tier=ToolTier.FREE,
            fn=functools.partial(_list_dir, workspace_root=ws, memory_root=mr),
        ),
        ToolSpec(
            name="python_eval",
            description=(
                "Evaluate a single pure-Python expression for a quick computation "
                "(e.g. '2**10' or 'sum(range(100))'). No file, network, or import access."
            ),
            params_schema={
                "type": "object",
                "properties": {"code": {"type": "string", "description": "A Python expression."}},
                "required": ["code"],
            },
            tier=ToolTier.FREE,
            fn=_python_eval,
        ),
        # --- RISKY tier (Story 9.3): each call PAUSES the loop for owner approval ---
        ToolSpec(
            name="write_file",
            description="Write a text file in your workspace (creates/overwrites). Requires owner approval.",
            params_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path within the workspace."},
                    "content": {"type": "string", "description": "Text to write."},
                },
                "required": ["path", "content"],
            },
            tier=ToolTier.RISKY,
            fn=functools.partial(_write_file, workspace_root=ws, memory_root=mr),
        ),
        ToolSpec(
            name="run_shell",
            description="Run a shell command in your workspace. Requires owner approval.",
            params_schema={
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command."}},
                "required": ["command"],
            },
            tier=ToolTier.RISKY,
            fn=functools.partial(_run_shell, workspace_root=ws),
        ),
        ToolSpec(
            name="http_get",
            description="Fetch an http(s) URL (GET). Requires owner approval.",
            params_schema={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "An http(s) URL."}},
                "required": ["url"],
            },
            tier=ToolTier.RISKY,
            fn=_http_get,
        ),
        ToolSpec(
            name="git",
            description="Run a git command in your workspace (e.g. 'status', 'log -1'). Requires owner approval.",
            params_schema={
                "type": "object",
                "properties": {"args": {"type": "string", "description": "Arguments after 'git'."}},
                "required": ["args"],
            },
            tier=ToolTier.RISKY,
            fn=functools.partial(_git, workspace_root=ws),
        ),
    ]
    registry = {s.name: s for s in specs}
    # Story 9.4: merge in the owner-approved self-coded tools (FREE). A discovered tool may NOT
    # shadow a built-in name — built-ins win (a self-coded `read_file` can't override the jailed
    # one); the collision is skipped + logged.
    for tool in discover_self_coded_tools(ws, skipped=import_failures):
        if tool.name in registry:
            log.warning("self-coded tool %r shadows a built-in; keeping the built-in", tool.name)
            continue
        registry[tool.name] = tool
    return registry
