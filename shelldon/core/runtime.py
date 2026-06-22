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
    Region,
    RequestToolApproval,
    Result,
    StateSnapshot,
    ToolCall,
)
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
from shelldon.core.scheduler import CostTier, Idle, Interval, Job, Scheduler
from shelldon.core.state import DEFAULT_CHECKPOINT_PATH, PersistentState
from shelldon.core.turn import TurnFence

log = logging.getLogger("shelldon.core.runtime")

#: Placeholder lifecycle face tokens — the real expression vocabulary and the
#: mood->face mapping are Story 3.3; the personality-state struct is Epic 3.
FACE_THINKING = "thinking"
FACE_REPLY = "happy"
FACE_DEGRADED = "cant-think"

#: Graceful "can't think right now" reply (AC3) — the chain-exhaustion reflex ack
#: (Story 2.3). The real resident reflex loop is Epic 3 / Story 3.2.
DEGRADE_TEXT = "…can't think right now…"

#: Default turn timeout (AC3 "rather than hanging"). Tests inject a small value.
#:
#: Coherent-timeout invariant (Story 5.0): this is the BINDING reap horizon (T) and is the
#: LARGEST of the chain W < R < T — the worker self-reports a failure Result (W,
#: `_COMPLETION_TIMEOUT_S`) and the fork-server SIGKILL-reclaims a wedged child (R,
#: `_REAP_TIMEOUT_S`) BEFORE core abandons the turn here. That keeps the arbiter slot and
#: the fork-server guard releasing in lockstep — no ~90s freeze from a worker holding the
#: fork past core's degrade. See `tests/test_resilience.py::test_timeout_chain_is_coherent`.
DEFAULT_TURN_TIMEOUT = 30.0

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

#: Default owner-idle threshold (seconds) before the pet speaks up on its own (Story 5.4;
#: owner decision = 1 hr, injectable/configurable). The proactive job is an Idle-cadence
#: turn job — it fires once per idle stretch, then stays quiet until the owner interacts
#: again. The 5.2 cooldown (30 min) + daily budget (12) still bound its frequency, and the
#: 5.3 battery backoff stretches this threshold on battery.
DEFAULT_PROACTIVE_IDLE_INTERVAL = 3600.0

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
        proactive_idle_interval: float = DEFAULT_PROACTIVE_IDLE_INTERVAL,
        dream_idle_interval: float = DEFAULT_DREAM_IDLE_INTERVAL,
        nudge_cooldown: float = DEFAULT_NUDGE_COOLDOWN,
        monotonic=None,
        power=None,
        faces_path=None,
        history_path=None,
        memory_root=None,
    ):
        if checkpoint_interval <= 0:
            raise ValueError(f"checkpoint_interval must be positive, got {checkpoint_interval!r}")
        if reflex_interval <= 0:
            raise ValueError(f"reflex_interval must be positive, got {reflex_interval!r}")
        if scheduler_interval <= 0:
            raise ValueError(f"scheduler_interval must be positive, got {scheduler_interval!r}")
        if proactive_idle_interval <= 0:
            raise ValueError(f"proactive_idle_interval must be positive, got {proactive_idle_interval!r}")
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
        self.proactive_idle_interval = proactive_idle_interval
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
        #: The owner prompt + turn_id of the in-flight turn (≤1, AD-9), stashed at
        #: turn start so a completed/degraded turn can be paired and recorded.
        self._current_prompt: str | None = None
        self._current_turn_id: str | None = None
        #: The face token currently on screen — both lifecycle and mood pushes update
        #: it, so a mood face re-pushes after a lifecycle face and identical mood ticks
        #: don't spam the display.
        self._last_face: str | None = None
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
        #: The proactive musing (Story 5.4, CAP-4) — the first self-initiated turn job. An
        #: Idle cadence fires it once per idle stretch after `proactive_idle_interval`s of
        #: owner silence; its prompt is BUILT at dispatch from live mood (no static text),
        #: and it records a synthetic owner-side marker (no real owner message). It rides the
        #: 5.2 cooldown/budget gate + the 5.3 battery gate unchanged (non-essential → eased
        #: off first on battery). The scheduler's idle signal is fed in `_scheduler_loop`.
        self.scheduler.register(
            Job(
                "proactive",
                Idle(self.proactive_idle_interval),
                CostTier.TURN,
                prompt_builder=self._dispatcher.build_proactive_prompt,
                history_owner_text=PROACTIVE_OWNER_MARKER,
            )
        )
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
        self.fence.open(turn_id)
        try:
            await self._push_face(FACE_THINKING)
        except Exception as exc:
            # A cosmetic face push must never abort the turn or tear down the core loop —
            # this runs on the always-reached catch-up path too (Story 5.0), so an
            # unguarded bus failure here would propagate out of run() and kill core.
            log.warning("turn %s face push failed (%s); continuing the turn", turn_id, exc)
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

    async def _handle_approval_decision(self, approval_turn_id: str, approved: bool) -> None:
        """Owner tapped Approve/Deny on a parked RISKY call (Story 9.3, AC3/AC4). Admission:
        a resume IS a turn (uses the ≤1 fence/arbiter/reap), so only proceed when idle — if a
        turn is in flight, leave the approval parked and ask the owner to tap again (single-
        owner, rare). A consumed approval that's expired/unknown is dropped with a note (AC4)."""
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
        self.arbiter.submit("[tool approval]")  # idle → reserves the ≤1 slot (returns the marker)
        await self._start_resume_turn(messages, call, approved)

    async def _start_resume_turn(self, messages, call, approved: bool) -> None:
        """Spawn a FRESH worker that resumes a paused RISKY turn from the restored state
        (Story 9.3). Mirrors `_start_turn` but calls `spawner.spawn_resume` — a NEW fork (AD-3),
        no prompt assembly. A synthetic owner marker pairs the final reply for history."""
        turn_id = uuid4().hex
        self._current_prompt = "[resumed after tool approval]"
        self._current_turn_id = turn_id
        self.fence.open(turn_id)
        try:
            await self._push_face(FACE_THINKING)
        except Exception as exc:
            log.warning("resume turn %s face push failed (%s); continuing", turn_id, exc)
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
                if approval is not None:
                    await self._send_reply(result.payload, approval_turn_id=env.turn_id)
                else:
                    await self._send_reply(result.payload)  # unchanged plain-reply path
                await self._push_face(FACE_REPLY)
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

    async def _degrade(self) -> None:
        """Graceful 'can't think right now' (AC3): a reply + an error face. Called on
        a failure Result AND on turn timeout."""
        await self._send_reply(DEGRADE_TEXT)
        await self._push_face(FACE_DEGRADED)
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
        m = self.state.state
        token = self.faces.select(m.mood.valence, m.mood.arousal, m.energy)
        if token != self._last_face:
            await self._push_face(token)

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
        for op in ops:
            try:
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
                    if len(op.messages) > _APPROVAL_MESSAGES_WARN:
                        # A chained-approval turn shouldn't grow unbounded; warn (hard cap = 9.5).
                        log.warning("parked approval %s carries %d messages (large)",
                                    self._current_turn_id, len(op.messages))
                    blob = msgspec.msgpack.encode((op.messages, op.call))
                    self.history.park_approval(self._current_turn_id, blob, datetime.now(UTC))
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
