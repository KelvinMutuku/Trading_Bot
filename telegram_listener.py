"""
telegram_listener.py

Logs into your Telegram account (user session, not a bot) and listens for
new messages in the channels you picked via setup_channels.py. 
Deduplicates signals based on asset, direction, entry time, and expiration.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events

from config import load_selected_channels, save_selected_channels, telegram_cfg, trading_cfg
from signal_parser import parse_signal
from state import state, ChannelInfo

logger = logging.getLogger("telegram_listener")


class PendingMessageBuffer:
    """Some channels split one signal across multiple messages (e.g. the
    asset/expiry posted first, direction posted a minute or two later).
    This holds, per channel, the most recent message that failed to parse
    on its own, so the next message from that channel can be tried
    combined with it before being treated as its own standalone signal."""

    def __init__(self, window_seconds: float):
        self.window_seconds = window_seconds
        self._pending: dict[int, tuple[str, datetime]] = {}

    def resolve(self, chat_id: int, text: str, msg_date: datetime):
        """Returns a valid ParsedSignal if `text` parses alone, or combined
        with a recent pending message from the same channel. Otherwise
        stores `text` as the new pending message for that channel and
        returns None."""
        signal = parse_signal(text)
        if signal.is_valid:
            self._pending.pop(chat_id, None)
            return signal

        pending = self._pending.get(chat_id)
        if pending:
            pending_text, pending_date = pending
            age = abs((msg_date - pending_date).total_seconds())
            if age <= self.window_seconds:
                for combined_text in (f"{pending_text}\n{text}", f"{text}\n{pending_text}"):
                    combined = parse_signal(combined_text)
                    if combined.is_valid:
                        logger.info(
                            "Combined two messages in channel %s into one valid signal (%s)",
                            chat_id, combined.asset,
                        )
                        self._pending.pop(chat_id, None)
                        return combined

        self._pending[chat_id] = (text, msg_date)
        return None

def get_signal_signature(signal, message_date: datetime) -> str:
    """
    Creates a unique fingerprint for a signal based on its core trade parameters.
    Format: YYYY-MM-DD:ASSET:DIRECTION:ENTRY_TIME:EXPIRATION
    """
    date_key = message_date.strftime("%Y-%m-%d")
    asset_key = signal.normalized_asset or signal.asset or "UNKNOWN"
    entry_key = signal.entry_time if signal.entry_time else f"immediate_{message_date.strftime('%H:%M')}"
    direction_key = signal.direction or "UNKNOWN"
    exp_key = str(signal.expiration_minutes or 0)
    
    return f"{date_key}:{asset_key}:{direction_key}:{entry_key}:{exp_key}".upper()


def should_execute_signal(signal, message_date: datetime) -> bool:
    """
    Checks if a signal falls within the strict execution window:
    - Immediate trades must be fresh (<60s old).
    - Scheduled trades must execute between -30s (brief network lag) and 600s (10 mins) from now.
    """
    now_utc = datetime.now(timezone.utc)
    message_age = (now_utc - message_date).total_seconds()

    # 1. Handle immediate "trade right now" signals
    if signal.immediate:
        if message_age <= 60:
            return True
        logger.info("Skipping expired immediate signal (age: %ds)", int(message_age))
        return False

    # 2. Handle scheduled signals
    if signal.entry_time:
        target_h, target_m = map(int, signal.entry_time.split(":"))
        tz_offset = signal.utc_offset_hours if signal.utc_offset_hours is not None else float(os.getenv("SIGNAL_UTC_OFFSET_HOURS", 0))
        
        # Reconstruct target execution time in UTC using the message date as anchor
        target_time_local = message_date.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        target_time_utc = target_time_local - timedelta(hours=tz_offset)
        
        wait_seconds = (target_time_utc - now_utc).total_seconds()
        
        # Enforce execution window: -30s to +600s (10 minutes)
        if -30 <= wait_seconds <= 600:
            return True
            
        logger.info("Skipping %s. Wait time %ds is outside the 10-minute window.", signal.asset, int(wait_seconds))
        return False
        
    return False


class TelegramSignalListener:
    def __init__(self, on_signal_callback):
        self.client = TelegramClient(
            telegram_cfg.session_name, telegram_cfg.api_id, telegram_cfg.api_hash
        )
        self.on_signal_callback = on_signal_callback
        self.selected_channel_ids: set[int] = set(load_selected_channels())

    def update_selected_channels(self, channel_ids: list[int]) -> None:
        """Called from the web dashboard. Takes effect immediately -- the
        live message handler checks this set on every message rather than
        being registered against a fixed list, so no restart is needed."""
        self.selected_channel_ids = set(channel_ids)
        save_selected_channels(channel_ids)
        for c in state.channels:
            c.selected = c.id in self.selected_channel_ids
        state.push_event("channels_updated", f"Monitoring {len(channel_ids)} channel(s)")
        logger.info("Selected channels updated: %s", channel_ids)

    async def _load_available_channels(self) -> None:
        from telethon.tl.types import Channel

        dialogs = await self.client.get_dialogs()
        channels = [d for d in dialogs if isinstance(d.entity, Channel)]
        state.channels = [
            ChannelInfo(id=d.entity.id, name=d.name, selected=d.entity.id in self.selected_channel_ids)
            for d in channels
        ]
        logger.info("Loaded %d available channel(s) for the dashboard.", len(state.channels))

    async def start(self):
        await self.client.start()
        await self._load_available_channels()

        if not self.selected_channel_ids:
            logger.warning(
                "No channels selected yet. Pick some from the dashboard, "
                "or run `python setup_channels.py`. Waiting..."
            )

        buffer = PendingMessageBuffer(trading_cfg.multi_message_window_seconds)

        # In-memory storage for deduplication
        processed_message_ids = set()
        executed_signal_signatures = set()

        # Catch up on missed signals first
        for channel_id in list(self.selected_channel_ids):
            logger.info("Checking for unexecuted signals in channel %s...", channel_id)
            try:
                # Fetch recent history, then process oldest-first so
                # multi-part signals combine in the order they were posted.
                recent = [m async for m in self.client.iter_messages(channel_id, limit=15)]
                for message in reversed(recent):
                    if not message.text or message.id in processed_message_ids:
                        continue

                    processed_message_ids.add(message.id)

                    signal = buffer.resolve(channel_id, message.text, message.date)
                    if signal and should_execute_signal(signal, message.date):
                        sig_id = get_signal_signature(signal, message.date)
                        if sig_id in executed_signal_signatures:
                            logger.info("Duplicate signal detected and skipped: %s", sig_id)
                            continue

                        executed_signal_signatures.add(sig_id)
                        logger.info("Recovered valid missed signal: %s (Sig: %s)", signal.asset, sig_id)
                        asyncio.create_task(self.on_signal_callback(signal))
            except Exception as e:
                logger.error("Failed to fetch history for %s: %s", channel_id, e)

        # Listen for live signals across ALL channels the account belongs to
        # -- filtering happens inside the handler against the *current*
        # self.selected_channel_ids, so toggling channels on the dashboard
        # takes effect immediately without re-registering this handler.
        @self.client.on(events.NewMessage())
        async def _handler(event):
            if event.chat_id not in self.selected_channel_ids:
                return

            processed_message_ids.add(event.message.id)
            text = event.raw_text or ""
            signal = buffer.resolve(event.chat_id, text, event.message.date)

            if signal:
                if should_execute_signal(signal, event.message.date):
                    sig_id = get_signal_signature(signal, event.message.date)
                    if sig_id in executed_signal_signatures:
                        logger.info("Duplicate signal ignored (already executing): %s", sig_id)
                        return

                    executed_signal_signatures.add(sig_id)
                    logger.info("Executing live signal: %s (Sig: %s)", signal.asset, sig_id)
                    asyncio.create_task(self.on_signal_callback(signal))
                else:
                    logger.info("Live signal ignored due to timeframe rules.")

        logger.info("Listening on %d channel(s)...", len(self.selected_channel_ids))
        await self.client.run_until_disconnected()