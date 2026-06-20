"""Story 8.2 — the Telegram chat transport (AD-13).

A second adapter on the SAME transport seam as the CLI: a raw Bot-API long-poll `inbound`
(allowlist-filtered) + a `sendMessage` `outbound`, over httpx (0 new deps). The bus side is
the shared `run_transport`. These tests drive the adapter's LOGIC through a fake httpx client
— no network — asserting the allowlist (the security gate), the reply routing, and offset
advance (so updates aren't re-fetched). The live bot is exercised on the Pi, not in CI.
"""

import pytest

from shelldon.transport.telegram import TelegramChat


def _update(*, uid, chat, text, update_id=1):
    return {"update_id": update_id, "message": {"text": text, "from": {"id": uid}, "chat": {"id": chat}}}


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class _FakeClient:
    """Mimics the bits of httpx.AsyncClient the transport uses: get(getUpdates) returns the
    next scripted batch; post(sendMessage) records the payload."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.sent: list[dict] = []
        self.get_calls: list[dict] = []

    async def get(self, url, params=None):
        self.get_calls.append(params or {})
        result = self._batches.pop(0) if self._batches else []
        return _FakeResp({"ok": True, "result": result})

    async def post(self, url, json=None):
        self.sent.append(json)
        return _FakeResp({"ok": True})


async def _first_n(chat, n):
    got = []
    async for text in chat.inbound():
        got.append(text)
        if len(got) >= n:
            break
    return got


async def test_inbound_yields_allowed_user_text_and_records_reply_chat():
    client = _FakeClient([[_update(uid=42, chat=99, text="hi shelldon")]])
    chat = TelegramChat(client, "TOK", allowed_users={42})
    assert await _first_n(chat, 1) == ["hi shelldon"]
    assert chat._chat_id == 99  # remembered where to reply


async def test_inbound_skips_a_message_from_an_unauthorized_user():
    # The security gate: a stranger messaging the bot must NOT reach core.
    client = _FakeClient([
        [_update(uid=666, chat=99, text="let me into your brain", update_id=1)],
        [_update(uid=42, chat=99, text="legit", update_id=2)],
    ])
    chat = TelegramChat(client, "TOK", allowed_users={42})
    assert await _first_n(chat, 1) == ["legit"]  # the stranger's message was dropped


async def test_allow_all_bypasses_the_allowlist():
    client = _FakeClient([[_update(uid=12345, chat=99, text="hello")]])
    chat = TelegramChat(client, "TOK", allow_all=True)
    assert await _first_n(chat, 1) == ["hello"]


async def test_outbound_sends_to_the_recorded_chat():
    client = _FakeClient([])
    chat = TelegramChat(client, "TOK", allow_all=True)
    chat._chat_id = 99
    await chat.outbound("here is my reply")
    assert client.sent == [{"chat_id": 99, "text": "here is my reply"}]


async def test_outbound_before_any_inbound_is_a_safe_noop():
    # No conversation yet -> nowhere to reply -> drop, don't crash.
    client = _FakeClient([])
    chat = TelegramChat(client, "TOK", allow_all=True)
    await chat.outbound("nobody to hear me")
    assert client.sent == []


async def test_inbound_advances_the_offset_so_updates_are_not_refetched():
    client = _FakeClient([
        [_update(uid=42, chat=99, text="a", update_id=5)],
        [_update(uid=42, chat=99, text="b", update_id=8)],
    ])
    chat = TelegramChat(client, "TOK", allowed_users={42})
    assert await _first_n(chat, 2) == ["a", "b"]
    assert client.get_calls[0]["offset"] == 0
    assert client.get_calls[1]["offset"] == 6  # 5 + 1 — the acked update isn't pulled again
