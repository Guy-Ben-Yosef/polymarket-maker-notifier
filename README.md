# Polymarket Maker Fill Notifier

Real-time alerts to Telegram when one of **your** resting orders is filled —
on Polymarket prediction markets (CLOB) and on Polymarket Perps.

## Features

- 🔌 **Real-time streaming** over authenticated WebSockets (no polling)
- 🎯 **Correct maker detection**
  - CLOB: uses the `trade` event's `trader_side` and your entry in `maker_orders`
  - Perps: uses the fill's `taker` flag
- 🏦 **Two venues** — CLOB and Perps run side by side; enable either or both
- 📊 **Order progress** in every alert — e.g. `170/200 shares filled (PARTIAL)` or `(FULL)`
- 🔁 **No missed fills** — REST backfill after every (re)connect, persisted de-duplication
- 💓 **Keepalive + watchdog** — venue-specific pings; reconnects on silence
- 📱 **Telegram** to multiple chats, sent in parallel, with retries and rate-limit handling
- 🪵 **Rotating log file**, optional raw-frame dump for debugging
- 🖥️ Pretty terminal output with `rich` (optional — falls back to plain text)

Taker fills are logged but **not** alerted.

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then edit .env
```

### CLOB credentials

```bash
pip install -r requirements-creds.txt
python generate_creds.py          # prints POLYMARKET_API_KEY / SECRET / PASSPHRASE
```

Set `POLYMARKET_WALLET` to the address of the `PRIVATE_KEY` wallet to enable
REST backfill of fills missed while disconnected.

### Perps credentials (optional)

Perps uses a *proxy signer* instead of API keys. The helper creates one and
registers it with a single EIP-712 signature from your account wallet:

```bash
pip install -r requirements-creds.txt
python generate_perps_creds.py --days 7     # prints PERPS_PROXY_ADDRESS / PERPS_PROXY_SECRET
```

Proxy credentials expire (default 7 days). Re-run the script before expiry.
Leave the two `PERPS_*` variables empty to disable Perps monitoring.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) → `/newbot`, copy the token.
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot).
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` (comma-separated).

Without Telegram config, alerts are printed to the terminal only.

## Usage

```bash
python main.py
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `POLYMARKET_API_KEY` / `_SECRET` / `_PASSPHRASE` | CLOB API credentials | required for CLOB |
| `POLYMARKET_WALLET` | Signer address; enables CLOB REST backfill | — |
| `PERPS_PROXY_ADDRESS` / `PERPS_PROXY_SECRET` | Perps proxy credentials | required for Perps |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | — |
| `TELEGRAM_CHAT_IDS` | Comma-separated chat IDs | — |
| `RECONNECT_DELAY` | Seconds before first reconnect (doubles each try) | `5` |
| `MAX_RECONNECT_DELAY` | Cap for the reconnect delay | `60` |
| `SSL_VERIFY` | Verify TLS certificates | `true` |
| `NAME_LOOKUP_TIMEOUT` | Max seconds to wait for a market name before alerting | `2` |
| `BACKFILL_WINDOW` | Look-back seconds on first start (no state file) | `21600` |
| `STATE_FILE` | De-duplication state | `state.json` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` | `INFO` |
| `LOG_FILE` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | Rotating log settings | `trades.log` / 5 MB / 3 |
| `LOG_RAW_EVENTS` | Log every raw WebSocket frame (needs `DEBUG`) | `false` |

At least one venue (CLOB or Perps) must be configured.

## How maker detection works

**CLOB.** The user channel sends a `trade` event for every trade you take part
in. `trader_side` says whether you were `MAKER` or `TAKER`. On a maker-side
event the top-level `side`/`price`/`outcome` describe the *taker's* order; your
own values come from the `maker_orders` entry whose `owner` is your API key.
Each trade arrives several times (`MATCHED → MINED → CONFIRMED`); the key
`trade_id:order_id` de-duplicates them. `order` events are only logged.

**Perps.** The private `fills` channel sends one record per execution with
`taker: true|false`. `false` → maker.

**Order progress.** `order` events (CLOB: `original_size`/`size_matched`;
Perps `orders` channel: `qty`/`fill`) are cached per order. Each alert reports
the larger of the venue's matched size and the running sum of fills seen, so
the figure is right whether the order update arrives before or after the fill.
Orders placed before the service started are fetched once over REST
(`/data/order/{id}` needs `POLYMARKET_WALLET`). When the size cannot be
determined the alert omits the progress line.

The stream does not replay missed events, so after each connect the service
fetches recent fills over REST (`/data/trades` for CLOB, `/v1/account/fills`
for Perps) and runs them through the same de-duplication.

## Architecture

```
CLOB user channel  ──▶ clob.py  ──┐
                                  ├──▶ main.py (Pipeline: dedupe → log → alert) ──▶ telegram_notifier.py
Perps fills channel ──▶ perps.py ──┘          │
                                              ├──▶ state.json   (seen fills, last timestamps)
                                              └──▶ trades.log   (rotating)
```

| File | Role |
|------|------|
| `config.py` | Settings from `.env` |
| `wsbase.py` | Reconnect loop, keepalive, stale-connection watchdog |
| `clob.py` | CLOB user channel, trade parsing, Gamma market names, REST backfill |
| `perps.py` | Perps auth, fills channel, instrument names, REST backfill |
| `state.py` | Persistent de-duplication |
| `telegram_notifier.py` | Message formatting and delivery |
| `console.py` | Terminal output (rich or plain) |

## Deployment (Railway)

The service runs on Railway as the `worker` service of the project
*Polymarket Maker Notifier*. It deploys automatically from the `main` branch
of the GitHub repo. The start command is `python main.py`.

```bash
railway link --project "Polymarket Maker Notifier"   # once per machine
railway status                                        # service health
railway logs                                          # live logs
railway variables --set PERPS_PROXY_ADDRESS=0x... --set PERPS_PROXY_SECRET=...
```

Set the same variables as in `.env.example` under the service's **Variables**
tab. `PRIVATE_KEY` is only needed by the local credential scripts; do not store
it on Railway.

The container filesystem is reset on every deploy, so `state.json` and
`trades.log` are lost. To keep de-duplication state across deploys, mount a
Railway volume (e.g. at `/data`) and set `STATE_FILE=/data/state.json` and
`LOG_FILE=/data/trades.log`. Without a volume, the service relies on
`BACKFILL_WINDOW` after each restart and may re-alert fills from that window.
