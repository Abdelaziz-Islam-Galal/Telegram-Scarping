"""
test_live_telegram.py

Exercises message_cleaner.py and telegram_loader.py against a REAL
Telegram account. Unlike test_usage.py (which fakes everything and
needs no credentials), this hits the network and your database.

Credentials are looked up by phone number only -- TelegramMessageLoader
uses TelegramAuthRepository under the hood to turn `phone_number` into
a saved session string, exactly like fetch_test.py / fetch_test3.py did.
You must already have a saved session for this number (run your
telegram_auth.py flow first if not).

Run:
    python3 test_live_telegram.py
"""

import asyncio
import sys

from message_cleaner import (
    append_report_to_file,
    chunk_cleaned_messages,
    clean_messages,
    format_messages_report,
)
from telegram_loader import TelegramMessageLoader

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PHONE_NUMBER = "+20..."  # REPLACE WITH YOUR PHONE NUMBER
OUTPUT_FILE = "everything_test_output.txt"


async def demo_list_dialogs(loader: TelegramMessageLoader):
    print("=" * 70)
    print("1. list_dialogs() -- your most recently active chats")
    print("=" * 70)
    dialogs = await loader.list_dialogs(PHONE_NUMBER, limit=10)
    for d in dialogs:
        kind = "channel" if d.is_channel else "group" if d.is_group else "DM"
        print(f"  - {d.name}  [{kind}, id={d.dialog_id}, unread={d.unread_count}]")
    if not dialogs:
        print("  (no chats found)")
    print()
    return dialogs


async def demo_fetch_recent_incoming(loader: TelegramMessageLoader, chat_id: int, chat_name: str):
    print("=" * 70)
    print(f"2. fetch_recent_incoming() -- newest incoming messages from '{chat_name}'")
    print("=" * 70)
    incoming = await loader.fetch_recent_incoming(PHONE_NUMBER, chat=chat_id, limit=5)
    for m in incoming:
        print(f"  - [{m.message_id}] {m.sender_name or 'Unknown'} (at {m.date.strftime('%I:%M %p')}): "
              f"{m.text[:80]!r}")
    if not incoming:
        print("  (no recent incoming messages)")
    print()
    return incoming


async def demo_fetch_across_dialogs(loader: TelegramMessageLoader):
    print("=" * 70)
    print("3. fetch_recent_across_dialogs() -- newest messages scanned across all recent chats")
    print("=" * 70)
    top_across = await loader.fetch_recent_across_dialogs(
        PHONE_NUMBER, dialog_limit=10, per_chat_limit=5, top_n=10
    )
    for m in top_across:
        print(f"  - [{m.chat_title}] {m.sender_name or 'Unknown'} (at {m.date.strftime('%I:%M %p')}): "
              f"{m.text[:80]!r}")
    if not top_across:
        print("  (nothing found across your recent chats)")
    print()
    return top_across


async def demo_raw_client_access(loader: TelegramMessageLoader):
    """
    Escape hatch: grab the underlying telethon client directly when you
    need something the loader doesn't expose yet. Same credential lookup
    as everything above -- still just phone_number -> session string.
    """
    print("=" * 70)
    print("4. Raw client access -- for anything not covered by the loader's API")
    print("=" * 70)
    client = await loader._get_client(PHONE_NUMBER)
    me = await client.get_me()
    print(f"  Connected as: {getattr(me, 'first_name', None) or getattr(me, 'username', 'you')}")
    print()


async def demo_pipeline(cleaned_source):
    print("=" * 70)
    print("5. clean -> chunk -> report -> save (full pipeline on live data)")
    print("=" * 70)
    cleaned = clean_messages(
        cleaned_source,
        emoji_mode="convert",
        strip_urls=True,
        collapse_repeats=True,
        min_length=2,
    )
    print(f"{len(cleaned_source)} raw messages -> {len(cleaned)} after cleaning/min_length filter")

    chunks = chunk_cleaned_messages(cleaned, chunk_size=800, overlap=100)
    print(f"chunk_cleaned_messages() -> {len(chunks)} chunk(s)")

    report = format_messages_report(cleaned, title="Recent Telegram activity")
    print("\n" + report)

    append_report_to_file(report, OUTPUT_FILE)
    print(f"Saved report to {OUTPUT_FILE}")


async def main():
    loader = await TelegramMessageLoader.create()
    try:
        dialogs = await demo_list_dialogs(loader)
        if not dialogs:
            print("Nothing to do without any chats.")
            return

        first = dialogs[0]
        await demo_fetch_recent_incoming(loader, chat_id=first.dialog_id, chat_name=first.name)
        top_across = await demo_fetch_across_dialogs(loader)
        await demo_raw_client_access(loader)
        await demo_pipeline(top_across)
    finally:
        # Disconnects every cached client (all phone numbers used this run)
        # and closes the DB connection pool.
        await loader.close()


if __name__ == "__main__":
    asyncio.run(main())