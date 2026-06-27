"""core/dispatch — the scheduler-turn dispatch DRIVER (AD-9/AD-14/AD-1).

The arbiter-gated admission seam for scheduler-proposed turn jobs, extracted from
`core/runtime.py` (Epic 6 retro action #3 / Story 7.0). `core/budget.py` is the pure
POLICY half (the daily turn-count budget + cooldown); this is the DRIVER half that
applies the spend patch, admits through the arbiter, and starts the turn — the same
policy/driver split as `core/reflexes.py`.

`TurnDispatcher` is constructed by `Core` with its shared collaborators injected
(arbiter, budget gate, persistent state, faces, history) plus a `start_turn` callback
into the turn lifecycle that STAYS on `Core`. The dispatch logic is behavior-identical
to its old home on `Core`; `Core` keeps thin delegators so existing callers are unchanged.

LLM-free (AD-1): like the rest of `core/`, this imports no provider lib.
"""

import logging
from datetime import UTC, datetime

from shelldon.core.budget import Decision
from shelldon.core.proactive import build_dream_prompt, build_proactive_prompt
from shelldon.core.scheduler import Job

log = logging.getLogger("shelldon.core.dispatch")


class TurnDispatcher:
    """Drives a due TURN-tier scheduler job through the arbiter + cooldown + budget gate
    and into the normal turn lifecycle. Holds no state of its own — every collaborator is
    injected by `Core` at composition (matching the `Scheduler(now=…, dispatch_turn=…)`
    injection style). `start_turn` is a callback into `Core._start_turn` (the lifecycle
    stays on `Core`)."""

    def __init__(self, *, arbiter, budget, state, faces, history, memory, start_turn):
        self.arbiter = arbiter
        self._budget = budget
        self.state = state
        self.faces = faces
        self.history = history
        #: The curated memory tree — read (never written) here to load the self-initiated-turn
        #: prompt templates (`HEARTBEAT.md`/`DREAM.md`, Story 10.3) at dispatch. The driver already
        #: reads state/history; reading memory is the same role (no provider lib — AD-1 holds).
        self.memory = memory
        #: Callback into the turn lifecycle on `Core` — injected so the dispatcher can
        #: start a turn without owning (or importing) the lifecycle it gates.
        self._start_turn = start_turn

    def build_proactive_prompt(self) -> str:
        """Build the proactive turn's prompt from live personality state (Story 5.4) — the
        Idle job's `prompt_builder`, resolved at dispatch. The feeling word reuses the face
        vocabulary (`faces.select`) as the single mood-label source, so the pet's musing is
        tinted by how it currently feels. Pure-ish: reads state, no await, never blocks the
        admit critical section."""
        m = self.state.state
        feeling = self.faces.select(m.mood.valence, m.mood.arousal, m.energy)
        return build_proactive_prompt(feeling, self.memory.read_heartbeat())

    def build_dream_prompt(self) -> str:
        """The dream Job's `prompt_builder` (Story 6.2), resolved at dispatch: read the pending
        learnings (core owns the store) and hand them to the pure `build_dream_prompt` policy
        (extracted to `core/proactive.py` — Epic 6 retro). Returns "" when nothing is pending →
        the dispatch skips (no dream, no spend). A sqlite read, no await (atomic in admit)."""
        pending = self.history.pending_learnings()
        return build_dream_prompt(
            [(r["id"], r["observation"], r["recurrence_count"]) for r in pending],
            self.memory.read_dream(),
        )

    async def dispatch_turn_job(self, job: Job) -> None:
        """The arbiter-gated dispatch seam (Story 5.2) for a due TURN-tier job (AD-9/AD-14).

        A scheduler turn is admitted ONLY when the slot is free AND the cooldown has
        elapsed AND the daily turn budget allows the job's cost; otherwise it is DEFERRED
        (re-proposed next cadence) or SKIPPED. On admission it records the spend through
        the single-writer `apply_patch` (AD-5), reserves the arbiter slot, and starts the
        turn via the normal lifecycle — AD-14: the scheduler never forks directly, and the
        turn rides the same 5.0-hardened path (release-safety: `submit` here is always
        balanced by `complete` on the Result/timeout, or `reset` on a spawn failure).

        A scheduler turn is NEVER coalesced into the owner's catch-up slot — only owner
        messages coalesce. If a turn is in flight, the job defers. (Concurrency note: the
        scheduler runs as a sibling task to the `run()` consumer, but the admit sequence
        below — is_idle check → apply_patch → submit — has NO `await`, so it is atomic
        w.r.t. the consumer; the arbiter's no-lock single-critical-section invariant holds.)"""
        text = self.resolve_job_prompt(job)
        if not text:
            return  # promptless / builder failed / empty -> skip (logged in the resolver)
        if not self.arbiter.is_idle:
            log.info("turn job %r deferred: a turn is already in flight", job.name)
            return
        now = datetime.now(UTC)
        decision = self._budget.evaluate(self.state.state.budget, now, cost=job.cost)
        if decision is Decision.DEFER:
            log.info("turn job %r deferred: within the turn cooldown", job.name)
            return
        if decision is Decision.SKIP:
            log.info("turn job %r skipped: daily turn budget (%d) exhausted", job.name, self._budget.daily_turn_budget)
            return
        # ADMIT. Reserve the arbiter slot first; the slot is free (checked above, no await
        # since), so submit returns the prompt. Guard the return defensively: a None would
        # mean the no-await invariant broke — don't spend budget or start a bogus turn.
        prompt = self.arbiter.submit(text)
        if prompt is None:  # unreachable while is_idle holds in this await-free section
            log.error("turn job %r: arbiter slot unexpectedly busy at admit; not spending budget", job.name)
            return
        # Record the spend BEFORE the spawn (a fork that then fails still counts —
        # conservative for credit protection), then start through the normal lifecycle. A
        # proactive turn (Story 5.4) records its synthetic owner-side marker, not the prompt.
        # The slot reserved by submit() above is released by _start_turn on a spawn failure
        # and by _handle_result/timeout otherwise — but if recording the spend itself raises,
        # nothing downstream runs to release it, so the slot would leak forever (every later
        # turn coalesces into a wedged slot). Release it before re-raising (Story 7.0 review #1).
        try:
            self.state.apply_patch(self._budget.admission_patch(self.state.state.budget, now, cost=job.cost))
        except Exception:
            self.arbiter.reset()
            raise
        log.info(
            "turn job %r admitted (cost=%d, %d/%d used today) — starting a self-driven turn",
            job.name, job.cost, self.state.state.budget.turns_used, self._budget.daily_turn_budget,
        )
        await self._start_turn(prompt, record_owner_text=job.history_owner_text)

    def resolve_job_prompt(self, job: Job) -> str | None:
        """The turn job's prompt: built live from state (Story 5.4 `prompt_builder`) or the
        static `prompt`. A builder that raises is guarded -> None (skip, never wedge — the
        promptless-skip guarantee). An empty/None resolved prompt also skips."""
        if job.prompt_builder is not None:
            try:
                built = job.prompt_builder()
                # `.strip()` INSIDE the try (Story 7.0 review #2): a non-str truthy return —
                # a misconfigured builder yielding a list/int — raises AttributeError here and
                # is caught → skip, instead of propagating unguarded and wedging the dispatch.
                # A whitespace-only return (truthy in Python) also normalizes to "" and skips.
                built = (built or "").strip()
            except Exception as exc:
                log.warning("turn job %r prompt builder failed (%s); skipping", job.name, exc)
                return None
            if not built:
                # An empty builder return is an INTENTIONAL skip signal (the dream builder
                # returns "" when no learnings are pending — the common 6h case), NOT a misconfig.
                # debug-level so a normal skip doesn't spam prod every cadence. (A static
                # promptless turn job below stays warning — that IS a misconfiguration.)
                log.debug("turn job %r prompt builder returned nothing; skipping this cadence", job.name)
                return None
            return built
        if job.prompt is None:
            log.warning("turn job %r has no prompt; skipping (a turn job must carry one)", job.name)
        return job.prompt
