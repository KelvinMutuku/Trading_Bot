"""
main.py

Entry point. Starts:
  1. the Pocket Option connection
  2. the Telegram listener (which calls executor.run_signal for each
     valid parsed signal, as a background task)

Run with:  python main.py
Deploy with the systemd unit / Docker setup described in README.md.
"""

import asyncio
import logging

from executor import run_signal
from martingale import DailyRiskGuard, MartingaleConfig
from po_client import PocketOptionExecutor
from telegram_listener import TelegramSignalListener
from config import trading_cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


async def main():
    po = PocketOptionExecutor()
    await po.connect()
    logger.info("Connected to Pocket Option. Balance: %s", po.balance)

    martingale_cfg = MartingaleConfig(
        starting_stake=trading_cfg.starting_stake,
        multiplier=trading_cfg.martingale_multiplier,
        max_level=trading_cfg.max_martingale_level,
    )
    risk_guard = DailyRiskGuard(trading_cfg.max_daily_loss)

    async def handle_signal(signal):
        try:
            await run_signal(signal, po, martingale_cfg, risk_guard)
        except Exception:
            logger.exception("Error running signal sequence for %s", signal.asset)

    listener = TelegramSignalListener(on_signal_callback=handle_signal)

    try:
        await listener.start()
    finally:
        await po.disconnect()


if __name__ == "__main__":
    asyncio.run(main())