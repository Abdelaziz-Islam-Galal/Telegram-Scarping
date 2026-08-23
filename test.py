import asyncio
import os
from telegram_loader import TelegramMessageLoader
from message_cleaner import clean_messages, chunk_cleaned_messages
from telegram_auth_service import TelegramAuthRepository

async def run_pipeline(phone_number: str, chat: str):
    # 1. Initialize DB repository table setup inside the loop
    repo = await TelegramAuthRepository.create()
    
    loader = await TelegramMessageLoader.create()
    try:
        messages = await loader.fetch_messages(phone_number, chat, limit=500)
        messages.reverse()  # oldest first, for sensible chunk overlap
        cleaned = clean_messages(messages)
        chunks = chunk_cleaned_messages(cleaned, chunk_size=1000, overlap=150)
        return chunks
    finally:
        # 2. Safely close connections
        await loader.close()
        await repo.close()

async def main():
    phone_number = "+201148266467"
    chat = "Mama"
    
    chunks = await run_pipeline(phone_number, chat)

    text = ""
    for i, chunk in enumerate(chunks):
        text += f"Chunk {i+1} ({len(chunk.text)} messages):" 
        text += f"[index: {chunk.chunk_index}] id={chunk.chat_id}\n"
        text += f"  {chunk.text}\n"
        for j, msg_id in enumerate(chunk.source_message_ids):
            text += f"    [{j+1}] source message id: {msg_id}\n"
        text += "\n" + "-"*40 + "\n"

    with open("output.txt", "w", encoding="utf-8") as f:
        f.write(text)

if __name__ == "__main__":
    asyncio.run(main())