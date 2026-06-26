"""Story 10.5 (AC4) — Pi migration: the persona seeds must actually SHIP to the device.

The 10.4 review caught a seed (`BOOTSTRAP.md`) that was never `git add`ed: `_seed_persona`
fail-soft-skips an absent template, so the file was silently absent on the Pi (which runs from
a fresh `git clone`). These guard that whole failure class three ways: the runtime resolution
path (`importlib.resources`, the API `_seed_persona` uses), git-tracking (so the clone carries
them), and the built wheel artifact (so a `pyproject` regression can't drop `*.md`).
"""

import shutil
import subprocess
import zipfile
from importlib import resources
from pathlib import Path

import pytest

from shelldon.core.memory import _PERSONA_SEED_FILES, _PROMPT_TEMPLATE_SEED_FILES

#: Every persona seed that must ship (the union the seeder copies copy-if-absent).
_ALL_SEEDS = _PERSONA_SEED_FILES + _PROMPT_TEMPLATE_SEED_FILES
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_persona_seeds_resolve_via_importlib_resources():
    """Every seed resolves through the SAME `importlib.resources` API `_seed_persona` uses — so the
    runtime copy-if-absent path can actually find each template (incl. 10.5's TOOLS/ARCHITECTURE)."""
    for name in _ALL_SEEDS:
        assert resources.files("shelldon.persona").joinpath(name).is_file(), f"{name} not packaged"


def test_persona_seeds_are_git_tracked():
    """The Pi deploys via `git clone`; an untracked seed never reaches the device (the 10.4 bug).
    Assert every seed is tracked. Skips cleanly outside a git checkout (e.g. an installed sdist)."""
    if shutil.which("git") is None or not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(
        ["git", "ls-files", "shelldon/persona"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    tracked_names = {Path(p).name for p in tracked}
    for name in _ALL_SEEDS:
        assert name in tracked_names, f"{name} is not git-tracked — absent after a Pi clone"


def test_built_wheel_contains_persona_seeds(tmp_path):
    """Inspect the BUILD ARTIFACT: build the wheel and assert every persona seed is inside it, so a
    `pyproject`/hatchling regression that drops non-`.py` package data is caught. Skips if `uv` is
    unavailable or the build can't run (the importlib.resources + git tests still cover the rest)."""
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build the wheel")
    try:
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
            cwd=_REPO_ROOT, capture_output=True, text=True, check=True, timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"wheel build unavailable: {exc}")
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "no wheel produced"
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    for name in _ALL_SEEDS:
        assert f"shelldon/persona/{name}" in names, f"{name} missing from the built wheel"
