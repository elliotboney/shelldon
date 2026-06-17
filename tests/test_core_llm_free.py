"""AC2: the import-linter forbidden contract passes on the clean tree.

Ties the LLM-free-core guard (AD-1) into the test suite. The guard is also
run as its own CI step; this test fails fast if core/ ever imports an LLM SDK.
"""
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("lint-imports") is None, reason="import-linter not installed")
def test_core_is_llm_free():
    result = subprocess.run(["lint-imports"], capture_output=True, text=True)
    assert result.returncode == 0, f"import-linter contract failed:\n{result.stdout}\n{result.stderr}"
