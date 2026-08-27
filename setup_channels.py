"""
setup_channels.py

Run this once (and again any time you want to change which channels are
monitored). It logs into YOUR Telegram account -- a user login is required
because bot accounts can't read messages in channels they don't admin --
lists the channels/groups you belong to, and saves your picks.

    python setup_channels.py
"""

import asyncio

from telethon import TelegramClient
from telethon.tl.types import Channel

from config import save_selected_channels, telegram_cfg


async def main():
    client = TelegramClient(telegram_cfg.session_name, telegram_cfg.api_id, telegram_cfg.api_hash)
    await client.start()  # prompts for phone number + login code the first time

    dialogs = await client.get_dialogs()
    channels = [d for d in dialogs if isinstance(d.entity, Channel)]

    if not channels:
        print("No channels found on this account.")
        await client.disconnect()
        return

    print("\nChannels you belong to:\n")
    for i, d in enumerate(channels):
        print(f"  [{i}] {d.name}  (id={d.entity.id})")

    raw = input("\nEnter the numbers of the channels to monitor, comma-separated (e.g. 0,2,5): ")
    indices = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    selected_ids = [channels[i].entity.id for i in indices if 0 <= i < len(channels)]

    save_selected_channels(selected_ids)
    print(f"\nSaved {len(selected_ids)} channel(s) to selected_channels.json")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())