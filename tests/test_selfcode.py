"""Story 9.4: core/selfcode — stage / gate / promote / discard of a self-coded tool.

The gate runs a REAL pytest subprocess on the staged test (bounded), so these tests are a touch
slower than the pure-unit suites; each uses a `tmp_path` workspace and never touches real $HOME.
"""

import asyncio

import pytest

from shelldon.core import selfcode
from shelldon.core.selfcode import live_tools_dir, quarantine_dir, staging_dir

# A minimal, well-formed tool module + a passing test for it (the convention: run/DESCRIPTION/
# PARAMS_SCHEMA at module level, no shelldon imports). The test imports the staged module by stem.
_GOOD_CODE = (
    "DESCRIPTION = 'add two ints'\n"
    "PARAMS_SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}}\n"
    "def run(a=0, b=0):\n"
    "    return str(int(a) + int(b))\n"
)
_GOOD_TEST = (
    "import adder\n"
    "def test_adds():\n"
    "    assert adder.run(2, 3) == '5'\n"
)


def test_stage_writes_the_pair(tmp_path):
    module_path, test_path = selfcode.stage("adder", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    assert module_path == staging_dir(tmp_path) / "adder.py"
    assert test_path == staging_dir(tmp_path) / "test_adder.py"
    assert module_path.read_text() == _GOOD_CODE
    assert test_path.read_text() == _GOOD_TEST


def test_stage_slugifies_unsafe_name(tmp_path):
    module_path, _ = selfcode.stage("../My Weather!", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    # Traversal/separators/spaces collapse to '_'; no path escape, import-clean stem.
    assert module_path.parent == staging_dir(tmp_path)
    assert module_path.stem == "_my_weather" or module_path.stem == "my_weather"  # leading run stripped


def test_stage_rejects_empty_name(tmp_path):
    with pytest.raises(ValueError):
        selfcode.stage("...///", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)


def test_stage_rejects_oversized_source(tmp_path):
    huge = "x = 1\n" * 20000  # > 64KB
    with pytest.raises(ValueError):
        selfcode.stage("big", huge, _GOOD_TEST, workspace_root=tmp_path)


async def test_gate_passes_a_good_tool(tmp_path):
    module_path, _ = selfcode.stage("adder", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    passed, output = await selfcode.run_gate(module_path.stem, workspace_root=tmp_path)
    assert passed, output


async def test_gate_fails_when_test_fails(tmp_path):
    bad_test = "import adder\ndef test_adds():\n    assert adder.run(2, 3) == '6'\n"
    module_path, _ = selfcode.stage("adder", _GOOD_CODE, bad_test, workspace_root=tmp_path)
    passed, output = await selfcode.run_gate(module_path.stem, workspace_root=tmp_path)
    assert not passed
    assert "fail" in output.lower() or "assert" in output.lower()


async def test_gate_rejects_llm_import(tmp_path):
    evil = "import anthropic\nDESCRIPTION='x'\nPARAMS_SCHEMA={}\ndef run():\n    return 'x'\n"
    module_path, _ = selfcode.stage("evil", evil, _GOOD_TEST, workspace_root=tmp_path)
    passed, output = await selfcode.run_gate(module_path.stem, workspace_root=tmp_path)
    assert not passed
    assert "anthropic" in output and "forbidden" in output.lower()


async def test_gate_rejects_core_import(tmp_path):
    evil = "from shelldon.core import runtime\nDESCRIPTION='x'\nPARAMS_SCHEMA={}\ndef run():\n    return 'x'\n"
    module_path, _ = selfcode.stage("sneaky", evil, _GOOD_TEST, workspace_root=tmp_path)
    passed, output = await selfcode.run_gate(module_path.stem, workspace_root=tmp_path)
    assert not passed
    assert "shelldon.core" in output


async def test_gate_times_out_on_a_hanging_test(tmp_path):
    hang = "import time\ndef test_hangs():\n    time.sleep(30)\n"
    module_path, _ = selfcode.stage("slow", _GOOD_CODE, hang, workspace_root=tmp_path)
    passed, output = await selfcode.run_gate(module_path.stem, workspace_root=tmp_path, timeout_s=2.0)
    assert not passed
    assert "timed out" in output.lower()


def test_promote_moves_staged_to_live(tmp_path):
    module_path, test_path = selfcode.stage("adder", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    assert selfcode.promote("adder", workspace_root=tmp_path) is True
    live = live_tools_dir(tmp_path) / "adder.py"
    assert live.read_text() == _GOOD_CODE
    assert not module_path.exists()  # moved out of staging
    assert not test_path.exists()  # staged test dropped on promote


def test_discard_deletes_the_pair(tmp_path):
    module_path, test_path = selfcode.stage("adder", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    selfcode.discard("adder", workspace_root=tmp_path)
    assert not module_path.exists() and not test_path.exists()


def test_discard_is_fail_soft_when_absent(tmp_path):
    selfcode.discard("never-staged", workspace_root=tmp_path)  # no raise


@pytest.mark.parametrize(
    "src",
    [
        "import anthropic",
        "import openai.types",
        "from openai import OpenAI",
        "from shelldon.core import runtime",
        "import shelldon.core.runtime",
        "from shelldon import core",  # review finding: the mod field alone is just 'shelldon'
        "from shelldon import core as c",
    ],
)
def test_forbidden_import_catches_all_reach_into_core_or_llm(src):
    assert selfcode._forbidden_import(src + "\n") is not None


@pytest.mark.parametrize(
    "src",
    ["import json", "from shelldon import contracts", "from shelldon.contracts import ToolCall", "x = 1"],
)
def test_forbidden_import_allows_clean_modules(src):
    assert selfcode._forbidden_import(src + "\n") is None


# --- Story 9.5: quarantine, gate-cancel cleanup, keyword/slug guards, dynamic-import detection ---


def test_quarantine_moves_live_to_quarantine(tmp_path):
    ld = live_tools_dir(tmp_path)
    ld.mkdir(parents=True)
    (ld / "bad.py").write_text("x = 1\n")
    assert selfcode.quarantine("bad", workspace_root=tmp_path) is True
    assert not (ld / "bad.py").exists()  # moved out of the live dir (no longer discovered)
    assert (quarantine_dir(tmp_path) / "bad.py").read_text() == "x = 1\n"


def test_quarantine_missing_is_fail_soft(tmp_path):
    assert selfcode.quarantine("never-live", workspace_root=tmp_path) is False


def test_quarantine_is_idempotent(tmp_path):
    ld = live_tools_dir(tmp_path)
    ld.mkdir(parents=True)
    (ld / "bad.py").write_text("x = 1\n")
    assert selfcode.quarantine("bad", workspace_root=tmp_path) is True
    # A repeat strike re-calls quarantine; the module is already gone → quiet False (review fix).
    assert selfcode.quarantine("bad", workspace_root=tmp_path) is False


async def test_run_gate_kills_subprocess_on_cancel(tmp_path, monkeypatch):
    selfcode.stage("ok", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    killed = []

    class FakeProc:
        returncode = None

        def kill(self):
            killed.append(True)

        async def communicate(self):
            await asyncio.Event().wait()  # hang until cancelled

        async def wait(self):
            return 0

    async def fake_exec(*a, **kw):
        return FakeProc()

    monkeypatch.setattr(selfcode.asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(selfcode.run_gate("ok", workspace_root=tmp_path, timeout_s=99))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True]  # the cancel killed the subprocess (no orphan)


async def test_run_gate_cancel_reaps_even_if_wait_raises(tmp_path, monkeypatch):
    """Review fix: a SECOND cancel during the reap (`proc.wait()` raises) must not abort the kill
    or mask the original CancelledError re-raise."""
    selfcode.stage("ok", _GOOD_CODE, _GOOD_TEST, workspace_root=tmp_path)
    killed = []

    class FakeProc:
        returncode = None

        def kill(self):
            killed.append(True)

        async def communicate(self):
            await asyncio.Event().wait()

        async def wait(self):
            raise asyncio.CancelledError()  # a second cancel lands during the reap

    async def fake_exec(*a, **kw):
        return FakeProc()

    monkeypatch.setattr(selfcode.asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(selfcode.run_gate("ok", workspace_root=tmp_path, timeout_s=99))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [True]  # killed despite wait() raising


def test_safe_tool_name_keyword_guard():
    assert selfcode._safe_tool_name("class") == "class_tool"  # hard keyword
    assert selfcode._safe_tool_name("match") == "match_tool"  # soft keyword
    assert selfcode._safe_tool_name("weather") == "weather"  # normal name untouched


def test_stage_re_stage_same_stem_is_clean(tmp_path, caplog):
    selfcode.stage("tool", "code1\n", "def test_a():\n    pass\n", workspace_root=tmp_path)
    with caplog.at_level("WARNING", logger="shelldon.core.selfcode"):
        mp, tp = selfcode.stage("tool", "code2\n", "def test_b():\n    pass\n", workspace_root=tmp_path)
    assert mp.read_text() == "code2\n" and tp.read_text() == "def test_b():\n    pass\n"
    assert any("overwriting" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "src",
    ['__import__("anthropic")', 'importlib.import_module("openai")',
     'importlib.import_module("shelldon.core.runtime")'],
)
def test_forbidden_import_catches_dynamic_imports(src):
    assert selfcode._forbidden_import(src + "\n") is not None


def test_forbidden_import_dynamic_nonliteral_is_unverifiable(caplog):
    # A non-literal arg can't be checked statically — not rejected, but logged as unverifiable.
    with caplog.at_level("WARNING", logger="shelldon.core.selfcode"):
        assert selfcode._forbidden_import("mod = something\n__import__(mod)\n") is None
    assert any("unverifiable" in r.message for r in caplog.records)
