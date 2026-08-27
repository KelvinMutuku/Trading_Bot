"""
martingale.py

Tracks stake sizing across a signal's martingale/reinforcement sequence.

IMPORTANT: martingale sizing means each loss increases your exposure
exponentially (or per your configured multiplier). A losing streak that
would be a minor dent at flat stakes can wipe out a large chunk of a
balance under martingale. Always set max_level and a hard daily-loss cap.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MartingaleConfig:
    starting_stake: float
    multiplier: float = 2.0       # stake *= multiplier after each loss
    max_level: int = 3            # hard cap regardless of what the signal says
    max_daily_loss: float | None = None   # stop trading for the day if exceeded


class MartingaleSequence:
    """One instance per signal. Call `next_stake()` before each entry,
    `record_result()` after each trade closes."""

    def __init__(self, config: MartingaleConfig):
        self.config = config
        self.level = 0
        self.stake = config.starting_stake
        self.total_staked = 0.0
        self.total_pnl = 0.0
        self.finished = False
        self.won = False

    def next_stake(self) -> float | None:
        """Returns the stake to use for the current level, or None if the
        sequence has hit its max level / is already finished."""
        if self.finished or self.level > self.config.max_level:
            return None
        return round(self.stake, 2)

    def record_result(self, won: bool, payout_multiplier: float = 0.8) -> None:
        """Update sequence state after a trade closes.

        payout_multiplier: broker's payout rate on a win, e.g. 0.8 = 80% profit.
        """
        self.total_staked += self.stake
        if won:
            self.total_pnl += self.stake * payout_multiplier
            self.finished = True
            self.won = True
            return

        self.total_pnl -= self.stake
        self.level += 1
        self.stake = round(self.stake * self.config.multiplier, 2)

        if self.level > self.config.max_level:
            self.finished = True
            self.won = False


class DailyRiskGuard:
    """Cross-sequence daily loss cap. One instance shared across the bot's
    lifetime for a trading day; reset it (or create a new one) at rollover."""

    def __init__(self, max_daily_loss: float | None):
        self.max_daily_loss = max_daily_loss
        self.realized_pnl = 0.0

    def register(self, pnl: float) -> None:
        self.realized_pnl += pnl

    def trading_allowed(self) -> bool:
        if self.max_daily_loss is None:
            return True
        return self.realized_pnl > -abs(self.max_daily_loss)