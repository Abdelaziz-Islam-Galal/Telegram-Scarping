"""
test_usage.py

A runnable demo/test that exercises message_cleaner.py and
telegram_loader.py together and shows the intended usage of each.

No live Telegram account, API keys, or database are required: the
telethon package and telegram_auth_service module are faked in-process
(see the "FAKES" section below), so this can be run as-is with:
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# FAKES -- stand in for `telethon` and `telegram_auth_service` so this file
# runs with zero external dependencies and zero live credentials. None of
# this is needed in your real project, where those packages already exist.
# ---------------------------------------------------------------------------

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy-hash-for-demo")

_telethon = types.ModuleType("telethon")
_telethon_sessions = types.ModuleType("telethon.sessions")
_telethon_errors = types.ModuleType("telethon.errors")
_telethon_tl = types.ModuleType("telethon.tl")
_telethon_tl_types = types.ModuleType("telethon.tl.types")


class _StubTelegramClient:
    def __init__(self, *args, **kwargs):
        pass


class _StubStringSession:
    def __init__(self, *args, **kwargs):
        pass


class FloodWaitError(Exception):
    def __init__(self, seconds: int = 0):
        super().__init__(f"Flood wait: {seconds}s")
        self.seconds = seconds


class Message:  # telethon.tl.types.Message stand-in
    pass


class User:  # telethon.tl.types.User stand-in
    pass


class Chat:  # telethon.tl.types.Chat stand-in
    pass


class Channel:  # telethon.tl.types.Channel stand-in
    pass


_telethon.TelegramClient = _StubTelegramClient
_telethon_sessions.StringSession = _StubStringSession
_telethon_errors.FloodWaitError = FloodWaitError
_telethon_tl_types.Message = Message
_telethon_tl_types.User = User
_telethon_tl_types.Chat = Chat
_telethon_tl_types.Channel = Channel

sys.modules["telethon"] = _telethon
sys.modules["telethon.sessions"] = _telethon_sessions
sys.modules["telethon.errors"] = _telethon_errors
sys.modules["telethon.tl"] = _telethon_tl
sys.modules["telethon.tl.types"] = _telethon_tl_types

_telegram_auth_service = types.ModuleType("telegram_auth_service")


class TelegramAuthRepository:
    """Stand-in for the real DB-backed session repository."""

    @classmethod
    async def create(cls) -> "TelegramAuthRepository":
        return cls()

    async def get_session_string(self, phone_number: str):
        return "fake-session-string"

    async def close(self):
        pass


_telegram_auth_service.TelegramAuthRepository = TelegramAuthRepository
sys.modules["telegram_auth_service"] = _telegram_auth_service

# Now that the fakes are registered, the real modules under test import cleanly.
import message_cleaner as mc
import telegram_loader as tl

# ---------------------------------------------------------------------------
# FAKE TELEGRAM DATA -- a couple of chats with realistically messy text
# (slang, emoji, URLs, a mention/hashtag, elongated words, an outgoing
# message, and one message whose sender lookup fails) so both the loader
# and the cleaner get exercised meaningfully.
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 24, 14, 0, 0)


class FakeUser(User):
    def __init__(self, id, first_name=None, last_name=None, username=None):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeChat(Chat):
    def __init__(self, id, title):
        self.id = id
        self.title = title


class FakeMessage(Message):
    def __init__(self, id, date, text, sender_id, out=False, sender=None,
                 reply_to_msg_id=None, media=None):
        self.id = id
        self.date = date
        self.text = text
        self.sender_id = sender_id
        self.out = out
        self.reply_to_msg_id = reply_to_msg_id
        self.media = media
        self._sender = sender

    async def get_sender(self):
        if self._sender is None:
            # Simulates a deleted account / privacy-restricted lookup.
            raise ValueError("could not fetch sender")
        return self._sender


class FakeDialog:
    def __init__(self, id, name, entity, unread_count=0):
        self.id = id
        self.name = name
        self.entity = entity
        self.unread_count = unread_count


ALEX = FakeUser(1001, "Alex", "Chen", username="alexc")
JORDAN = FakeUser(2001, "Jordan")
SAM = FakeUser(2002, "Sam")

DIRECT_CHAT_ID = 1001
GROUP_CHAT_ID = -1002

FAKE_DIALOGS = [
    FakeDialog(DIRECT_CHAT_ID, "Alex Chen", ALEX, unread_count=2),
    FakeDialog(GROUP_CHAT_ID, "Weekend Trip Planning", FakeChat(GROUP_CHAT_ID, "Weekend Trip Planning"), unread_count=5),
]

FAKE_MESSAGES = {
    DIRECT_CHAT_ID: [
        FakeMessage(501, NOW, "OMG did you see this?! 😂😂😂 check it out https://example.com/cool-video",
                    sender_id=1001, sender=ALEX),
        FakeMessage(500, NOW - timedelta(minutes=1), "brb, grabbing coffee ☕", sender_id=1001, sender=ALEX),
        FakeMessage(499, NOW - timedelta(minutes=5), "haha yeah for sure", sender_id=None, out=True),
        FakeMessage(498, NOW - timedelta(minutes=10), "heyyyyy long time no talk!! @alexc #reunion",
                    sender_id=1001, sender=None),  # sender lookup will fail -> "Unknown"
    ],
    GROUP_CHAT_ID: [
        FakeMessage(302, NOW - timedelta(minutes=2), "np, I'll bring the tent 🏕️ asap", sender_id=2001, sender=JORDAN),
        FakeMessage(301, NOW - timedelta(minutes=3), "ttyl everyone, heading out", sender_id=None, out=True),
        FakeMessage(300, NOW - timedelta(minutes=8), "lol same here 🤣", sender_id=2002, sender=SAM),
    ],
}


class FakeTelegramClient:
    """Minimal stand-in for telethon.TelegramClient's async surface."""

    async def iter_dialogs(self, limit=20):
        for dialog in FAKE_DIALOGS[:limit]:
            yield dialog

    async def get_entity(self, chat):
        chat_id = chat if isinstance(chat, int) else None
        for dialog in FAKE_DIALOGS:
            if dialog.id == chat_id or dialog.name == chat:
                return dialog.entity
        raise ValueError(f"No fake entity for chat={chat!r}")

    async def iter_messages(self, entity, limit=100, offset_date=None):
        chat_id = entity if isinstance(entity, int) else getattr(entity, "id", None)
        for msg in FAKE_MESSAGES.get(chat_id, [])[:limit]:
            yield msg


# ---------------------------------------------------------------------------
# DEMO 1 -- message_cleaner.py used on its own (no Telegram involved at all)
# ---------------------------------------------------------------------------

def demo_message_cleaner_standalone():
    print("=" * 70)
    print("DEMO 1: message_cleaner.py standalone")
    print("=" * 70)

    raw = "OMG this is soooo cool!! 😂 check https://example.com @alexc #wow"

    convert = mc.clean_text(raw, emoji_mode="convert")
    stripped = mc.clean_text(raw, emoji_mode="strip", strip_mentions=True, strip_hashtags=True)
    collapsed = mc.clean_text(raw, collapse_repeats=True)

    print(f"raw:        {raw!r}")
    print(f"convert:    {convert!r}")
    print(f"strip/etc:  {stripped!r}")
    print(f"collapsed:  {collapsed!r}")

    assert "laughing out loud" not in convert  # 'lol' wasn't in this text; sanity check abbrev scope
    assert "oh my god" in convert.lower()      # 'OMG' expanded
    assert "https://example.com" not in stripped
    assert "@alexc" not in stripped
    assert "#wow" not in stripped
    assert "soooo" not in collapsed            # collapsed down to 'soo'
    print("✓ clean_text options behave as expected\n")

    long_run = mc.normalize_repeated_chars("heyyyyyy nooooo committee")
    print(f"normalize_repeated_chars: {long_run!r}")
    assert "committee" in long_run  # short legitimate double 'tt' left alone
    assert "heyyyyyy" not in long_run
    print("✓ normalize_repeated_chars collapses elongation without mangling real words\n")


# ---------------------------------------------------------------------------
# DEMO 2 -- telegram_loader.py's read APIs, against the faked client
# ---------------------------------------------------------------------------

async def demo_telegram_loader():
    print("=" * 70)
    print("DEMO 2: telegram_loader.py (fake Telegram backend)")
    print("=" * 70)

    loader = await tl.TelegramMessageLoader.create()
    # Swap in the fake client instead of a real telethon connection.
    fake_client = FakeTelegramClient()
    loader._get_client = lambda phone_number: _immediate(fake_client)

    phone = "+10000000000"

    async with loader:
        dialogs = await loader.list_dialogs(phone, limit=10)
        print("list_dialogs():")
        for d in dialogs:
            print(f"  - {d.name} (id={d.dialog_id}, unread={d.unread_count}, "
                  f"group={d.is_group}, channel={d.is_channel}, user={d.is_user})")
        assert len(dialogs) == 2
        print("✓ list_dialogs returns both fake chats\n")

        incoming = await loader.fetch_recent_incoming(phone, chat=DIRECT_CHAT_ID, limit=3)
        print("fetch_recent_incoming(chat=Alex Chen, limit=3):")
        for m in incoming:
            print(f"  - [{m.message_id}] {m.sender_name}: {m.text!r} (out={m.is_outgoing})")
        assert all(not m.is_outgoing for m in incoming)  # outgoing message (499) filtered out
        assert any(m.sender_name is None for m in incoming)  # msg 498's sender lookup fails gracefully
        print("✓ outgoing messages filtered out; failed sender lookups degrade to None instead of raising\n")

        top_across_chats = await loader.fetch_recent_across_dialogs(
            phone, dialog_limit=10, per_chat_limit=10, top_n=5
        )
        print("fetch_recent_across_dialogs(top_n=5):")
        for m in top_across_chats:
            print(f"  - [{m.chat_title}] {m.sender_name}: {m.text!r} (at {m.date.strftime('%H:%M')})")
        assert len(top_across_chats) == 5
        assert list(top_across_chats) == sorted(top_across_chats, key=lambda m: m.date, reverse=True)
        print("✓ multi-chat scan returns newest-first across both chats\n")

        print("iter_messages() streaming, chat=Weekend Trip Planning:")
        streamed = []
        async for m in loader.iter_messages(phone, chat=GROUP_CHAT_ID, limit=10):
            streamed.append(m)
            print(f"  - [{m.message_id}] {m.sender_name or 'Unknown'}: {m.text!r}")
        assert len(streamed) == len(FAKE_MESSAGES[GROUP_CHAT_ID])
        print("✓ iter_messages streams every message in the chat\n")

    return top_across_chats


async def _immediate(value):
    return value


# ---------------------------------------------------------------------------
# DEMO 3 -- the two modules working together: fetch -> clean -> chunk -> report
# ---------------------------------------------------------------------------

async def demo_full_pipeline():
    print("=" * 70)
    print("DEMO 3: fetch -> clean -> chunk -> report (both modules together)")
    print("=" * 70)

    loader = await tl.TelegramMessageLoader.create()
    loader._get_client = lambda phone_number: _immediate(FakeTelegramClient())
    phone = "+10000000000"

    async with loader:
        raw_messages = await loader.fetch_recent_across_dialogs(
            phone, dialog_limit=10, per_chat_limit=10, top_n=20
        )

    # Clean everything, dropping trivial one-word messages, then chunk for
    # embedding, then build the same report format the old fetch_test*.py
    # scripts hand-rolled.
    cleaned = mc.clean_messages(
        raw_messages,
        emoji_mode="convert",
        collapse_repeats=True,
        min_length=5,
    )
    print(f"{len(raw_messages)} raw messages -> {len(cleaned)} cleaned (short/empty ones dropped)")
    for c in cleaned:
        print(f"  - [{c.chat_title}] {c.sender_name or 'Unknown'}: {c.text!r}")

    chunks = mc.chunk_cleaned_messages(cleaned, chunk_size=120, overlap=20)
    print(f"\nchunk_cleaned_messages() -> {len(chunks)} chunk(s)")
    for chunk in chunks:
        print(f"  chunk {chunk.chunk_index} ({len(chunk.text)} chars, "
              f"messages {chunk.source_message_ids}):")
        print(f"    {chunk.text!r}")

    report = mc.format_messages_report(cleaned, title="What you missed today")
    print("\nformat_messages_report():\n")
    print(report)

    out_path = os.path.join(os.path.dirname(__file__), "test_without_database.txt")
    mc.append_report_to_file(report, out_path)
    print(f"✓ append_report_to_file() wrote the report to {out_path}\n")

    assert os.path.exists(out_path)
    with open(out_path, encoding="utf-8") as f:
        assert "What you missed today" in f.read()
    print("✓ report file contains the expected content\n")


# ---------------------------------------------------------------------------

async def main():
    demo_message_cleaner_standalone()
    await demo_telegram_loader()
    await demo_full_pipeline()
    print("=" * 70)
    print("All demos completed successfully.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())