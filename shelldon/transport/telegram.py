"""Telegram chat transport (Story 8.2, AD-13) — a second adapter on the CLI's transport seam.

Raw Bot-API over `httpx` (already a transitive dep — **0 new deps**, no Telegram framework):
a long-poll `getUpdates` `inbound` (allowlist-filtered — the security gate) + a `sendMessage`
`outbound`. The bus side is the shared `run_transport`. The bot token is the adapter's OWN
connection credential (AD-2/NFR9: a transport holds only that, never a model/tool cred); `httpx`
is lazily imported so the module loads without it and the import-linter stays clean.

Single-owner: replies go to the chat the last permitted message came from. `ALLOWED_USERS`
(Telegram user ids) is the gate — a stranger messaging the bot never reaches core (the brain).
"""

import asyncio
import html
import logging
import os
import re
from collections.abc import AsyncIterator

from shelldon.contracts import InboundMessage
from shelldon.transport.runner import run_transport

log = logging.getLogger("shelldon.transport.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 25  # getUpdates long-poll seconds (the server holds the request open)
_ERROR_BACKOFF = 3.0  # sleep after a transient getUpdates failure, so an outage doesn't busy-spin
_TYPING_INTERVAL = 4.0  # re-send the 'typing…' action this often — Telegram clears it after ~5s

#: Slash commands registered via setMyCommands on startup (Story 9.3, field-note item 5).
_COMMANDS = [
    {"command": "start", "description": "Say hi to shelldon"},
    {"command": "help", "description": "What shelldon can do"},
]

#: A `code` span in the model/approval text → an HTML <pre> block (Story 9.3 AC2: tool output
#: renders as a code block). Used only on the HTML-parse_mode approval path.
_CODE_SPAN = re.compile(r"`([^`]*)`")


def _to_html(text: str) -> str:
    """HTML-escape `text` and render markdown `code` spans as <pre>…</pre> blocks (AC2). Escapes
    code-span content too, so a tool path/command with <,>,& can never break the HTML parse."""
    out: list[str] = []
    last = 0
    for m in _CODE_SPAN.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        out.append(f"<pre>{html.escape(m.group(1))}</pre>")
        last = m.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


class TelegramChat:
    """Bot-API inbound/outbound over an injected httpx-like client (tests inject a fake)."""

    def __init__(
        self,
        client,
        token: str,
        *,
        allowed_users=frozenset(),
        allow_all: bool = False,
        poll_timeout: int = _POLL_TIMEOUT,
        error_backoff: float = _ERROR_BACKOFF,
        typing_interval: float = _TYPING_INTERVAL,
    ) -> None:
        self._client = client
        self._token = token
        self._allowed = frozenset(allowed_users)
        self._allow_all = allow_all
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff
        self._typing_interval = typing_interval
        self._chat_id = None  # where to reply — set from the last permitted message
        self._typing_task = None  # the in-flight 'typing…' refresh loop, if any

    def _url(self, method: str) -> str:
        return _API.format(token=self._token, method=method)

    def _permitted(self, user_id) -> bool:
        return self._allow_all or user_id in self._allowed

    async def inbound(self) -> "AsyncIterator[str | InboundMessage]":
        """Long-poll getUpdates forever; yield the text of each PERMITTED message (recording
        its chat for replies) and ack it via the offset so it's never re-fetched. A transient
        API/network error backs off and retries — the adapter never dies on one bad poll."""
        offset = 0
        log.info("telegram: poll loop started")
        first = True
        while True:
            try:
                resp = await self._client.get(
                    self._url("getUpdates"), params={"offset": offset, "timeout": self._poll_timeout}
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
                if first:
                    log.info("telegram: first poll OK (%d update(s) waiting)", len(updates))
                    first = False
            except Exception as exc:
                log.warning("telegram getUpdates failed (%s); backing off", exc)
                await asyncio.sleep(self._error_backoff)
                continue
            for u in updates:
                offset = u["update_id"] + 1  # ack: the next poll starts after this update
                cq = u.get("callback_query")
                if cq is not None:  # Story 9.3: an Approve/Deny tap
                    decision = await self._handle_callback(cq)
                    if decision is not None:
                        yield decision
                    continue
                msg = u.get("message") or {}
                text = msg.get("text")
                user_id = (msg.get("from") or {}).get("id")
                if text and self._permitted(user_id):
                    self._chat_id = (msg.get("chat") or {}).get("id")
                    self._start_typing()  # show 'typing…' while core works the (slow) turn
                    yield text
                elif text:
                    log.info("telegram: dropping message from unauthorized user %r", user_id)

    async def _typing_loop(self) -> None:
        """Re-send the 'typing…' chat action every `_typing_interval` until cancelled, so the
        owner can see the bot is working through a turn. A failed ping is swallowed — a missed
        typing action must never break or end the turn."""
        while True:
            try:
                await self._client.post(
                    self._url("sendChatAction"), json={"chat_id": self._chat_id, "action": "typing"}
                )
            except Exception:
                log.debug("telegram sendChatAction failed", exc_info=True)
            await asyncio.sleep(self._typing_interval)

    def _start_typing(self) -> None:
        """(Re)start the typing-refresh loop for the current chat — cancels any prior one so a
        second inbound before a reply doesn't leak a task."""
        self._stop_typing()
        if self._chat_id is not None:
            self._typing_task = asyncio.create_task(self._typing_loop())

    def _stop_typing(self) -> None:
        if self._typing_task is not None:
            self._typing_task.cancel()
            self._typing_task = None

    async def _send(self, text: str, parse_mode: str | None) -> bool:
        """POST one sendMessage (optionally with `parse_mode`); return Telegram's `ok` flag."""
        payload = {"chat_id": self._chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = await self._client.post(self._url("sendMessage"), json=payload)
        return bool(resp.json().get("ok"))

    async def _handle_callback(self, cq: dict) -> "InboundMessage | None":
        """Turn an Approve/Deny `callback_query` into an approval-decision InboundMessage
        (Story 9.3). Always answer the query (clears the client spinner); gate on the user id
        like chat. `callback_data` is `"{turn_id}:approve|deny"`."""
        try:
            await self._client.post(
                self._url("answerCallbackQuery"), json={"callback_query_id": cq.get("id")}
            )
        except Exception:
            log.debug("telegram answerCallbackQuery failed", exc_info=True)
        user_id = (cq.get("from") or {}).get("id")
        data = cq.get("data") or ""
        if not self._permitted(user_id) or ":" not in data:
            if data:
                log.info("telegram: dropping callback from unauthorized user %r", user_id)
            return None
        turn_id, _, decision = data.rpartition(":")
        if not turn_id:  # malformed callback data ("…:approve" with no id) — ignore, don't mislead
            log.info("telegram: dropping callback with empty turn_id (%r)", data)
            return None
        # Reply to the chat the button lived in (so a tap also (re)sets the reply target) and
        # show 'typing…' while the resumed worker runs (it can take seconds).
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is not None:
            self._chat_id = chat_id
        self._start_typing()
        return InboundMessage(text="", approval_turn_id=turn_id, approved=(decision == "approve"))

    async def send_approval(self, text: str, approval_turn_id: str) -> None:
        """Send an approval request with an inline Approve/Deny keyboard (Story 9.3 AC2). HTML
        parse_mode (code spans → <pre>); the buttons' callback_data carries `approval_turn_id`
        so the tap echoes the id the resumable state is parked under. Drops on no-chat/failure."""
        self._stop_typing()
        if self._chat_id is None:
            log.debug("telegram: no chat yet; dropping approval request")
            return
        payload = {
            "chat_id": self._chat_id,
            "text": _to_html(text),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "✅ Approve", "callback_data": f"{approval_turn_id}:approve"},
                    {"text": "❌ Deny", "callback_data": f"{approval_turn_id}:deny"},
                ]]
            },
        }
        try:
            await self._client.post(self._url("sendMessage"), json=payload)
        except Exception as exc:
            log.warning("telegram approval send failed (%s); dropped", exc)

    async def set_commands(self) -> None:
        """Register the slash-command set once on startup (Story 9.3, field-note item 5)."""
        try:
            await self._client.post(self._url("setMyCommands"), json={"commands": _COMMANDS})
        except Exception as exc:
            log.warning("telegram setMyCommands failed (%s); continuing", exc)

    async def outbound(self, text: str) -> None:
        """Send `text` to the current chat, stopping the typing indicator first. Rendered as
        Markdown, but on a parse rejection (unbalanced `*`/`_`/backticks in free-form model
        text) resend as PLAIN so a reply is never dropped over formatting. Before any inbound
        there is no chat to reply to (drop, don't crash); any failure drops the reply (never
        crashes)."""
        self._stop_typing()
        if self._chat_id is None:
            log.debug("telegram: no chat yet; dropping outbound")
            return
        try:
            if await self._send(text, parse_mode="Markdown"):
                return
            if not await self._send(text, parse_mode=None):  # markdown rejected → plain fallback
                log.warning("telegram sendMessage failed (both markdown and plain); reply dropped")
        except Exception as exc:
            log.warning("telegram sendMessage failed (%s); reply dropped", exc)


def parse_allowed_users(raw: str | None) -> frozenset[int]:
    """Parse `ALLOWED_USERS` ("12,34, 56") into a set of ints; bad/blank entries are dropped."""
    out: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ALLOWED_USERS: ignoring non-integer id %r", part)
    return frozenset(out)


async def run_telegram_transport(
    socket_path: str,
    *,
    token: str,
    allowed_users=frozenset(),
    allow_all: bool = False,
    client=None,
) -> None:
    """Run the Telegram adapter as a bus client. Lazily builds an httpx client (its read
    timeout must exceed the long-poll) unless one is injected; closes it on teardown."""
    import httpx  # lazy: only when telegram is actually selected (keeps the module import-clean)

    own = client is None
    if own:
        # `local_address="0.0.0.0"` forces IPv4: some hosts (a Tailscale-MagicDNS Pi with no IPv6
        # egress) resolve api.telegram.org to an AAAA record that then can't be reached, stalling
        # the client. Binding the source to the IPv4 wildcard makes httpx never attempt IPv6.
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        client = httpx.AsyncClient(timeout=httpx.Timeout(_POLL_TIMEOUT + 10), transport=transport)
    chat = TelegramChat(client, token, allowed_users=allowed_users, allow_all=allow_all)
    # set_commands is cosmetic (the slash-command menu) and MUST NOT gate the poll loop that
    # receives messages — fire it fully in the background so a slow/hung Bot-API call here can
    # never delay or wedge inbound (Story 9.3). The reference is held so it isn't GC'd mid-flight.
    commands_task = asyncio.create_task(chat.set_commands())
    log.info("telegram: connecting to bus + starting poll loop")
    try:
        await run_transport(
            socket_path, chat.inbound(), chat.outbound, on_approval_request=chat.send_approval
        )
    finally:
        if own:
            await client.aclose()


def resolve_token(env) -> str | None:
    """shelldon's OWN bot token wins (`SHELLDON_TELEGRAM_BOT_TOKEN`) so v2 runs a separate bot
    from a v1 that uses `TELEGRAM_BOT_TOKEN` — even in a shared env — falling back to the plain
    name when only one bot is configured."""
    return env.get("SHELLDON_TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")


async def run_telegram_from_env(socket_path: str, env=None) -> None:
    """Build + run the Telegram adapter from the environment (the app's production glue):
    `SHELLDON_TELEGRAM_BOT_TOKEN`/`TELEGRAM_BOT_TOKEN` (required), `ALLOWED_USERS`,
    `ALLOW_ALL_USERS`."""
    env = os.environ if env is None else env
    token = resolve_token(env)
    if not token:
        raise RuntimeError(
            "SHELLDON_TELEGRAM_BOT_TOKEN (or TELEGRAM_BOT_TOKEN) is required for the telegram transport"
        )
    allow_all = env.get("ALLOW_ALL_USERS", "").strip().lower() in ("1", "true", "yes", "on")
    await run_telegram_transport(
        socket_path,
        token=token,
        allowed_users=parse_allowed_users(env.get("ALLOWED_USERS")),
        allow_all=allow_all,
    )
