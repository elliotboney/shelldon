"""Story 7.2 (AC4) — core emits a `message-answered` broadcast event when a turn is
successfully answered, and only then. Best-effort: an event-publish failure never
breaks the turn (reply still delivered, slot still released).

Reuses the fenced `_handle_result` harness from test_proposed_ops (open a turn, hand
core a Result), spying `bus.deliver` to capture what core publishes.
"""

import asyncio

from shelldon.contracts import Actor, Envelope, Event, EventKind, MsgKind, Result
from shelldon.core.runtime import Core
from shelldon.plugins.host import run_plugin_host
from shelldon.plugins.manifest import BasePlugin, PluginManifest


class _NoopSpawner:
    async def ready(self):  # pragma: no cover
        pass

    async def spawn_turn(self, turn_id, prompt):  # pragma: no cover
        pass


def _core(sock_path, tmp_path):
    return Core(sock_path, _NoopSpawner(), memory_root=tmp_path / "memory")


def _open_turn(core, turn_id, prompt="owner says hi"):
    core.arbiter.submit(prompt)
    core._current_prompt = prompt
    core._current_turn_id = turn_id
    core.fence.open(turn_id)


def _result_env(turn_id, *, ok=True, payload="ok"):
    return Envelope(
        id="r", kind=MsgKind.RESULT, src=Actor.WORKER, dst=Actor.CORE,
        body=Result(ok=ok, payload=payload), turn_id=turn_id,
    )


def _spy_deliver(core):
    delivered: list[Envelope] = []
    orig = core.bus.deliver

    async def deliver(env):
        delivered.append(env)
        await orig(env)

    core.bus.deliver = deliver
    return delivered


def _events(delivered):
    return [e for e in delivered if e.kind is MsgKind.EVENT]


async def test_successful_turn_emits_message_answered(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t1")
    delivered = _spy_deliver(core)

    await core._handle_result(_result_env("t1", ok=True))

    events = _events(delivered)
    assert len(events) == 1
    assert events[0].src is Actor.CORE
    assert events[0].dst is None  # broadcast
    assert isinstance(events[0].body, Event)
    assert events[0].body.event is EventKind.MESSAGE_ANSWERED
    assert core.fence.is_idle


async def test_degraded_turn_emits_no_event(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t2")
    delivered = _spy_deliver(core)

    await core._handle_result(_result_env("t2", ok=False, payload=""))

    assert _events(delivered) == []  # a degrade is not a real answer
    assert core.fence.is_idle


async def test_event_publish_failure_does_not_break_the_turn(sock_path, tmp_path):
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t3")

    replies: list[str] = []
    orig_reply = core._send_reply

    async def _rec_reply(text):
        replies.append(text)
        await orig_reply(text)

    core._send_reply = _rec_reply

    # Make ONLY the event publish fail; the reply delivery must be unaffected.
    orig_deliver = core.bus.deliver

    async def _deliver(env):
        if env.kind is MsgKind.EVENT:
            raise OSError("event sink down")
        await orig_deliver(env)

    core.bus.deliver = _deliver

    await core._handle_result(_result_env("t3", ok=True, payload="hi"))

    assert replies == ["hi"]  # reply still delivered
    assert core.fence.is_idle  # turn closed, slot released despite the event failure


# --- AC5: end-to-end CAP-7 proof (core -> hub broadcast -> host -> subscriber) ---


class _Recorder(BasePlugin):
    def __init__(self, name, subscribes):
        super().__init__(PluginManifest(name=name, subscribes=subscribes))
        self.got: list[EventKind] = []

    async def on_event(self, event: Event) -> None:
        self.got.append(event.event)


async def _poll(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_slot_is_released_even_if_the_event_emit_suspends(sock_path, tmp_path):
    # Review Decision 1: _emit_event awaits bus.deliver->drain(), which can SUSPEND under
    # a backpressured plugin-host. It must run AFTER arbiter.complete() so a suspended emit
    # can never hold the turn slot.
    core = _core(sock_path, tmp_path)
    _open_turn(core, "t1")

    completed: list[bool] = []
    orig_complete = core.arbiter.complete

    def _spy_complete():
        r = orig_complete()
        completed.append(True)
        return r

    core.arbiter.complete = _spy_complete

    orig_deliver = core.bus.deliver

    async def _deliver(env):
        if env.kind is MsgKind.EVENT:
            await asyncio.Event().wait()  # hang forever (simulates a wedged sink)
        await orig_deliver(env)

    core.bus.deliver = _deliver

    task = asyncio.create_task(core._handle_result(_result_env("t1", ok=True)))
    try:
        await _poll(lambda: completed)  # arbiter.complete() ran -> slot released
        await asyncio.sleep(0.02)
        assert not task.done()  # still hung in the emit -> emit is AFTER the slot release
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_message_answered_reaches_only_the_subscribed_plugin(sock_path, tmp_path):
    sub = _Recorder("sub", (EventKind.MESSAGE_ANSWERED,))
    other = _Recorder("other", (EventKind.DAY_ALIVE,))

    core = _core(sock_path, tmp_path)
    await core.bus.start()  # the host connects over the socket, so the hub must listen
    host_task = asyncio.create_task(run_plugin_host(sock_path, plugins=[sub, other]))
    try:
        await _poll(lambda: core.bus._registry.get(Actor.PLUGIN_HOST) is not None)

        _open_turn(core, "t1")
        await core._handle_result(_result_env("t1", ok=True))

        await _poll(lambda: sub.got == [EventKind.MESSAGE_ANSWERED])
        assert other.got == []  # the non-subscriber never receives it
    finally:
        await core.bus.stop()
        await asyncio.wait_for(host_task, timeout=1.0)
