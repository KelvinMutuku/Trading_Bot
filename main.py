"""
main.py

Entry point. Runs three things concurrently in one process:
  1. the Pocket Option connection (with retry -- see connect_pocket_option below)
  2. the Telegram listener (which calls executor.run_signal for each
     valid parsed signal, as a background task)
  3. the web dashboard (FastAPI + uvicorn) at http://<DASHBOARD_HOST>:<DASHBOARD_PORT>

These start together, not one-after-another. A Pocket Option connection
problem (bad SSID, DNS issue, Pocket Option being briefly unreachable)
retries in the background instead of crashing the whole process -- so the
dashboard is always reachable to show you what's actually going on, even
while Pocket Option itself is down.

Run with:  python main.py
Deploy with the systemd unit / Docker setup described in README.md.
"""

import asyncio
import logging

import uvicorn

from executor import run_signal
from martingale import DailyRiskGuard, MartingaleConfig
from po_client import PocketOptionExecutor
from telegram_listener import TelegramSignalListener
from web_ui import create_app
from state import state, TradingSettings
from config import trading_cfg, pocket_option_cfg, dashboard_cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")

_INITIAL_RETRY_DELAY = 5
_MAX_RETRY_DELAY = 60


async def connect_pocket_option(po: PocketOptionExecutor) -> None:
    """Keeps retrying po.connect() with backoff instead of raising, so a
    connection problem doesn't take the dashboard/listener down with it.
    Returns once connected; state.connected/state.balance reflect progress
    the whole time via po_client's own event pushes."""
    delay = _INITIAL_RETRY_DELAY
    while True:
        try:
            await po.connect()
            logger.info("Connected to Pocket Option. Balance: %s", po.balance)
            return
        except Exception as exc:
            logger.error("Pocket Option connection failed: %s -- retrying in %ds", exc, delay)
            state.connected = False
            state.push_event("connection", f"Pocket Option connection failed: {exc}. Retrying in {delay}s.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_RETRY_DELAY)


async def main():
    # Seed dashboard state with what mode we're actually connecting in,
    # before the dashboard's own setup reads it.
    state.settings = TradingSettings(
        starting_stake=trading_cfg.starting_stake,
        martingale_multiplier=trading_cfg.martingale_multiplier,
        max_martingale_level=trading_cfg.max_martingale_level,
        max_daily_loss=trading_cfg.max_daily_loss,
        is_demo=pocket_option_cfg.is_demo,
    )

    po = PocketOptionExecutor()

    martingale_cfg = MartingaleConfig(
        starting_stake=trading_cfg.starting_stake,
        multiplier=trading_cfg.martingale_multiplier,
        max_level=trading_cfg.max_martingale_level,
    )
    risk_guard = DailyRiskGuard(trading_cfg.max_daily_loss)

    async def handle_signal(signal):
        if not po.is_ready:
            logger.warning("Skipping %s -- Pocket Option isn't connected yet.", signal.asset)
            state.push_event("skipped", f"Skipped {signal.asset}: Pocket Option not connected yet")
            return
        try:
            await run_signal(signal, po, martingale_cfg, risk_guard)
        except Exception:
            logger.exception("Error running signal sequence for %s", signal.asset)

    listener = TelegramSignalListener(on_signal_callback=handle_signal)

    app = create_app(listener, martingale_cfg, risk_guard)
    server = uvicorn.Server(
        uvicorn.Config(app, host=dashboard_cfg.host, port=dashboard_cfg.port, log_level="warning")
    )
    logger.info("Dashboard available at http://%s:%d", dashboard_cfg.host, dashboard_cfg.port)

    try:
        # All three start together. Pocket Option retries forever in the
        # background rather than blocking the other two; if the listener
        # or the dashboard server itself raises, we stop and disconnect.
        await asyncio.gather(
            connect_pocket_option(po),
            listener.start(),
            server.serve(),
        )
    finally:
        await po.disconnect()


if __name__ == "__main__":
    asyncio.run(main())