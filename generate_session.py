import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()
api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nYour Telethon StringSession:\n")
    print(client.session.save())
    print("\nKeep it private. Paste it into the bot's ➕ Add Session menu.\n")
