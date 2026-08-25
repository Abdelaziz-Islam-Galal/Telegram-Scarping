"""
message_cleaner.py

Normalizes raw Telegram message text and groups it into chunks suitable
for embedding / downstream RAG ingestion. Also provides shared report
formatting used by ad-hoc fetch scripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Optional

try:
    import emoji as _emoji_lib
except ImportError:  # Keep abbreviation cleaning usable without the optional package.
    _emoji_lib = None

if TYPE_CHECKING:
    from telegram_loader import RawTelegramMessage

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_HASHTAG_RE = re.compile(r"#\w+")
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


def normalize_repeated_chars(text: str, threshold: int = 3, collapse_to: int = 2) -> str:
    """
    Collapses long runs of the same character, e.g. 'sooooo good' -> 'soo good'.
    `threshold` is the minimum run length (in repeats beyond the first char)
    that triggers collapsing, so short legitimate doubles ('committee',
    'Mississippi') are left untouched by the default settings.
    """
    pattern = re.compile(r"(.)\1{" + str(threshold) + r",}")
    return pattern.sub(lambda m: m.group(1) * collapse_to, text)


@dataclass
class CleanedMessage:
    message_id: int
    chat_id: int
    text: str
    original_length: int
    cleaned_length: int
    # Optional passthrough metadata from RawTelegramMessage, when available,
    # so downstream reporting/formatting doesn't need to re-join against
    # the raw messages.
    sender_name: Optional[str] = None
    chat_title: Optional[str] = None
    date: Optional[datetime] = None
    is_outgoing: bool = False


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
    strip_hashtags: bool = False,
    emoji_mode: EmojiMode = "convert",
    expand_abbrevs: bool = True,
    collapse_repeats: bool = False,
) -> str:
    """
    Normalize a single message body: strip noise and collapse whitespace.

    emoji_mode:
        "convert" (default) -- rule-based: emoji -> ':text_tag:', keeps
                                the sentiment/context legible to the LLM
        "strip"             -- remove emoji entirely (old behavior)
        "keep"              -- leave emoji as-is
    collapse_repeats:
        Off by default (it's a lossy transform). Turn on for noisy chat
        exports where "heyyyyy" / "nooooo" style elongation is common.
    """
    cleaned = text.strip()

    if strip_urls:
        cleaned = _URL_RE.sub("", cleaned)
    if strip_mentions:
        cleaned = _MENTION_RE.sub("", cleaned)
    if strip_hashtags:
        cleaned = _HASHTAG_RE.sub("", cleaned)

    if emoji_mode == "strip":
        cleaned = _EMOJI_RE.sub("", cleaned)
    elif emoji_mode == "convert":
        cleaned = convert_emoji_to_text(cleaned)
    # "keep" -> no-op, leave emoji untouched

    if expand_abbrevs:
        cleaned = expand_abbreviations(cleaned)

    if collapse_repeats:
        cleaned = normalize_repeated_chars(cleaned)

    cleaned = _MULTI_WS_RE.sub(" ", cleaned)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def clean_message(
    message: RawTelegramMessage,
    strip_urls: bool = True,
    strip_mentions: bool = False,
    strip_hashtags: bool = False,
    emoji_mode: EmojiMode = "convert",
    expand_abbrevs: bool = True,
    collapse_repeats: bool = False,
    min_length: int = 0,
) -> Optional[CleanedMessage]:
    """
    Clean one message; returns None if nothing meaningful remains, or if
    the cleaned text is shorter than `min_length` characters (useful for
    dropping "ok" / "lol" / single-emoji messages before embedding).
    """
    cleaned = clean_text(
        message.text,
        strip_urls=strip_urls,
        strip_mentions=strip_mentions,
        strip_hashtags=strip_hashtags,
        emoji_mode=emoji_mode,
        expand_abbrevs=expand_abbrevs,
        collapse_repeats=collapse_repeats,
    )
    if not cleaned or len(cleaned) < min_length:
        return None
    return CleanedMessage(
        message_id=message.message_id,
        chat_id=message.chat_id,
        text=cleaned,
        original_length=len(message.text),
        cleaned_length=len(cleaned),
        sender_name=getattr(message, "sender_name", None),
        chat_title=getattr(message, "chat_title", None),
        date=getattr(message, "date", None),
        is_outgoing=getattr(message, "is_outgoing", False),
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


def format_messages_report(
    messages: list[CleanedMessage],
    title: str = "Recent Telegram messages",
    time_format: str = "%I:%M %p",
) -> str:
    """
    Builds the same human-readable "N. From: ... / Message: ... / [chat id: ...]"
    report block that fetch_test.py / fetch_test3.py each hand-rolled, from a
    list of already-cleaned messages. Pass messages produced by clean_message()
    / clean_messages() so sender_name / chat_title / date are populated.
    """
    lines = [title, "=" * max(len(title), 10), ""]

    if not messages:
        lines.append("No recent messages found.")
        return "\n".join(lines)

    for i, msg in enumerate(messages, 1):
        sender = msg.sender_name or "Unknown"
        chat = msg.chat_title or str(msg.chat_id)
        when = msg.date.strftime(time_format) if msg.date else "unknown time"
        lines.extend(
            [
                f"{i}. From: {sender} in {chat} (at {when})",
                f"   Message: {msg.text}",
                f"   [chat id: {msg.chat_id}]",
                f"   source message id: {msg.message_id}",
                "",
                "-" * 50,
                "",
            ]
        )
    return "\n".join(lines)


def append_report_to_file(report: str, path: str) -> None:
    """Appends a formatted report block to `path` (creates the file if needed)."""
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n\n" + report)