"""core/selfcode — the self-coded-tool workspace owner: stage / gate / promote / discard
(Epic 9, Story 9.4; AD-5/AD-1/AD-8).

Core is the SOLE writer of the workspace tool dirs (AD-5): the model proposes a `ProposeTool`
op, core stages the module + its pytest test to a STAGING dir, runs a bounded GATE (a pytest
subprocess + an AST import-check), and — only on a pass + owner approval — PROMOTES the module
to the LIVE dir, where the next fresh worker discovers it FREE (`worker/tools.discover_self_coded_tools`).

LLM-free (AD-1): this runs `pytest` via `asyncio.create_subprocess_exec` — a SUBPROCESS, never
an import — and AST-scans the staged source; no provider SDK ever enters `core/`. The dir layout
(`DEFAULT_WORKSPACE_ROOT` + the live/staging helpers) lives here so `worker/tools.py` and `app.py`
import it from core (worker→core / app→core are allowed; core→worker is the smell we avoid).

The gate runs UNTRUSTED, model-written code before the owner approves it (running the test
imports + calls the tool — inherent to "run the test to verify it"). Accepted for single-owner
(design §6): bounded by a subprocess timeout, run from the staging cwd, and the owner still
reviews the code at the Approve step. RLIMIT / no-network sandboxing is Story 9.5.
"""

import ast
import asyncio
import logging
import re
import shutil
import sys
import unicodedata
from pathlib import Path

log = logging.getLogger("shelldon.core.selfcode")

#: The single workspace root all FREE file tools are jailed to (relocated here from
#: `worker/tools.py` in Story 9.4 so core owns the layout without a core→worker import).
#: Sibling of the memory tree (`~/.shelldon/memory`), NOT under it. `app.py` creates it +
#: the live/staging subdirs at startup. Module const, overridable in tests.
DEFAULT_WORKSPACE_ROOT = Path.home() / ".shelldon" / "workspace"

#: Cap on a proposed tool's code/test source (chars). A runaway proposal can't fill the
#: 416MB Pi's disk; mirrors `worker/tools._MAX_READ_BYTES`. Rejected (not truncated).
_MAX_TOOL_SOURCE_CHARS = 64 * 1024

#: Default wall-clock bound for the gate's pytest subprocess (seconds). Generous enough for
#: pytest's startup; a gate that exceeds it is KILLED and treated as a fail. Tests inject small.
DEFAULT_GATE_TIMEOUT_S = 30.0

#: Cap on the gate's captured combined output (chars) — keep a verbose pytest log off the bus /
#: out of the owner's reply (mirrors the worker's `_MAX_TOOL_OUTPUT_CHARS`).
_MAX_GATE_OUTPUT_CHARS = 16 * 1024

#: The LLM SDKs a self-coded tool may NOT import — the import-linter "core is LLM-free" set
#: (AD-1), PLUS `shelldon.core` (a promoted tool is imported by the worker; it must never reach
#: back into core's domain). Mirrors `pyproject.toml`'s forbidden_modules.
_FORBIDDEN_TOP_MODULES = frozenset({"openai", "anthropic", "google", "litellm", "zhipuai", "ollama"})

#: A safe module stem: ascii word chars only (so the file is import-clean for the worker's
#: `spec_from_file_location` AND collectible by pytest, which can't import a hyphenated name).
_UNSAFE_TOOL_RE = re.compile(r"[^a-z0-9_]+")


def live_tools_dir(workspace_root) -> Path:
    """The dir the worker discovers promoted tools from (`<workspace>/tools/`)."""
    return Path(workspace_root) / "tools"


def staging_dir(workspace_root) -> Path:
    """The dir a proposed tool is staged + gated in before promotion (`<workspace>/tools-staging/`)."""
    return Path(workspace_root) / "tools-staging"


def _safe_tool_name(name: str) -> str:
    """`name` → a path-safe, import-safe module stem (mirrors `core/memory._safe_filename`'s
    discipline: NFC-normalize, casefold, collapse every non-`[a-z0-9_]` run — so all separators,
    `..`, dots, control chars — to `_`, strip edges). A leading digit is prefixed (`t_`) so the
    stem is a valid module identifier. An empty result (name was all separators) raises — the
    caller's reject signal (a tool needs a usable name)."""
    normalized = unicodedata.normalize("NFC", name).strip().casefold()
    stem = _UNSAFE_TOOL_RE.sub("_", normalized).strip("_")
    if not stem:
        raise ValueError(f"invalid tool name: {name!r}")
    if stem[0].isdigit():
        stem = "t_" + stem
    return stem


def stage(name: str, code: str, test: str, *, workspace_root) -> tuple[Path, Path]:
    """Write the proposed tool module (`<stem>.py`) + its pytest test (`test_<stem>.py`) to the
    staging dir (created if absent), returning their paths. Caps the source size (rejects an
    oversized proposal — a partial write would corrupt). The caller derives the stem from
    `module_path.stem` for the later gate/promote/discard."""
    if len(code) > _MAX_TOOL_SOURCE_CHARS or len(test) > _MAX_TOOL_SOURCE_CHARS:
        raise ValueError(
            f"tool source too large (code {len(code)} / test {len(test)} chars > {_MAX_TOOL_SOURCE_CHARS} cap)"
        )
    stem = _safe_tool_name(name)
    sd = staging_dir(workspace_root)
    sd.mkdir(parents=True, exist_ok=True)
    module_path = sd / f"{stem}.py"
    test_path = sd / f"test_{stem}.py"
    module_path.write_text(code, encoding="utf-8")
    test_path.write_text(test, encoding="utf-8")
    return module_path, test_path


def _forbidden_import(src: str) -> str | None:
    """AST-scan `src`; return the name of the first forbidden import (an LLM SDK or
    `shelldon.core`), else None. A syntax error is NOT an import rejection — the pytest gate
    fails it on its own with a clearer message — so it returns None here."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _FORBIDDEN_TOP_MODULES or alias.name == "shelldon.core" \
                        or alias.name.startswith("shelldon.core."):
                    return alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if top in _FORBIDDEN_TOP_MODULES or mod == "shelldon.core" or mod.startswith("shelldon.core."):
                return mod
            # `from shelldon import core` (or `... as c`): the module field is just "shelldon", so
            # the dotted path is only complete WITH the imported name — check each alias's fqn, or
            # the tool reaches core through a name the `mod`-only test above can't see (review fix).
            for alias in node.names:
                fqn = f"{mod}.{alias.name}" if mod else alias.name
                if fqn == "shelldon.core" or fqn.startswith("shelldon.core."):
                    return fqn
    return None


async def run_gate(stem: str, *, workspace_root, timeout_s: float = DEFAULT_GATE_TIMEOUT_S) -> tuple[bool, str]:
    """Gate a staged tool (AC1): (1) AST import-check the module — reject if it imports an LLM
    SDK or `shelldon.core` (the LLM-free-core invariant); (2) run `pytest -q` on the staged test
    as a bounded subprocess from the staging cwd. Returns `(passed, capped_output)`. A timeout
    KILLS the subprocess and is a fail; the import-check failing short-circuits before pytest
    ever runs the model's code. Never raises — a gate failure is data, not an exception."""
    sd = staging_dir(workspace_root)
    module_path = sd / f"{stem}.py"
    test_path = sd / f"test_{stem}.py"
    try:
        src = module_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"could not read staged tool module: {exc}"
    forbidden = _forbidden_import(src)
    if forbidden is not None:
        return False, f"rejected: tool imports forbidden module {forbidden!r} (core stays LLM-free)"

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_path),
            cwd=str(sd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return False, f"could not launch the gate: {exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        # The process may have exited in the race between the timeout firing and the kill —
        # `kill()` then raises ProcessLookupError. Swallow it so the gate honors "never raises".
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return False, f"gate timed out after {timeout_s}s (killed)"
    output = (out or b"").decode("utf-8", errors="replace")
    if len(output) > _MAX_GATE_OUTPUT_CHARS:
        output = output[:_MAX_GATE_OUTPUT_CHARS] + "\n…[output truncated]"
    return proc.returncode == 0, output


def promote(stem: str, *, workspace_root) -> bool:
    """Move a passed, approved tool from staging to the live dir (AC3) and drop its staged test
    (the worker discovers `*.py`; the test is not a tool). Fail-soft — a move failure logs and
    returns False (the caller confirms accordingly), never raises into the turn loop."""
    sd = staging_dir(workspace_root)
    ld = live_tools_dir(workspace_root)
    module_path = sd / f"{stem}.py"
    try:
        ld.mkdir(parents=True, exist_ok=True)
        shutil.move(str(module_path), str(ld / f"{stem}.py"))
    except OSError as exc:
        log.warning("promote %r failed (%s); not live", stem, exc)
        return False
    _unlink(sd / f"test_{stem}.py")
    return True


def discard(stem: str, *, workspace_root) -> None:
    """Delete a staged tool pair (a failed gate, or an owner deny) — fail-soft (AC2/AC3)."""
    sd = staging_dir(workspace_root)
    _unlink(sd / f"{stem}.py")
    _unlink(sd / f"test_{stem}.py")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
