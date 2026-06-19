---
baseline_commit: 3d63807e25aefa3a9ac345d37966e85d470b8104
---
# Story 7.1: Plugin-host and the generalized plugin contract

Status: done

<!-- First FEATURE story of Epic 7 (optional EXTEND fork). Gated by Story 7.0 (dispatch extract, done). -->
<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->
<!-- Scoping decisions D1–D8 are made explicit in Dev Notes → "Scoping decisions (made, not assumed)". Open questions for the owner are listed at the very end. -->

## Story

As a developer extending shelldon,
I want one generalized plugin contract (a typed manifest + a bus-client base) and a `plugin-host` process that discovers plugins as modules from `plugins/`, validates their claims, and rejects conflicts at load,
so that anyone can add a capability — hardware (sensor/actuator) or behavioral (e.g. an XP widget) — without ever touching `core/`, proving CAP-7's bus-only extensibility boundary.

**Why this is the foundational Epic-7 story:** AD-8 mandates **one** plugin model for both hardware and behavioral plugins — no second class. This story builds the *contract and the host*, not any actual plugin (the XP widget is 7.3, physical sensing is 7.4) and not the event fan-out (that is 7.2). The bar is the architecture's CAP-7 success line: **a plugin is a bus client speaking only the `Envelope`/bus contract, never imports `core/`, and adding it leaves the import-linter passing.** This story makes that mechanically true and testable.

## Acceptance Criteria

### AC1 — A typed plugin manifest + a `Plugin` bus-client contract live in `shelldon/plugins/`

**Given** AD-8 requires every plugin to declare "everything it touches" — emitted event kinds, subscribed broadcast event kinds, claimed GPIO/BLE resources, and claimed display regions
**When** the plugin contract is introduced
**Then** `shelldon/plugins/manifest.py` defines:
- a frozen, typed `PluginManifest` (a `msgspec.Struct`, mirroring the `contracts/` style) with fields: `name: str`, `subscribes: tuple[EventKind, ...] = ()`, `emits: tuple[EventKind, ...] = ()`, `resources: tuple[str, ...] = ()` (opaque claim strings, e.g. `"gpio:17"`, `"ble:AA:BB:CC:DD:EE:FF"`), `regions: tuple[Region, ...] = ()` (claimed display regions)
- a `Plugin` `typing.Protocol` (the bus-client contract) declaring the minimal surface the host drives — at least `manifest: PluginManifest` (or a `MANIFEST` module constant — see D1) and an `async def run(self, reader, writer) -> None` entry the host calls after connecting it to the bus (the 7.2 event loop hangs off this; in 7.1 the default base does nothing but stay alive until the hub closes)

**And** the closed vocabularies the manifest references are added to `shelldon/contracts/__init__.py` as **pure declarations** (D2): a new `EventKind(StrEnum)` with the closed broadcast set (`MESSAGE_ANSWERED = "message-answered"`, `TOOL_USED = "tool-used"`, `DAY_ALIVE = "day-alive"`) and at least one claimable widget `Region` member (`STATUS_BAR = "status-bar"`). **No** new `MsgKind`, **no** new `Envelope` body, **no** `ROUTING_TABLE` row, **no** `SCHEMA_VERSION` bump — adding a `StrEnum` and an enum member is additive (AD-13) and no wire message uses `EventKind` yet (that is 7.2).

### AC2 — The host discovers plugins as modules from `plugins/` and builds a load-time registry

**Given** AD-8: "a single plugin-host process loads plugins as modules (discovered from a `plugins/` dir, each with a manifest)"
**When** `shelldon/plugins/host.py` runs its discovery
**Then** it enumerates submodules of the `shelldon.plugins` package (via `pkgutil.iter_modules`), imports each, and collects those exposing a manifest (D1) — the infrastructure modules (`host`, `manifest`, `__init__`) are naturally skipped because they expose no manifest
**And** discovery produces a typed load result holding, across all loaded plugins: the plugin instances, the merged **resource-claim map**, the merged **region-claim map**, and the **subscription registry** (`EventKind -> [plugin]`, the AD-11 "subscription registry built at load from plugin manifests" — built here; *consumed* by 7.2's fan-out, unused in 7.1)

### AC3 — Conflicting claims are a load-time failure (no two writers to one resource/region)

**Given** AD-5/AD-8: "the host rejects conflicting claims at load — two plugins claiming the same GPIO pin or the same display region is a load-time failure … no two writers ever target one region"
**When** two loaded manifests claim the **same** display `Region` or the **same** `resources` string
**Then** the loader raises a `PluginLoadError` (or equivalent) naming the conflicting claim and both plugins, and the host does **not** start — fail-fast at load, never a silent second writer

**And** a duplicate **subscription** (two plugins subscribing to the same `EventKind`) is **NOT** a conflict — broadcast is 1→N, so two subscribers to `message-answered` is the normal case and must load fine. Only *writer claims* (regions, resources) conflict.

### AC4 — The plugin-host is a bus client following the established adapter pattern

**Given** the transport (`transport/cli.py`) and display (`display/service.py`) bus-client precedent — `connect(socket_path, Actor.X)` then run loops, per-frame resilience, tear-down on hub EOF
**When** `run_plugin_host(socket_path, *, plugins_package=...)` runs
**Then** it connects as `Actor.PLUGIN_HOST` (the enum member already exists, `contracts/__init__.py:27`), discovers + validates plugins (AC2/AC3 — a conflict raises *before* connecting), and drives each loaded plugin's `run(reader, writer)` entry, tearing down cleanly when the hub goes away (mirroring `run_cli_transport`/`run_display`'s `asyncio.wait(..., FIRST_COMPLETED)` + cancel-the-rest shape)
**And** it imports only `shelldon.contracts` and the shared bus client `shelldon.core.bus` (`connect`/`read_frame`/`write_frame`) — exactly the imports `transport/cli.py:24-25` and `display/service.py:23-24` already use — and **nothing else from `core/`**

### AC5 — The "plugins never import core" boundary is mechanically enforced (CAP-7)

**Given** AD-8/CAP-7: "a plugin is a bus client speaking only `Envelope` and never imports core; adding it leaves the import-linter passing"
**When** the import-linter contracts are extended
**Then** `pyproject.toml` gains a new `forbidden` contract for `source_modules = ["shelldon.plugins"]` that forbids the LLM/provider SDKs (plugins are LLM-free, like `transport`) **and** `shelldon.core`, with `ignore_imports` permitting only the shared bus-client edge `shelldon.plugins.* -> shelldon.core.bus` (+ its submodules) — see D8 for why this exact shape (it matches how `transport`/`display` already legally import `shelldon.core.bus` while reaching no core *domain* module)
**And** `uv run lint-imports` stays green: the new contract is **KEPT**, alongside the existing "core is LLM-free" and "transport holds no model/tool creds" contracts (now 3 contracts, 0 broken)

### AC6 — The host is wired into the composition root as an optional actor

**Given** `app.py` launches the actor topology (`launch_in_process` for dev/test, `launch_multiprocess` for production) — Story 4.3
**When** the plugin-host is added
**Then** `app.py` gains a `_plugin_host_proc(socket_path)` child target and adds `ctx.Process(target=_plugin_host_proc, ..., name="shelldon-plugin-host")` to `launch_multiprocess`'s `children` list (alongside broker/display/transport), and an `asyncio.create_task(run_plugin_host(socket_path))` line to `launch_in_process`'s `tasks` list — same shape as the other three actors
**And** with **zero real plugins** shipped in 7.1 (XP=7.3, sensing=7.4), the host loads an **empty** plugin set in production and stays a healthy idle bus client — adding it changes no existing actor's behavior (the in-process smoke + full suite stay green)

### Out of scope (explicit — do NOT do here)

- **The event fan-out / broadcast routing (Story 7.2).** No `Event` envelope body, no `MsgKind.EVENT`, no hub fan-out, no emit path, no second `ROUTING_TABLE` mode wired. 7.1 only *declares* the closed `EventKind` set and *builds* the subscription registry as a load artifact; nothing is routed on it. A plugin's `run()` in 7.1 is a stay-alive stub.
- **Any real plugin.** The XP/leveling widget is Story 7.3; physical sensing (PiSugar2 button / BLE) is Story 7.4. 7.1 ships the host + contract only; test fixtures supply fake plugin modules.
- **Any hardware code** — no GPIO/BLE libraries, no pin reads, no scanning. `resources` are opaque claim strings; the actual hardware access + BLE pair-first rule are 7.4.
- **Drawing in a claimed region.** 7.1 adds the `STATUS_BAR` region member and validates region *claims*; the display already composites regions generically (`display/service.py:55-58`, keyed by `Region`) and needs **no change**. Actually rendering a widget is 7.3.
- **Touching `core/`** beyond reading `shelldon.core.bus` as a client. No change to `runtime.py`, the dispatcher (7.0), the bus *server*, the broker, or the worker.
- **Promoting the bus client out of `shelldon.core.bus` into a neutral package** — tempting (it would make "plugins never import core" literally true), but it is a cross-cutting refactor of `core/transport/display` imports. Deferred; D8 + Open Question 3 capture it. Use the precedent-matching `ignore_imports` shape instead.

## Tasks / Subtasks

- [x] **Task 1 — Add the closed manifest vocabulary to `contracts/`** (AC1)
  - [x] In `shelldon/contracts/__init__.py`: added `class EventKind(StrEnum)` with `MESSAGE_ANSWERED`/`TOOL_USED`/`DAY_ALIVE` (closed AD-11 broadcast set; fan-out is Story 7.2). Added widget `Region` member `STATUS_BAR = "status-bar"` (fulfilling the enum docstring's Epic-7 reservation).
  - [x] Added `EventKind` to `__all__`. Did **not** touch `_KIND_FOR_BODY`, `ROUTING_TABLE`, the `Envelope.body` union, or `SCHEMA_VERSION` — it is a declaration, not a wire body (D2).
  - [x] verify: `test_plugin_contract.py` asserts the vocab + wire-additivity (SCHEMA_VERSION==1, no `MsgKind.EVENT`); full suite + `ROUTING_TABLE` completeness test green.
- [x] **Task 2 — Define the plugin contract: `shelldon/plugins/manifest.py`** (AC1)
  - [x] `PluginManifest` frozen `msgspec.Struct` (name/subscribes/emits/resources/regions). `Plugin` `Protocol` (manifest + `async def run(self, reader, writer)`). `BasePlugin` whose `run` drains the reader to hub EOF and returns (stay-alive stub; per-frame resilience set for 7.2 to override).
  - [x] Manifest-exposure convention (D1): a plugin module exposes `MANIFEST: PluginManifest` and OPTIONALLY a `PLUGIN` instance; manifest-only is wrapped in `BasePlugin`. Typed — closed enums make a typo a decode error (tested).
  - [x] verify: `import shelldon.plugins.manifest` imports only `shelldon.contracts` + stdlib `typing`/`msgspec` (the `read_frame` import is local-scoped inside `run`; the import-linter `shelldon.plugins -> shelldon.core.bus` ignore covers it). 3 contracts KEPT.
- [x] **Task 3 — The host: discovery + claim validation + registry** (AC2, AC3)
  - [x] `shelldon/plugins/host.py`: `discover_plugins(package)` → `pkgutil.iter_modules` over the package, import each submodule, collect those whose module passes `plugin_from_module` (has a `MANIFEST`). Infra modules self-exclude (no `MANIFEST`).
  - [x] `validate_claims(plugins)` → builds region-claim + resource-claim maps; a duplicate region/resource across two plugins raises `PluginLoadError` (names the claim + both plugins). Builds the subscription registry `dict[EventKind, list[Plugin]]` (duplicates allowed). Returns typed `LoadedPlugins`.
  - [x] verify: `test_plugin_host.py` — distinct claims load; dup `STATUS_BAR` → `PluginLoadError`; dup `"gpio:17"` → `PluginLoadError`; dup subscription → loads, registry maps both; real empty `shelldon.plugins` → `[]`; an on-disk fake package discovers the plugin module + skips the non-plugin.
- [x] **Task 4 — The host: bus-client lifecycle** (AC4)
  - [x] `run_plugin_host(socket_path, *, plugins_package=None)`: discover + validate **first** (conflict raises before connect), then `connect(socket_path, Actor.PLUGIN_HOST)`, drive each plugin's `run` concurrently, tear down on hub EOF — the `asyncio.wait({...}, FIRST_COMPLETED)` + cancel-pending + re-raise + `finally: writer.close()` shape copied from `run_cli_transport`. Empty set → one idle `BasePlugin` so the lifecycle matches the other actors.
  - [x] verify: integration test runs `run_plugin_host` against a real `BusServer`; asserts it registers as `PLUGIN_HOST` and tears down cleanly when the hub stops; a conflicting on-disk package → `PluginLoadError` and the host NEVER registers (fail-fast before connect).
- [x] **Task 5 — Import-linter contract: plugins never import core** (AC5)
  - [x] `pyproject.toml`: added the 3rd `forbidden` contract "plugins never import core (AD-8/CAP-7)" — `source_modules = ["shelldon.plugins"]`, forbids the 6 providers + `shelldon.core`, `ignore_imports = ["shelldon.plugins.* -> shelldon.core.bus"]` (the `.bus.*` submodule wildcard was dropped — import-linter errors on an unmatched ignore; only the package edge exists). (D8)
  - [x] verify: `uv run lint-imports` → 3 KEPT / 0 broken. Probed by temporarily adding `import shelldon.core.runtime` to `host.py` → contract correctly went BROKEN (2 kept/1 broken); probe removed, back to 3 KEPT.
- [x] **Task 6 — Wire into the composition root** (AC6)
  - [x] `app.py`: imported `run_plugin_host`; added `_plugin_host_proc(socket_path)` + the `ctx.Process(... name="shelldon-plugin-host")` child in `launch_multiprocess`, and `asyncio.create_task(run_plugin_host(socket_path))` in `launch_in_process`.
  - [x] verify: the existing app smoke (`test_app_root_smoke_turn_and_clean_teardown`, which uses `launch_in_process`) now exercises the host and stays green — the empty-set host registers, idles, and tears down with the others (clean socket unlink). Plus a structural wiring guard test.
- [x] **Task 7 — Final gate**
  - [x] verify: `uv run pytest -q` → **477 passed**, 3 skipped (460 baseline + 17 new); `uv run lint-imports` → 3 KEPT / 0 broken; `uv sync --locked` → 0 new deps (stdlib + msgspec only, already pinned). No ruff/formatter in this project (CI = sync-locked → lint-imports → pytest).

### Review Findings

- [x] [Review][Decision] Shared reader/writer across multiple plugin tasks — `run_plugin_host` passes the *same* `asyncio.StreamReader`/`asyncio.StreamWriter` to every plugin's `run(reader, writer)`. Concurrent `read_frame` calls from N tasks will interleave reads and corrupt framing. Zero impact in 7.1 (only the idle sentinel runs), but structurally broken for 7.3+ when real plugins call `read_frame`. Options: (A) fix now — each plugin gets its own `connect()` call inside `run_plugin_host`; (B) defer to 7.3 with a doc note that the 7.1 host supports exactly one concurrent reader. — **RESOLVED 2026-06-19 (owner chose Defer-to-7.2 + doc-note):** Option A is a TRAP — N connections all register as `Actor.PLUGIN_HOST` and clobber the hub's actor-keyed registry (`server.py:93`). The correct design IS Story 7.2: the host owns the single read loop and fans out to subscribed plugins via `loaded.subscriptions` (a plugin never reads the socket itself). Added an explicit SINGLE-READER-LIMIT doc-note on `run_plugin_host` (records the limit + the Option-A trap + the 7.2 fix) and an icebox prerequisite on 7.2. No 7.1 behavior change (zero real plugins).
- [x] [Review][Patch] `plugin_from_module` returns any non-None `PLUGIN` attribute without validating it satisfies the `Plugin` Protocol — a malformed `PLUGIN = 42` passes discovery and crashes post-`connect()` [shelldon/plugins/host.py:94-97] — **FIXED 2026-06-19:** `isinstance(plugin, Plugin)` check (the runtime_checkable Protocol); a non-Plugin is logged + skipped (AD-8 isolation), not fatal. Test: `test_module_with_malformed_plugin_is_skipped`.
- [x] [Review][Patch] `discover_plugins` lets `importlib.import_module` exceptions propagate — one broken plugin module crashes the entire host rather than skipping the offending module [shelldon/plugins/host.py:105] — **FIXED 2026-06-19:** import wrapped in try/except → log.warning(exc_info) + skip; healthy plugins still load (AD-8: a bad plugin kills only itself). Test: `test_discovery_skips_a_module_that_fails_to_import`.
- [x] [Review][Patch] `BasePlugin.run` exits silently on `ValueError` (framing error) with no log entry — operator gets zero signal on why a plugin ended [shelldon/plugins/manifest.py:78] — **FIXED 2026-06-19:** added a `shelldon.plugins.manifest` logger + a warning on the framing-error exit. Test: `test_baseplugin_logs_on_a_framing_error`.
- [x] [Review][Defer] `_idle` sentinel bypasses `validate_claims` — `LoadedPlugins` doesn't include the idle placeholder, inconsistent with what actually runs [shelldon/plugins/host.py:130-133] — deferred, zero real plugins in 7.1; revisit when 7.2 builds on `loaded.subscriptions`
- [x] [Review][Defer] `LoadedPlugins` lacks `__eq__`/`__repr__` — plain class vs rest of codebase's `msgspec.Struct` style makes it awkward to test or debug [shelldon/plugins/host.py:31-49] — deferred, pre-existing; low urgency
- [x] [Review][Defer] `emits` field on `PluginManifest` declared but never consumed by the host — dead field implying future obligations not yet stated [shelldon/plugins/manifest.py:33] — deferred, intentional in 7.1; emit registry is a future story concern
- [x] [Review][Defer] Tests access `srv._registry` (private `BusServer` attribute) — tight coupling to internals [tests/test_plugin_host.py:140] — deferred, pre-existing pattern across all bus-client lifecycle tests
- [x] [Review][Defer] `connect()` in `run_plugin_host` has no retry/timeout — consistent with transport/display pattern [shelldon/plugins/host.py:128] — deferred, pre-existing; trigger = hub startup ordering becomes a production pain
- [x] [Review][Defer] `BasePlugin.run` doesn't catch `OSError`/`asyncio.IncompleteReadError` from `read_frame` — consistent with transport/display frame-loop pattern [shelldon/plugins/manifest.py:71-81] — deferred, pre-existing; trigger = `read_frame` starts surfacing `OSError` explicitly
- [x] [Review][Defer] Multiple tasks in `done` simultaneously — second exception silently discarded via `for task in done: task.result()` short-circuit [shelldon/plugins/host.py:135-141] — deferred, pre-existing FIRST_COMPLETED teardown pattern
- [x] [Review][Defer] `pkgutil.iter_modules` discovery order is filesystem-dependent — `test_host_refuses_to_start` name ordering is fragile under non-alphabetical filesystems [shelldon/plugins/host.py:100-110] — deferred, tests control insertion order today; trigger = 7.3+ real plugins expose ordering sensitivity
- [x] [Review][Defer] `_plugin_host_proc` doesn't pass `dict(os.environ)` to child unlike `_broker_proc` — style inconsistency; env is inherited on spawn [shelldon/app.py:130] — deferred, 7.1 has no plugins needing explicit env; trigger = 7.4 hardware plugin requires a credential not in parent env
- [x] [Review][Defer] `package.__path__` is None for namespace packages — `pkgutil.iter_modules` silently discovers nothing [shelldon/plugins/host.py:100-110] — deferred, theoretical; shelldon.plugins is a regular package with `__init__.py`

## Dev Notes

### Scoping decisions (made, not assumed — flagged for the owner at the end)

- **D1 — Manifest is a typed Python object, not a TOML file.** Each plugin module exposes `MANIFEST: PluginManifest`. Rationale: the codebase is pure-stdlib + `msgspec`, every contract is a typed frozen struct, and the manifest references **closed enums** (`EventKind`, `Region`) — making a typo a construction/decode error for free. A TOML manifest would add a parser and re-validate enums by hand. (Open Q1.)
- **D2 — `EventKind` + `STATUS_BAR` are pure declarations in `contracts/` in 7.1.** They give the manifest a closed vocabulary to reference (AD-11 "event kinds declared in `contracts/`"). No `Envelope` body / `MsgKind` / `ROUTING_TABLE` / `SCHEMA_VERSION` change — the actual `Event` wire message + fan-out is 7.2. Adding a `StrEnum` and an enum member is additive (AD-13), no version bump.
- **D3 — 7.1 / 7.2 boundary:** 7.1 = host + manifest + loader + conflict-rejection + bus-client lifecycle + import-linter + the subscription registry **as a load-time data artifact**. 7.2 = the hub broadcast routing mode, the `Event` body, the emit path, and *consuming* the registry to fan out. No event flows in 7.1; a plugin's `run()` is a stay-alive stub.
- **D4 — Discovery = `pkgutil.iter_modules` over `shelldon.plugins`**, importing each submodule and collecting those with `MANIFEST`. Infra modules (`host`/`manifest`/`__init__`) expose no `MANIFEST`, so they self-exclude. (Open Q1 also covers whether discovery should be explicit-registry instead.)
- **D5 — GPIO/BLE resources are opaque claim strings** (`"gpio:17"`, `"ble:<mac>"`); a conflict is two manifests with the same string. No hardware library, no parsing of the string's meaning — that is 7.4. This keeps 7.1 hardware-free and testable on any box.
- **D6 — No real plugin ships in 7.1.** Production load = empty set (an idle, healthy host). Tests inject a fake plugins package / fake manifests. (The XP plugin 7.3 and sensing 7.4 are the first real consumers and will validate the contract end-to-end.)
- **D7 — Only writer-claims conflict.** Regions and resources are single-writer (AD-5: "no two writers ever target one region"), so a duplicate is a load failure. Subscriptions are 1→N by design, so duplicate subscriptions load fine.
- **D8 — The import-linter shape for "plugins never import core."** The architecture's prose "plugin never imports core" is enforced *in spirit* the same way `transport`/`display` already are: those two modules legally `from shelldon.core.bus import connect, read_frame, write_frame` (`transport/cli.py:25`, `display/service.py:24`) — the bus *client framing* is shared infra living under `shelldon.core.bus`, while no core *domain* module (runtime/state/memory/arbiter) is touched. So the new contract forbids `shelldon.core` **and** the provider SDKs, with `ignore_imports` allowing ONLY the `shelldon.plugins.* -> shelldon.core.bus` edge. This auto-forbids any future core-domain import while permitting the one shared seam. (The cleaner-but-bigger alternative — promote the bus client into a neutral `shelldon.bus` package so nobody "imports core" — is Open Q3, deferred.)

### The bus-client template (copy this exact shape)

`run_plugin_host` is the fourth bus-client of the same lineage. Study and mirror:
- **`transport/cli.py:90-126`** (`run_cli_transport`) — the canonical shape: `connect(...)`, two `create_task`s, `asyncio.wait({...}, FIRST_COMPLETED)`, cancel the pending, re-raise a genuine failure from `done`, `finally: writer.close(); await writer.wait_closed()` guarded.
- **`display/service.py:81-110`** (`run_display`) — the pure-receiver variant (the host is receiver-leaning in 7.1 since nothing emits yet); registration-as-`Actor.X` alone makes it addressable.
- **Per-frame resilience** (`transport/cli.py:73-87`): a `msgspec.ValidationError` skips one frame and continues; a `ValueError` (framing) or `None` (hub EOF) ends the loop cleanly. A long-lived host must never die on one bad frame. (In 7.1 the host barely reads frames — but the loop it hands each plugin in 7.2 will, so set the pattern now.)

### The bus + contracts seams (verified line refs)

- **`connect(socket_path, Actor.PLUGIN_HOST)`** — `shelldon/core/bus/frame.py:70-74` (opens the UDS connection + sends the mandatory registration frame). `Actor.PLUGIN_HOST` already exists: `contracts/__init__.py:27`.
- **Hub registration** — `core/bus/server.py:86-93`: the host's first frame (its `Actor`) is read by `read_registration`; the hub then has the host in `_registry`, addressable for 7.2's fan-out.
- **`Region` enum** — `contracts/__init__.py:41-48` (only `FACE` today; the docstring explicitly reserves widget regions for Epic 7). **`MsgKind`** — `contracts/__init__.py:30-38` (untouched). **`ROUTING_TABLE`** — `contracts/__init__.py:274-281` (every `MsgKind` must have a row, test-enforced; do not add `EventKind` here).
- **Display composites regions generically** — `display/service.py:55-58` keys `latest_seq`/`pending` by `Region`, so a new `STATUS_BAR` region needs **no** display change; a snapshot to it would just be a new latest-wins stream (the *writer* of that stream is the 7.3 widget, not 7.1).

### Composition-root wiring (verified)

`app.py` is the only place processes are spawned. `launch_in_process` (`app.py:97-113`) lists actors as `asyncio.create_task`s; `launch_multiprocess` (`app.py:128-156`) lists them as `ctx.Process(...)` children with `name="shelldon-<actor>"` and `child.start()`, terminated+joined on teardown (`app.py:150-156`). Add the plugin-host to both, identically. The child proc target pattern is `def _x_proc(socket_path): asyncio.run(run_x(socket_path))` (`app.py:116-125`).

### Import-linter (verified)

`pyproject.toml` `[tool.importlinter]` `root_package = "shelldon"`, `include_external_packages = true`. Two `forbidden` contracts today: "core is LLM-free (AD-1)" (`source_modules = ["shelldon.core"]`, forbids the 6 provider SDKs) and "transport holds no model/tool creds" (`source_modules = ["shelldon.transport"]`, forbids the 6 providers + `shelldon.broker`). The plugins contract is the same family — copy the provider list, add `shelldon.core`, add the `ignore_imports` bus edge (D8). The CI gate is `uv sync --locked → uv run lint-imports → uv run pytest` (per `shelldon-dev-conventions`).

### Testing standards summary

- `uv run pytest -q` (default `addopts = -m 'not live'` keeps it offline). New tests live in `tests/` (e.g. `tests/test_plugin_host.py`). Use the in-process `BusServer` harness the transport/display tests already use for the lifecycle test; use plain fake `PluginManifest` objects + a fake plugins package (or monkeypatched `iter_modules`) for the load/conflict unit tests.
- Success = the new ACs covered AND the full suite green at the new count (baseline **460 pass** + new) AND `uv run lint-imports` 3 contracts KEPT / 0 broken AND `uv sync --locked` 0 new deps.
- This is an ADDITIVE feature story (unlike 7.0's behavior-preserving refactor): new modules + new tests are expected. But it must touch **no existing actor's behavior** — the empty-set production host and the 3rd import contract are the only changes existing tests should "see," and they should stay green.

### Project Structure Notes

- New: `shelldon/plugins/manifest.py` (`PluginManifest`, `Plugin` protocol, `BasePlugin`), `shelldon/plugins/host.py` (`discover_plugins`, `validate_claims`, `PluginLoadError`, `LoadedPlugins`, `run_plugin_host`). `shelldon/plugins/__init__.py` already exists with the right docstring ("bus clients, never import core").
- Modified: `shelldon/contracts/__init__.py` (`EventKind` enum + `Region.STATUS_BAR` + `__all__`), `pyproject.toml` (3rd import contract), `shelldon/app.py` (plugin-host wiring).
- Naming: `plugin-host` is the architecture's exact term (AD-8, renamed from "peripheral-host"). `Actor.PLUGIN_HOST` = `"plugin-host"`. Keep `host.py`/`manifest.py` as the infra split (loader/lifecycle vs contract types).

### References

- [Source: _bmad-output/planning-artifacts/epics.md#Story 7.1] — the three epic ACs (load from `plugins/` with manifests; reject conflicting claims at load; bus client never imports core, import-linter passes / CAP-7).
- [Source: ARCHITECTURE-SPINE.md#AD-8] — one generalized bus-only plugin model (hardware + behavioral); manifest declares emitted/subscribed kinds + GPIO/BLE + regions; host rejects conflicting claims at load; plugin is a bus client speaking only `Envelope`, never imports core; crashed plugin kills only itself.
- [Source: ARCHITECTURE-SPINE.md#AD-5] — closed/registered `region-id` type in `contracts/`; core owns `face`, plugins claim widget regions; claims arbitrated like GPIO/BLE, conflicts rejected at load; no two writers per region.
- [Source: ARCHITECTURE-SPINE.md#AD-11] — closed envelope header + two routing modes; the closed `event` kind set declared in `contracts/`; subscription registry built at load from manifests, no runtime self-registration.
- [Source: ARCHITECTURE-SPINE.md#CAP-7] — pluggable plugins via plugin-host + one bus-only plugin contract (events, subscriptions, private state, display regions); governed by AD-8 + AD-5.
- [Source: shelldon/contracts/__init__.py:19-48,274-281] — `Actor.PLUGIN_HOST` (exists), `MsgKind`/`Region` enums (the `Region` docstring reserves widget regions for Epic 7), `ROUTING_TABLE` (do not touch).
- [Source: shelldon/transport/cli.py:24-126] — the bus-client adapter template to mirror (imports `shelldon.core.bus`; `connect`→two loops→`FIRST_COMPLETED` teardown; per-frame resilience).
- [Source: shelldon/display/service.py:23-110] — the pure-receiver bus-client variant; region-keyed compositor (generic, needs no change for a new region).
- [Source: shelldon/core/bus/frame.py:40-74] — `connect`/`write_registration`/`read_frame`/`write_frame` (the shared bus client the host imports).
- [Source: shelldon/core/bus/server.py:76-145] — the hub: registration handshake + `_registry` (how a `PLUGIN_HOST` becomes addressable for 7.2 fan-out) + `ROUTING_TABLE` routing.
- [Source: shelldon/app.py:97-156] — composition root: add the plugin-host to both launchers identically.
- [Source: pyproject.toml (tool.importlinter)] — the two existing `forbidden` contracts to mirror for the plugins contract.
- [Source: _bmad-output/implementation-artifacts/7-0-extract-turn-dispatch-from-runtime.md] — the prep story that gated this one (dispatch seam now lives in `core/dispatch.py`; core is ready for Epic 7).
- [Source: _bmad-output/implementation-artifacts/deferred-work.md:183,190,174] — Epic-7-plugin-host-boundary markers (PiSugar2 `power` typing, `PowerState.charge` validation, learnings string caps) — confirm 7.4/later, NOT 7.1.

### Open questions for the owner (do not block dev — defaults are chosen above)

1. **Manifest shape (D1):** typed `MANIFEST: PluginManifest` per module (default, chosen) vs a `manifest.toml` per plugin dir. Typed wins on type-safety + zero new deps; TOML wins if you foresee non-Python plugin authors. Also: discovery via `iter_modules` (default) vs an explicit registry/entry-points — relevant if plugins should live *outside* the `shelldon.plugins` package later.
2. **Vocabulary surface (D2):** add just `STATUS_BAR` (one widget region, default) or a small set now (e.g. `STATUS_BAR`, `WIDGET_A`/`WIDGET_B`) so 7.3's XP widget + a future second widget have room without a contracts touch each? And: the closed `EventKind` set — is `message-answered`/`tool-used`/`day-alive` the final v1 list, or reserve a couple more now?
3. **Bus-client location (D8):** keep the precedent-matching `ignore_imports` shape (default — plugins import `shelldon.core.bus`), or do the cleaner refactor that promotes the bus client into a neutral `shelldon.bus` package so "plugins never import core" is literally true (bigger blast radius: re-points `core`/`transport`/`display` imports too — would be its own prep story, 7.0-style)?

## Dev Agent Record

### Agent Model Used

claude-opus-4-8[1m] (Opus 4.8, 1M context)

### Debug Log References

- Baseline (pre-change): `uv run pytest -q` → 460 passed, 3 skipped, 5 deselected; `uv run lint-imports` → 2 contracts KEPT.
- Post-change gate: `uv run pytest -q` → **477 passed, 3 skipped, 5 deselected** (+17 new tests); `uv run lint-imports` → 3 contracts KEPT, 0 broken; `uv sync --locked` → 0 dep changes.
- Import-boundary probe: temporarily appended `import shelldon.core.runtime` to `plugins/host.py` → "plugins never import core (AD-8/CAP-7) BROKEN" (2 kept/1 broken); removed → 3 KEPT. The contract bites.

### Completion Notes List

- **CAP-7 boundary is mechanically real.** A plugin is a bus client speaking only the `Envelope`/bus vocabulary; the new import-linter contract forbids `shelldon.core` + the 6 provider SDKs, permitting only the shared `shelldon.core.bus` client edge (the same seam `transport`/`display` already use — D8). Verified by the probe.
- **Scope held to contract + host (D3).** No event fan-out, no `Event` body, no `MsgKind`, no hardware. `EventKind` + `Region.STATUS_BAR` are pure declarations (no `SCHEMA_VERSION` bump, no `ROUTING_TABLE`/union change). A plugin's `run()` is a stay-alive stub; Story 7.2 gives it events to react to.
- **Conflict rejection is fail-fast at load (AC3).** `validate_claims` runs *before* the bus connect, so a duplicate region/resource raises `PluginLoadError` and the host never registers. Duplicate *subscriptions* are allowed (broadcast is 1→N).
- **Manifest is typed, not TOML (D1).** Each plugin module exposes `MANIFEST: PluginManifest` (+ optional `PLUGIN` instance for a custom `run`); manifest-only modules are wrapped in `BasePlugin`. Closed `EventKind`/`Region` enums make a typo a decode error (tested) — no parser, no hand-rolled validation.
- **Lifecycle mirrors the established adapter (AC4).** `run_plugin_host` copies `run_cli_transport`'s `connect → asyncio.wait(FIRST_COMPLETED) → cancel-pending → re-raise → finally close` shape. The empty 7.1 production set runs one idle `BasePlugin` so the host's teardown matches the other actors.
- **Wired into both launchers (AC6).** The existing app smoke (`launch_in_process`) now starts the host and stays green — proving it composes without disturbing the turn or the clean teardown (socket unlink). 0 new deps (stdlib `pkgutil`/`importlib` + `msgspec`).
- **One small deviation from the story's Task 5 text:** the `ignore_imports` `"shelldon.plugins.* -> shelldon.core.bus.*"` submodule wildcard was dropped — import-linter errors on an ignore that matches nothing, and only the package-level edge (`-> shelldon.core.bus`) actually exists (the `connect` import resolves to the package `__init__`). Add the submodule wildcard later only if a plugin imports a bus submodule directly.
- **Open questions (Q1–Q3 in the story) were left at their chosen defaults** — the owner ran dev without overriding them.

### File List

- `shelldon/contracts/__init__.py` — MODIFIED. Added `EventKind` StrEnum (closed broadcast set) + `Region.STATUS_BAR` widget region + `EventKind` in `__all__`. Pure declarations (no `SCHEMA_VERSION`/`ROUTING_TABLE`/union change).
- `shelldon/plugins/manifest.py` — NEW. `PluginManifest` (typed frozen struct), `Plugin` protocol, `BasePlugin` (stay-alive bus loop). Review: added a `shelldon.plugins.manifest` logger + a warning on the framing-error exit.
- `shelldon/plugins/host.py` — NEW. `PluginLoadError`, `LoadedPlugins`, `validate_claims` (load-time conflict rejection + subscription registry), `plugin_from_module`, `discover_plugins` (`pkgutil.iter_modules`), `run_plugin_host` (bus-client lifecycle). Review: `plugin_from_module` validates `PLUGIN` against the `Plugin` protocol (skip+log); `discover_plugins` isolates a broken-import module (skip+log); `run_plugin_host` gained the SINGLE-READER-LIMIT doc-note (7.2 prerequisite).
- `shelldon/app.py` — MODIFIED. Imported `run_plugin_host`; added `_plugin_host_proc` + the `shelldon-plugin-host` child in `launch_multiprocess`; added the host task to `launch_in_process`.
- `pyproject.toml` — MODIFIED. Added the 3rd import-linter contract "plugins never import core (AD-8/CAP-7)".
- `tests/test_plugin_contract.py` — NEW. 6 tests: the closed vocabulary, wire-additivity, the typed manifest + closed-enum decode guard.
- `tests/test_plugin_host.py` — NEW. 14 tests: conflict rejection (region/resource/subscription), discovery (empty real package + on-disk fake package + module mapping), the bus-client lifecycle (connect/teardown + fail-fast-on-conflict), the both-launchers wiring guard, and 3 review-patch regressions (malformed `PLUGIN` skipped, broken-import skipped, framing-error logged).
- `_bmad-output/implementation-artifacts/sprint-status.yaml` — MODIFIED. `7-1 → in-progress → review`.

### Change Log

- 2026-06-19 — Story 7.1 implemented: the generalized plugin contract (`PluginManifest` + `Plugin`/`BasePlugin`) + the `plugin-host` (discovery from `plugins/`, load-time conflict rejection, subscription registry, bus-client lifecycle). Added the closed `EventKind` vocabulary + `Region.STATUS_BAR` to contracts (pure declarations, no schema bump) and the 3rd import-linter contract enforcing "plugins never import core" (CAP-7). Wired the host into both composition-root launchers (empty set idles). +17 tests, suite **477 pass**, 3 import contracts KEPT, 0 new deps. Events fan-out = 7.2, XP widget = 7.3, sensing = 7.4. Status → review.
- 2026-06-19 — Code review addressed (3 Patches fixed, 1 Decision resolved, 10 deferred). Patches: `plugin_from_module` now `isinstance`-validates a `PLUGIN` against the `Plugin` protocol (malformed → log+skip); `discover_plugins` wraps `import_module` (broken module → log+skip, AD-8 isolation); `BasePlugin.run` logs the framing-error exit. Decision (shared reader/writer across N plugin tasks): owner chose **defer-to-7.2 + doc-note** — Option A (per-plugin connect) is a trap (Actor-key registry clobber); the correct fix IS 7.2's host-owned read loop + fan-out. Added a SINGLE-READER-LIMIT doc-note to `run_plugin_host` and a `plugin-host-owns-the-read-loop` icebox prerequisite on 7.2. +3 regression tests, suite **480 pass**, 3 import contracts KEPT, 0 new deps. Status → done.
