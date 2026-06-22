"""Story 9.2: the FREE-tier tool pack — read_file / list_dir / python_eval.

Pure unit tests over a tmp_path workspace (no bus, no LLM). File tools are driven through
`execute_tool` so the fail-closed → `ToolResult(ok=False)` contract (AC1/AC3/AC4) is what's
asserted; `python_eval`'s time bound is unit-tested directly so a tiny timeout keeps it fast.
"""

import pytest

from shelldon.contracts import ToolCall
from shelldon.worker.tools import (
    _MAX_READ_BYTES,
    _python_eval,
    build_tool_registry,
    execute_tool,
)


@pytest.fixture
def workspace(tmp_path):
    """A workspace root + a separate memory root (with a vault), both under tmp_path."""
    ws = tmp_path / "workspace"
    mem = tmp_path / "memory"
    (mem / "vault").mkdir(parents=True)
    ws.mkdir()
    return ws, mem


def _call(registry, name, **args):
    return execute_tool(ToolCall(id="t1", name=name, args=args), registry)


# --- read_file (AC1) ---


def test_read_file_happy_path(workspace):
    ws, mem = workspace
    (ws / "notes.txt").write_text("hello shelldon")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="notes.txt")
    assert res.ok and res.content == "hello shelldon"


def test_read_file_in_subdir(workspace):
    ws, mem = workspace
    (ws / "sub").mkdir()
    (ws / "sub" / "a.txt").write_text("nested")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="sub/a.txt")
    assert res.ok and res.content == "nested"


def test_read_file_missing_is_fail_closed(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="nope.txt")
    assert res.ok is False


def test_read_file_relative_escape_rejected(workspace, tmp_path):
    ws, mem = workspace
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="../secret.txt")
    assert res.ok is False and "TOP SECRET" not in res.content


def test_read_file_absolute_escape_rejected(workspace, tmp_path):
    ws, mem = workspace
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path=str(secret))
    assert res.ok is False and "TOP SECRET" not in res.content


def test_read_file_symlink_escape_rejected(workspace, tmp_path):
    ws, mem = workspace
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (ws / "link").symlink_to(secret)  # symlink inside the jail pointing OUT
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="link")
    assert res.ok is False and "TOP SECRET" not in res.content


def test_read_file_truncates_large_file(workspace):
    ws, mem = workspace
    (ws / "big.txt").write_text("x" * (_MAX_READ_BYTES + 100))
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "read_file", path="big.txt")
    assert res.ok and "truncated" in res.content
    assert len(res.content) <= _MAX_READ_BYTES + 64  # body capped + a short marker


# --- list_dir (AC1) ---


def test_list_dir_happy_path(workspace):
    ws, mem = workspace
    (ws / "notes.txt").write_text("x")
    (ws / "sub").mkdir()
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "list_dir", path=".")
    assert res.ok and "notes.txt" in res.content and "sub/" in res.content


def test_list_dir_escape_rejected(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "list_dir", path="..")
    assert res.ok is False


def test_list_dir_missing_is_fail_closed(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "list_dir", path="nope")
    assert res.ok is False


# --- vault / credential denial (AC4) ---


def test_vault_path_denied_even_inside_jail(tmp_path):
    """If the workspace overlaps the memory root, vault/ is still refused (defense in depth)."""
    root = tmp_path / "root"
    (root / "vault").mkdir(parents=True)
    (root / "vault" / "secret.txt").write_text("API_KEY=leak")
    reg = build_tool_registry(workspace_root=root, memory_root=root)
    res = _call(reg, "read_file", path="vault/secret.txt")
    assert res.ok is False and "leak" not in res.content


def test_dotenv_file_denied(workspace):
    ws, mem = workspace
    (ws / ".env").write_text("API_KEY=leak")
    (ws / "config.env").write_text("API_KEY=leak2")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "read_file", path=".env").ok is False
    assert _call(reg, "read_file", path="config.env").ok is False


# --- python_eval (AC2/AC3) ---


def test_python_eval_happy_path(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="1 + 2").content == "3"
    assert _call(reg, "python_eval", code="sum(range(101))").content == "5050"


def test_python_eval_blocks_open(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "python_eval", code="open('/etc/passwd').read()")
    assert res.ok is False  # NameError: open not in the restricted builtins


def test_python_eval_blocks_import(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="__import__('os').getcwd()").ok is False
    assert _call(reg, "python_eval", code="import os").ok is False  # not an expression


def test_python_eval_syntax_error_fails_closed(workspace):
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="1 +").ok is False


def test_python_eval_time_bound_raises():
    """A runaway pure-Python loop is killed by the SIGALRM bound (best-effort, AC2). A
    genexpr (Python bytecode per item, O(1) memory) is interruptible — a tiny timeout
    keeps the test fast. execute_tool's catch-all maps the raise to ToolResult(ok=False)."""
    with pytest.raises(TimeoutError):
        _python_eval("sum(1 for _ in range(10**9))", timeout_s=0.1)


# --- Review fixes (2026-06-21) ---


def test_execute_tool_drops_injected_jail_root(workspace):
    """Review #1 (HIGH): the model controls call.args, so an injected `workspace_root` must
    NOT override the privately-bound jail root — schema-filtering keeps it sealed."""
    ws, mem = workspace
    (ws / "notes.txt").write_text("inside the jail")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    # Inject a wide-open root + try to escape: the injected root is dropped, real jail holds.
    res = _call(reg, "read_file", path="notes.txt", workspace_root="/", memory_root="/")
    assert res.ok and res.content == "inside the jail"  # still read from the bound ws
    escape = _call(reg, "read_file", path="/etc/hostname", workspace_root="/")
    assert escape.ok is False  # injected root ignored → path still escapes the real jail


def test_python_eval_output_capped(workspace):
    """Review #2 (MED): a big result string is truncated so it can't bloat the bus/messages."""
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "python_eval", code="'x' * 1000000")
    assert res.ok and "truncated" in res.content
    assert len(res.content) < 1000000  # capped, not the full 1M chars


def test_dotenv_denial_is_case_insensitive(workspace):
    """Review #3 (LOW): `.ENV` / `*.ENV` are credential-shaped too."""
    ws, mem = workspace
    (ws / ".ENV").write_text("API_KEY=leak")
    (ws / "config.ENV").write_text("API_KEY=leak2")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "read_file", path=".ENV").ok is False
    assert _call(reg, "read_file", path="config.ENV").ok is False


# --- Review round 2 (2026-06-21) ---


def test_python_eval_blocks_dunder_mro_escape(workspace):
    """Round 2 #1 (Decision→A): the classic restricted-eval escape walks the object graph via
    dunders using zero restricted builtins — the AST guard must reject it (AC3 'nothing escapes')."""
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="().__class__").ok is False
    assert _call(reg, "python_eval", code="().__class__.__mro__[-1].__subclasses__()").ok is False
    assert _call(reg, "python_eval", code="__builtins__").ok is False


def test_python_eval_blocks_format_getattr_escape(workspace):
    """Round 2 #1: `str.format` reaches attributes via the format string — `format` builtin is
    dropped AND the `.format` method is blocked by the AST guard."""
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="'{0.__class__}'.format(())").ok is False


def test_python_eval_allows_normal_method_calls(workspace):
    """The dunder guard must NOT over-block ordinary compute (non-underscore methods / `_` locals)."""
    ws, mem = workspace
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    assert _call(reg, "python_eval", code="'hello'.upper()").content == "HELLO"
    assert _call(reg, "python_eval", code="sum(1 for _ in range(5))").content == "5"


def test_python_eval_restores_sigalrm_handler_after_run_and_timeout():
    """Round 2 #2: the SIGALRM handler is always restored (no stale `_on_timeout` left
    installed) — after a normal run AND after a timeout."""
    import signal

    before = signal.getsignal(signal.SIGALRM)
    _python_eval("1 + 1")
    assert signal.getsignal(signal.SIGALRM) is before
    with pytest.raises(TimeoutError):
        _python_eval("sum(1 for _ in range(10**9))", timeout_s=0.1)
    assert signal.getsignal(signal.SIGALRM) is before


def test_list_dir_empty_string_lists_root(workspace):
    """Round 2 deferred-test: `path=""` resolves to the workspace root (the `candidate == root`
    carve-out), same as `"."`."""
    ws, mem = workspace
    (ws / "a.txt").write_text("x")
    reg = build_tool_registry(workspace_root=ws, memory_root=mem)
    res = _call(reg, "list_dir", path="")
    assert res.ok and "a.txt" in res.content
