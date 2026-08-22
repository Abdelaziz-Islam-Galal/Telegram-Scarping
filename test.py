import asyncio
from telegram_loader import TelegramMessageLoader
from message_cleaner import clean_messages, chunk_cleaned_messages

async def run_pipeline(phone_number: str, chat: str):
    loader = await TelegramMessageLoader.create()
    try:
        messages = await loader.fetch_messages(phone_number, chat, limit=500)
        messages.reverse()  # oldest first, for sensible chunk overlap
        cleaned = clean_messages(messages)
        chunks = chunk_cleaned_messages(cleaned, chunk_size=1000, overlap=150)
        return chunks
    finally:
        await loader.close()

chunks = asyncio.run(run_pipeline("+201095035672", "Telegram"))

for i, chunk in enumerate(chunks):
    print(f"Chunk {i+1} ({len(chunk.text)} messages):") 
    print(f"[index: {chunk.chunk_index}] id={chunk.chat_id}\n")
    print(f"  {chunk.text}\n")
    for j, id in enumerate(chunk.source_message_ids):
        print(f"    [{j+1}] source message id: {id}")
    print("\n" + "-"*40 + "\n")