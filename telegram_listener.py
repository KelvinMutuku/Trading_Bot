"""
telegram_listener.py

Logs into your Telegram account (user session, not a bot) and listens for
new messages in the channels you picked via setup_channels.py. Each new
message is parsed and, if valid, handed off to the executor as an asyncio
task (so a slow/long martingale sequence for one signal never blocks the
next incoming signal).
"""

import asyncio
import logging

from telethon import TelegramClient, events

from config import load_selected_channels, telegram_cfg
from signal_parser import parse_signal

logger = logging.getLogger("telegram_listener")


class TelegramSignalListener:
    def __init__(self, on_signal_callback):
        """on_signal_callback: async function taking a ParsedSignal."""
        self.client = TelegramClient(
            telegram_cfg.session_name, telegram_cfg.api_id, telegram_cfg.api_hash
        )
        self.on_signal_callback = on_signal_callback

    async def start(self):
        channel_ids = load_selected_channels()
        if not channel_ids:
            raise RuntimeError(
                "No channels selected. Run `python setup_channels.py` first."
            )

        await self.client.start()

        @self.client.on(events.NewMessage(chats=channel_ids))
        async def _handler(event):
            text = event.raw_text or ""
            signal = parse_signal(text)
            logger.info("New message in %s -> valid=%s asset=%s", event.chat_id, signal.is_valid, signal.asset)
            if signal.is_valid:
                asyncio.create_task(self.on_signal_callback(signal))

        logger.info("Listening on %d channel(s)...", len(channel_ids))
        await self.client.run_until_disconnected()