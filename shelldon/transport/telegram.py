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
import logging
import os
from collections.abc import AsyncIterator

from shelldon.transport.runner import run_transport

log = logging.getLogger("shelldon.transport.telegram")

_API = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 25  # getUpdates long-poll seconds (the server holds the request open)
_ERROR_BACKOFF = 3.0  # sleep after a transient getUpdates failure, so an outage doesn't busy-spin


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
    ) -> None:
        self._client = client
        self._token = token
        self._allowed = frozenset(allowed_users)
        self._allow_all = allow_all
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff
        self._chat_id = None  # where to reply — set from the last permitted message

    def _url(self, method: str) -> str:
        return _API.format(token=self._token, method=method)

    def _permitted(self, user_id) -> bool:
        return self._allow_all or user_id in self._allowed

    async def inbound(self) -> AsyncIterator[str]:
        """Long-poll getUpdates forever; yield the text of each PERMITTED message (recording
        its chat for replies) and ack it via the offset so it's never re-fetched. A transient
        API/network error backs off and retries — the adapter never dies on one bad poll."""
        offset = 0
        while True:
            try:
                resp = await self._client.get(
                    self._url("getUpdates"), params={"offset": offset, "timeout": self._poll_timeout}
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except Exception as exc:
                log.warning("telegram getUpdates failed (%s); backing off", exc)
                await asyncio.sleep(self._error_backoff)
                continue
            for u in updates:
                offset = u["update_id"] + 1  # ack: the next poll starts after this update
                msg = u.get("message") or {}
                text = msg.get("text")
                user_id = (msg.get("from") or {}).get("id")
                if text and self._permitted(user_id):
                    self._chat_id = (msg.get("chat") or {}).get("id")
                    yield text
                elif text:
                    log.info("telegram: dropping message from unauthorized user %r", user_id)

    async def outbound(self, text: str) -> None:
        """Send `text` to the chat of the current conversation. Before any inbound there is no
        chat to reply to (drop, don't crash); a send failure drops the reply (never crashes)."""
        if self._chat_id is None:
            log.debug("telegram: no chat yet; dropping outbound")
            return
        try:
            resp = await self._client.post(
                self._url("sendMessage"), json={"chat_id": self._chat_id, "text": text}
            )
            resp.raise_for_status()
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
        client = httpx.AsyncClient(timeout=httpx.Timeout(_POLL_TIMEOUT + 10))
    chat = TelegramChat(client, token, allowed_users=allowed_users, allow_all=allow_all)
    try:
        await run_transport(socket_path, chat.inbound(), chat.outbound)
    finally:
        if own:
            await client.aclose()


async def run_telegram_from_env(socket_path: str, env=None) -> None:
    """Build + run the Telegram adapter from the environment (the app's production glue):
    `TELEGRAM_BOT_TOKEN` (required), `ALLOWED_USERS`, `ALLOW_ALL_USERS`."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for the telegram transport")
    allow_all = env.get("ALLOW_ALL_USERS", "").strip().lower() in ("1", "true", "yes", "on")
    await run_telegram_transport(
        socket_path,
        token=token,
        allowed_users=parse_allowed_users(env.get("ALLOWED_USERS")),
        allow_all=allow_all,
    )
