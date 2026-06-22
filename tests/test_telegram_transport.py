"""Story 8.2 — the Telegram chat transport (AD-13).

A second adapter on the SAME transport seam as the CLI: a raw Bot-API long-poll `inbound`
(allowlist-filtered) + a `sendMessage` `outbound`, over httpx (0 new deps). The bus side is
the shared `run_transport`. These tests drive the adapter's LOGIC through a fake httpx client
— no network — asserting the allowlist (the security gate), the reply routing, and offset
advance (so updates aren't re-fetched). The live bot is exercised on the Pi, not in CI.
"""

import asyncio

import pytest

from shelldon.transport.telegram import TelegramChat, resolve_token


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
    next scripted batch; post records the (method, payload). `sendMessage` ok-values can be
    scripted (`post_oks`) to exercise the markdown→plain fallback."""

    def __init__(self, batches, post_oks=None):
        self._batches = list(batches)
        self.sent: list[dict] = []  # sendMessage payloads (back-compat)
        self.posts: list[tuple[str, dict]] = []  # (telegram method, payload) for every post
        self.get_calls: list[dict] = []
        self._post_oks = list(post_oks) if post_oks else None

    async def get(self, url, params=None):
        self.get_calls.append(params or {})
        result = self._batches.pop(0) if self._batches else []
        return _FakeResp({"ok": True, "result": result})

    async def post(self, url, json=None):
        method = url.rsplit("/", 1)[-1]
        self.posts.append((method, json))
        if method == "sendMessage":
            self.sent.append(json)
            ok = self._post_oks.pop(0) if self._post_oks else True
            return _FakeResp({"ok": ok})
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
    assert client.sent == [{"chat_id": 99, "text": "here is my reply", "parse_mode": "Markdown"}]


# --- Item 4a: render markdown, but never drop a reply on a parse error ---


async def test_outbound_sends_with_markdown_when_accepted():
    client = _FakeClient([])
    chat = TelegramChat(client, "TOK", allow_all=True)
    chat._chat_id = 99
    await chat.outbound("**hi**")
    sends = [j for m, j in client.posts if m == "sendMessage"]
    assert len(sends) == 1 and sends[0]["parse_mode"] == "Markdown"


async def test_outbound_falls_back_to_plain_when_markdown_fails():
    # Telegram rejects the markdown parse (ok:false) -> resend WITHOUT parse_mode, never drop.
    client = _FakeClient([], post_oks=[False, True])
    chat = TelegramChat(client, "TOK", allow_all=True)
    chat._chat_id = 99
    await chat.outbound("oops *unbalanced")
    sends = [j for m, j in client.posts if m == "sendMessage"]
    assert len(sends) == 2
    assert sends[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in sends[1]  # plain fallback carries the same text


# --- Item 2: show a 'typing…' action while a (slow, LLM) turn is in flight ---


async def test_typing_action_sent_while_handling_a_message():
    client = _FakeClient([[_update(uid=42, chat=99, text="hi")]])
    chat = TelegramChat(client, "TOK", allowed_users={42}, typing_interval=0.01)
    assert await _first_n(chat, 1) == ["hi"]
    await asyncio.sleep(0.02)  # let the typing task fire its first action
    typing = [j for m, j in client.posts if m == "sendChatAction"]
    assert typing and typing[0] == {"chat_id": 99, "action": "typing"}
    chat._stop_typing()  # don't leak the task past the test


async def test_outbound_stops_typing_then_replies():
    client = _FakeClient([[_update(uid=42, chat=99, text="hi")]])
    chat = TelegramChat(client, "TOK", allowed_users={42}, typing_interval=0.01)
    await _first_n(chat, 1)
    assert chat._typing_task is not None  # started on inbound
    await chat.outbound("done")
    assert chat._typing_task is None  # stopped when the reply went out
    assert client.sent[-1]["text"] == "done"


async def test_outbound_before_any_inbound_is_a_safe_noop():
    # No conversation yet -> nowhere to reply -> drop, don't crash.
    client = _FakeClient([])
    chat = TelegramChat(client, "TOK", allow_all=True)
    await chat.outbound("nobody to hear me")
    assert client.sent == []


def test_shelldon_bot_token_wins_over_the_plain_name():
    # v2 runs its OWN bot, separate from a v1 using TELEGRAM_BOT_TOKEN — even in a shared env.
    assert resolve_token({"SHELLDON_TELEGRAM_BOT_TOKEN": "v2", "TELEGRAM_BOT_TOKEN": "v1"}) == "v2"
    assert resolve_token({"TELEGRAM_BOT_TOKEN": "v1"}) == "v1"  # falls back when only one is set
    assert resolve_token({}) is None


async def test_inbound_advances_the_offset_so_updates_are_not_refetched():
    client = _FakeClient([
        [_update(uid=42, chat=99, text="a", update_id=5)],
        [_update(uid=42, chat=99, text="b", update_id=8)],
    ])
    chat = TelegramChat(client, "TOK", allowed_users={42})
    assert await _first_n(chat, 2) == ["a", "b"]
    assert client.get_calls[0]["offset"] == 0
    assert client.get_calls[1]["offset"] == 6  # 5 + 1 — the acked update isn't pulled again
