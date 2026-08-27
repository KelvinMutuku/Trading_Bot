"""
signal_parser.py

Turns raw Telegram "signal" messages (from various channel formats) into a
normalized ParsedSignal object the trade executor can act on.

Handles formats like:

    EUR/GBP OTC
    Expiration 5M
    Entry at 08:20
    BUY
    Martingale levels
    1) level at 08:25
    2) level at 08:30
    3) level at 08:35

    CADJPY - CALL - 08:15
    Expiration: 5 minutes (M5)
    If you lose, make up to 2 Gale's.

    EUR/NZD OTC
    Expiration: 5M
    Entry: 08:30
    BUY
    Reinforcement Levels
    Level 1: 08:35
    Level 2: 08:40
    Level 3: 08:45

New channel formats will usually need a small tweak to the regexes below,
not a rewrite -- keep them here in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedSignal:
    raw: str
    asset: str | None = None
    is_otc: bool = False
    direction: str | None = None          # "call" or "put"
    entry_time: str | None = None         # "HH:MM" (exchange/channel local time)
    expiration_minutes: int | None = None
    martingale_times: list[str] = field(default_factory=list)
    martingale_count: int = 0
    is_valid: bool = False
    error: str | None = None

    @property
    def normalized_asset(self) -> str | None:
        """Return the asset in POCKET_OPTION style, e.g. EURGBP_otc."""
        if not self.asset:
            return None
        base = self.asset.replace("/", "").upper()
        return f"{base}_otc" if self.is_otc else base


_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")          # markdown links -> strip
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_SLASH_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b")
_SMASHED_PAIR_RE = re.compile(r"\b([A-Z]{6})\b")
_EXPIRY_RE_1 = re.compile(r"expir\w*\s*:?\s*(\d+)\s*m\b", re.IGNORECASE)
_EXPIRY_RE_2 = re.compile(r"\(m(\d+)\)", re.IGNORECASE)
_ENTRY_RE = re.compile(r"entry\w*\s*(?:at)?:?\s*([01]?\d|2[0-3]):([0-5]\d)", re.IGNORECASE)
_GALE_COUNT_RE = re.compile(r"(\d+)\s*gale", re.IGNORECASE)
_BUY_RE = re.compile(r"\b(buy|call)\b", re.IGNORECASE)
_SELL_RE = re.compile(r"\b(sell|put)\b", re.IGNORECASE)

# Words that legitimately appear as 6 consecutive uppercase letters but are
# NOT currency pairs -- filtered out of the smashed-pair guess.
_SMASHED_PAIR_BLOCKLIST = {"SIGNAL", "SIGNALS", "MARKET", "LEVELS"}


def _clean(text: str) -> str:
    return _LINK_RE.sub("", text)


def _find_asset(text: str) -> tuple[str | None, bool]:
    is_otc = bool(re.search(r"\botc\b", text, re.IGNORECASE))
    m = _SLASH_PAIR_RE.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}", is_otc
    for m in _SMASHED_PAIR_RE.finditer(text):
        if m.group(1) not in _SMASHED_PAIR_BLOCKLIST:
            return m.group(1), is_otc
    return None, is_otc


def _find_direction(text: str) -> str | None:
    buy = _BUY_RE.search(text)
    sell = _SELL_RE.search(text)
    if buy and not sell:
        return "call"
    if sell and not buy:
        return "put"
    if buy and sell:
        # Whichever keyword appears first in the message wins.
        return "call" if buy.start() < sell.start() else "put"
    return None


def _find_expiration_minutes(text: str) -> int | None:
    m = _EXPIRY_RE_1.search(text)
    if m:
        return int(m.group(1))
    m = _EXPIRY_RE_2.search(text)
    if m:
        return int(m.group(1))
    return None


def _find_entry_time(text: str, fallback_times: list[str]) -> str | None:
    m = _ENTRY_RE.search(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    # Fallback: first bare HH:MM found anywhere (covers the "CADJPY - CALL - 08:15" style)
    return fallback_times[0] if fallback_times else None


def _find_martingale_times(all_times: list[str], entry_time: str | None) -> list[str]:
    times = [t for t in all_times if t != entry_time]
    # de-dupe while preserving order
    seen = set()
    out = []
    for t in times:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _find_gale_count(text: str) -> int:
    m = _GALE_COUNT_RE.search(text)
    return int(m.group(1)) if m else 0


def parse_signal(raw_text: str) -> ParsedSignal:
    """Parse a raw Telegram message into a ParsedSignal.

    Never raises: on failure, returns a ParsedSignal with is_valid=False
    and .error set, so callers can log-and-skip instead of crashing the bot.
    """
    text = _clean(raw_text)
    sig = ParsedSignal(raw=raw_text)

    try:
        all_times = [f"{int(h):02d}:{m}" for h, m in _TIME_RE.findall(text)]

        asset, is_otc = _find_asset(text)
        sig.asset = asset
        sig.is_otc = is_otc
        sig.direction = _find_direction(text)
        sig.expiration_minutes = _find_expiration_minutes(text)
        sig.entry_time = _find_entry_time(text, all_times)
        sig.martingale_times = _find_martingale_times(all_times, sig.entry_time)
        sig.martingale_count = _find_gale_count(text)

        # If we only got a count (e.g. "up to 2 Gale's") with no explicit
        # times, derive them: convention is one expiration-cycle apart.
        if not sig.martingale_times and sig.martingale_count and sig.entry_time and sig.expiration_minutes:
            h, m = map(int, sig.entry_time.split(":"))
            base_minutes = h * 60 + m
            derived = []
            for level in range(1, sig.martingale_count + 1):
                t = (base_minutes + level * sig.expiration_minutes) % (24 * 60)
                derived.append(f"{t // 60:02d}:{t % 60:02d}")
            sig.martingale_times = derived

        sig.is_valid = bool(sig.asset and sig.direction and sig.entry_time and sig.expiration_minutes)
        if not sig.is_valid:
            missing = [
                name
                for name, val in (
                    ("asset", sig.asset),
                    ("direction", sig.direction),
                    ("entry_time", sig.entry_time),
                    ("expiration_minutes", sig.expiration_minutes),
                )
                if not val
            ]
            sig.error = f"Could not extract: {', '.join(missing)}"

    except Exception as exc:  # defensive: a bad message should never crash the listener
        sig.is_valid = False
        sig.error = f"parser exception: {exc}"

    return sig


if __name__ == "__main__":
    samples = [
        """🇪🇺 EUR/GBP 🇬🇧 OTC
🕘 Expiration 5M
⏺️ Entry at 08:20
🟩 BUY

🔼 Martingale levels
1️⃣ level at 08:25
2️⃣ level at 08:30
3️⃣ level at 08:35""",
        """📊 FREE SIGNALS 📊
⏰ Time Zone: UTC -3

• CADJPY - CALL 🟩 - 08:15
• Expiration: 5 minutes (M5)
• If you lose, make up to 2 Gale's.""",
        """📊 🇪🇺 EUR/NZD 🇳🇿 OTC
🕘 Expiration: 5M
⏺ Entry: 08:30
🟩 BUY

🔽 Reinforcement Levels
(if the market reverses)
1️⃣ Level 1: 08:35
2️⃣ Level 2: 08:40
3️⃣ Level 3: 08:45""",
    ]
    for s in samples:
        result = parse_signal(s)
        print(result)
        print("-" * 60)