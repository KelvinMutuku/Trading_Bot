# Telegram → Pocket Option Signal Bot

Reads trading signals from Telegram channels you choose, executes them on
Pocket Option at your configured stake, and runs the martingale/reinforcement
sequence the signal specifies (capped by your own safety limits), refreshing
your account balance after each sequence completes.

## ⚠️ Before you run this

- Pocket Option has **no official public trading API**. This bot uses an
  **unofficial, reverse-engineered** client (`pocket_option` on PyPI) that
  talks to the same WebSocket protocol their website uses. It can break
  whenever Pocket Option changes something on their end, and using bots
  against their platform may be outside their Terms of Service — that's a
  risk you're taking on, not something this code can shield you from.
- **Martingale sizing compounds losses exponentially.** A losing streak
  that would be a minor dent at flat stake sizes can be severe under
  martingale. `MAX_MARTINGALE_LEVEL` and `MAX_DAILY_LOSS` in `.env` are
  hard caps — set them deliberately, not to whatever the signal channel
  suggests.
- Test everything with `PO_IS_DEMO=1` (demo account) before ever pointing
  this at a real-money account.
- Signal channels that push affiliate/referral links (as yours do) are
  monetizing broker sign-ups. That doesn't necessarily mean the signals are
  bad, but it's a reason to track their actual win rate yourself rather
  than assume it.

## 1. Install

```bash
python -m venv venv
.\venv\Scripts\activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Telegram credentials

1. Go to https://my.telegram.org → API Development Tools → create an app.
2. Copy `api_id` and `api_hash` into `.env` (`TG_API_ID`, `TG_API_HASH`).

This bot logs in as **your own Telegram user account**, not a bot account —
that's required to read messages from channels you're a member of but don't
administer.

## 3. Pick which channels to monitor

```bash
python setup_channels.py
```

First run asks for your phone number and the login code Telegram sends you
(normal Telethon login, one-time). It then lists every channel you belong
to and lets you pick which ones to monitor. Re-run this any time to change
your selection — it overwrites `selected_channels.json`.

## 4. Pocket Option session (SSID)

The unofficial API authenticates with a session token pulled from your
browser, not your username/password:

1. Log into Pocket Option in Chrome/Firefox.
2. Open DevTools → **Network** tab → filter by **WS** (WebSocket).
3. Click the live WebSocket connection, look at the messages, and find one
   starting with `42["auth",{"session":"...","uid":...`.
4. Copy the `session` string into `PO_SESSION` and the `uid` number into
   `PO_UID` in `.env`.

This token expires when your browser session does, so if the bot starts
failing to authenticate, redo this step.

## 5. Configure trading behavior

Edit `.env`:

| Variable                  | Meaning                                                                 |
| ------------------------- | ----------------------------------------------------------------------- |
| `STARTING_STAKE`        | Stake for level 0 (the initial entry)                                   |
| `MARTINGALE_MULTIPLIER` | Stake multiplier applied after each loss                                |
| `MAX_MARTINGALE_LEVEL`  | Hard cap on levels, regardless of what a signal lists                   |
| `MAX_DAILY_LOSS`        | Bot stops opening new sequences once today's realized loss exceeds this |
| `PO_IS_DEMO`            | `1` = demo account, `0` = real money                                |

## 6. Run it

```bash
python main.py
```

It connects to Pocket Option, then listens on your selected channels. Each
valid signal spawns its own martingale sequence as a background task, so
overlapping signals from different channels don't block each other.

## Running it automatically (recommended: small VPS)

A cheap always-on Linux VPS (Hetzner, DigitalOcean, Vultr — the $5–6/mo
tier is plenty) beats running this on your laptop: it stays up regardless
of your machine sleeping or losing wifi, and restarts itself if it crashes.

### Option A — systemd (simplest, no Docker needed)

Create `/etc/systemd/system/signal-bot.service` on your VPS:

```ini
[Unit]
Description=Telegram to Pocket Option signal bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/telebot
ExecStart=/home/youruser/telebot/venv/bin/python main.py
Restart=on-failure
RestartSec=10
EnvironmentFile=/home/youruser/telebot/.env

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now signal-bot
sudo journalctl -u signal-bot -f     # tail logs
```

`Restart=on-failure` means it auto-restarts if it crashes (e.g. after a
Pocket Option session hiccup); you'll just need to refresh `PO_SESSION`
manually if the token itself expires.

### Option B — Docker

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

```bash
docker build -t signal-bot .
docker run -d --name signal-bot --env-file .env --restart unless-stopped \
  -v $(pwd)/selected_channels.json:/app/selected_channels.json \
  -v $(pwd)/trader_session.session:/app/trader_session.session \
  signal-bot
```

The two volume mounts persist your Telegram login session and channel
selection across container restarts/rebuilds.

## Project layout

```
signal_parser.py     Parses raw Telegram text into a normalized signal
martingale.py         Stake sizing + daily loss cap logic
po_client.py           Pocket Option connection + trade placement/results
telegram_listener.py  Telethon client, listens on selected channels
executor.py            Runs one signal's full martingale sequence
setup_channels.py      One-time CLI to pick which channels to monitor
main.py                 Wires it all together
```

## Extending the parser for a new channel format

`signal_parser.py` is regex-based and channel-agnostic by design, but a
channel with a very different layout may need a tweak. Run it directly
against a sample to see the extracted fields:

```bash
python -c "from signal_parser import parse_signal; print(parse_signal('''<paste message here>'''))"
```

If `is_valid` comes back `False`, `.error` tells you which field it
couldn't find — usually a one-line regex adjustment near the top of the
file.
