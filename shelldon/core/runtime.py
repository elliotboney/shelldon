"""The core runtime / turn orchestrator (AD-9/AD-12/AD-13/AD-5/AD-1).

`Core` ties the five actors together around one real turn: an INBOUND_MSG becomes
a worker turn, the worker's Result returns over the bus and is fenced, a reply
leaves as OUTBOUND_MSG, and the display is pushed a face snapshot on every turn
lifecycle event. It is the single-consumer loop over `bus.core_inbox`, so the
Arbiter (admission policy) is accessed serially — no lock needed.

LLM-free (AD-1): this imports no provider lib and not `worker/`. It depends on an
injected **spawner** (anything with `async spawn_turn(turn_id, prompt)`,
`async reap_current()`, `async ready()`) so `core/` never reaches into an adapter;
the composition root (the integration test, or a later `app.py`) injects the real
`ForkServer`.

Scope: ≤1 + coalescing + degrade-on-chain-exhaustion + a minimal turn timeout. The
prompt IS the owner's message text (real prompt assembly is Epic 4); faces are
placeholder lifecycle tokens (real expressions are Story 3.3). The provider chain +
fallback + the degrade-to-reflex-ack on whole-chain exhaustion are live (Epic 2,
Story 2.3); cooldown/budget are Epic 5; a full watchdog/supersession escalation is
later.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from datetime import time as dt_time

import msgspec
from uuid import uuid4

from shelldon.contracts import (
    Actor,
    AddFace,
    CaptureLearning,
    Envelope,
    Event,
    EventKind,
    Message,
    ResolveLearning,
    MsgKind,
    OutboundMessage,
    ProposeTool,
    Region,
    RequestToolApproval,
    Result,
    RewriteDirective,
    StateSnapshot,
    ToolCall,
)
from shelldon.core import selfcode
from shelldon.core.arbiter import Arbiter
from shelldon.core.bus import BusServer
from shelldon.core.faces import DEFAULT_FACES_PATH, FaceRegistry
from shelldon.core.history import DEFAULT_HISTORY_PATH, HistoryStore
from shelldon.core.memory import DEFAULT_MEMORY_ROOT, CuratedMemory
from shelldon.core.reflexes import compute_reflex_patch
from shelldon.core.budget import BudgetGate
from shelldon.core.dispatch import TurnDispatcher
from shelldon.core.power import BackoffPolicy, PowerState
from shelldon.core.reactions import compute_nudge_patch
from shelldon.core.scheduler import CostTier, Idle, Interval, Job, QuietHours, Scheduler
from shelldon.core.state import DEFAULT_CHECKPOINT_PATH, PersistentState
from shelldon.core.turn import TurnFence
from shelldon.timeouts import TURN_TIMEOUT

log = logging.getLogger("shelldon.core.runtime")

#: Placeholder lifecycle face tokens — the real expression vocabulary and the
#: mood->face mapping are Story 3.3; the personality-state struct is Epic 3.
FACE_THINKING = "thinking"
FACE_REPLY = "happy"
FACE_DEGRADED = "cant-think"

#: Graceful "can't think right now" reply (AC3) — the chain-exhaustion reflex ack
#: (Story 2.3). The real resident reflex loop is Epic 3 / Story 3.2.
DEGRADE_TEXT = "…can't think right now…"

#: Max chars of a reply/dream shown on the bottom caption strip (B.3) before an ellipsis.
#: The panel auto-shrinks the font, but a hard cap keeps the line readable, not microscopic.
_CAPTION_MAX = 48

#: How long a REACTION (the model's chosen face + its thought) LINGERS on the screen before
#: the at-rest mood is allowed to replace it (B.3 review: without this the very next reflex
#: tick overwrote the reply, so it only flashed). Holds BOTH the face and the caption, so the
#: deliberate expression + thought settle to the ambient mood together, not on split cadences.
_REACTION_DWELL_S = 60.0

#: The expressions the model may pick as its reaction face (B.3) — the emotional subset of the
#: renderer's FACE_ART. The system/lifecycle tokens (thinking/cant-think/low-battery) are
#: core-driven states, not reactions, so they're not offered; an unknown/absent pick → the
#: default reply face. Kept here (not imported from display) so core stays display-agnostic.
_REACTION_FACES = frozenset({"happy", "excited", "curious", "content", "grumpy", "sleepy"})


def _caption_for(payload: str) -> str:
    """The bottom-strip caption (B.3) for a reply/dream: the first line of the REAL text,
    trimmed + truncated. An empty/whitespace payload (e.g. a dream that only proposed ops)
    → a resting ellipsis, so the line shows the pet is alive without inventing content. Pure."""
    text = (payload or "").strip()
    if not text:
        return "…"
    line = text.splitlines()[0].strip()
    if len(line) > _CAPTION_MAX:
        line = line[: _CAPTION_MAX - 1].rstrip() + "…"
    return line

#: Default turn timeout (AC3 "rather than hanging"). Tests inject a small value.
#:
#: Coherent-timeout invariant (Story 5.0): this is the BINDING reap horizon (T) and is the
#: LARGEST of the chain W < R < T — the worker self-reports a failure Result (W,
#: `_COMPLETION_TIMEOUT_S`) and the fork-server SIGKILL-reclaims a wedged child (R,
#: `_REAP_TIMEOUT_S`) BEFORE core abandons the turn here. That keeps the arbiter slot and
#: the fork-server guard releasing in lockstep — no ~90s freeze from a worker holding the
#: fork past core's degrade. See `tests/test_resilience.py::test_timeout_chain_is_coherent`.
#: The whole chain derives from one env knob (`SHELLDON_TURN_TIMEOUT`) in shelldon.timeouts;
#: unset keeps the historical 30s. Re-exported here so the name stays stable for callers/tests.
DEFAULT_TURN_TIMEOUT = TURN_TIMEOUT

#: Default personality-state checkpoint cadence (Story 3.1). Periodic, NOT per change
#: (NFR7) — tests inject a small interval. Epic 5's scheduler will subsume this.
DEFAULT_CHECKPOINT_INTERVAL = 60.0

#: Default resident-reflex tick cadence (Story 3.2). A gentle in-core drift between
#: turns (no LLM) — tests inject a small interval. Now the reflex job's interval (5.1).
DEFAULT_REFLEX_INTERVAL = 10.0

#: Default scheduler base tick (Story 5.1). The resolution at which the scheduler
#: re-checks job due-ness — finer than the smallest job period (reflex, 10s) so each
#: job fires close to its own cadence. Injectable so the 1.9 soak can park the
#: scheduler far out of its measurement window (the background-emitter rule).
DEFAULT_SCHEDULER_INTERVAL = 1.0

#: Default daily cap on scheduler-initiated turns (Story 5.2; owner decision). The pet's
#: self-driven LLM activity can't exceed this many turns/day — the credit guardrail (AD-9).
#: Injectable; reflex jobs (no LLM) are never counted.
DEFAULT_DAILY_TURN_BUDGET = 12

#: Default minimum interval (seconds) between scheduler-initiated turns (Story 5.2; owner
#: decision = 30 min). Stops a proactive stampede; orthogonal to the daily budget.
DEFAULT_TURN_COOLDOWN = 1800.0

#: Battery-aware backoff defaults (Story 5.3; owner decision). On battery the scheduler
#: stretches all Interval/Idle job cadences by EASED_SCALE (charge OK) or LOW_SCALE (charge
#: < LOW_CHARGE_THRESHOLD) and skips non-essential (EASED) / all (LOW) turn jobs. All
#: injectable; validated by BackoffPolicy. The real PiSugar2 read is Epic 7 (plugin-host);
#: the default power reader is the plugged-in stub (LIVELY → behaves exactly as 5.2).
DEFAULT_EASED_SCALE = 3.0
DEFAULT_LOW_SCALE = 6.0
DEFAULT_LOW_CHARGE_THRESHOLD = 0.20

#: Default period (seconds) of the proactive check-in (Story 5.4; owner decision = recurring
#: "every few hours regardless of replies"). The proactive job is an Interval-cadence turn job —
#: it fires every `proactive_interval`s since its OWN last run, independent of owner activity (a
#: steady drumbeat, not a fire-once-per-idle ping). The 5.2 cooldown (30 min) + daily budget (12)
#: still bound its frequency (4 hr → ~6/day, well under the cap), the 5.3 battery backoff stretches
#: it on battery, and the PERSISTED cooldown/budget bound boot/crash-loop re-fires. Injectable.
DEFAULT_PROACTIVE_INTERVAL = 14400.0

#: Default quiet-hours window (owner-local) during which the periodic proactive check-in is
#: suppressed — no unprompted pings overnight (owner decision). Overridable via the
#: `SHELLDON_QUIET_HOURS` env var (see `parse_quiet_hours`); set it to "off" to disable.
#: Only the proactive job is gated — the dream cadence is untouched (dreams run overnight).
DEFAULT_QUIET_HOURS = (dt_time(22, 0), dt_time(7, 0))


def parse_quiet_hours(raw: str | None) -> tuple[dt_time, dt_time] | None:
    """Parse `SHELLDON_QUIET_HOURS` into a `(start, end)` owner-local window, or `None` to disable.

    Accepts ``"HH:MM-HH:MM"`` (e.g. ``"22:00-07:00"``, midnight-crossing allowed); ``""`` /
    ``"off"`` / ``"none"`` / ``"disabled"`` disable quiet hours entirely (24/7 proactive). An UNSET
    (None) value yields the default window. An unparseable value logs a warning and falls back to
    the default — a typo must never silently switch overnight pinging on."""
    if raw is None:
        return DEFAULT_QUIET_HOURS
    s = raw.strip().lower()
    if s in ("", "off", "none", "disabled", "false", "0"):
        return None
    try:
        start_s, end_s = s.split("-")
        start, end = dt_time.fromisoformat(start_s.strip()), dt_time.fromisoformat(end_s.strip())
        if start.tzinfo is not None or end.tzinfo is not None or start == end:
            raise ValueError("bounds must be tz-naive and differ")
        return (start, end)
    except (ValueError, AttributeError) as exc:
        log.warning("invalid SHELLDON_QUIET_HOURS %r (%s); using default %s", raw, exc, DEFAULT_QUIET_HOURS)
        return DEFAULT_QUIET_HOURS

#: Recorded as the owner-side of a proactive turn's history row (Story 5.4) — the pet spoke
#: with no real owner message, so the directive is NOT stored as if the owner typed it; this
#: marker preserves continuity (the next turn knows it reached out) without that pollution.
PROACTIVE_OWNER_MARKER = "(shelldon spoke up on its own)"

#: Default owner-idle threshold (seconds) before the dream cycle runs (Story 6.2; owner
#: decision = ~6 hr). The dream is an Idle-cadence turn job like the proactive one, but
#: heavier (`cost=3`) and only when there are pending learnings to consolidate. Injectable.
DEFAULT_DREAM_IDLE_INTERVAL = 21600.0

#: Budget weight of one dream turn (Story 6.2 / 5.2 decision 3 — "a dream counts as several
#: pings"). A dream spends 3 of the daily turn budget (default 12).
DREAM_COST = 3

#: Recorded as the owner-side of a dream turn's history row (Story 6.2) — the dream is
#: self-initiated (no owner message), so its directive is not stored as if the owner typed it.
DREAM_OWNER_MARKER = "(shelldon dreamed)"

#: Default per-kind cooldown (seconds) between mood nudges (Story 7.5). A buggy or chatty
#: plugin can't peg the mood: a second nudge of the SAME affect kind within this window is
#: dropped (the reflex baseline-settle, Story 3.2, provides the decay back to neutral). 30s
#: mirrors the 5.2 turn-cooldown idiom. Injectable; per-kind so distinct affects are independent.
DEFAULT_NUDGE_COOLDOWN = 30.0

#: Cap on the memory-ops one turn may propose (Story 4.5). A runaway/abusive reply
#: can't flood the curated tree; the overflow is dropped with a warning (never silently
#: truncated). Generous — a normal turn proposes a handful.
MAX_PROPOSED_OPS = 16

#: Story 9.3: warn (don't reject) when a parked approval's message list is unusually large —
#: a chained-approval turn shouldn't balloon the sqlite blob. ~2 msgs/iteration × the worker's
#: 6-iteration cap, with headroom. A hard cap is Story 9.5.
_APPROVAL_MESSAGES_WARN = 16

#: Story 9.5: how many failures (import/run) a self-coded tool may accrue before core moves it to
#: `tools-quarantine/` (AD-8). Generous — a transient one-off failure shouldn't banish a tool; a
#: repeatedly-broken one stops log-spamming + wasting calls each fork.
_QUARANTINE_STRIKE_THRESHOLD = 3

#: Story 9.5: cadence of the housekeeping `prune` job that drops expired parked approvals (9.3) +
#: promotions (9.4) — their `prune_expired_*` methods had no call site. Hourly is ample (rows are
#: also consumed/expired passively on tap); a REFLEX-tier job (no LLM, touches only sqlite).
DEFAULT_PRUNE_INTERVAL = 3600.0

#: Story 9.4: how much of a failed gate's pytest output to show the owner in the reply — enough
#: to see what broke, short enough for a chat message (the full capped output is logged).
_GATE_REPLY_OUTPUT_CHARS = 500


def _brief(output: str) -> str:
    """The tail of a failed gate's output for the owner's note (the failure summary pytest prints
    last), capped — the full output is already logged at the gate."""
    tail = output.strip()[-_GATE_REPLY_OUTPUT_CHARS:]
    return tail if tail else "(no output)"


class Core:
    """The turn orchestrator: owns the bus, fence, arbiter, an injected spawner, and
    the persistent personality-state (restored on construction, AD-7)."""

    def __init__(
        self,
        socket_path,
        spawner,
        *,
        turn_timeout: float = DEFAULT_TURN_TIMEOUT,
        checkpoint_path=None,
        checkpoint_interval: float = DEFAULT_CHECKPOINT_INTERVAL,
        reflex_interval: float = DEFAULT_REFLEX_INTERVAL,
        scheduler_interval: float = DEFAULT_SCHEDULER_INTERVAL,
        daily_turn_budget: int = DEFAULT_DAILY_TURN_BUDGET,
        turn_cooldown: float = DEFAULT_TURN_COOLDOWN,
        eased_scale: float = DEFAULT_EASED_SCALE,
        low_scale: float = DEFAULT_LOW_SCALE,
        low_charge_threshold: float = DEFAULT_LOW_CHARGE_THRESHOLD,
        proactive_interval: float = DEFAULT_PROACTIVE_INTERVAL,
        quiet_hours: tuple[dt_time, dt_time] | None = DEFAULT_QUIET_HOURS,
        dream_idle_interval: float = DEFAULT_DREAM_IDLE_INTERVAL,
        nudge_cooldown: float = DEFAULT_NUDGE_COOLDOWN,
        monotonic=None,
        power=None,
        faces_path=None,
        history_path=None,
        memory_root=None,
        workspace_root=None,
    ):
        if checkpoint_interval <= 0:
            raise ValueError(f"checkpoint_interval must be positive, got {checkpoint_interval!r}")
        if reflex_interval <= 0:
            raise ValueError(f"reflex_interval must be positive, got {reflex_interval!r}")
        if scheduler_interval <= 0:
            raise ValueError(f"scheduler_interval must be positive, got {scheduler_interval!r}")
        if proactive_interval <= 0:
            raise ValueError(f"proactive_interval must be positive, got {proactive_interval!r}")
        if dream_idle_interval <= 0:
            raise ValueError(f"dream_idle_interval must be positive, got {dream_idle_interval!r}")
        self.bus = BusServer(socket_path=socket_path)
        self.fence = TurnFence()
        self.arbiter = Arbiter()
        self.spawner = spawner
        self.turn_timeout = turn_timeout
        self.checkpoint_path = checkpoint_path if checkpoint_path is not None else DEFAULT_CHECKPOINT_PATH
        self.checkpoint_interval = checkpoint_interval
        self.reflex_interval = reflex_interval
        self.scheduler_interval = scheduler_interval
        self.proactive_interval = proactive_interval
        self.quiet_hours = quiet_hours
        self.dream_idle_interval = dream_idle_interval
        self.nudge_cooldown = nudge_cooldown
        #: Monotonic clock for the per-kind nudge cooldown (Story 7.5). Injectable so the
        #: cooldown is testable without real sleeps; defaults to the wall-free `time.monotonic`.
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        #: Last-applied timestamp per affect kind — the debounce ledger (Story 7.5).
        self._last_nudge: dict[EventKind, float] = {}
        #: Personality state lives in RAM for the process lifetime, restored from the
        #: last checkpoint (defaults cleanly on first run — AC1).
        self.state = PersistentState.load(self.checkpoint_path)
        #: The editable faces registry — core owns the mood->face vocabulary (AD-5)
        #: and maps the drifting mood to a token (Story 3.3); the display renders it.
        self.faces = FaceRegistry.load(faces_path if faces_path is not None else DEFAULT_FACES_PATH)
        #: Conversation history — core is the sole writer (AD-6/AD-5); records each
        #: completed turn's (owner, pet) pair. Workers read it read-only (Story 4.4).
        self.history = HistoryStore.open(history_path if history_path is not None else DEFAULT_HISTORY_PATH)
        #: The curated markdown memory tree — core is the sole writer (AD-5). The worker
        #: proposes memory-ops on its Result (Story 4.5); core validates+applies them here
        #: via the 4.2 apply path. Injectable root; tests redirect off real $HOME.
        self.memory = CuratedMemory(memory_root if memory_root is not None else DEFAULT_MEMORY_ROOT)
        #: The self-coded-tool workspace root (Story 9.4) — core stages/gates/promotes tools here
        #: (sole writer, AD-5); the worker discovers the live dir per fork. Defaults to the module
        #: const; tests inject a tmp root. Same root the worker's `build_tool_registry` reads.
        self.workspace_root = workspace_root if workspace_root is not None else selfcode.DEFAULT_WORKSPACE_ROOT
        #: The owner prompt + turn_id of the in-flight turn (≤1, AD-9), stashed at
        #: turn start so a completed/degraded turn can be paired and recorded.
        self._current_prompt: str | None = None
        self._current_turn_id: str | None = None
        #: Story 10.2: whether the in-flight turn is owner-present (a chat turn) vs unattended
        #: (proactive/dream). A `rewrite_directive` proposal is gated to owner-present turns only —
        #: the constitution can't drift while the owner isn't there to approve (AC6).
        self._current_turn_is_owner: bool = False
        #: The face token currently on screen — both lifecycle and mood pushes update
        #: it, so a mood face re-pushes after a lifecycle face and identical mood ticks
        #: don't spam the display.
        self._last_face: str | None = None
        #: The caption text currently on the bottom strip (B.3) — tracked like `_last_face`
        #: so an identical caption doesn't re-push (and re-flash the slow E-Ink panel).
        self._last_caption: str | None = None
        #: Monotonic deadline until which a reaction (face + thought) holds the screen against
        #: the at-rest mood (B.3). 0.0 = nothing holding it (settle to mood immediately).
        self._reaction_hold_until = 0.0
        self._seq = 0
        self._timeout_task: asyncio.Task | None = None
        #: The in-flight worker's reap task (Story 5.0). Held (not just fire-and-forget)
        #: so a turn end can AWAIT it before releasing the arbiter slot — the fork-server
        #: guard and the arbiter slot then free in lockstep, with no divergence window.
        self._reap_task: asyncio.Task | None = None
        #: The named-job scheduler is a long-lived SINGLETON task, so it lives in its
        #: own slot (like `_timeout_task`), NOT in `_bg`. `_bg` holds transient per-turn
        #: reap tasks that drain to empty; a permanent resident there would break that
        #: "drains to 0" invariant (1.9 soak — see test_endurance_soak).
        self._scheduler_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        #: The scheduler-turn spend gate (Story 5.2, AD-9) — a daily turn-count budget +
        #: a cooldown. Constructing it validates the config (delegated, like Interval()).
        self._budget = BudgetGate(daily_turn_budget=daily_turn_budget, turn_cooldown=turn_cooldown)
        #: Battery-aware backoff (Story 5.3, AD-14) — the policy that stretches cadences +
        #: skips non-essential turn jobs on battery. Constructing it validates the config.
        self._backoff = BackoffPolicy(
            eased_scale=eased_scale, low_scale=low_scale, low_charge_threshold=low_charge_threshold
        )
        #: The injected power reader the scheduler samples each tick (Story 5.3). Default =
        #: the plugged-in stub (LIVELY), so an un-instrumented deployment behaves exactly as
        #: 5.2. The real PiSugar2 read is a plugin-host plugin (Epic 7, AD-8) that will push
        #: cached PowerState updates into core; this seam swaps the stub for that cached read
        #: with zero policy change. The reader MUST be non-blocking (no socket I/O in a tick).
        self._power = power if power is not None else (lambda: PowerState(on_battery=False, charge=None))
        #: The in-core scheduler (Story 5.1, AD-14) — "heartbeat is now just one job."
        #: The Epic 3 reflex drift and the periodic checkpoint flush are wired here as
        #: reflex-tier jobs (they moved off their standalone loops; behavior unchanged).
        #: Built-ins are registered explicitly at composition (a general plugin API is
        #: Epic 7). Turn-tier jobs route to `_dispatch_turn_job` — the arbiter-gated +
        #: cooldown + daily-budget seam (Story 5.2). No turn job is registered in 5.1/5.2
        #: (the first proactive job is Story 5.4); the gate is live and tested for it.
        #: The turn-dispatch driver (Story 7.0) — the arbiter + cooldown + budget admit gate
        #: for scheduler-proposed turn jobs, extracted from this class into `core/dispatch.py`.
        #: Constructed with the collaborators it reads injected, plus a `start_turn` callback
        #: into `_start_turn` (the turn lifecycle stays here). `Core` keeps thin delegators
        #: (`_dispatch_turn_job` etc.) so existing callers are unchanged.
        self._dispatcher = TurnDispatcher(
            arbiter=self.arbiter,
            budget=self._budget,
            state=self.state,
            faces=self.faces,
            history=self.history,
            memory=self.memory,
            start_turn=self._start_turn,
        )
        self.scheduler = Scheduler(
            now=lambda: datetime.now(UTC),
            dispatch_turn=self._dispatcher.dispatch_turn_job,
            power=self._power,
            backoff=self._backoff,
        )
        self.scheduler.register(
            Job("reflex", Interval(self.reflex_interval), CostTier.REFLEX, self._run_reflex_job)
        )
        self.scheduler.register(
            Job("checkpoint", Interval(self.checkpoint_interval), CostTier.REFLEX, self._run_checkpoint_job)
        )
        #: Story 9.5: the housekeeping prune job — drops expired parked approvals (9.3) + promotions
        #: (9.4). REFLEX-tier (no LLM); the scheduler guards a bad tick like the other reflex jobs.
        self.scheduler.register(
            Job("prune", Interval(DEFAULT_PRUNE_INTERVAL), CostTier.REFLEX, self._run_prune_job)
        )
        #: The proactive musing (Story 5.4, CAP-4) — the first self-initiated turn job. An
        #: Interval cadence fires it every `proactive_interval`s since its OWN last run — a
        #: steady periodic check-in INDEPENDENT of owner activity (owner decision: recurring
        #: "every few hours regardless of replies", not the original fire-once-per-idle-stretch).
        #: Its prompt is BUILT at dispatch from live mood (no static text), and it records a
        #: synthetic owner-side marker (no real owner message). It rides the 5.2 cooldown/budget
        #: gate + the 5.3 battery gate unchanged (non-essential → eased off first on battery); the
        #: PERSISTED cooldown/budget also bound boot/crash-loop re-fires across restarts.
        #:
        #: Story 9.5 (AC3 credit gating): `cost=1` (the default) is INTENTIONAL. A proactive turn
        #: is a self-initiated MUSING built from live mood (`build_proactive_prompt`) — it checks in,
        #: it doesn't run a tool task — so it virtually never invokes the loop. The hard runaway-spend
        #: bound does NOT rely on this cost: `_MAX_TOOL_EXECUTIONS` (worker) caps model-calls PER TURN
        #: regardless of cost, so worst-case self-driven spend = daily_turn_budget × that ceiling no
        #: matter what any turn's cost is. The `cost` weight rations TURNS, not calls; weighting
        #: proactive at 6 would gut its frequency (~2/day) for a worst case the ceiling already bounds.
        #: (The dream is `cost=3` because it's a deliberately heavier review, not because of tools.)
        #: The proactive cadence: a periodic Interval, wrapped in QuietHours so it stays silent
        #: overnight (owner-local window, default 22:00–07:00; `None` = no quiet hours / 24/7).
        proactive_cadence = Interval(self.proactive_interval)
        if self.quiet_hours is not None:
            proactive_cadence = QuietHours(proactive_cadence, *self.quiet_hours)
        self.scheduler.register(
            Job(
                "proactive",
                proactive_cadence,
                CostTier.TURN,
                prompt_builder=self._dispatcher.build_proactive_prompt,
                history_owner_text=PROACTIVE_OWNER_MARKER,
            )
        )
        #: Defer the FIRST periodic proactive check-in by one interval: Interval is due on a None
        #: last_run, so without this the musing fires on the very first tick — before the transport/
        #: display bus clients have connected — spending a turn whose reply is dropped into the void
        #: (and re-firing on every crash-loop restart). Seeding last_run = boot time waits a full
        #: period, by which point the transports are up and the owner has likely interacted.
        self.scheduler.mark_ran("proactive", datetime.now(UTC))
        #: The dream cycle (Story 6.2, AD-15/CAP-11) — a proactive-turn VARIANT: an Idle turn
        #: job (longer 6h cadence), heavier `cost=3`, that reviews the pending learnings 6.1
        #: captured and proposes promotions/prunes + a running summary. Its prompt is BUILT at
        #: dispatch from the pending learnings (empty → the 5.4 skip path fires, no dream when
        #: nothing's pending). Two Idle turn jobs now coexist (proactive 1h, dream 6h): each
        #: fires once per idle stretch on its own period; a cold start while already >6h idle
        #: could make both due in one tick → the arbiter admits one and defers the other. The
        #: deferred job's `last_run` still advances this tick, so it does NOT retry in the same
        #: stretch — it waits for a fresh owner interaction + another idle stretch. Acceptable
        #: (a rare cold-start case); no cross-job coordination.
        self.scheduler.register(
            Job(
                "dream",
                Idle(self.dream_idle_interval),
                CostTier.TURN,
                prompt_builder=self._dispatcher.build_dream_prompt,
                history_owner_text=DREAM_OWNER_MARKER,
                cost=DREAM_COST,
            )
        )

    async def run(self) -> None:
        """Start the bus, wait for the spawner, then consume core_inbox forever.

        Single consumer: only this loop touches the arbiter/fence, so admission is
        serial (no `await` interleaves a second submit mid-decision). On
        cancellation (teardown) it cancels every background task it scheduled.
        """
        await self.bus.start()
        await self.spawner.ready()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        try:
            while True:
                env = await self.bus.core_inbox.get()
                if env.kind is MsgKind.INBOUND_MSG:
                    if env.body.approval_turn_id is not None:
                        # An approval DECISION (the owner tapped Approve/Deny), not chat text
                        # (Story 9.3) — route to the resume path, never the arbiter/coalescer.
                        self._mark_interaction()
                        approved = env.body.approved
                        if approved is None:  # malformed decision frame → fail safe (deny), logged
                            log.warning(
                                "approval decision for %s missing 'approved'; treating as deny",
                                env.body.approval_turn_id,
                            )
                            approved = False
                        await self._handle_approval_decision(env.body.approval_turn_id, approved)
                    else:
                        self._mark_interaction()
                        prompt = self.arbiter.submit(env.body.text)
                        if prompt is not None:
                            await self._start_turn(prompt)
                elif env.kind is MsgKind.RESULT:
                    await self._handle_result(env)
                elif env.kind is MsgKind.EVENT:
                    # A broadcast event the hub delivered to core (Story 7.5) — today only a
                    # plugin-emitted affect nudge moves mood; any other kind is a no-op.
                    await self._handle_nudge(env.body.event)
                else:
                    log.warning("core ignoring unexpected inbox envelope %s (%s)", env.id, env.kind)
        finally:
            self._cleanup()

    async def _start_turn(self, prompt: str, *, record_owner_text: str | None = None) -> None:
        """Open a fenced turn, push the 'thinking' face, spawn the worker, schedule
        its reap (fire-and-forget — the Result returns over the bus), arm the timeout.

        `record_owner_text` is what to record as the owner side in history (Story 5.4): for
        a proactive turn it's a synthetic marker, so the worker runs `prompt` but history
        doesn't store the self-directive as an owner message. None ⇒ record `prompt` (the
        owner-turn behavior — unchanged for every existing caller)."""
        turn_id = uuid4().hex
        # Stash the history owner-side (the marker for a proactive turn, else the prompt)
        # to pair with the reply for history (4.1); the worker still gets `prompt` below.
        self._current_prompt = record_owner_text if record_owner_text is not None else prompt
        self._current_turn_id = turn_id
        # Story 10.2: an owner chat turn passes record_owner_text=None; a proactive/dream turn
        # passes a synthetic marker (non-None). Only owner-present turns may park a directive change.
        self._current_turn_is_owner = record_owner_text is None
        self.fence.open(turn_id)
        try:
            await self._push_face(FACE_THINKING)
        except Exception as exc:
            # A cosmetic face push must never abort the turn or tear down the core loop —
            # this runs on the always-reached catch-up path too (Story 5.0), so an
            # unguarded bus failure here would propagate out of run() and kill core.
            log.warning("turn %s face push failed (%s); continuing the turn", turn_id, exc)
        await self._push_caption("…")  # working — the real reply replaces this on _handle_result
        try:
            await self.spawner.spawn_turn(turn_id, prompt)
        except Exception as exc:
            # The turn never actually started — a real spawn failure (e.g. os.fork()
            # ENOMEM/EAGAIN, surfaced as a RuntimeError). Release BOTH guards, otherwise
            # the fence stays open and the arbiter slot stays reserved forever, silently
            # coalescing every later message into a pending slot that never flushes. The
            # dropped catch-up prompt is accepted degraded behavior (redelivery is Epic 2).
            # (The old WorkerBusyError catch-up race is now closed: turn-end awaits the
            # reap before releasing the arbiter — see _await_reap.)
            log.warning("spawn_turn failed for %s (%s); releasing turn guards", turn_id, exc)
            self.fence.close(turn_id)
            self.arbiter.reset()
            return
        self._reap_task = asyncio.create_task(self.spawner.reap_current())
        self._track(self._reap_task)
        self._arm_timeout(turn_id)

    async def _handle_propose_tool(self, op: ProposeTool, turn_id: str) -> None:
        """A self-coded-tool proposal (Story 9.4, AC1/AC2): stage the module + its test, run the
        bounded gate (pytest + AST import-check), and either ask the owner to add it (PASS) or
        discard it with a brief note (FAIL). Fully fail-soft — a bad/oversized proposal or a gate
        error never crashes the turn loop (the worker fork already finished its Result)."""
        ws = self.workspace_root
        try:
            module_path, _test_path = selfcode.stage(op.name, op.code, op.test, workspace_root=ws)
        except Exception as exc:
            log.warning("propose_tool %r: staging failed (%s)", op.name, exc)
            await self._send_reply(f"I tried to write a tool `{op.name}` but couldn't stage it ({exc}).")
            return
        stem = module_path.stem
        try:
            passed, output = await selfcode.run_gate(stem, workspace_root=ws)
        except Exception as exc:  # defensive — run_gate is self-guarded, but never let the loop die
            log.warning("propose_tool %r: gate raised (%s); discarding", stem, exc)
            selfcode.discard(stem, workspace_root=ws)
            await self._send_reply(f"I wrote a tool `{stem}` but its check errored out, so I tossed it.")
            return
        if not passed:
            selfcode.discard(stem, workspace_root=ws)
            await self._send_reply(
                f"I wrote a tool `{stem}` but it failed its check, so I tossed it.\n{_brief(output)}"
            )
            return
        self.history.park_promotion(turn_id, stem, datetime.now(UTC))
        await self._send_reply(
            f"I wrote a tool `{stem}` and it passed its test — add it?", approval_turn_id=turn_id
        )

    def _handle_tool_failures(self, names: tuple[str, ...]) -> None:
        """Strike each self-coded tool that failed this turn (Story 9.5, AC1) and quarantine one
        that crosses the threshold — core is the sole writer of the tool dirs (AD-5). Fail-soft:
        a sqlite/file error logs and is skipped, never crashes the turn loop (best-effort, like
        `_record_turn`/`_apply_proposed_ops`). A quarantined tool stops the next fork discovering it."""
        for name in names:
            try:
                strikes = self.history.record_tool_failure(name, datetime.now(UTC))
                if strikes >= _QUARANTINE_STRIKE_THRESHOLD:
                    selfcode.quarantine(name, workspace_root=self.workspace_root)
            except Exception as exc:
                log.warning("tool-health update for %r failed (%s); skipping", name, exc)

    async def _handle_approval_decision(self, approval_turn_id: str, approved: bool) -> None:
        """Owner tapped Approve/Deny. TWO parked-kinds share the turn-id keyspace: a Story 9.4
        tool PROMOTION (a file move + reply, no worker/slot needed) and a Story 9.3 RISKY-call
        approval (a resume turn). Check the promotion FIRST — it needs no ≤1 slot, so it must run
        before the `arbiter.is_idle` guard below. An expired/unknown decision is dropped (AC3/AC4).

        9.3 admission: a resume IS a turn (uses the ≤1 fence/arbiter/reap), so only proceed when
        idle — if a turn is in flight, leave the approval parked and ask the owner to tap again
        (single-owner, rare). A consumed approval that's expired/unknown is dropped with a note."""
        # Story 9.4: a parked tool promotion — promote (approve) or discard (deny), no slot needed.
        tool_name = self.history.take_promotion(approval_turn_id, datetime.now(UTC))
        if tool_name is not None:
            if approved:
                ok = selfcode.promote(tool_name, workspace_root=self.workspace_root)
                if ok:
                    await self._send_reply(f"`{tool_name}` is live.")
                else:
                    # A failed move must not orphan the staged pair on disk (review fix) — discard it.
                    selfcode.discard(tool_name, workspace_root=self.workspace_root)
                    await self._send_reply(f"I couldn't add `{tool_name}`, sorry — it didn't promote.")
            else:
                selfcode.discard(tool_name, workspace_root=self.workspace_root)
                await self._send_reply(f"Okay, discarded `{tool_name}`.")
            return
        if not self.arbiter.is_idle:
            await self._send_reply("I'm mid-thought — tap that again in a moment.")
            return
        blob = self.history.take_approval(approval_turn_id, datetime.now(UTC))
        if blob is None:  # expired or unknown — NEVER executes (AC4)
            await self._send_reply("That approval expired or is no longer pending.")
            return
        try:
            messages, call = msgspec.msgpack.decode(
                blob, type=tuple[tuple[Message, ...], ToolCall]
            )
        except msgspec.DecodeError as exc:
            log.warning("approval %s: corrupt parked state (%s); dropping", approval_turn_id, exc)
            await self._send_reply("Sorry — I couldn't restore that pending action.")
            return
        # Story 10.2: a parked DIRECTIVE change applies in CORE on approve — no worker resume (the
        # worker can't write memory, AD-5). Handled BEFORE the arbiter.submit below so it reserves
        # no slot (it starts no turn). Core is the sole writer; the owner is the sole authority.
        if call.name == "rewrite_directive":
            if approved:
                try:
                    self.memory._apply_rewrite_directive(call.args["content"])
                    await self._send_reply("Done — I updated your directive.")
                except Exception as exc:
                    log.warning("directive rewrite apply failed (%s); leaving prior", exc)
                    await self._send_reply("I couldn't apply that directive change, sorry.")
            else:
                await self._send_reply("Okay, I left your directive as-is.")
            return
        self.arbiter.submit("[tool approval]")  # idle → reserves the ≤1 slot (returns the marker)
        await self._start_resume_turn(messages, call, approved)

    async def _start_resume_turn(self, messages, call, approved: bool) -> None:
        """Spawn a FRESH worker that resumes a paused RISKY turn from the restored state
        (Story 9.3). Mirrors `_start_turn` but calls `spawner.spawn_resume` — a NEW fork (AD-3),
        no prompt assembly. A synthetic owner marker pairs the final reply for history."""
        turn_id = uuid4().hex
        self._current_prompt = "[resumed after tool approval]"
        self._current_turn_id = turn_id
        # A resume follows the owner tapping Approve — owner-present (Story 10.2).
        self._current_turn_is_owner = True
        self.fence.open(turn_id)
        try:
            await self._push_face(FACE_THINKING)
        except Exception as exc:
            log.warning("resume turn %s face push failed (%s); continuing", turn_id, exc)
        await self._push_caption("…")  # working — the real reply replaces this on _handle_result
        try:
            await self.spawner.spawn_resume(turn_id, messages, call, approved)
        except Exception as exc:
            log.warning("spawn_resume failed for %s (%s); releasing turn guards", turn_id, exc)
            self.fence.close(turn_id)
            self.arbiter.reset()
            return
        self._reap_task = asyncio.create_task(self.spawner.reap_current())
        self._track(self._reap_task)
        self._arm_timeout(turn_id)

    async def _handle_result(self, env: Envelope) -> None:
        """Admit a Result only for the open turn (AD-12); reply+react or degrade,
        then drive at most one coalesced catch-up turn."""
        if not self.fence.accept(env):
            return  # late / zombie / superseded — discard (AD-12)
        self._disarm_timeout()  # synchronous, before any await — can't race the timeout
        self.fence.close(env.turn_id)
        result: Result = env.body
        try:
            # Only the bus delivery is guarded here — a transport failure must not skip the
            # slot release below (Story 5.0 AC1). The narrow scope keeps the broad except
            # from masking a bug in the self-guarded ops/record helpers that follow.
            if result.ok:
                # Story 9.3: if the worker paused on a RISKY call, its reply is an approval
                # request — tag the outbound with the turn_id so the transport renders the
                # Approve/Deny surface (and the tap echoes the same id the state is parked under).
                approval = next(
                    (o for o in result.proposed_ops if isinstance(o, RequestToolApproval)), None
                )
                # Story 10.2: a proposed directive change also needs the Approve/Deny surface — but
                # ONLY on an owner-present turn (a dream proposal is dropped, AC6, never surfaced).
                directive = next(
                    (o for o in result.proposed_ops if isinstance(o, RewriteDirective)), None
                )
                needs_approval = approval is not None or (
                    directive is not None and self._current_turn_is_owner
                )
                if needs_approval:
                    await self._send_reply(result.payload, approval_turn_id=env.turn_id)
                else:
                    await self._send_reply(result.payload)  # unchanged plain-reply path
                # The reaction face (B.3): the expression the model picked for THIS message if
                # it's a valid one, else the default reply face. A deliberate reaction, not mood.
                await self._push_face(self._reaction_face(result.face))
                # The screen thought (B.3): the model's distilled THOUGHT line if it wrote one,
                # else a truncation of the reply. Held on the strip so it lingers, not flashes.
                blurb = result.blurb.strip()
                caption = _caption_for(blurb) if blurb else _caption_for(result.payload)
                await self._push_caption(caption, dwell=_REACTION_DWELL_S)
            else:
                await self._degrade()
        except Exception as exc:
            log.warning("turn %s delivery failed (%s); releasing the slot anyway", env.turn_id, exc)
        if result.ok:
            # Apply ops + record AFTER the reply (AC2), OUTSIDE the delivery try: both are
            # self-guarded (never raise), and the pet should still record + apply what it
            # learned even if the reply failed to reach the owner.
            self._apply_proposed_ops(result.proposed_ops)
            self._record_turn(result.payload)
            # Story 9.4: a self-coded-tool proposal is handled here (not in the SYNC
            # _apply_proposed_ops) because the gate is an async subprocess. It rides the worker
            # turn that's already done (the fork emitted its Result) — off the turn-critical path
            # in that sense; it stages + gates + (on pass) parks a promotion + asks the owner.
            # Awaited inline: single-owner, and the gate is bounded (a timeout is a fail), so a
            # brief wait before the slot frees below is acceptable and keeps the flow deterministic.
            # Story 9.5: search WITHIN the MAX_PROPOSED_OPS-capped slice (the same bound
            # `_apply_proposed_ops` applies) so a ProposeTool buried past the cap can't sneak the
            # gate; warn if a turn proposes more than one tool (only the first is handled — by design).
            capped = result.proposed_ops[:MAX_PROPOSED_OPS]
            proposes = [o for o in capped if isinstance(o, ProposeTool)]
            if len(proposes) > 1:
                log.warning("turn proposed %d tools; handling only the first", len(proposes))
            if proposes:
                await self._handle_propose_tool(proposes[0], env.turn_id)
        # Story 9.5: strike + maybe-quarantine any self-coded tool that failed this turn — runs
        # whether or not the turn ultimately succeeded (a tool that crashed is worth a strike).
        if result.tool_failures:
            self._handle_tool_failures(result.tool_failures)
        await self._await_reap()  # release the fork-server guard BEFORE the arbiter slot (AC1)
        folded = self.arbiter.complete()  # ALWAYS runs — guaranteed slot release
        if result.ok:
            # Publish the "answered" broadcast event (Story 7.2) AFTER the slot is released
            # (review Decision 1): _emit_event awaits bus.deliver->drain(), which can SUSPEND
            # under a backpressured/wedged plugin-host. Before arbiter.complete() that suspend
            # would hold the turn slot forever; after it the slot is already free, and the
            # event is best-effort + order-independent. (Bounding core->consumer drain
            # backpressure for ALL emit helpers is a separate, pre-existing concern — iceboxed.)
            await self._emit_event(EventKind.MESSAGE_ANSWERED)
        if folded is not None:
            await self._start_turn(folded)

    # --- turn timeout (AC3 "rather than hanging") ---

    def _arm_timeout(self, turn_id: str) -> None:
        self._timeout_task = asyncio.create_task(self._timeout_watch(turn_id))

    def _disarm_timeout(self) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None

    async def _timeout_watch(self, turn_id: str) -> None:
        """Fire if no Result is accepted in time: close the turn (so a late Result
        is then discarded by the fence — AD-12), degrade, and maybe start the
        coalesced next turn. Cancelled (disarmed) the moment a Result lands."""
        try:
            await asyncio.sleep(self.turn_timeout)
        except asyncio.CancelledError:
            return
        if self.fence.current != turn_id:
            return  # already closed/superseded by the Result path
        self.fence.close(turn_id)
        self._timeout_task = None
        try:
            await self._degrade()
        except Exception as exc:
            # Same guarantee as _handle_result: even if the degrade reply fails to send,
            # the slot MUST release so the next turn can proceed (Story 5.0 AC1).
            log.warning("turn %s degrade-on-timeout failed (%s); releasing the slot anyway", turn_id, exc)
        await self._await_reap()  # reclaim the wedged worker BEFORE freeing the arbiter (AC1)
        folded = self.arbiter.complete()  # ALWAYS runs — guaranteed slot release
        if folded is not None:
            await self._start_turn(folded)

    async def _await_reap(self) -> None:
        """Await the in-flight worker's reap so the fork-server guard is released BEFORE the
        arbiter slot frees (Story 5.0 AC1) — the two ≤1 guards then release in lockstep, so
        a catch-up turn can never hit a freed arbiter while the fork is still held
        (WorkerBusyError). The reap is bounded (SIGKILL at `_REAP_TIMEOUT_S`), so this can't
        block the loop indefinitely; a worker that emitted a Result exits at once, so the
        common case returns immediately."""
        task = self._reap_task
        self._reap_task = None
        if task is not None:
            try:
                await task
            except Exception as exc:  # reap_current self-guards + releases in a finally
                log.warning("reap await failed (%s); the guard is released by reap's finally", exc)

    # --- emit helpers (core ORIGINATES traffic via bus.deliver) ---

    async def _send_reply(self, text: str, *, approval_turn_id: str | None = None) -> None:
        log.info("reply → owner (%d chars%s)", len(text), ", approval" if approval_turn_id else "")
        await self.bus.deliver(
            Envelope(
                id=uuid4().hex,
                kind=MsgKind.OUTBOUND_MSG,
                src=Actor.CORE,
                dst=Actor.CHAT_TRANSPORT,
                body=OutboundMessage(text=text, approval_turn_id=approval_turn_id),
            )
        )

    async def _emit_event(self, kind: EventKind) -> None:
        """Publish a broadcast pet-lifecycle event (AD-11 mode 2, Story 7.2): the hub fans
        it out to the plugin-host, which dispatches to subscribed plugins. Best-effort — a
        publish failure must NEVER break the turn loop or the slot release (same discipline
        as `_record_turn`). Core stays plugin-agnostic: it emits a closed `EventKind`,
        knowing nothing about who (if anyone) subscribes. `dst=None` = broadcast."""
        try:
            await self.bus.deliver(
                Envelope(
                    id=uuid4().hex,
                    kind=MsgKind.EVENT,
                    src=Actor.CORE,
                    dst=None,
                    body=Event(event=kind),
                )
            )
        except Exception as exc:
            log.warning("event %s publish failed (%s); turn unaffected", kind.value, exc)

    async def _push_face(self, face: str) -> None:
        self._last_face = face  # track what's on screen (lifecycle AND mood pushes)
        await self.bus.deliver(
            Envelope(
                id=uuid4().hex,
                kind=MsgKind.STATE_SNAPSHOT,
                src=Actor.CORE,
                dst=Actor.DISPLAY,
                body=StateSnapshot(region=Region.FACE, seq=self._next_seq(), face=face),
            )
        )

    def _reaction_face(self, face: str) -> str:
        """The model's chosen reaction expression if it's in the palette, else the default reply
        face (B.3). Defends the panel from arbitrary model text being drawn as a 'face'. Pure."""
        token = face.strip().lower()
        return token if token in _REACTION_FACES else FACE_REPLY

    async def _push_caption(self, text: str, *, dwell: float = 0.0) -> None:
        """Push the bottom-strip caption (Region.CAPTION, B.3) — the short 'what I'm doing /
        feeling / just said' line that rides alongside the face. INTERNALLY guarded: it is
        purely cosmetic, so a bus hiccup here must never abort a turn, which lets every call
        site stay a one-liner (unlike `_push_face`, whose callers guard it). An identical
        caption is skipped to avoid re-flashing the panel.

        `dwell` > 0 marks this a REACTION (a reply/dream): the screen (face + this text) holds
        against the at-rest mood for that many seconds (set even when the text is unchanged, so
        a repeated reply still extends the hold)."""
        if dwell > 0:
            self._reaction_hold_until = self._monotonic() + dwell
        if text == self._last_caption:
            return
        self._last_caption = text
        try:
            await self.bus.deliver(
                Envelope(
                    id=uuid4().hex,
                    kind=MsgKind.STATE_SNAPSHOT,
                    src=Actor.CORE,
                    dst=Actor.DISPLAY,
                    body=StateSnapshot(region=Region.CAPTION, seq=self._next_seq(), face=text),
                )
            )
        except Exception as exc:
            log.warning("caption push failed (%s); turn unaffected", exc)

    async def _degrade(self) -> None:
        """Graceful 'can't think right now' (AC3): a reply + an error face. Called on
        a failure Result AND on turn timeout."""
        await self._send_reply(DEGRADE_TEXT)
        await self._push_face(FACE_DEGRADED)
        await self._push_caption(_caption_for(DEGRADE_TEXT), dwell=_REACTION_DWELL_S)
        self._record_turn(DEGRADE_TEXT)  # the degrade ack IS the pet's reply this turn

    def _record_turn(self, pet_text: str) -> None:
        """Record the in-flight turn's (owner, pet) pair to history (AD-6). Skipped
        if no turn is in flight (e.g. a spawn that never produced a reply)."""
        if self._current_prompt is None:
            return
        try:
            self.history.record_turn(self._current_turn_id, self._current_prompt, pet_text, datetime.now(UTC))
        except Exception as exc:
            # History is best-effort bookkeeping — the reply is already delivered.
            # A sqlite failure (locked/disk-full) must NOT take down the turn loop.
            log.warning("history record failed (%s); reply already delivered", exc)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # --- the scheduler loop + the reflex/checkpoint jobs (Story 5.1; AD-14) ---

    async def _scheduler_loop(self) -> None:
        """The single resident scheduler task (AC1): tick the named jobs on the base
        cadence. ONE task in its own slot — NEVER `_bg` (a permanent resident there
        breaks the 1.9 soak's '_bg drains to 0'); parkable via `scheduler_interval` so
        the soak can push it out of its measurement window (the background-emitter rule,
        see test_endurance_soak). Per-job guards live in `Scheduler.tick`. Cancelled
        cleanly on teardown."""
        try:
            while True:
                await asyncio.sleep(self.scheduler_interval)
                try:
                    # Feed the idle signal so the proactive Idle job (Story 5.4) can fire:
                    # parsed from state.last_interaction, fail-soft (None when no/unusable
                    # signal -> the Idle cadence simply doesn't fire this tick).
                    await self.scheduler.tick(last_interaction=self._last_interaction_dt())
                except Exception as exc:
                    # Scheduler.tick guards each JOB; this guards the tick scaffolding
                    # itself (the clock read + due-set computation) so one bad tick can
                    # NEVER kill the resident scheduler task. CancelledError is a
                    # BaseException, so teardown still propagates past this guard.
                    log.warning("scheduler tick failed (%s); ticking on", exc)
        except asyncio.CancelledError:
            return

    def _last_interaction_dt(self) -> datetime | None:
        """Parse `state.last_interaction` (ISO-8601 UTC) into a tz-aware datetime for the
        scheduler's idle cadence, or None when there is no usable signal. Mirrors
        `reflexes._idle_seconds`' defensive parse: a None / unparseable / tz-naive value
        degrades to None (warned), never raises — so the Idle job just doesn't fire."""
        raw = self.state.state.last_interaction
        if raw is None:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError) as exc:
            log.warning("unusable last_interaction %r (%s); no idle trigger this tick", raw, exc)
            return None
        if dt.tzinfo is None:
            # A tz-naive stamp parses fine but can't be subtracted from the tz-aware `now`
            # in Idle.is_due (TypeError) — and that subtraction runs in due() BEFORE the
            # per-job guard, so it would silence the WHOLE tick. Reject it (mirrors the
            # reflexes._idle_seconds guard, which catches the same case at subtraction time).
            log.warning("tz-naive last_interaction %r; no idle trigger this tick", raw)
            return None
        return dt

    def _build_proactive_prompt(self) -> str:
        """Thin delegator to `TurnDispatcher.build_proactive_prompt` (Story 7.0 extract)."""
        return self._dispatcher.build_proactive_prompt()

    def _build_dream_prompt(self) -> str:
        """Thin delegator to `TurnDispatcher.build_dream_prompt` (Story 7.0 extract)."""
        return self._dispatcher.build_dream_prompt()

    async def _run_prune_job(self) -> None:
        """Drop expired parked approvals (Story 9.3) + promotions (Story 9.4) — REFLEX-tier
        housekeeping (the `prune_expired_*` methods finally get a call site, Story 9.5). The
        scheduler guards it: a transient sqlite error logs + retries next tick."""
        now = datetime.now(UTC)
        self.history.prune_expired_approvals(now)  # state_blob is in-DB — pruning the row fully cleans it
        # An expired promotion leaves its staged files on disk (Story 9.5 review): discard each
        # pruned tool's staged pair so tools-staging/ doesn't leak (core is the sole writer, AD-5).
        for stem in self.history.prune_expired_promotions(now):
            selfcode.discard(stem, workspace_root=self.workspace_root)

    async def _run_checkpoint_job(self) -> None:
        """The periodic checkpoint flush (Story 3.1, NFR7) as a reflex-tier job — the
        seam the 3.1 comment said Epic 5's scheduler subsumes. Flushes only if dirty
        (periodic, not per change). The scheduler guards it: a transient disk error
        logs and retries next tick (state stays dirty)."""
        self._checkpoint_if_dirty()

    async def _dispatch_turn_job(self, job: Job) -> None:
        """Thin delegator to `TurnDispatcher.dispatch_turn_job` (Story 7.0 extract)."""
        await self._dispatcher.dispatch_turn_job(job)

    def _resolve_job_prompt(self, job: Job) -> str | None:
        """Thin delegator to `TurnDispatcher.resolve_job_prompt` (Story 7.0 extract)."""
        return self._dispatcher.resolve_job_prompt(job)

    def _checkpoint_if_dirty(self) -> None:
        if self.state.dirty:
            self.state.checkpoint(self.checkpoint_path)

    # --- resident reflexes (Story 3.2; AD-5/AD-14, CAP-2) ---

    async def _run_reflex_job(self) -> None:
        """The Epic 3 reflex drift as a reflex-tier scheduler job (AC2) — no LLM, no
        network (it touches only `self.state`). Computes a sparse patch via the pure
        reflex policy, applies it through the single-writer `apply_patch` (a no-op tick
        is skipped), then pushes the mood face between turns. Behavior is unchanged: it
        moved off the standalone `_reflex_loop`, it did not change. The scheduler guards
        it (one bad tick logs + keeps ticking)."""
        self._reflex_tick()
        await self._maybe_push_mood_face()

    def _reflex_tick(self) -> None:
        patch = compute_reflex_patch(self.state.state, datetime.now(UTC))
        if patch:
            self.state.apply_patch(patch)

    async def _maybe_push_mood_face(self) -> None:
        """Push the mood-derived face token BETWEEN turns only (Story 3.3). While a
        turn is in flight the lifecycle face (thinking/reply/cant-think) owns the
        screen, so skip — the reflex still mutated state (3.2). Push only on a token
        change to avoid spamming identical snapshots."""
        if not (self.fence.is_idle and self.arbiter.is_idle):
            return
        # A reaction (the model's face + thought) holds the screen against mood drift until its
        # dwell elapses, so the deliberate expression + thought linger together, then settle to
        # the ambient mood as one (B.3). Both pushes dedup, so a settled screen stays quiet.
        if self._monotonic() < self._reaction_hold_until:
            return
        m = self.state.state
        token = self.faces.select(m.mood.valence, m.mood.arousal, m.energy)
        if token != self._last_face:
            await self._push_face(token)
        await self._push_caption(token)

    # --- plugin-affect nudges (Story 7.5; AD-5/AD-1) ---

    async def _handle_nudge(self, kind: EventKind) -> None:
        """Apply a plugin-emitted affect nudge to mood — reflex-tier: no arbiter, no fork,
        no LLM, no budget (a state nudge, like `_run_reflex_job`). The pure `reactions` map
        owns the magnitude (core, not the plugin, decides how far the soul moves); a non-affect
        kind core happens to see (e.g. its own MESSAGE_ANSWERED) maps to nothing and is ignored.
        A per-kind cooldown debounces a flood; the patch goes through the single-writer
        `apply_patch` and the mood face re-renders between turns (the existing 3.3 guard). It
        deliberately does NOT touch `last_interaction` — a nudge moves mood, not the idle clock."""
        m = self.state.state
        patch = compute_nudge_patch(kind, m.mood.valence, m.mood.arousal)
        if not patch:
            return
        now = self._monotonic()
        last = self._last_nudge.get(kind)
        if last is not None and now - last < self.nudge_cooldown:
            return  # same affect kind within the cooldown — debounced
        self._last_nudge[kind] = now
        self.state.apply_patch(patch)
        await self._maybe_push_mood_face()

    def apply_add_face(self, name: str, **kwargs) -> None:
        """Validate and apply a face addition to the registry (atomic, comment-
        preserving). The single-writer apply path (AD-5); Story 3.4 wires the LLM
        proposal to call this. Raises ValueError on an invalid/duplicate face."""
        self.faces.add_face(name, **kwargs)

    def apply_memory_op(self, op) -> None:
        """Validate and apply one proposed memory-op to the curated tree (sole writer,
        AD-5). Story 4.5's thin wire passthrough to the 4.2 apply path. Raises on an
        invalid op (the caller guards — a bad op never crashes the turn)."""
        self.memory.apply_memory_op(op)

    def _apply_proposed_ops(self, ops: list) -> None:
        """Apply the worker's proposed ops, guarded (AC2). The count is capped (overflow
        dropped with a warning — never silently truncated); each op is applied in a
        try/except so one invalid op is logged+skipped, never raised into the turn loop
        and never affecting the already-delivered reply (best-effort, like _record_turn)."""
        if len(ops) > MAX_PROPOSED_OPS:
            log.warning(
                "dropping %d proposed op(s) over the cap of %d", len(ops) - MAX_PROPOSED_OPS, MAX_PROPOSED_OPS
            )
            ops = ops[:MAX_PROPOSED_OPS]
        # Story 10.2 review fix: every approval-parking op (RequestToolApproval, RewriteDirective)
        # parks under the SAME key (`self._current_turn_id`); `park_approval` is INSERT-OR-REPLACE,
        # so a SECOND parking op in one Result would silently clobber the first (owner then approves
        # the wrong thing). Park only the FIRST; log + skip any extras (mirrors the single-tool guard).
        parked_approval = False
        for op in ops:
            try:
                if isinstance(op, ProposeTool):
                    # Story 9.4: handled async in _handle_result (the gate is a subprocess) — it
                    # is NOT a synchronous single-writer apply, so skip it here (never route it to
                    # apply_memory_op, which would reject it as a non-memory op).
                    continue
                if isinstance(op, AddFace):
                    # The face op (Story 3.4) goes to the registry writer, not the memory tree.
                    self.apply_add_face(
                        op.name,
                        valence=op.valence,
                        arousal=op.arousal,
                        energy=op.energy,
                        token=op.token,
                        replace=op.replace,
                    )
                elif isinstance(op, CaptureLearning):
                    # The learnings op (Story 6.1) goes to the sqlite learnings table, NOT the
                    # markdown tree — raw capture, deduped by pattern_key, no extra LLM call.
                    self.history.capture_learning(op.observation, op.pattern_key, datetime.now(UTC))
                elif isinstance(op, ResolveLearning):
                    # The dream's learning-resolution (Story 6.2) — a soft sqlite status
                    # transition (promoted/pruned); a stale/unknown id is a safe no-op.
                    self.history.resolve_learning(op.id, op.status)
                elif isinstance(op, RequestToolApproval):
                    # Story 9.3: the worker paused on a RISKY call. Park the resumable state
                    # (messages + the pending call) in sqlite keyed by this turn's id; the
                    # owner's tap (echoing the id) resumes a fresh worker. msgpack of a 2-tuple.
                    if parked_approval:
                        log.warning("a second approval-parking op in one turn; skipping (would clobber the first)")
                        continue
                    if len(op.messages) > _APPROVAL_MESSAGES_WARN:
                        # A chained-approval turn shouldn't grow unbounded; warn (hard cap = 9.5).
                        log.warning("parked approval %s carries %d messages (large)",
                                    self._current_turn_id, len(op.messages))
                    blob = msgspec.msgpack.encode((op.messages, op.call))
                    self.history.park_approval(self._current_turn_id, blob, datetime.now(UTC))
                    parked_approval = True
                elif isinstance(op, RewriteDirective):
                    # Story 10.2: a proposed change to the owner's constitution. NEVER applied
                    # autonomously (not a MemoryOp). On an unattended turn it is DROPPED (AC6 — no
                    # drift while the owner's away). On an owner turn, park it through the SAME 9.3
                    # plumbing (encode as a (messages=(), call) blob with a rewrite_directive ToolCall),
                    # so the owner's Approve/Deny tap resumes into the core-apply branch in
                    # _handle_approval_decision. The approval-tagged reply is sent in _handle_result.
                    if not self._current_turn_is_owner:
                        log.info("dropping rewrite_directive proposed on an unattended turn (AC6)")
                        continue
                    if not op.content.strip():
                        log.warning("rewrite_directive: empty content; dropping")
                        continue
                    if parked_approval:
                        log.warning("a second approval-parking op in one turn; skipping rewrite_directive (would clobber the first)")
                        continue
                    call = ToolCall(id=self._current_turn_id, name="rewrite_directive",
                                    args={"content": op.content})
                    blob = msgspec.msgpack.encode(((), call))
                    self.history.park_approval(self._current_turn_id, blob, datetime.now(UTC))
                    parked_approval = True
                else:
                    self.apply_memory_op(op)
            except Exception as exc:
                log.warning("rejected proposed op %s (%s)", type(op).__name__, exc)

    def _mark_interaction(self) -> None:
        """Record 'now' as the last interaction (the idle signal the reflex reads),
        through the single-writer state API (AD-5). Called when an owner message
        arrives — the only new state write on the turn path."""
        self.state.apply_patch({"last_interaction": datetime.now(UTC).isoformat()})

    # --- background task bookkeeping ---

    def _track(self, task: asyncio.Task) -> None:
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _cleanup(self) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        self._reap_task = None  # the reap task lived in _bg (cancelled above)
        # Best-effort durable flush on graceful shutdown (the checkpoint is atomic, so
        # no partial-write risk). Never let a teardown-time write escalate an error.
        try:
            self._checkpoint_if_dirty()
        except Exception as exc:  # pragma: no cover - defensive teardown guard
            log.warning("checkpoint on shutdown failed (%s); state left at last good", exc)
        try:
            self.history.close()
        except Exception as exc:  # pragma: no cover - defensive teardown guard
            log.warning("history close failed (%s)", exc)
