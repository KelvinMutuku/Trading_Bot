"""
test_channel_offsets.py -- run with:  python test_channel_offsets.py

Checks the per-channel timezone setting: ID handling, precedence
(message > channel > .env default), robustness to a bad file, and the
timing-window outcome for two channels whose clocks differ.
"""

import logging
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

# telegram_listener imports telethon at module level; stub it so this test
# runs anywhere (the real package isn't needed for these checks).
if "telethon" not in sys.modules:
    t = types.ModuleType("telethon"); t.TelegramClient = object
    ev = types.ModuleType("telethon.events"); ev.NewMessage = object; t.events = ev
    sys.modules["telethon"] = t; sys.modules["telethon.events"] = ev

import config
import telegram_listener as tl
from signal_parser import parse_signal

logging.disable(logging.CRITICAL)
failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ok   {name}")
    else:
        failures += 1
        print(f"  FAIL {name} {detail}")


tmp = Path(tempfile.mkdtemp())
config.CHANNEL_OFFSETS_FILE = tmp / "channel_offsets.json"

print("Channel id normalisation")
check("marked id -> bare id", config.bare_channel_id(-1001765226347) == 1765226347)
check("bare id unchanged", config.bare_channel_id(1765226347) == 1765226347)
check("short bare id starting with 100 is NOT mangled", config.bare_channel_id(1005550123) == 1005550123)

print("Offsets file")
check("missing file -> {}", config.load_channel_offsets() == {})
config.save_channel_offsets({1765226347: 0.0, 1426104550: -4.0, 99: 5.5})
loaded = config.load_channel_offsets()
check("round-trips", loaded == {1765226347: 0.0, 1426104550: -4.0, 99: 5.5}, f"-> {loaded}")
config.CHANNEL_OFFSETS_FILE.write_text('{"-1001426104550": 3, "bad": 1, "77": "x"}')
loaded = config.load_channel_offsets()
check("hand-edited marked id is normalised; bad entries ignored", loaded == {1426104550: 3.0}, f"-> {loaded}")
config.CHANNEL_OFFSETS_FILE.write_text("{ this is not json")
check("corrupt file -> {} (never crashes the bot)", config.load_channel_offsets() == {})

print("Precedence: message > channel > .env default")
config.save_channel_offsets({1765226347: 0.0, 1426104550: -4.0})
tl.load_channel_offsets = config.load_channel_offsets

plain = "CAD/CHF OTC\nExpiration 5M\nEntry at 08:20\nBUY\nMartingale levels\n1) level at 08:25"
s = parse_signal(plain)
tl.apply_channel_offset(s, -1001426104550)          # event.chat_id style
check("channel offset applied (marked id from a live event)", s.utc_offset_hours == -4.0 and s.offset_source == "channel")

s = parse_signal(plain)
tl.apply_channel_offset(s, 1426104550)              # bare id from the catch-up loop
check("channel offset applied (bare id from catch-up)", s.utc_offset_hours == -4.0)

s = parse_signal("Time Zone: UTC -3\n" + plain)
tl.apply_channel_offset(s, 1426104550)
check("a zone stated in the message wins over the channel setting", s.utc_offset_hours == -3.0 and s.offset_source == "message")

s = parse_signal(plain)
tl.apply_channel_offset(s, 555)                     # channel with no entry
check("unconfigured channel left alone (falls back to .env)", s.utc_offset_hours is None and s.offset_source is None)

print("Timing window with two channels on different clocks")


class FakeNow(datetime):
    fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


tl.datetime = FakeNow
os.environ["SIGNAL_UTC_OFFSET_HOURS"] = "0"         # the global fallback


def runs(chat_id, posted_utc):
    sig = parse_signal(plain)
    tl.apply_channel_offset(sig, chat_id)
    FakeNow.fixed = posted_utc
    return tl.should_execute_signal(sig, posted_utc)


# Channel A's clock is UTC: entry 08:20 -> posted 08:16 UTC.
check("channel A (UTC, offset 0) trades", runs(1765226347, datetime(2026, 9, 1, 8, 16, tzinfo=timezone.utc)))
# Channel B's clock is UTC-4: entry 08:20 there = 12:20 UTC -> posted 12:16 UTC.
check("channel B (UTC-4) trades with its own offset", runs(1426104550, datetime(2026, 9, 1, 12, 16, tzinfo=timezone.utc)))
# Same message from an unconfigured channel would have been skipped under the global 0:
check("channel B WITHOUT its setting would have been skipped", not runs(555, datetime(2026, 9, 1, 12, 16, tzinfo=timezone.utc)))

print()
print("ALL PASSED" if failures == 0 else f"{failures} FAILURE(S)")
raise SystemExit(1 if failures else 0)
