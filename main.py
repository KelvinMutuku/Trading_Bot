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
from config import describe_settings, trading_cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("main")


async def main():
    for line in describe_settings():
        (logger.warning if line.startswith(("WARNING", "NO .env")) else logger.info)(line)

    po = PocketOptionExecutor()
    await po.connect()
    logger.info("Connected to Pocket Option. Balance: %s", po.balance)

    # Sanity check on stake size: a martingale run multiplies the stake, so
    # show the worst case up front instead of finding out after a losing streak.
    worst_case = sum(
        trading_cfg.starting_stake * trading_cfg.martingale_multiplier ** i
        for i in range(trading_cfg.max_martingale_level + 1)
    )
    logger.info("Worst-case cost of one losing martingale sequence: %.2f", worst_case)
    if po.balance:
        share = worst_case / po.balance * 100
        if share > 10:
            logger.warning(
                "That worst case is %.0f%% of your balance (%.2f). If this isn't what you intended, "
                "check STARTING_STAKE / MARTINGALE_MULTIPLIER / MAX_MARTINGALE_LEVEL in .env.",
                share, po.balance,
            )

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
