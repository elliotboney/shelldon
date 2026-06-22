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
import logging
import shlex
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from shelldon.contracts import ToolCall, ToolResult, ToolTier
from shelldon.core.memory import DEFAULT_MEMORY_ROOT

log = logging.getLogger("shelldon.worker.tools")

#: The single workspace root all FREE file tools are jailed to (Story 9.2). Sibling of
#: the memory tree (`~/.shelldon/memory`), NOT under it — so `vault/` is structurally
#: outside the jail. `app.py` creates it at startup with NORMAL perms so the dropped
#: worker uid can read it (contrast `vault/`'s 0o700). Module const, overridable in tests.
DEFAULT_WORKSPACE_ROOT = Path.home() / ".shelldon" / "workspace"

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


def _deny_sensitive(candidate: Path, memory_root: Path) -> None:
    """Defense in depth (AC4): refuse the secrets tree and credential-shaped files even
    if they somehow sit inside the jail (uid-drop is a no-op on the non-root Pi). `vault/`
    is at `<memory_root>/vault`; `.env`/`*.env` are credential files."""
    vault = (Path(memory_root) / "vault").resolve()
    if candidate == vault or candidate.is_relative_to(vault):
        raise ValueError(f"access denied: {candidate} is in the vault")
    if candidate.name.lower() == ".env" or candidate.suffix.lower() == ".env":
        raise ValueError(f"access denied: {candidate.name} is a credential file")


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


def _run_subprocess(argv, *, cwd: Path, shell: bool = False) -> str:
    """Run a subprocess bounded by `_RISKY_TIMEOUT_S` in `cwd`, returning capped combined
    output + exit code. A timeout raises (→ ToolResult(ok=False)); a non-zero exit is NOT an
    error (it's normal tool output the model should see)."""
    proc = subprocess.run(
        argv, cwd=str(cwd), shell=shell, capture_output=True, text=True,
        timeout=_RISKY_TIMEOUT_S,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return _cap(f"exit {proc.returncode}\n{out}".rstrip())


def _run_shell(command: str, *, workspace_root: Path) -> str:
    """RISKY: run a shell command in the workspace cwd (best-effort time-bounded). Gated by
    approval — the owner sees the exact command before it runs."""
    return _run_subprocess(command, cwd=workspace_root, shell=True)


def _git(args: str, *, workspace_root: Path) -> str:
    """RISKY: run `git <args>` in the workspace cwd. `args` is split safely (no shell)."""
    return _run_subprocess(["git", *shlex.split(args)], cwd=workspace_root, shell=False)


def _http_get(url: str) -> str:
    """RISKY: plain HTTP(S) GET (no credentials — NFR9: credentialed API tools stay
    broker-side/deferred). `httpx` is already a dep (transport uses it), lazily imported."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"http_get only supports http(s) URLs, got {url!r}")
    if urlparse(url).username or urlparse(url).password:
        # Credentials embedded in the URL would be sent worker-side (NFR9: creds are broker-only).
        raise ValueError("credentials in the URL are not allowed")
    import httpx  # lazy: only when this tool actually runs

    resp = httpx.get(url, timeout=_RISKY_TIMEOUT_S, follow_redirects=True)
    return _cap(f"HTTP {resp.status_code}\n{resp.text}")


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


def build_tool_registry(
    workspace_root: Path | None = None, memory_root: Path | None = None
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
    return {s.name: s for s in specs}
