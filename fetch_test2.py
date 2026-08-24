"""
message_cleaner.py

Normalizes raw Telegram message text and groups it into chunks suitable
for embedding / downstream RAG ingestion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Optional

try:
    import emoji as _emoji_lib
except ImportError:  # Keep abbreviation cleaning usable without the optional package.
    _emoji_lib = None

if TYPE_CHECKING:
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
    "\u200D\uFE0E\uFE0F"
    "]+",
    flags=re.UNICODE,
)

EmojiMode = Literal["convert", "strip", "keep"]

# Rule-based, not NLP: fast dictionary lookups instead of a model call.
# Keep deliberately conservative -- only unambiguous, high-frequency
# chat abbreviations. Easy to extend; if results look poor for your
# corpus, swap expand_abbreviations()'s body for an NLP-based pass
# without touching any of the calling code.
ABBREVIATIONS: dict[str, str] = {
    "lol": "laughing out loud",
    "lmao": "laughing my ass off",
    "rofl": "rolling on the floor laughing",
    "brb": "be right back",
    "btw": "by the way",
    "idk": "i don't know",
    "imo": "in my opinion",
    "imho": "in my honest opinion",
    "omg": "oh my god",
    "smh": "shaking my head",
    "tbh": "to be honest",
    "afaik": "as far as i know",
    "asap": "as soon as possible",
    "fyi": "for your information",
    "np": "no problem",
    "nvm": "never mind",
    "ttyl": "talk to you later",
    "rn": "right now",
    "irl": "in real life",
    "dm": "direct message",
    "gg": "good game",
    "gj": "good job",
    "wyd": "what are you doing",
    "hbu": "how about you",
    "ikr": "i know right",
    "tysm": "thank you so much",
    "ngl": "not gonna lie",
    "fr": "for real",
}

# Longest-first so multi-word-looking keys never get shadowed by a
# shorter substring match; \b keeps it to whole words only.
_ABBREV_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(ABBREVIATIONS, key=len, reverse=True)) + r")\b",
    flags=re.IGNORECASE,
)


def expand_abbreviations(text: str) -> str:
    """Rule-based expansion, e.g. 'brb' -> 'be right back'. Case-insensitive."""
    return _ABBREV_RE.sub(lambda m: ABBREVIATIONS[m.group(0).lower()], text)


def convert_emoji_to_text(text: str) -> str:
    """
    Rule-based (dictionary lookup, not a model) emoji -> text tag
    conversion using the `emoji` package's demojize, e.g. '😂' becomes
    ':face_with_tears_of_joy:'. Cheap and deterministic; the resulting
    tag is still legible to the downstream LLM agent without needing
    the raw glyph.
    """
    if _emoji_lib is None:
        return text

    converted = _emoji_lib.demojize(text)
    converted = converted.replace(
        ":smiling_face_with_hearts:", ":smiling_face_with_three_hearts:"
    )
    # demojize glues tags directly onto neighboring characters
    # ("great!😂" -> "great!:face_with_tears_of_joy:") -- add spacing
    # so tags read as separate tokens.
    converted = re.sub(r"(:[a-z0-9_+\-]+:)", r" \1 ", converted)
    return converted


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
    emoji_mode: EmojiMode = "convert",
    expand_abbrevs: bool = True,
) -> str:
    """
    Normalize a single message body: strip noise and collapse whitespace.

    emoji_mode:
        "convert" (default) -- rule-based: emoji -> ':text_tag:', keeps
                                the sentiment/context legible to the LLM
        "strip"             -- remove emoji entirely (old behavior)
        "keep"              -- leave emoji as-is
    """
    cleaned = text.strip()

    if strip_urls:
        cleaned = _URL_RE.sub("", cleaned)
    if strip_mentions:
        cleaned = _MENTION_RE.sub("", cleaned)

    if emoji_mode == "strip":
        cleaned = _EMOJI_RE.sub("", cleaned)
    elif emoji_mode == "convert":
        cleaned = convert_emoji_to_text(cleaned)
    # "keep" -> no-op, leave emoji untouched

    if expand_abbrevs:
        cleaned = expand_abbreviations(cleaned)

    cleaned = _MULTI_WS_RE.sub(" ", cleaned)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def clean_message(
    message: RawTelegramMessage,
    strip_urls: bool = True,
    strip_mentions: bool = False,
    emoji_mode: EmojiMode = "convert",
    expand_abbrevs: bool = True,
) -> Optional[CleanedMessage]:
    """Clean one message; returns None if nothing meaningful remains."""
    cleaned = clean_text(
        message.text,
        strip_urls=strip_urls,
        strip_mentions=strip_mentions,
        emoji_mode=emoji_mode,
        expand_abbrevs=expand_abbrevs,
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