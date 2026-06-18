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
from datetime import UTC, datetime
from uuid import uuid4

from shelldon.contracts import (
    Actor,
    Envelope,
    MsgKind,
    OutboundMessage,
    Region,
    Result,
    StateSnapshot,
)
from shelldon.core.arbiter import Arbiter
from shelldon.core.bus import BusServer
from shelldon.core.faces import DEFAULT_FACES_PATH, FaceRegistry
from shelldon.core.history import DEFAULT_HISTORY_PATH, HistoryStore
from shelldon.core.memory import DEFAULT_MEMORY_ROOT, CuratedMemory
from shelldon.core.reflexes import compute_reflex_patch
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
DEFAULT_TURN_TIMEOUT = 30.0

#: Default personality-state checkpoint cadence (Story 3.1). Periodic, NOT per change
#: (NFR7) — tests inject a small interval. Epic 5's scheduler will subsume this.
DEFAULT_CHECKPOINT_INTERVAL = 60.0

#: Default resident-reflex tick cadence (Story 3.2). A gentle in-core drift between
#: turns (no LLM) — tests inject a small interval. Epic 5's scheduler subsumes it.
DEFAULT_REFLEX_INTERVAL = 10.0

#: Cap on the memory-ops one turn may propose (Story 4.5). A runaway/abusive reply
#: can't flood the curated tree; the overflow is dropped with a warning (never silently
#: truncated). Generous — a normal turn proposes a handful.
MAX_PROPOSED_OPS = 16


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
        faces_path=None,
        history_path=None,
        memory_root=None,
    ):
        if checkpoint_interval <= 0:
            raise ValueError(f"checkpoint_interval must be positive, got {checkpoint_interval!r}")
        if reflex_interval <= 0:
            raise ValueError(f"reflex_interval must be positive, got {reflex_interval!r}")
        self.bus = BusServer(socket_path=socket_path)
        self.fence = TurnFence()
        self.arbiter = Arbiter()
        self.spawner = spawner
        self.turn_timeout = turn_timeout
        self.checkpoint_path = checkpoint_path if checkpoint_path is not None else DEFAULT_CHECKPOINT_PATH
        self.checkpoint_interval = checkpoint_interval
        self.reflex_interval = reflex_interval
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
        #: The periodic checkpoint flush AND the resident reflex tick are long-lived
        #: SINGLETON tasks, so each lives in its own slot (like `_timeout_task`), NOT
        #: in `_bg`. `_bg` holds transient per-turn reap tasks that drain to empty; a
        #: permanent resident there would break that "drains to 0" invariant (1.9 soak).
        self._checkpoint_task: asyncio.Task | None = None
        self._reflex_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Start the bus, wait for the spawner, then consume core_inbox forever.

        Single consumer: only this loop touches the arbiter/fence, so admission is
        serial (no `await` interleaves a second submit mid-decision). On
        cancellation (teardown) it cancels every background task it scheduled.
        """
        await self.bus.start()
        await self.spawner.ready()
        self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())
        self._reflex_task = asyncio.create_task(self._reflex_loop())
        try:
            while True:
                env = await self.bus.core_inbox.get()
                if env.kind is MsgKind.INBOUND_MSG:
                    self._mark_interaction()
                    prompt = self.arbiter.submit(env.body.text)
                    if prompt is not None:
                        await self._start_turn(prompt)
                elif env.kind is MsgKind.RESULT:
                    await self._handle_result(env)
                else:
                    log.warning("core ignoring unexpected inbox envelope %s (%s)", env.id, env.kind)
        finally:
            self._cleanup()

    async def _start_turn(self, prompt: str) -> None:
        """Open a fenced turn, push the 'thinking' face, spawn the worker, schedule
        its reap (fire-and-forget — the Result returns over the bus), arm the timeout."""
        turn_id = uuid4().hex
        self._current_prompt = prompt  # stash to pair with the reply for history (4.1)
        self._current_turn_id = turn_id
        self.fence.open(turn_id)
        await self._push_face(FACE_THINKING)
        try:
            await self.spawner.spawn_turn(turn_id, prompt)
        except Exception as exc:
            # The turn never actually started. Two real paths land here: an OS-level
            # spawn failure, and the timeout+catch-up race where the prior worker's
            # reap hasn't released ForkServer.worker_in_flight yet, so spawn_turn
            # raises WorkerBusyError. Release BOTH guards — otherwise the fence stays
            # open and the arbiter slot stays reserved forever, silently coalescing
            # every later message into a pending slot that never flushes. The dropped
            # catch-up prompt is accepted degraded behavior (redelivery is Epic 2).
            log.warning("spawn_turn failed for %s (%s); releasing turn guards", turn_id, exc)
            self.fence.close(turn_id)
            self.arbiter.reset()
            return
        self._track(asyncio.create_task(self.spawner.reap_current()))
        self._arm_timeout(turn_id)

    async def _handle_result(self, env: Envelope) -> None:
        """Admit a Result only for the open turn (AD-12); reply+react or degrade,
        then drive at most one coalesced catch-up turn."""
        if not self.fence.accept(env):
            return  # late / zombie / superseded — discard (AD-12)
        self._disarm_timeout()  # synchronous, before any await — can't race the timeout
        self.fence.close(env.turn_id)
        result: Result = env.body
        if result.ok:
            await self._send_reply(result.payload)
            await self._push_face(FACE_REPLY)
            # Apply the worker's proposed ops AFTER the reply is out (AC2): a bad/oversized
            # proposal must never block or alter the user-facing reply.
            self._apply_proposed_ops(result.proposed_ops)
            self._record_turn(result.payload)
        else:
            await self._degrade()
        folded = self.arbiter.complete()
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
        await self._degrade()
        folded = self.arbiter.complete()
        if folded is not None:
            await self._start_turn(folded)

    # --- emit helpers (core ORIGINATES traffic via bus.deliver) ---

    async def _send_reply(self, text: str) -> None:
        await self.bus.deliver(
            Envelope(
                id=uuid4().hex,
                kind=MsgKind.OUTBOUND_MSG,
                src=Actor.CORE,
                dst=Actor.CHAT_TRANSPORT,
                body=OutboundMessage(text=text),
            )
        )

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

    # --- personality-state periodic checkpoint (Story 3.1; NFR7) ---

    async def _checkpoint_loop(self) -> None:
        """Flush the personality state on a fixed interval, ONLY if dirty (periodic,
        not per change). A single in-core interval task — the seam Story 3.2's reflex
        tick and Epic 5's scheduler subsume later without changing checkpoint behavior.
        Cancelled cleanly on teardown."""
        try:
            while True:
                await asyncio.sleep(self.checkpoint_interval)
                try:
                    self._checkpoint_if_dirty()
                except Exception as exc:
                    # A transient disk error must NOT permanently kill periodic
                    # checkpointing. Log and keep going — state stays dirty, so the
                    # next interval retries the flush. (CancelledError is a
                    # BaseException, so teardown still propagates past this guard.)
                    log.warning("periodic checkpoint failed (%s); retrying next interval", exc)
        except asyncio.CancelledError:
            return

    def _checkpoint_if_dirty(self) -> None:
        if self.state.dirty:
            self.state.checkpoint(self.checkpoint_path)

    # --- resident reflexes (Story 3.2; AD-5/AD-14, CAP-2) ---

    async def _reflex_loop(self) -> None:
        """Drift the personality-state on a fixed in-core tick — no LLM, no network
        (it touches only `self.state`). Computes a sparse patch via the pure reflex
        policy and applies it through the single-writer `apply_patch`, skipping a
        no-op tick. A single in-core interval task — the seam Epic 5's scheduler
        subsumes as a cost-tier 'reflex job' without changing behavior. Cancelled
        cleanly on teardown."""
        try:
            while True:
                await asyncio.sleep(self.reflex_interval)
                try:
                    self._reflex_tick()
                    await self._maybe_push_mood_face()
                except Exception as exc:
                    # One bad tick must NOT permanently kill reflexes — log and keep
                    # ticking (the pet stays alive). Same hardening as the checkpoint
                    # loop. CancelledError is a BaseException, so teardown still exits.
                    log.warning("reflex tick failed (%s); retrying next interval", exc)
        except asyncio.CancelledError:
            return

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
                # Story 3.4 inserts a branch here: an AddFace op → self.apply_add_face(...).
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
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            self._checkpoint_task = None
        if self._reflex_task is not None:
            self._reflex_task.cancel()
            self._reflex_task = None
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
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
