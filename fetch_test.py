import asyncio
import sys

from message_cleaner import clean_text
from telegram_loader import TelegramMessageLoader

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

async def main():
    # 1. Connect to the database
    loader = await TelegramMessageLoader.create()
    phone_number = "+201127766006" # REPLACE WITH YOUR PHONE NUMBER
    
    # 2. Access the raw Telethpython fetch_test.pyon client directly
    client = await loader._get_client(phone_number)
    
    try:
        incoming_messages = []
        
        # Look at your 10 most recently active chats (dialogs)
        async for dialog in client.iter_dialogs(limit=10):
            
            # Pull the last 5 messages from each of these chats
            async for msg in client.iter_messages(dialog.id, limit=5):
                
                # msg.out is True if YOU sent it. We only want messages sent TO you.
                # We also make sure the message actually has text.
                if msg.text and not msg.out:
                    incoming_messages.append(msg)
        
        # Sort all collected messages by date (newest first)
        incoming_messages.sort(key=lambda x: x.date, reverse=True)
        
        # Take the absolute top 5 newest messages
        top_5 = incoming_messages[:5]

        output_lines = [
            "Recent incoming Telegram messages",
            "=" * 40,
            "",
        ]

        if not top_5:
            output_lines.append("No recent text messages found.")

        for i, msg in enumerate(top_5, 1):
            # Get the name of the person or group who sent it
            sender = await msg.get_sender()
            name = getattr(sender, 'first_name', getattr(sender, 'title', 'Unknown'))
            
            output_lines.extend(
                [
                    f"{i}. From: {name} (at {msg.date.strftime('%I:%M %p')})",
                    f"   Message: {clean_text(msg.text, emoji_mode='convert')}",
                    f"   [chat id: {msg.chat_id}]",
                    f"   source message id: {msg.id}",
                    "",
                    "-" * 50,
                    "",
                ]
            )

        with open("output.txt", "a", encoding="utf-8") as output_file:
            output_file.write("\n\n" + "\n".join(output_lines))

        print("Saved results to output.txt")
            
    finally:
        # Clean up connections
        await client.disconnect()
        await loader.close()

if __name__ == "__main__":
    asyncio.run(main())