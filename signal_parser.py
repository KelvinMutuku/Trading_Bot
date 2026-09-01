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
import difflib
import logging
from dataclasses import dataclass, field

try:
    from pocket_option.models import Asset
except ImportError:
    Asset = None

logger = logging.getLogger("signal_parser")


@dataclass
class ParsedSignal:
    raw: str
    asset: str | None = None
    is_otc: bool = False
    direction: str | None = None          # "call" or "put"
    entry_time: str | None = None         # "HH:MM" (in the channel's stated timezone, if any)
    expiration_minutes: int | None = None
    martingale_times: list[str] = field(default_factory=list)
    martingale_count: int = 0
    utc_offset_hours: float | None = None  # detected from message text, e.g. "UTC -3" -> -3.0
    immediate: bool = False                # True if the signal has no entry time at all -> trade now
    has_martingale: bool = False           # True only if the signal itself specified martingale levels/count
    is_valid: bool = False
    error: str | None = None

    @property
    def normalized_asset(self) -> str | None:
        """Return the asset mapped strictly to Pocket Option Enums if possible.

        Logs which resolution path was used so it's visible whether a trade
        is about to use an exact match or a guessed one:
          - exact: raw_symbol is a real Asset enum member as-is
          - case-insensitive: same name, different casing
          - fuzzy (>=95% similar): NOT an exact name -- treat as a guess
          - unresolved: no Asset enum available, or no match at all;
            raw_symbol is returned unchanged and will likely fail downstream
        """
        if not self.asset:
            return None

        base = re.sub(r"[/\-_]", "", self.asset).upper()
        raw_symbol = f"{base}_otc" if self.is_otc else base

        if Asset is not None:
            valid_assets = {a.name: a.name for a in Asset}

            # 1. Exact match
            if raw_symbol in valid_assets:
                logger.debug("Asset '%s' resolved exactly.", raw_symbol)
                return raw_symbol

            # 2. Case-insensitive exact match
            for valid_name in valid_assets:
                if valid_name.lower() == raw_symbol.lower():
                    logger.info(
                        "Asset '%s' resolved via case-insensitive match -> '%s'.",
                        raw_symbol, valid_name,
                    )
                    return valid_name

            # 3. Fuzzy match (requires 95% similarity to ensure safe trading)
            matches = difflib.get_close_matches(raw_symbol, list(valid_assets.keys()), n=1, cutoff=0.95)
            if matches:
                logger.warning(
                    "Asset '%s' has NO exact match -- using FUZZY match -> '%s'. "
                    "Verify this is correct before trading it for real; consider "
                    "adding an explicit ASSET_ALIASES entry instead.",
                    raw_symbol, matches[0],
                )
                return matches[0]

            logger.warning(
                "Asset '%s' did not resolve against the installed Asset enum "
                "(no exact, case-insensitive, or fuzzy match). It will likely "
                "fail when a trade is attempted unless ASSET_ALIASES covers it.",
                raw_symbol,
            )

        return raw_symbol


_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")          # markdown links -> strip
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_SLASH_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*[/\-_]\s*([A-Z]{3})\b")
_SMASHED_PAIR_RE = re.compile(r"\b([A-Z]{6})\b")
_EXPIRY_RE_1 = re.compile(r"exp(?:ir\w*)?\s*:?\s*(\d+)\s*m\b", re.IGNORECASE)
_EXPIRY_RE_2 = re.compile(r"\(m(\d+)\)", re.IGNORECASE)
_EXPIRY_RE_3 = re.compile(r"\b(\d{1,3})\s*minutes?\b", re.IGNORECASE)   # "15 minutes", no label
_EXPIRY_RE_4 = re.compile(r"\bm(\d{1,3})\b", re.IGNORECASE)             # bare "M2", no parens/label
_ENTRY_RE = re.compile(r"entry\w*\s*(?:at)?:?\s*([01]?\d|2[0-3]):([0-5]\d)", re.IGNORECASE)
_GALE_COUNT_RE = re.compile(r"(\d+)\s*gale", re.IGNORECASE)
_BUY_RE = re.compile(r"\b(buy|call|higher|up)\b", re.IGNORECASE)
_SELL_RE = re.compile(r"\b(sell|put|lower|down)\b", re.IGNORECASE)
_TZ_RE = re.compile(r"utc\s*([+-]?\s?\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
_MARTINGALE_KEYWORD_RE = re.compile(r"martingale|\bgale\b|reinforcement|\blevel\b", re.IGNORECASE)

# Emoji fallback: only used when no BUY/SELL-style word is found anywhere.
_UP_EMOJI = ("🟩", "⬆️", "🔺", "📈")
_DOWN_EMOJI = ("🟥", "⬇️", "🔻", "📉")

# Words that legitimately appear as 6 consecutive uppercase letters but are
# NOT currency pairs -- filtered out of the smashed-pair guess.
# Words that are 3 uppercase letters but are never actually a currency code
# -- without this, something like "NZDUSD-OTC - PUT" would misparse "OTC -
# PUT" as a fake currency pair via _SLASH_PAIR_RE, since both sides happen
# to be 3 letters separated by a dash.
_NON_CURRENCY_WORDS = {
    "OTC", "BUY", "SELL", "CALL", "PUT", "LOW", "HIGH", "GALE", "GMT", "UTC", "WIN",
}
_SMASHED_PAIR_BLOCKLIST = {"SIGNAL", "SIGNALS", "MARKET", "LEVELS"}


def _clean(text: str) -> str:
    return _LINK_RE.sub("", text)


def _find_asset(text: str) -> tuple[str | None, bool]:
    is_otc = bool(re.search(r"\botc\b", text, re.IGNORECASE))

    for m in _SLASH_PAIR_RE.finditer(text):
        if m.group(1) not in _NON_CURRENCY_WORDS and m.group(2) not in _NON_CURRENCY_WORDS:
            return f"{m.group(1)}/{m.group(2)}", is_otc

    for m in _SMASHED_PAIR_RE.finditer(text):
        if m.group(1) not in _SMASHED_PAIR_BLOCKLIST and m.group(1) not in _NON_CURRENCY_WORDS:
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

    # No BUY/SELL-style word anywhere -- fall back to color/arrow emoji,
    # common in channels that only signal direction visually.
    up_pos = min((text.find(e) for e in _UP_EMOJI if e in text), default=-1)
    down_pos = min((text.find(e) for e in _DOWN_EMOJI if e in text), default=-1)
    if up_pos != -1 and (down_pos == -1 or up_pos < down_pos):
        return "call"
    if down_pos != -1:
        return "put"
    return None


def _find_utc_offset(text: str) -> float | None:
    """Looks for an explicit stated zone like 'UTC -3' or 'UTC+2:30'."""
    m = _TZ_RE.search(text)
    if not m:
        return None
    hours = float(m.group(1).replace(" ", ""))
    minutes = int(m.group(2)) if m.group(2) else 0
    sign = -1 if hours < 0 else 1
    return hours + sign * (minutes / 60)


def _find_expiration_minutes(text: str) -> int | None:
    for pattern in (_EXPIRY_RE_1, _EXPIRY_RE_2, _EXPIRY_RE_3, _EXPIRY_RE_4):
        m = pattern.search(text)
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
        sig.utc_offset_hours = _find_utc_offset(text)

        # Only trust martingale/reinforcement data if the message actually
        # says so. Without this, a coincidental extra time elsewhere in the
        # message (e.g. an unrelated timestamp) could be mistaken for a
        # martingale level on a signal that never mentioned one.
        if not _MARTINGALE_KEYWORD_RE.search(text):
            sig.martingale_times = []
            sig.martingale_count = 0

        # No time of any kind found anywhere in the message -> this is a
        # "trade it right now" signal rather than a scheduled one.
        if sig.entry_time is None and not all_times:
            sig.immediate = True

        # If we only got a count (e.g. "up to 2 Gale's") with no explicit
        # times, derive them: convention is one expiration-cycle apart.
        if (
            not sig.martingale_times
            and sig.martingale_count
            and sig.entry_time
            and sig.expiration_minutes
        ):
            h, m = map(int, sig.entry_time.split(":"))
            base_minutes = h * 60 + m
            derived = []
            for level in range(1, sig.martingale_count + 1):
                t = (base_minutes + level * sig.expiration_minutes) % (24 * 60)
                derived.append(f"{t // 60:02d}:{t % 60:02d}")
            sig.martingale_times = derived

        sig.has_martingale = bool(sig.martingale_times or sig.martingale_count)

        sig.is_valid = bool(
            sig.asset
            and sig.direction
            and sig.expiration_minutes
            and (sig.entry_time or sig.immediate)
        )
        if not sig.is_valid:
            missing = [
                name
                for name, val in (
                    ("asset", sig.asset),
                    ("direction", sig.direction),
                    ("entry_time or immediate", sig.entry_time or sig.immediate),
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