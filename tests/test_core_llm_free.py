"""AC2: the import-linter forbidden contract passes on the clean tree.

Ties the LLM-free-core guard (AD-1) into the test suite. This must never be
silently skipped — if `lint-imports` is unavailable the test fails, surfacing a
broken dev environment rather than a bypassed guard.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_core_is_llm_free():
    result = subprocess.run(
        ["lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import-linter contract failed:\n{result.stdout}\n{result.stderr}"
    )
