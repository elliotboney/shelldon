"""Story 9.4: core/selfcode — stage / gate / promote / discard of a self-coded tool.

The gate runs a REAL pytest subprocess on the staged test (bounded), so these tests are a touch
slower than the pure-unit suites; each uses a `tmp_path` workspace and never touches real $HOME.
"""

import pytest

from shelldon.core import selfcode
from shelldon.core.selfcode import live_tools_dir, staging_dir

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
