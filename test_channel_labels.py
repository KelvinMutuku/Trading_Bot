"""
test_channel_labels.py -- run with:  python test_channel_labels.py

Runs a signal through the real executor with a stand-in Pocket Option client
and checks that every log line names the channel it came from.
"""

import asyncio
import logging
import sys
import types

# Stubs so this runs without telethon / pocket_option installed.
def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

if "telethon" not in sys.modules:
    _stub("telethon", TelegramClient=object, events=_stub("telethon.events", NewMessage=object))
if "pocket_option" not in sys.modules:
    class _EnumMeta(type):
        def __iter__(cls):          # the real Asset is an iterable enum
            return iter(())

    class _Enum(metaclass=_EnumMeta):
        pass
    _stub("pocket_option", PocketOptionClient=object)
    _stub("pocket_option.constants", Regions=object)
    _stub("pocket_option.contrib", )
    _stub("pocket_option.contrib.default_init", default_init=lambda *a, **k: None)
    _stub("pocket_option.models", Asset=_Enum, AuthorizationData=object, DealAction=object)

import executor
import telegram_listener as tl
from martingale import DailyRiskGuard, MartingaleConfig
from signal_parser import parse_signal

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ok   {name}")
    else:
        failures += 1
        print(f"  FAIL {name} {detail}")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class FakePO:
    balance = 50000.0

    def __init__(self):
        self.tags_seen = []

    async def place_trade(self, normalized_asset, direction, amount, expiration_seconds, tag=""):
        self.tags_seen.append(("place", tag, amount))
        return object()

    async def wait_for_result(self, deal, expiration_seconds, tag=""):
        self.tags_seen.append(("result", tag))
        return True

    async def refresh_balance(self):
        return self.balance


print("Naming the channel")
names = {1765226347: "Lorenzo VIP", 1426104550: "Micha Trader | Vip"}
s = parse_signal("EURGBP 15 minutes Up")
tl.tag_channel(s, -1001765226347, names)         # live-event id style
check("name found from a live-event (marked) id", s.source_channel == "Lorenzo VIP", f"-> {s.source_channel}")
s = parse_signal("EURGBP 15 minutes Up")
tl.tag_channel(s, 1426104550, names)             # catch-up (bare) id style
check("name found from a bare id", s.source_channel == "Micha Trader | Vip")
s = parse_signal("EURGBP 15 minutes Up")
tl.tag_channel(s, 424242, names)
check("unknown channel falls back to its id", s.source_channel == "channel 424242")

print("Executor log lines")
cap = Capture()
logging.getLogger("executor").addHandler(cap)
logging.getLogger("executor").setLevel(logging.INFO)

sig = parse_signal("EURGBP 15 minutes Up")
tl.tag_channel(sig, 1765226347, names)
po = FakePO()
asyncio.run(executor.run_signal(sig, po, MartingaleConfig(starting_stake=2.0, multiplier=2.0, max_level=3), DailyRiskGuard(None)))

tagged = [l for l in cap.lines if l.startswith("[Lorenzo VIP] ")]
check("every executor line names the channel", len(tagged) == len(cap.lines) and len(cap.lines) >= 3, f"-> {cap.lines}")
check("the stake used is visible in the log", any("stake=2.00" in l for l in cap.lines), f"-> {cap.lines}")
check("channel tag is passed down to the trade/result logs", po.tags_seen == [("place", "[Lorenzo VIP] ", 2.0), ("result", "[Lorenzo VIP] ")], f"-> {po.tags_seen}")

cap.lines.clear()
sig2 = parse_signal("EURGBP 15 minutes Up")          # no channel set (e.g. old callers)
asyncio.run(executor.run_signal(sig2, FakePO(), MartingaleConfig(starting_stake=2.0), DailyRiskGuard(None)))
check("still works with no channel (no stray brackets)", cap.lines and not any(l.startswith("[") for l in cap.lines), f"-> {cap.lines}")

print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILURE(S)")
raise SystemExit(1 if failures else 0)
