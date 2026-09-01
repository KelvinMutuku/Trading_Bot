"""
state.py

Single shared state object the trading logic writes to and the web
dashboard reads from / streams out over a WebSocket. Everything here is
in-memory only -- it resets on restart. That's fine for "what's happening
right now" (balance, live feed, connection status); channel selection and
.env values are the things that actually need to persist, and those still
go through selected_channels.json / .env as before.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any


def _json_safe(value: Any) -> Any:
    """Recursively converts values that json.dumps can't handle on its own
    (notably Decimal, which the pocket_option library returns for money
    fields) into plain floats, so nothing downstream -- REST responses,
    WebSocket messages -- can crash on an unexpected type from the library."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class ChannelInfo:
    id: int
    name: str
    selected: bool


@dataclass
class TradingSettings:
    starting_stake: float
    martingale_multiplier: float
    max_martingale_level: int
    max_daily_loss: float | None
    is_demo: bool


class AppState:
    def __init__(self):
        self.connected: bool = False
        self.balance: float | None = None
        self.daily_pnl: float = 0.0
        self.channels: list[ChannelInfo] = []
        self.settings: TradingSettings | None = None
        self.events: list[dict[str, Any]] = []  # capped ring buffer, newest last
        self._max_events = 200
        self._subscribers: list[asyncio.Queue] = []

    # ---- event feed -------------------------------------------------

    def push_event(self, kind: str, message: str, **extra: Any) -> None:
        event = _json_safe({"ts": time.time(), "kind": kind, "message": message, **extra})
        self.events.append(event)
        if len(self.events) > self._max_events:
            self.events.pop(0)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    # ---- snapshot for the initial page load / REST polling ----------

    def snapshot(self) -> dict[str, Any]:
        return _json_safe({
            "connected": self.connected,
            "balance": self.balance,
            "daily_pnl": round(self.daily_pnl, 2),
            "channels": [asdict(c) for c in self.channels],
            "settings": asdict(self.settings) if self.settings else None,
            "events": self.events[-50:],
        })


state = AppState()