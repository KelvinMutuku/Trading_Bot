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

from config import bare_channel_id, load_channel_offsets, load_selected_channels, telegram_cfg, trading_cfg
from signal_parser import parse_signal

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



def _tag(signal) -> str:
    return f"[{signal.source_channel}] " if getattr(signal, "source_channel", None) else ""


def tag_channel(signal, chat_id: int, names: dict[int, str]) -> None:
    """Records which channel a signal came from (by name, falling back to the
    id) so every later log line can show it."""
    bare = bare_channel_id(chat_id)
    signal.source_channel = names.get(bare) or f"channel {bare}"


def apply_channel_offset(signal, chat_id: int) -> None:
    """Gives the signal its channel's UTC offset -- but only if the message
    didn't state its own timezone (a zone written in the message always wins).
    Precedence: message > per-channel setting > SIGNAL_UTC_OFFSET_HOURS.
    The file is re-read here, so you can edit channel_offsets.json without
    restarting the bot."""
    if signal.utc_offset_hours is not None:
        return
    offset = load_channel_offsets().get(bare_channel_id(chat_id))
    if offset is not None:
        signal.utc_offset_hours = offset
        signal.offset_source = "channel"
        logger.info("%sUsing the per-channel UTC offset %+g", _tag(signal) or f"[channel {bare_channel_id(chat_id)}] ", offset)


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
        logger.info("%sSkipping expired immediate signal (age: %ds)", _tag(signal), int(message_age))
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
            
        logger.info("%sSkipping %s. Wait time %ds is outside the 10-minute window.", _tag(signal), signal.asset, int(wait_seconds))
        return False
        
    return False


class TelegramSignalListener:
    def __init__(self, on_signal_callback):
        self.client = TelegramClient(
            telegram_cfg.session_name, telegram_cfg.api_id, telegram_cfg.api_hash
        )
        self.on_signal_callback = on_signal_callback
        self.channel_names: dict[int, str] = {}

    async def _load_channel_names(self) -> None:
        """Maps channel id -> display name so logs can say which channel a
        signal came from. Failure here only costs the names (ids are shown
        instead); it never stops the bot."""
        self.channel_names = {}
        try:
            for d in await self.client.get_dialogs():
                self.channel_names[bare_channel_id(d.entity.id)] = d.name
        except Exception as exc:
            logger.warning("Couldn't load channel names (%s); logs will show channel ids instead.", exc)

    async def start(self):
        channel_ids = load_selected_channels()
        if not channel_ids:
            raise RuntimeError(
                "No channels selected. Run `python setup_channels.py` first."
            )

        await self.client.start()
        await self._load_channel_names()

        buffer = PendingMessageBuffer(trading_cfg.multi_message_window_seconds)

        # In-memory storage for deduplication
        processed_message_ids = set()
        executed_signal_signatures = set()

        # Catch up on missed signals first
        for channel_id in channel_ids:
            logger.info("Checking for unexecuted signals in %s...", self.channel_names.get(bare_channel_id(channel_id)) or f"channel {channel_id}")
            try:
                # Fetch recent history, then process oldest-first so
                # multi-part signals combine in the order they were posted.
                recent = [m async for m in self.client.iter_messages(channel_id, limit=15)]
                for message in reversed(recent):
                    if not message.text or message.id in processed_message_ids:
                        continue

                    processed_message_ids.add(message.id)

                    signal = buffer.resolve(channel_id, message.text, message.date)
                    if signal:
                        tag_channel(signal, channel_id, self.channel_names)
                        apply_channel_offset(signal, channel_id)
                    if signal and should_execute_signal(signal, message.date):
                        sig_id = get_signal_signature(signal, message.date)
                        if sig_id in executed_signal_signatures:
                            logger.info("%sDuplicate signal detected and skipped: %s", _tag(signal), sig_id)
                            continue

                        executed_signal_signatures.add(sig_id)
                        logger.info("%sRecovered valid missed signal: %s (Sig: %s)", _tag(signal), signal.asset, sig_id)
                        asyncio.create_task(self.on_signal_callback(signal))
            except Exception as e:
                logger.error("Failed to fetch history for %s: %s", channel_id, e)

        # Listen for live signals
        @self.client.on(events.NewMessage(chats=channel_ids))
        async def _handler(event):
            processed_message_ids.add(event.message.id)
            text = event.raw_text or ""
            signal = buffer.resolve(event.chat_id, text, event.message.date)
            if signal:
                tag_channel(signal, event.chat_id, self.channel_names)
                apply_channel_offset(signal, event.chat_id)

            if signal:
                if should_execute_signal(signal, event.message.date):
                    sig_id = get_signal_signature(signal, event.message.date)
                    if sig_id in executed_signal_signatures:
                        logger.info("%sDuplicate signal ignored (already executing): %s", _tag(signal), sig_id)
                        return

                    executed_signal_signatures.add(sig_id)
                    logger.info("%sExecuting live signal: %s (Sig: %s)", _tag(signal), signal.asset, sig_id)
                    asyncio.create_task(self.on_signal_callback(signal))
                else:
                    logger.info("%sLive signal ignored due to timeframe rules.", _tag(signal))

        logger.info(
            "Listening on %d channel(s): %s",
            len(channel_ids),
            ", ".join(self.channel_names.get(bare_channel_id(c)) or str(c) for c in channel_ids),
        )
        await self.client.run_until_disconnected()