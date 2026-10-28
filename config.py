"""
config.py

All settings load from environment variables (see .env.example).
Keeping secrets out of source code means you can commit this repo safely
and just manage the .env file per-deployment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SELECTED_CHANNELS_FILE = BASE_DIR / "selected_channels.json"
CHANNEL_OFFSETS_FILE = BASE_DIR / "channel_offsets.json"


@dataclass
class TelegramConfig:
    api_id: int = int(os.getenv("TG_API_ID", "0"))
    api_hash: str = os.getenv("TG_API_HASH", "")
    session_name: str = os.getenv("TG_SESSION_NAME", "trader_session")


@dataclass
class PocketOptionConfig:
    session: str = os.getenv("PO_SESSION", "")
    uid: int = int(os.getenv("PO_UID", "0"))
    is_demo: bool = os.getenv("PO_IS_DEMO", "1") == "1"


@dataclass
class TradingConfig:
    starting_stake: float = float(os.getenv("STARTING_STAKE", "1.0"))
    martingale_multiplier: float = float(os.getenv("MARTINGALE_MULTIPLIER", "2.0"))
    max_martingale_level: int = int(os.getenv("MAX_MARTINGALE_LEVEL", "3"))
    max_daily_loss: float | None = (
        float(os.getenv("MAX_DAILY_LOSS")) if os.getenv("MAX_DAILY_LOSS") else None
    )
    default_expiration_seconds: int = int(os.getenv("DEFAULT_EXPIRATION_SECONDS", "300"))
    # Hours offset from UTC that your signal channels use when stating times
    # (e.g. a channel showing "Time Zone: UTC -3" -> set this to -3).
    # If different channels use different zones, you'll need a per-channel
    # mapping instead of one global value -- ask if you need that.
    signal_utc_offset_hours: float = float(os.getenv("SIGNAL_UTC_OFFSET_HOURS", "0"))
    # Safety cap: if the computed wait until an entry time exceeds this,
    # something is misconfigured (timezone, stale signal) -- skip instead
    # of silently sleeping for hours.
    max_entry_wait_seconds: int = int(os.getenv("MAX_ENTRY_WAIT_SECONDS", "3600"))
    # Some channels split one signal across multiple messages (e.g. asset
    # posted first, direction posted a minute or two later). If a message
    # fails to parse on its own, the bot tries combining it with the
    # previous unresolved message from the same channel, as long as it
    # arrived within this many seconds.
    multi_message_window_seconds: int = int(os.getenv("MULTI_MESSAGE_WINDOW_SECONDS", "180"))


def load_selected_channels() -> list[int]:
    """Channel IDs the user picked via the setup CLI (setup_channels.py)."""
    if not SELECTED_CHANNELS_FILE.exists():
        return []
    return json.loads(SELECTED_CHANNELS_FILE.read_text()).get("channel_ids", [])


def save_selected_channels(channel_ids: list[int]) -> None:
    SELECTED_CHANNELS_FILE.write_text(json.dumps({"channel_ids": channel_ids}, indent=2))


def bare_channel_id(chat_id: int) -> int:
    """Telethon reports channels two ways: the bare id (1765226347, what
    setup_channels.py saves) and the 'marked' id (-1001765226347, what live
    events carry). Both mean the same channel; this returns the bare form."""
    digits = str(abs(int(chat_id)))
    if len(digits) >= 13 and digits.startswith("100"):
        return int(digits[3:])
    return int(digits)


def load_channel_offsets() -> dict[int, float]:
    """Per-channel UTC offset (hours) for signals that don't state their own
    timezone. Maps bare channel id -> offset. A missing/unreadable file or a
    bad entry just means 'no override' -- it never stops the bot."""
    try:
        raw = json.loads(CHANNEL_OFFSETS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    offsets: dict[int, float] = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        try:
            offsets[bare_channel_id(int(key))] = float(value)
        except (TypeError, ValueError):
            continue
    return offsets


def save_channel_offsets(offsets: dict[int, float]) -> None:
    CHANNEL_OFFSETS_FILE.write_text(
        json.dumps({str(k): v for k, v in sorted(offsets.items())}, indent=2)
    )


telegram_cfg = TelegramConfig()
pocket_option_cfg = PocketOptionConfig()
trading_cfg = TradingConfig()
