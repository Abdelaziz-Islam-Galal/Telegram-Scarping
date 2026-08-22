"""
message_cleaner.py

Normalizes raw Telegram message text and groups it into chunks suitable
for embedding / downstream RAG ingestion.
"""

import re
from dataclasses import dataclass
from typing import Optional

from telegram_loader import RawTelegramMessage

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_MULTI_WS_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


@dataclass
class CleanedMessage:
    message_id: int
    chat_id: int
    text: str
    original_length: int
    cleaned_length: int


@dataclass
class TextChunk:
    chat_id: int
    source_message_ids: list[int]
    text: str
    chunk_index: int


def clean_text(
    text: str,
    strip_urls: bool = True,
    strip_mentions: bool = False,
    strip_emoji: bool = False,
) -> str:
    """Normalize a single message body: strip noise and collapse whitespace."""
    cleaned = text.strip()

    if strip_urls:
        cleaned = _URL_RE.sub("", cleaned)
    if strip_mentions:
        cleaned = _MENTION_RE.sub("", cleaned)
    if strip_emoji:
        cleaned = _EMOJI_RE.sub("", cleaned)

    cleaned = _MULTI_WS_RE.sub(" ", cleaned)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def clean_message(
    message: RawTelegramMessage,
    strip_urls: bool = True,
    strip_mentions: bool = False,
    strip_emoji: bool = False,
) -> Optional[CleanedMessage]:
    """Clean one message; returns None if nothing meaningful remains."""
    cleaned = clean_text(
        message.text,
        strip_urls=strip_urls,
        strip_mentions=strip_mentions,
        strip_emoji=strip_emoji,
    )
    if not cleaned:
        return None
    return CleanedMessage(
        message_id=message.message_id,
        chat_id=message.chat_id,
        text=cleaned,
        original_length=len(message.text),
        cleaned_length=len(cleaned),
    )


def clean_messages(
    messages: list[RawTelegramMessage],
    dedupe: bool = True,
    **clean_kwargs,
) -> list[CleanedMessage]:
    """Clean a batch of messages, optionally dropping exact-duplicate text."""
    cleaned: list[CleanedMessage] = []
    seen: set[str] = set()

    for msg in messages:
        result = clean_message(msg, **clean_kwargs)
        if result is None:
            continue
        if dedupe:
            key = result.text.lower()
            if key in seen:
                continue
            seen.add(key)
        cleaned.append(result)

    return cleaned


def chunk_cleaned_messages(
    cleaned: list[CleanedMessage],
    chunk_size: int = 1000,
    overlap: int = 150,
) -> list[TextChunk]:
    """
    Groups consecutive cleaned messages into chunks of ~chunk_size
    characters, carrying `overlap` trailing characters into the next
    chunk for context continuity.

    Assumes `cleaned` is in chronological order (oldest first).
    """
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")
    if not cleaned:
        return []

    chunks: list[TextChunk] = []
    buffer = ""
    buffer_ids: list[int] = []
    chunk_index = 0

    for msg in cleaned:
        candidate = f"{buffer}\n{msg.text}".strip() if buffer else msg.text

        if len(candidate) > chunk_size and buffer:
            chunks.append(
                TextChunk(
                    chat_id=msg.chat_id,
                    source_message_ids=buffer_ids,
                    text=buffer,
                    chunk_index=chunk_index,
                )
            )
            chunk_index += 1
            carry_over = buffer[-overlap:]
            buffer = f"{carry_over}\n{msg.text}".strip()
            buffer_ids = [msg.message_id]
        else:
            buffer = candidate
            buffer_ids.append(msg.message_id)

    if buffer:
        chunks.append(
            TextChunk(
                chat_id=cleaned[-1].chat_id,
                source_message_ids=buffer_ids,
                text=buffer,
                chunk_index=chunk_index,
            )
        )

    return chunks
