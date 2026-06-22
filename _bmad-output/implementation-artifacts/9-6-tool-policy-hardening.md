---
baseline_commit: 386ad9e1e7eaaaa3c00ff3515573a6d95ee08e04
---

# Story 9.6: Tool-policy hardening

Status: done

## Story

As the owner,
I want the RISKY network/shell tools to refuse the dangerous-but-plausible commands I might wave through,
so that one careless Approve tap can't exfiltrate data or reach internal services.

## Scope decisions (locked at the Epic 9 retro 2026-06-22)

1. **Defense-in-depth ONLY** — these tools already gate on the Story 9.3 owner-approval tap (the owner sees the exact command/URL before it runs). 9.6 RAISES THE FLOOR so a benign-looking approval can't be turned against the pet; it does NOT replace the tap.
2. **Worker-side only, single file** — all four surfaces live in `shelldon/worker/tools.py` (the RISKY-tier tools, FREE of the LLM-free-core contract — the worker may import anything). NO `core/`, broker, transport, or contract changes. Import-linter is unaffected.
3. **0 new deps** — `socket`/`ipaddress`/`shlex` are stdlib; `httpx` is already available (transitive via `anthropic`/`openai`, lazily imported by `_http_get` since 9.3). `uv sync --locked` must show 0 changes.
4. **SSRF policy (a real choice, baked in — dev may confirm/adjust):** block **loopback + link-local (incl. the `169.254.169.254` cloud-metadata IP) on EVERY hop**; additionally block **private ranges (10/8, 172.16/12, 192.168/16, ULA/`fc00::/7`) on REDIRECT hops only** — NOT on the initial owner-approved URL (the owner explicitly approved that host, so a LAN fetch they typed is allowed; a *redirect* to internal space is the attack). Resolve the host to IP(s) and check the resolved address (catches `evil.com → 10.0.0.1`).

## Acceptance Criteria

### AC1 — `http_get` blocks SSRF on every hop + streams with a byte cap

**Given** `http_get(url)` runs (after approval)
**When** it fetches
**Then** it follows redirects MANUALLY (httpx `follow_redirects=False`, a bounded redirect loop, max ~5 hops), validating EACH hop's host: the host is resolved to its IP(s) and rejected (fail-closed `ValueError`) if any resolved IP is loopback or link-local (incl. `169.254.169.254`) on any hop, or is in a private range on a redirect hop (the initial owner-approved URL is exempt from the private-range check — decision 4)
**And** the body is STREAMED with a pre-read byte cap (`httpx.stream` + `iter_bytes`, stop at `_MAX_TOOL_OUTPUT_CHARS`-worth of bytes) so a multi-MB response never buffers fully into RAM on the 416MB Pi
**And** the existing guards are kept: http(s)-only scheme, no URL-embedded credentials (NFR9), `_RISKY_TIMEOUT_S` bound, `_cap()` on the returned text

### AC2 — `run_shell` runs in its own process group and cleans up orphans

**Given** `run_shell(command)` runs a command that backgrounds children (`cmd &`, `disown`, a daemonizing process)
**When** the command finishes or the `_RISKY_TIMEOUT_S` timeout fires
**Then** the subprocess is started in its OWN session/process group (`start_new_session=True`) and, on timeout (or normal exit), the WHOLE process group is signalled (`os.killpg`) so orphaned background children are reaped — not left running past the turn
**And** a normal (non-backgrounding) command behaves exactly as before (output + exit code, capped); `git` (no shell, no `&`) is unaffected by the group-kill except it too runs in its own group harmlessly

### AC3 — `git` is gated by a safe-subcommand allowlist

**Given** `git(args)` is invoked
**When** the args are parsed
**Then** the FIRST non-flag token (the subcommand) must be in a closed allowlist of read/local-only verbs (e.g. `status`, `log`, `diff`, `show`, `add`, `commit`, `branch`, `checkout`, `stash`, `restore`, `init`, `fetch`, `pull`, `push`, `remote`) — anything else (`clone`, `submodule`, `daemon`, …) is rejected fail-closed
**And** dangerous GLOBAL flags BEFORE the subcommand are rejected: any `-c`/`--config-env` (config injection, e.g. `-c core.sshCommand=…`), `--exec`/`--upload-pack`/`--receive-pack` anywhere, and `-c protocol.ext.allow` forms — so an allowed subcommand can't be turned into code execution
**And** a rejected git call returns a clear error (the model sees why) and runs nothing

### AC4 — the credential blocklist is broadened

**Given** `_deny_sensitive` guards a file path (read_file/list_dir/write_file)
**When** the path names a credential-shaped file
**Then** the blocklist additionally refuses `.pem`, `.key`, `.crt`, `.p12`/`.pfx`, any `id_rsa`/`id_ed25519`/`id_*` private-key stem, `.htpasswd`, and `.env.*` variants (`.env.bak`/`.env.backup`/`.env.local`/`.env.old`) — case-insensitive — on top of the existing `vault/` + `.env`/`*.env` denial
**And** a normal file (`notes.txt`, `tool.py`) is unaffected

### AC5 — Spine invariants + boundary gate

**Given** 9.6 lands
**Then** all changes are in `shelldon/worker/tools.py` (worker-side); import-linter 3 contracts KEPT (no core/broker/transport touched); the RISKY tools still PAUSE on the 9.3 approval flow unchanged (this story only changes what runs AFTER approval); every new guard fails CLOSED (a blocked host/subcommand/file raises → `execute_tool` → `ToolResult(ok=False)`, the turn survives)
**And** all existing tests pass (696+ baseline), import-linter 3 contracts green, `uv sync --locked` 0 new deps
**And** the corresponding entries are removed from `deferred-work.md` (9.2/9.3 review sections) as resolved

---

## Tasks / Subtasks

- [x] **Task 1 — `http_get` SSRF guard + streaming byte cap** (AC1)
  - [x] Add a pure `_assert_host_allowed(url, *, is_redirect: bool)` helper: parse host (`urlparse`), resolve via `socket.getaddrinfo`, check each resolved IP with `ipaddress.ip_address(...)` — reject `is_loopback`/`is_link_local` (covers `169.254.169.254`) on any hop, and `is_private`/ULA on redirect hops. Fail-closed `ValueError` with a clear message. Reuse `_RISKY_TIMEOUT_S`.
  - [x] Rewrite `_http_get` to follow redirects MANUALLY: `follow_redirects=False`, a loop bounded at `_MAX_REDIRECTS` (~5), validating each hop's URL (`is_redirect=False` for the first, `True` for the rest); on a 3xx with a `Location`, resolve+validate the next URL and continue; non-3xx → read the body.
  - [x] Stream the body: `httpx.stream("GET", url, timeout=_RISKY_TIMEOUT_S)`, accumulate `iter_bytes()` until a byte budget (~`_MAX_TOOL_OUTPUT_CHARS`) is reached, then stop + mark truncated. Decode utf-8 errors="replace". Keep `_cap()` as the final guard.
  - [x] Keep the scheme check + URL-credential rejection (NFR9).

- [x] **Task 2 — `run_shell` process-group isolation + orphan cleanup** (AC2)
  - [x] In `_run_subprocess`, when `shell=True` (or always — harmless for git), pass `start_new_session=True` so the child leads its own session/process group. Keep the existing `preexec_fn=resource_cap_preexec()` (compose both in one preexec, OR rely on `start_new_session` which uses `setsid` — verify they coexist; `start_new_session` + a `preexec_fn` are both supported by `subprocess`).
  - [x] On `subprocess.TimeoutExpired` (and after a normal run), kill the whole group: `os.killpg(os.getpgid(proc.pid), SIGKILL)` guarded (ProcessLookupError → already gone). Use `Popen` + `communicate(timeout=...)` so the pid is available for `killpg` (the current `subprocess.run` hides it) — OR keep `run` and use a `preexec_fn` setsid + a separate group-kill on timeout via the returned pid. Pick the cleanest; document it.
  - [x] A normal command's output/exit-code behavior is unchanged (regression: existing `test_risky_tools.py` must still pass).

- [x] **Task 3 — `git` safe-subcommand allowlist** (AC3)
  - [x] Add `_GIT_ALLOWED_SUBCOMMANDS = frozenset({...})` (read/local verbs). In `_git`, `shlex.split(args)`, then scan: reject any token before the subcommand that is a global `-c`/`--config-env`/`-C`-with-traversal form; reject `--upload-pack`/`--receive-pack`/`--exec` anywhere; find the first non-`-`-prefixed token as the subcommand and require membership in the allowlist. Fail-closed `ValueError` with the offending token.
  - [x] Then run via the existing `_run_subprocess(["git", *parts], shell=False)` path.

- [x] **Task 4 — broaden `_deny_sensitive` credential blocklist** (AC4)
  - [x] Add a closed set of credential suffixes (`.pem`/`.key`/`.crt`/`.p12`/`.pfx`/`.htpasswd`) + private-key stems (`id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`, with or without extension) + `.env.*` variants, all case-insensitive (casefold the name/suffix). Keep the existing `vault/` containment + `.env`/`*.env` checks. Raise the same `access denied` `ValueError`.

- [x] **Task 5 — Tests** (AC1–AC5)
  - [x] `tests/test_risky_tools.py` (UPDATE) OR `tests/test_tool_policy.py` (NEW): SSRF — `_assert_host_allowed` rejects `169.254.169.254`/`localhost`/`127.0.0.1`/a name resolving to a private IP (monkeypatch `socket.getaddrinfo`); allows a public IP on the initial hop; rejects a private IP on a redirect hop. Redirect-following — use `httpx.MockTransport` to simulate `evil.com → 169.254.169.254` and assert it's rejected mid-chain. Streaming — a large mock body is truncated at the byte cap (no full buffer).
  - [x] `run_shell` — assert `start_new_session=True` is passed (monkeypatch/inspect) and the group is killed on timeout (monkeypatch `os.killpg`); a normal command still returns output+exit (existing tests stay green).
  - [x] `git` — `status`/`log -1` allowed; `clone …`, `-c core.sshCommand=x status`, `--upload-pack=…`, `submodule …` rejected with a clear error.
  - [x] `_deny_sensitive` — rejects `key.pem`, `server.key`, `id_rsa`, `.env.bak`, `cert.p12` (case-insensitive); allows `notes.txt`/`tool.py`.
  - [x] Boundary gate: `uv run pytest -q` (696+ baseline + new); `uv run lint-imports` 3 KEPT; `uv sync --locked` 0 new deps. Remove the resolved items from `deferred-work.md`.

---

## Dev Notes

### What 9.1–9.5 already built (read first)

- **`shelldon/worker/tools.py`** is the sole file to touch. Current state of the four surfaces (all worker-side, RISKY-tier, run only AFTER the 9.3 approval tap):
  - `_http_get(url)` (line ~281) — http(s)-only + URL-credential rejection (NFR9), then `httpx.get(url, timeout=_RISKY_TIMEOUT_S, follow_redirects=True)` → `_cap(...)`. **9.6 replaces `follow_redirects=True` + `.text` with a manual validated redirect loop + streamed read.** `httpx` is lazily imported (transitive dep — keep it lazy).
  - `_run_subprocess(argv, *, cwd, shell)` (line ~257) — `subprocess.run(..., timeout=_RISKY_TIMEOUT_S, preexec_fn=resource_cap_preexec())`. **9.6 adds `start_new_session=True` + a process-group kill on timeout.** Keep the 9.5 `preexec_fn` (RLIMIT caps).
  - `_run_shell(command)` → `_run_subprocess(command, shell=True)`; `_git(args)` → `shlex.split` + `_run_subprocess(["git", ...], shell=False)`. **9.6 inserts the allowlist check in `_git` before `_run_subprocess`.**
  - `_deny_sensitive(candidate, memory_root)` (line ~166) — vault containment + `.env`/`*.env`. **9.6 broadens the credential suffix/stem set.** Used by read_file/list_dir/write_file (so the broadened blocklist also tightens the FREE read tools — fine, defense-in-depth).
- **`execute_tool`** catches every exception into `ToolResult(ok=False)` — so a fail-closed `ValueError` from any new guard surfaces to the model as a recoverable error, the turn never crashes (9.1 discipline). This is why every guard should RAISE, not silently skip.
- **`_RISKY_TIMEOUT_S=20.0`, `_MAX_TOOL_OUTPUT_CHARS=16*1024`, `_cap()`** — reuse these; don't invent new caps unless needed (a `_MAX_REDIRECTS` const + a byte budget derived from `_MAX_TOOL_OUTPUT_CHARS` are the only new constants).

### Architecture constraints

- **Worker-side only — import-linter unaffected.** `shelldon.worker.tools` is NOT under the `core is LLM-free` contract; `socket`/`ipaddress`/`os`/`subprocess`/`shlex` are stdlib, `httpx` is already present. Do NOT add a dependency.
- **Fail-closed, not fail-open.** Every new check raises `ValueError` on the dangerous case (→ `ToolResult(ok=False)`). A guard that can't decide (e.g. DNS resolution fails) should fail CLOSED (reject), since this is a security boundary.
- **Don't touch the 9.3 approval flow.** The RISKY tools still PAUSE → `RequestToolApproval` → resume; 9.6 only changes what executes after approval. No contract/runtime/transport changes.
- **No new SCHEMA / no broker change.** Pure tool-implementation hardening.

### The SSRF redirect loop (recommended shape)

```
def _http_get(url):
    _check_scheme_and_creds(url)            # existing guards
    _assert_host_allowed(url, is_redirect=False)
    import httpx
    with httpx.Client(follow_redirects=False, timeout=_RISKY_TIMEOUT_S) as client:
        for hop in range(_MAX_REDIRECTS + 1):
            with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = _resolve_redirect(url, resp.headers["location"])
                    _assert_host_allowed(url, is_redirect=True)   # private-range blocked here
                    continue
                body = _read_capped(resp)     # iter_bytes to a byte budget
                return _cap(f"HTTP {resp.status_code}\n{body}")
        raise ValueError("too many redirects")
```
`_assert_host_allowed` resolves the host (`socket.getaddrinfo(host, None)`), iterates the returned IPs, and rejects per the decision-4 policy. Resolving (not just string-matching the host) defeats `evil.com → 10.0.0.1`.

### Testing notes

- `httpx.MockTransport(handler)` lets you script redirect chains + large bodies with NO network (stays out of the `-m live` lane — these are ordinary unit tests). Inject it via `httpx.Client(transport=...)` — so structure `_http_get` to accept an optional injected client/transport for tests, or monkeypatch `httpx.Client`.
- Monkeypatch `socket.getaddrinfo` to map a test hostname to a chosen IP (public vs private) without real DNS.
- For `run_shell` orphan cleanup, assert the MECHANISM (`start_new_session=True` passed, `os.killpg` called on timeout) rather than spawning real daemons — deterministic + fast, same discipline as 9.5's RLIMIT tests.

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 9.6: Tool-policy hardening] — the AC backbone
- [Source: _bmad-output/implementation-artifacts/deferred-work.md] — the originating 9.2/9.3 review defers (http_get SSRF + streaming, run_shell process-group, git allowlist, _deny_sensitive cred-blocklist); REMOVE these as resolved when 9.6 lands
- [Source: _bmad-output/implementation-artifacts/epic-9-retro-2026-06-22.md] — the retro that scheduled this story (action item #2)
- [Source: shelldon/worker/tools.py] — the four functions to harden (`_http_get`, `_run_subprocess`/`_run_shell`, `_git`, `_deny_sensitive`)

### Project Structure Notes

- One file changed (`shelldon/worker/tools.py`) + one test file (update `tests/test_risky_tools.py` or add `tests/test_tool_policy.py`). No new modules, no new deps, no core/broker/transport/contract changes.
- This is the FINAL Epic 9 story. After it: Epic 9 fully complete → consider flipping `epic-9` → done (the retro is already done). The separate live-smoke-on-Pi action item (retro #1) remains.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Dev Story workflow)

### Debug Log References

- `uv run pytest -q` → 735 passed, 3 skipped, 7 deselected (`-m live`). Baseline 699 collected; +32 new cases (incl. the new `test_tool_policy.py`).
- `uv run lint-imports` → 3 contracts KEPT (all changes are worker-side; `socket`/`ipaddress`/`os` are stdlib).
- `uv sync --locked` → 0 changes (`httpx` is a transitive dep, lazily imported as before).

### Completion Notes List

- **AC1 (http_get SSRF + streaming):** `_http_get` now follows redirects MANUALLY (`httpx.Client(follow_redirects=False)`, a `_MAX_REDIRECTS`-bounded loop). `_assert_host_allowed` resolves each hop's host via `socket.getaddrinfo` and rejects loopback/link-local (incl. `169.254.169.254`)/unspecified/multicast on EVERY hop, plus private/reserved on REDIRECT hops only (the owner-approved initial host may be a LAN box — decision 4). Body is streamed with `_read_capped` (stop at `_MAX_TOOL_OUTPUT_CHARS` bytes). Fail-closed: an unresolvable host raises. A `client=` kwarg is a test-only seam (MockTransport) — not in the tool schema, so the model can't inject it.
- **AC2 (run_shell process group):** `_run_subprocess` moved from `subprocess.run` to `Popen` with `start_new_session=True`; the child's process group is SIGKILLed (`os.killpg`) on timeout AND in a `finally` on normal exit, so a backgrounded `&`/daemonized child can't outlive the turn. The 9.5 `preexec_fn` (RLIMIT) is preserved (both `start_new_session` + `preexec_fn` apply). Timeout still raises → `ToolResult(ok=False)` (9.3 behavior unchanged). pgid is captured while the child is alive.
- **AC3 (git allowlist):** `_git` requires the subcommand ∈ `_GIT_ALLOWED_SUBCOMMANDS` (rejecting `clone`/`submodule`/`daemon`/…); rejects exec/pack specifiers (`--upload-pack`/`--receive-pack`/`--exec`/`--namespace`) ANYWHERE; rejects config-injection/chdir/dir-redirect flags (`-c`/`-C`/`--config-env`/`--git-dir`/`--work-tree`/`--exec-path`) only as GLOBAL flags (before the subcommand) — so legit post-subcommand uses (`git commit -c <commit>`, `git log -C`) still work. `git --version`/`--help` (no subcommand) allowed via `_GIT_BENIGN_NO_SUBCOMMAND`.
- **AC4 (credential blocklist):** `_deny_sensitive` broadened — `.env.*` variants, `_CREDENTIAL_SUFFIXES` (`.pem`/`.key`/`.crt`/`.cer`/`.p12`/`.pfx`/`.htpasswd`/`.jks`/`.ppk`), and `_CREDENTIAL_STEMS` (`id_rsa`/`id_dsa`/`id_ecdsa`/`id_ed25519`), all case-insensitive (casefold). Existing vault + `.env`/`*.env` checks kept. This also tightens the FREE read tools (read_file/list_dir) — intended defense-in-depth.
- **AC5 (spine):** all changes in `shelldon/worker/tools.py` (worker-side, NOT under the LLM-free-core contract); import-linter 3 KEPT; no core/broker/transport/contract change; the 9.3 approval PAUSE flow is untouched (this story only changes what runs after Approve). Every guard fails CLOSED (raises → `ToolResult(ok=False)`). The 5 resolved items removed/marked in `deferred-work.md` (4 by 9.6 + the prune-scheduling one already done by 9.5).
- **Test isolation:** all http_get tests use `httpx.MockTransport` + a stubbed `socket.getaddrinfo` (no real network/DNS, stays out of the `-m live` lane); run_shell tests inspect the mechanism (`start_new_session`, `os.killpg`) rather than spawning real daemons. Fixed a stale 9.5 test (`test_run_subprocess_passes_a_preexec_fn`) that monkeypatched `subprocess.run` — now monkeypatches `Popen`.
- **One scope decision (decision 4):** private-range hosts are allowed on the initial owner-approved URL, blocked only on redirects. Flagged for reviewer confirmation — it trades strict SSRF for not breaking a LAN fetch the owner explicitly approved.

### File List

- shelldon/worker/tools.py (UPDATE — http_get SSRF+streaming, run_subprocess process-group, git allowlist, _deny_sensitive blocklist; new imports os/socket/ipaddress + `_MAX_REDIRECTS`)
- tests/test_tool_policy.py (NEW — SSRF/streaming/redirect, run_shell group, git allowlist, credential blocklist)
- tests/test_risky_tools.py (UPDATE — http_get happy-path converted to MockTransport + stubbed DNS)
- tests/test_resource_caps.py (UPDATE — `_run_subprocess` preexec test now monkeypatches `Popen`, not `subprocess.run`)
- _bmad-output/implementation-artifacts/deferred-work.md (UPDATE — 5 resolved items marked)

### Change Log

- 2026-06-22 — Review findings addressed: 1 [Decision] (removed `git config` from the allowlist — option A) + 3 [Patch] (`id_*` credential wildcard, `--git-dir`/`--work-tree` rejected on every token, timeout-reap no longer masks the real TimeoutExpired). 4 [Defer] accepted (DNS-rebind TOCTOU, peak-RAM ±1 chunk, extra SSH-auth files, clean-exit hook SIGKILL — all spec-bounded). +7 tests (742 pass).
- 2026-06-22 — Story 9.6 implemented (tool-policy hardening). Worker-side defense-in-depth on the RISKY tools: `http_get` manual SSRF-validated redirect loop + streamed byte-cap; `run_shell` process-group isolation + orphan kill; `git` safe-subcommand allowlist + dangerous-flag rejection; `_deny_sensitive` broadened credential blocklist. All in `shelldon/worker/tools.py` — no core/broker/transport/contract changes, 0 new deps, import-linter 3 KEPT. 32 new test cases (735 pass). 5 deferred items resolved in deferred-work.md. Status → review.

### Review Findings

_Code review 2026-06-22 — 3 layers (Blind Hunter, Edge Case Hunter, Acceptance Auditor). 1 decision-needed, 3 patch, 4 defer, 10 dismissed._

- [x] [Review][Decision] `git config` in `_GIT_ALLOWED_SUBCOMMANDS` — RESOLVED via option A (removed `config` entirely). `git config core.sshCommand=…` persists a hook that turns a later approved `fetch`/`commit` into code-exec — the exact escalation AC3's `-c` global-flag block exists to stop, so allowing the `config` SUBCOMMAND was an inconsistent hole. Removed from the allowlist; the model can no longer persist git config via the tool. New rejection tests (`config core.sshCommand=…`, `config user.name x`).
- [x] [Review][Patch] `id_*` wildcard not covered — FIXED: replaced the 4-entry `_CREDENTIAL_STEMS` with an `id_` name-prefix match (`_CREDENTIAL_NAME_PREFIXES`), so `id_ecdsa_sk`/`id_ed448`/any `id_*` key is blocked. Tests added (`id_ecdsa_sk`/`id_ed448`/`id_custom`).
- [x] [Review][Patch] `--git-dir`/`--work-tree` only rejected as GLOBAL flags — FIXED: moved `--git-dir`/`--work-tree`/`--exec-path`/`--config-env` into `_GIT_EXEC_FLAGS` (checked on EVERY token), so `git status --git-dir=/etc` is now rejected post-subcommand too. Only the SHORT `-c`/`-C` stay global-only (legitimately benign post-subcommand: `git commit -c`, `git log -C`). Tests added.
- [x] [Review][Patch] `communicate(timeout=1)` reap can raise a second `TimeoutExpired` masking the real one — FIXED: the timeout handler now does `_kill_pgroup` → `proc.kill()` (guarded) → unbounded `proc.wait()` → re-raise the ORIGINAL TimeoutExpired (after SIGKILL the leader dies promptly, so `wait()` returns at once and can't raise a fresh timeout).
- [x] [Review][Defer] DNS rebinding TOCTOU — `_assert_host_allowed` resolves the host via `socket.getaddrinfo` before `client.stream()` is called; httpx resolves again on the actual TCP connection. An attacker DNS record could return a public IP on validation and a private IP on connect. Requires socket-level IP pinning to fix — out of scope for this story (defense-in-depth only, per Story 9.6 scope decision #1). — deferred, pre-existing
- [x] [Review][Defer] `_read_capped` peak-RAM may exceed the byte cap by one server chunk — the break fires after a chunk pushes `total >= cap`, so the peak in-memory allocation is `cap + max_chunk_size`. On a server sending 1MB chunks, up to 2MB could land before the break. Not fixable without controlling httpx's internal chunk size. — deferred, pre-existing
- [x] [Review][Defer] `authorized_keys`, `known_hosts`, `.netrc` not in `_deny_sensitive` blocklist — pre-existing omission not introduced by 9.6; AC4 defines a specific scope that doesn't include these SSH auth files. — deferred, pre-existing
- [x] [Review][Defer] SIGKILL on clean-exit process group may affect synchronous git hooks — `finally: _kill_pgroup(pgid)` runs even on clean exit (specified in AC2), and any short-lived child still running at communicate() return time gets SIGKILLed. Git hooks are normally synchronous and complete before communicate() returns, so this is theoretical. — deferred, pre-existing
