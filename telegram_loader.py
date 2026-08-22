"""
telegram_loader.py

Fetches messages from Telegram using a previously-saved session string
(loaded from PostgreSQL via TelegramAuthRepository).

Requires: telethon
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional, Union

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Message

from telegram_auth_service import TelegramAuthRepository

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]


@dataclass
class RawTelegramMessage:
    message_id: int
    chat_id: int
    sender_id: Optional[int]
    date: datetime
    text: str
    reply_to_message_id: Optional[int] = None
    media_type: Optional[str] = None


class TelegramMessageLoader:
    """Loads messages from a chat/channel on behalf of an authenticated user."""

    def __init__(self, repository: TelegramAuthRepository):
        self._repo = repository

    @classmethod
    async def create(cls) -> "TelegramMessageLoader":
        repo = await TelegramAuthRepository.create()
        return cls(repo)

    async def close(self):
        await self._repo.close()

    async def _get_client(self, phone_number: str) -> TelegramClient:
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
        return client

    async def fetch_messages(
        self,
        phone_number: str,
        chat: Union[str, int],
        limit: int = 100,
        min_date: Optional[datetime] = None,
        max_date: Optional[datetime] = None,
    ) -> list[RawTelegramMessage]:
        """
        Fetch up to `limit` messages from `chat` (username, invite link, or
        numeric chat id), using the account tied to `phone_number`.
        """
        client = await self._get_client(phone_number)
        try:
            entity = await client.get_entity(chat)
            messages: list[RawTelegramMessage] = []
            async for msg in client.iter_messages(entity, limit=limit, offset_date=max_date):
                if not isinstance(msg, Message):
                    continue
                if min_date and msg.date < min_date:
                    break
                if not msg.text:
                    continue
                messages.append(
                    RawTelegramMessage(
                        message_id=msg.id,
                        chat_id=entity.id,
                        sender_id=msg.sender_id,
                        date=msg.date,
                        text=msg.text,
                        reply_to_message_id=msg.reply_to_msg_id,
                        media_type=type(msg.media).__name__ if msg.media else None,
                    )
                )
            return messages
        finally:
            await client.disconnect()

    async def iter_messages(
        self,
        phone_number: str,
        chat: Union[str, int],
        limit: int = 100,
    ) -> AsyncIterator[RawTelegramMessage]:
        """Streaming variant for large exports."""
        client = await self._get_client(phone_number)
        try:
            entity = await client.get_entity(chat)
            async for msg in client.iter_messages(entity, limit=limit):
                if not isinstance(msg, Message) or not msg.text:
                    continue
                yield RawTelegramMessage(
                    message_id=msg.id,
                    chat_id=entity.id,
                    sender_id=msg.sender_id,
                    date=msg.date,
                    text=msg.text,
                    reply_to_message_id=msg.reply_to_msg_id,
                    media_type=type(msg.media).__name__ if msg.media else None,
                )
        finally:
            await client.disconnect()
