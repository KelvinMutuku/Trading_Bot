"""
executor.py

Given a ParsedSignal, runs the full trade + martingale sequence:
  1. wait until entry_time
  2. place the trade at the current stake
  3. wait for the result
  4. on loss, bump stake per martingale config and repeat at the next
     martingale_time from the signal (or, if it ran out of listed
     times before max_level, fall back to one expiration-cycle later)
  5. on win, or after max_level losses, stop and refresh balance
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from martingale import DailyRiskGuard, MartingaleConfig, MartingaleSequence
from po_client import AssetNotSupportedError, PocketOptionExecutor
from signal_parser import ParsedSignal
from config import trading_cfg

logger = logging.getLogger("executor")

# A signal that arrives after its stated entry time (channel posted it late,
# or processing lag) is still tradeable within a short grace window --
# don't skip it just because the clock ticked past HH:MM by a few seconds.
_GRACE_SECONDS = 20


class StaleSignalError(Exception):
    pass


def _seconds_until(hhmm: str) -> float:
    """Seconds from now until HH:MM, interpreting HH:MM in the channel's
    stated timezone (config.trading_cfg.signal_utc_offset_hours from UTC),
    converted to this machine's local time.

    Raises StaleSignalError if the computed wait is absurdly long, which
    almost always means a timezone mismatch or a bug, not a real signal.
    """
    now_utc = datetime.now(timezone.utc)
    h, m = int(hhmm[:2]), int(hhmm[3:])

    # HH:MM is in "signal time" (UTC + offset). Convert to UTC by
    # subtracting the offset.
    target_utc = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
    target_utc -= timedelta(hours=trading_cfg.signal_utc_offset_hours)

    if target_utc < now_utc - timedelta(seconds=_GRACE_SECONDS):
        target_utc += timedelta(days=1)

    wait_seconds = max((target_utc - now_utc).total_seconds(), 0)

    if wait_seconds > trading_cfg.max_entry_wait_seconds:
        raise StaleSignalError(
            f"Computed wait of {wait_seconds:.0f}s for entry {hhmm} exceeds "
            f"max_entry_wait_seconds ({trading_cfg.max_entry_wait_seconds}s). "
            "Check SIGNAL_UTC_OFFSET_HOURS in .env against what this channel "
            "actually posts, and that your machine's system clock/timezone is correct."
        )
    return wait_seconds


async def run_signal(
    signal: ParsedSignal,
    po: PocketOptionExecutor,
    martingale_cfg: MartingaleConfig,
    risk_guard: DailyRiskGuard,
) -> None:
    if not signal.is_valid:
        logger.warning("Skipping unparseable signal: %s", signal.error)
        return

    if not risk_guard.trading_allowed():
        logger.warning("Daily loss cap hit -- skipping signal for %s", signal.asset)
        return

    seq = MartingaleSequence(martingale_cfg)
    entry_points = [signal.entry_time, *signal.martingale_times]
    expiration_seconds = signal.expiration_minutes * 60

    for level, entry_time in enumerate(entry_points):
        stake = seq.next_stake()
        if stake is None:
            break

        try:
            wait_s = _seconds_until(entry_time)
        except StaleSignalError as exc:
            logger.error("Skipping signal for %s: %s", signal.asset, exc)
            return

        logger.info(
            "Level %d for %s: waiting %.0fs until %s, stake=%.2f",
            level, signal.asset, wait_s, entry_time, stake,
        )
        await asyncio.sleep(wait_s)

        if not risk_guard.trading_allowed():
            logger.warning("Daily loss cap hit mid-sequence -- stopping")
            break

        try:
            deal = await po.place_trade(
                normalized_asset=signal.normalized_asset,
                direction=signal.direction,
                amount=stake,
                expiration_seconds=expiration_seconds,
            )
        except AssetNotSupportedError as exc:
            logger.error(str(exc))
            return

        won = await po.wait_for_result(deal, expiration_seconds)
        seq.record_result(won)

        if seq.finished:
            break

    risk_guard.register(seq.total_pnl)
    await po.refresh_balance()

    logger.info(
        "Sequence done for %s: won=%s levels_used=%d total_staked=%.2f pnl=%.2f new_balance=%s",
        signal.asset, seq.won, seq.level, seq.total_staked, seq.total_pnl, po.balance,
    )
