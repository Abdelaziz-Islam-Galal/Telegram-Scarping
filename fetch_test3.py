import asyncio
import sys

from message_cleaner import clean_text
from telegram_loader import TelegramMessageLoader

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def choose_dialog(client):
    dialogs = []
    async for dialog in client.iter_dialogs(limit=10):
        dialogs.append(dialog)

    if not dialogs:
        raise RuntimeError("No Telegram chats were found.")

    print("Choose a chat:")
    for index, dialog in enumerate(dialogs, 1):
        print(f"{index}. {dialog.name} (chat id: {dialog.id})")

    while True:
        choice = input("Enter the chat number: ").strip()
        try:
            dialog_index = int(choice) - 1
        except ValueError:
            print("Please enter a number from the list.")
            continue

        if 0 <= dialog_index < len(dialogs):
            return dialogs[dialog_index]
        print(f"Please enter a number from 1 to {len(dialogs)}.")


async def main():
    loader = await TelegramMessageLoader.create()
    phone_number = "+201127766006"  # REPLACE WITH YOUR PHONE NUMBER
    client = await loader._get_client(phone_number)

    try:
        dialog = await choose_dialog(client)
        incoming_messages = []

        # Fetch the five newest incoming messages from the selected chat.
        async for msg in client.iter_messages(dialog.id, limit=5):
            if msg.text and not msg.out:
                incoming_messages.append(msg)

        output_lines = [
            f"Recent incoming messages from {dialog.name}",
            "=" * 40,
            "",
        ]

        if not incoming_messages:
            output_lines.append("No recent incoming text messages found.")

        for index, msg in enumerate(incoming_messages, 1):
            output_lines.extend(
                [
                    f"{index}. From: {dialog.name} (at {msg.date.strftime('%I:%M %p')})",
                    f"   Message: {clean_text(msg.text, emoji_mode='keep')}",
                    f"   [chat id: {dialog.id}]",
                    f"   source message id: {msg.id}",
                    "",
                    "-" * 50,
                    "",
                ]
            )

        with open("output3.txt", "a", encoding="utf-8") as output_file:
            output_file.write("\n\n" + "\n".join(output_lines))

        print("Saved results to output3.txt")
    finally:
        await client.disconnect()
        await loader.close()


if __name__ == "__main__":
    asyncio.run(main())
