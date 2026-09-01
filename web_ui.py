"""
web_ui.py

FastAPI dashboard: lets you pick which Telegram channels to monitor and
tune trading settings while the bot runs, with a live feed of what it's
doing. Runs in the same process as the trading logic (see main.py), so
channel/settings changes take effect immediately -- no restart needed for
those. Changing DEMO/LIVE mode still requires a restart with a different
PO_IS_DEMO in .env, since that's tied to which region the Pocket Option
connection itself was opened against.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from martingale import DailyRiskGuard, MartingaleConfig
from state import state, TradingSettings
from telegram_listener import TelegramSignalListener

logger = logging.getLogger("web_ui")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ChannelSelection(BaseModel):
    channel_ids: list[int]


class SettingsUpdate(BaseModel):
    starting_stake: float
    martingale_multiplier: float
    max_martingale_level: int
    max_daily_loss: float | None = None


def create_app(
    listener: TelegramSignalListener,
    martingale_cfg: MartingaleConfig,
    risk_guard: DailyRiskGuard,
) -> FastAPI:
    app = FastAPI(title="Signal Bot Dashboard")

    # Keep state.settings in sync with the live config objects the
    # executor actually reads from, so the dashboard always shows what's
    # really in effect.
    state.settings = TradingSettings(
        starting_stake=martingale_cfg.starting_stake,
        martingale_multiplier=martingale_cfg.multiplier,
        max_martingale_level=martingale_cfg.max_level,
        max_daily_loss=risk_guard.max_daily_loss,
        is_demo=state.settings.is_demo if state.settings else True,
    )

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state():
        return JSONResponse(state.snapshot())

    @app.post("/api/channels")
    async def set_channels(body: ChannelSelection):
        listener.update_selected_channels(body.channel_ids)
        return JSONResponse({"ok": True})

    @app.post("/api/settings")
    async def set_settings(body: SettingsUpdate):
        martingale_cfg.starting_stake = body.starting_stake
        martingale_cfg.multiplier = body.martingale_multiplier
        martingale_cfg.max_level = body.max_martingale_level
        risk_guard.max_daily_loss = body.max_daily_loss

        state.settings = TradingSettings(
            starting_stake=body.starting_stake,
            martingale_multiplier=body.martingale_multiplier,
            max_martingale_level=body.max_martingale_level,
            max_daily_loss=body.max_daily_loss,
            is_demo=state.settings.is_demo if state.settings else True,
        )
        state.push_event("settings_updated", "Trading settings updated from dashboard")
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws_feed(websocket: WebSocket):
        await websocket.accept()
        queue = state.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", "data": state.snapshot()})
            while True:
                event = await queue.get()
                await websocket.send_json({"type": "event", "data": event})
        except WebSocketDisconnect:
            pass
        finally:
            state.unsubscribe(queue)

    return app