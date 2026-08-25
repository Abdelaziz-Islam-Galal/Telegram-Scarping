"""
telegram_loader.py

Fetches messages from Telegram using a previously-saved session string
(loaded from PostgreSQL via TelegramAuthRepository).

Requires: telethon
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional, Union

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, Message, User

from telegram_auth_service import TelegramAuthRepository

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


def _display_name(entity) -> str:
    """Best-effort human-readable name for a user/chat/channel entity."""
    if entity is None:
        return "Unknown"
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    title = getattr(entity, "title", None)
    if title:
        return title
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return "Unknown"


@dataclass
class RawTelegramMessage:
    message_id: int
    chat_id: int
    sender_id: Optional[int]
    date: datetime
    text: str
    reply_to_message_id: Optional[int] = None
    media_type: Optional[str] = None
    sender_name: Optional[str] = None
    chat_title: Optional[str] = None
    is_outgoing: bool = False


@dataclass
class DialogInfo:
    dialog_id: int
    name: str
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int


class TelegramMessageLoader:
    """Loads messages from a chat/channel on behalf of an authenticated user."""

    def __init__(self, repository: TelegramAuthRepository):
        self._repo = repository
        self._clients: dict[str, TelegramClient] = {}

    @classmethod
    async def create(cls) -> "TelegramMessageLoader":
        repo = await TelegramAuthRepository.create()
        return cls(repo)

    async def close(self):
        """Disconnects all cached clients and closes the auth repository."""
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        await self._repo.close()

    async def __aenter__(self) -> "TelegramMessageLoader":
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def _get_client(self, phone_number: str) -> TelegramClient:
        """
        Returns a connected, authorized client for `phone_number`.

        Connections are cached per phone number and reused across calls
        instead of reconnecting every time -- callers doing several
        fetches in one script no longer pay a fresh handshake each call.
        Use close() (or `async with` on the loader) to tear connections
        down when done.
        """
        existing = self._clients.get(phone_number)
        if existing is not None and existing.is_connected():
            return existing

        session_string = await self._repo.get_session_string(phone_number)
        if session_string is None:
            raise ValueError(
                f"No saved Telegram session for {phone_number}. "
                "Run the auth flow (telegram_auth.py) first."
            )
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise ValueError(
                f"Stored session for {phone_number} is no longer valid; re-authenticate."
            )
        self._clients[phone_number] = client
        return client

    async def _iter_messages_with_retry(self, client: TelegramClient, entity, **kwargs):
        """Wraps client.iter_messages, retrying once Telegram's flood-wait clears."""
        while True:
            try:
                async for msg in client.iter_messages(entity, **kwargs):
                    yield msg
                return
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)

    async def _to_raw_message(
        self,
        msg: Message,
        chat_id: int,
        chat_title: Optional[str] = None,
        resolve_sender: bool = True,
    ) -> RawTelegramMessage:
        sender_name = None
        if resolve_sender:
            try:
                sender = await msg.get_sender()
                sender_name = _display_name(sender)
            except Exception:
                # Deleted accounts / privacy restrictions can make sender
                # lookups fail; degrade gracefully instead of aborting the
                # whole fetch over one unresolved sender.
                sender_name = None
        return RawTelegramMessage(
            message_id=msg.id,
            chat_id=chat_id,
            sender_id=msg.sender_id,
            date=msg.date,
            text=msg.text,
            reply_to_message_id=msg.reply_to_msg_id,
            media_type=type(msg.media).__name__ if msg.media else None,
            sender_name=sender_name,
            chat_title=chat_title,
            is_outgoing=bool(msg.out),
        )

    async def list_dialogs(self, phone_number: str, limit: int = 20) -> list[DialogInfo]:
        """Lists the most recently active chats for `phone_number`."""
        client = await self._get_client(phone_number)
        dialogs: list[DialogInfo] = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            dialogs.append(
                DialogInfo(
                    dialog_id=dialog.id,
                    name=dialog.name or _display_name(entity),
                    is_group=isinstance(entity, Chat)
                    or (isinstance(entity, Channel) and entity.megagroup),
                    is_channel=isinstance(entity, Channel) and not entity.megagroup,
                    is_user=isinstance(entity, User),
                    unread_count=dialog.unread_count,
                )
            )
        return dialogs

    async def fetch_messages(
        self,
        phone_number: str,
        chat: Union[str, int],
        limit: int = 100,
        min_date: Optional[datetime] = None,
        max_date: Optional[datetime] = None,
        include_outgoing: bool = True,
        resolve_sender_names: bool = True,
    ) -> list[RawTelegramMessage]:
        """
        Fetch up to `limit` messages from `chat` (username, invite link, or
        numeric chat id), using the account tied to `phone_number`.

        include_outgoing=False restricts results to messages sent *to* the
        account (skips anything the account itself sent).
        """
        client = await self._get_client(phone_number)
        entity = await client.get_entity(chat)
        chat_title = _display_name(entity)
        messages: list[RawTelegramMessage] = []
        async for msg in self._iter_messages_with_retry(
            client, entity, limit=limit, offset_date=max_date
        ):
            if not isinstance(msg, Message):
                continue
            if min_date and msg.date < min_date:
                break
            if not msg.text:
                continue
            if not include_outgoing and msg.out:
                continue
            messages.append(
                await self._to_raw_message(
                    msg,
                    chat_id=entity.id,
                    chat_title=chat_title,
                    resolve_sender=resolve_sender_names,
                )
            )
        return messages

    async def fetch_recent_incoming(
        self,
        phone_number: str,
        chat: Union[str, int],
        limit: int = 5,
    ) -> list[RawTelegramMessage]:
        """
        Convenience wrapper: the `limit` newest incoming (non-outgoing)
        messages from a single chat, newest first.
        """
        # Over-fetch a bit since outgoing messages get filtered out.
        messages = await self.fetch_messages(
            phone_number, chat, limit=limit * 4, include_outgoing=False
        )
        messages.sort(key=lambda m: m.date, reverse=True)
        return messages[:limit]

    async def fetch_recent_across_dialogs(
        self,
        phone_number: str,
        dialog_limit: int = 10,
        per_chat_limit: int = 5,
        top_n: int = 5,
        include_outgoing: bool = False,
        resolve_sender_names: bool = True,
    ) -> list[RawTelegramMessage]:
        """
        Scans the `dialog_limit` most recently active chats, pulls up to
        `per_chat_limit` messages from each, and returns the newest `top_n`
        overall -- a "what did I miss across all my chats" scan.
        """
        client = await self._get_client(phone_number)
        collected: list[RawTelegramMessage] = []

        async for dialog in client.iter_dialogs(limit=dialog_limit):
            chat_title = dialog.name or _display_name(dialog.entity)
            async for msg in self._iter_messages_with_retry(
                client, dialog.id, limit=per_chat_limit
            ):
                if not isinstance(msg, Message) or not msg.text:
                    continue
                if not include_outgoing and msg.out:
                    continue
                collected.append(
                    await self._to_raw_message(
                        msg,
                        chat_id=dialog.id,
                        chat_title=chat_title,
                        resolve_sender=resolve_sender_names,
                    )
                )

        collected.sort(key=lambda m: m.date, reverse=True)
        return collected[:top_n]

    async def iter_messages(
        self,
        phone_number: str,
        chat: Union[str, int],
        limit: int = 100,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Streaming variant for large exports."""
        client = await self._get_client(phone_number)
        entity = await client.get_entity(chat)
        chat_title = _display_name(entity)
        async for msg in self._iter_messages_with_retry(client, entity, limit=limit):
            if not isinstance(msg, Message) or not msg.text:
                continue
            yield await self._to_raw_message(msg, chat_id=entity.id, chat_title=chat_title)