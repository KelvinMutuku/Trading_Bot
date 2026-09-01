"""
po_client.py

Thin wrapper around the unofficial `pocket_option` (lordralinc) async SDK
(pip install pocket-option). This library talks to Pocket Option's own
WebSocket protocol -- it is NOT an official, supported API, so:

  * it can break whenever Pocket Option changes their frontend
  * it may fall outside Pocket Option's Terms of Service
  * you're relying on a session token (SSID), not username/password, which
    means you'll need to refresh it in .env whenever your browser session
    on their site expires

See README.md for how to extract PO_SESSION / PO_UID from your browser.
"""

from __future__ import annotations

import asyncio
import logging

from pocket_option import PocketOptionClient
from pocket_option.constants import Regions
from pocket_option.contrib.default_init import default_init
from pocket_option.models import Asset, AuthorizationData, DealAction

from config import pocket_option_cfg
from state import state

logger = logging.getLogger("po_client")

# Pocket Option's binary "option_type" for classic HIGH/LOW turbo/binary trades.
_OPTION_TYPE_TURBO = 100

# Manual overrides for asset symbols where signal_parser's own resolution
# (exact -> case-insensitive -> fuzzy match against the live Asset enum,
# see signal_parser.normalized_asset) still can't find a match. Most common
# pairs DON'T need an entry here -- the parser already resolves them
# correctly on its own. Only add an entry if validate_asset_aliases() (or
# a genuine trade failure) shows a specific pair is unresolved AND you've
# confirmed the real name via a fresh dump of pocket_option.models.Asset.
#
# Previous guesses using an uppercase "_OTC" suffix were all wrong and have
# been removed -- they were actively overriding correct lowercase "_otc"
# resolutions from signal_parser with broken ones. If you see this warning
# again for a pair, don't guess the casing -- dump the real enum and copy
# the exact name:
#   python -c "from pocket_option.models import Asset; [print(a.name) for a in Asset]" > assets.txt
ASSET_ALIASES: dict[str, str] = {
    # "USDARS_otc": "<confirmed real name>",  # still unresolved -- searching the enum for "ARS" previously returned nothing at all, so this pair may simply not exist in this library version
}


class AssetNotSupportedError(Exception):
    pass


def validate_asset_aliases() -> None:
    """Logs a warning for every ASSET_ALIASES entry whose value isn't an
    actual member of the installed library's Asset enum. Call this once at
    startup so bad guesses surface immediately in the logs instead of
    failing silently mid-sequence later."""
    bad = [(k, v) for k, v in ASSET_ALIASES.items() if not hasattr(Asset, v)]
    if bad:
        logger.warning(
            "%d ASSET_ALIASES entries point to a non-existent Asset enum member "
            "and will fail if a signal ever needs them: %s",
            len(bad), ", ".join(f"{k} -> {v}" for k, v in bad),
        )
    elif ASSET_ALIASES:
        logger.info("All %d ASSET_ALIASES entries verified against the installed Asset enum.", len(ASSET_ALIASES))


class PocketOptionExecutor:
    def __init__(self):
        self.client = PocketOptionClient(logger=True)
        self._balance: float | None = None
        self._ready = asyncio.Event()

    async def connect(self) -> None:
        default_init(
            self.client,
            authorization=AuthorizationData.model_validate(
                {
                    "session": pocket_option_cfg.session,
                    "isDemo": 1 if pocket_option_cfg.is_demo else 0,
                    "uid": pocket_option_cfg.uid,
                    "platform": 2,
                    "isFastHistory": True,
                    "isOptimized": True,
                }
            ),
        )

        @self.client.on.success_auth
        async def _on_auth(data):
            logger.info("Pocket Option authorized")
            self._ready.set()
            state.connected = True
            state.push_event("connection", "Connected to Pocket Option")
            await self.refresh_balance()

        @self.client.on.balance_success_update
        async def _on_balance(data):
            raw = getattr(data, "balance", None) or getattr(data, "value", None)
            # Pocket Option returns balance as a Decimal for precision;
            # convert to float here so nothing downstream (dashboard JSON,
            # logging, arithmetic) has to think about it.
            self._balance = float(raw) if raw is not None else None
            logger.info("Balance updated: %s", self._balance)
            state.balance = self._balance
            state.push_event("balance", f"Balance updated: {self._balance}")

        region = Regions.DEMO if pocket_option_cfg.is_demo else Regions.REAL
        await self.client.connect(region)
        await asyncio.wait_for(self._ready.wait(), timeout=30)
        validate_asset_aliases()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    def _resolve_asset(self, normalized_asset: str) -> Asset:
        """normalized_asset looks like 'EURGBP_otc' or 'CADJPY'."""
        lookup_name = ASSET_ALIASES.get(normalized_asset, normalized_asset)
        try:
            return getattr(Asset, lookup_name)
        except AttributeError as exc:
            raise AssetNotSupportedError(
                f"'{normalized_asset}' isn't a known Pocket Option asset symbol "
                f"(tried '{lookup_name}'). Check pocket_option.models.Asset for "
                "the exact enum name, then add an entry to ASSET_ALIASES in "
                "po_client.py mapping the signal's spelling to the real one."
            ) from exc

    async def place_trade(self, normalized_asset: str, direction: str, amount: float, expiration_seconds: int):
        """direction: 'call' or 'put'. Returns the opened deal object."""
        asset = self._resolve_asset(normalized_asset)
        action = DealAction.CALL if direction == "call" else DealAction.PUT

        logger.info("Opening %s %s amount=%s exp=%ss", asset, action, amount, expiration_seconds)
        state.push_event(
            "trade_opened",
            f"{normalized_asset} {direction.upper()} — stake {amount}",
            asset=normalized_asset, direction=direction, amount=amount,
        )
        deal = await self.client.deals.open_deal(
            asset=asset,
            amount=amount,
            action=action,
            is_demo=1 if pocket_option_cfg.is_demo else 0,
            option_type=_OPTION_TYPE_TURBO,
            time=expiration_seconds,
        )
        return deal

    async def wait_for_result(self, deal, expiration_seconds: int) -> bool:
        """Blocks until the deal closes. Returns True if it won."""
        result = await self.client.deals.check_deal_result(
            wait_time=expiration_seconds + 5,
            deal=deal,
        )
        profit = getattr(result, "profit", None)
        profit = float(profit) if profit is not None else None
        won = (profit is not None and profit > 0)
        logger.info("Deal result: won=%s profit=%s", won, profit)
        state.push_event(
            "trade_result",
            f"Trade {'WON' if won else 'LOST'} (profit {profit})",
            won=won, profit=profit,
        )
        return won

    async def refresh_balance(self) -> float | None:
        await self.client.emit.update_balance()
        await asyncio.sleep(1)  # give the balance_success_update event a moment to land
        return self._balance

    @property
    def balance(self) -> float | None:
        return self._balance

    @property
    def is_ready(self) -> bool:
        """True once Pocket Option auth has completed. Trades attempted
        before this is True would fail anyway -- callers should check it
        and skip/queue instead of calling place_trade."""
        return self._ready.is_set()