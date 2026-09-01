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


def load_selected_channels() -> list[int]:
    """Channel IDs the user picked via the setup CLI (setup_channels.py)."""
    if not SELECTED_CHANNELS_FILE.exists():
        return []
    return json.loads(SELECTED_CHANNELS_FILE.read_text()).get("channel_ids", [])


def save_selected_channels(channel_ids: list[int]) -> None:
    SELECTED_CHANNELS_FILE.write_text(json.dumps({"channel_ids": channel_ids}, indent=2))


telegram_cfg = TelegramConfig()
pocket_option_cfg = PocketOptionConfig()
trading_cfg = TradingConfig()
