"""
setup_channels.py

Run this once (and again any time you want to change which channels are
monitored or their timezone settings). It logs into YOUR Telegram account --
a user login is required because bot accounts can't read messages in channels
they don't admin -- lists the channels/groups you belong to, saves your
picks, and asks each picked channel's timezone offset.

    python setup_channels.py

Timezone offset: some channels post entry times in their own clock without
saying which timezone it is (e.g. "Entry at 08:20"). Enter that clock's UTC
offset in hours -- 0 for UTC, -3 for UTC-3, 4 for UTC+4. Press Enter to keep
the current value (or to use the SIGNAL_UTC_OFFSET_HOURS default from .env if
none is set). A timezone written inside a signal always overrides this.

You can also edit channel_offsets.json by hand; changes apply without a
restart.
"""

import asyncio

from telethon import TelegramClient
from telethon.tl.types import Channel

from config import (
    load_channel_offsets,
    save_channel_offsets,
    save_selected_channels,
    telegram_cfg,
    trading_cfg,
)


def ask_offset(name: str, current: float | None) -> float | None:
    default_text = f"{current:+g}" if current is not None else f"none, uses .env default {trading_cfg.signal_utc_offset_hours:+g}"
    while True:
        raw = input(f"  UTC offset for '{name}' [{default_text}] (or 'clear'): ").strip().lower()
        if raw == "":
            return current
        if raw == "clear":
            return None
        try:
            value = float(raw)
        except ValueError:
            print("    Enter a number like 0, -3 or 4 (half-hours like 5.5 are fine).")
            continue
        if not -12 <= value <= 14:
            print("    Offsets run from -12 to +14.")
            continue
        return value


async def main():
    client = TelegramClient(telegram_cfg.session_name, telegram_cfg.api_id, telegram_cfg.api_hash)
    await client.start()  # prompts for phone number + login code the first time

    dialogs = await client.get_dialogs()
    channels = [d for d in dialogs if isinstance(d.entity, Channel)]

    if not channels:
        print("No channels found on this account.")
        await client.disconnect()
        return

    offsets = load_channel_offsets()

    print("\nChannels you belong to:\n")
    for i, d in enumerate(channels):
        tag = f"  [UTC{offsets[d.entity.id]:+g}]" if d.entity.id in offsets else ""
        print(f"  [{i}] {d.name}  (id={d.entity.id}){tag}")

    raw = input("\nEnter the numbers of the channels to monitor, comma-separated (e.g. 0,2,5): ")
    indices = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    picked = [channels[i] for i in indices if 0 <= i < len(channels)]
    selected_ids = [d.entity.id for d in picked]

    save_selected_channels(selected_ids)
    print(f"\nSaved {len(selected_ids)} channel(s) to selected_channels.json")

    if picked:
        print("\nTimezone of each channel's clock (see the note at the top of this file):")
        for d in picked:
            value = ask_offset(d.name, offsets.get(d.entity.id))
            if value is None:
                offsets.pop(d.entity.id, None)
            else:
                offsets[d.entity.id] = value
        save_channel_offsets(offsets)
        print("Saved timezone settings to channel_offsets.json")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
