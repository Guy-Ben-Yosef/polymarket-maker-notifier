# Polymarket Real-Time Trade Alert System

Real-time trade monitoring via authenticated WebSocket User channel with Telegram notifications.

## Features

- 🔌 **Real-time streaming** via WebSocket (no polling)
- 🔐 **Authenticated User channel** — only your own trades
- 🎯 **Maker-only alerts** — notifies only when your limit order is filled
- 🔍 **Auto-discovery** of active markets via REST API on startup
- 🔄 **Periodic subscription refresh** — picks up new markets automatically
- 📱 **Telegram alerts** to multiple recipients
- 🔁 **Auto-reconnect** with exponential backoff
- 💓 **PING keepalive** — prevents idle connection drops

## Setup

1. Create and activate virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # venv\Scripts\activate   # Windows
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env with your CLOB API credentials and Telegram config
   ```

## Telegram Setup

1. **Create a bot**: Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. **Get your bot token**: BotFather will give you a token like `123456789:ABCdef...`
3. **Get chat IDs**: Message [@userinfobot](https://t.me/userinfobot) to get your ID
4. **Configure**: Add the token and chat IDs to your `.env` file

## Usage

```bash
python main.py
```

Press `Ctrl+C` to stop.

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `POLYMARKET_API_KEY` | CLOB API key | Required |
| `POLYMARKET_API_SECRET` | CLOB API secret | Required |
| `POLYMARKET_API_PASSPHRASE` | CLOB API passphrase | Required |
| `RECONNECT_DELAY` | Seconds before reconnect attempt | 5 |
| `MARKET_REFRESH_INTERVAL` | Seconds between subscription refreshes | 300 |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | Optional |
| `TELEGRAM_CHAT_IDS` | Comma-separated chat IDs | Optional |

> **Note**: If Telegram is not configured, alerts display in the terminal instead.

## Architecture

```
Polymarket WebSocket ──▶ main.py ──▶ telegram_notifier.py ──▶ Telegram Bot API
                            │
                            ├──▶ trades.log
                            └──▶ Rich Console
```

## Deployment (Heroku)

```bash
heroku create
heroku config:set POLYMARKET_API_KEY=... POLYMARKET_API_SECRET=... POLYMARKET_API_PASSPHRASE=...
git push heroku main
heroku ps:scale worker=1
```
