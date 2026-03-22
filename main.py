"""
Polymarket Real-Time Trade Alert System (WebSocket)

Streams your own Polymarket trades in real-time via the authenticated
WebSocket User channel and sends maker-only alerts to Telegram.
"""

import asyncio
import json
import logging
import os
import sys
import urllib3
from datetime import datetime
from typing import Any

import httpx
import websockets
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from telegram_notifier import TelegramNotifier

# Suppress SSL warnings (for corporate proxies)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Load environment variables
load_dotenv()

# Configuration
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_WALLET = os.getenv("POLYMARKET_WALLET", "")

RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
PING_INTERVAL = 30  # seconds — Polymarket drops idle WS connections

# Initialize Rich console
console = Console()

# Configure logging — use DEBUG to see raw WS messages
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("trades.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Quiet down noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.INFO)


# ─── Market name cache ────────────────────────────────────────────────────────

_market_name_cache: dict[str, str] = {}


async def resolve_market_name(
    token_id: str, client: httpx.AsyncClient
) -> str:
    """
    Resolve a token_id (asset_id) to a human-readable market question.
    Uses the Gamma API. Caches results.
    """
    if token_id in _market_name_cache:
        return _market_name_cache[token_id]

    try:
        url = f"{GAMMA_API_URL}/markets"
        params = {"clob_token_ids": token_id}
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if data and len(data) > 0:
            question = data[0].get("question", "")
            if question:
                _market_name_cache[token_id] = question
                if len(_market_name_cache) > 1000:
                    _market_name_cache.pop(next(iter(_market_name_cache)))
                logger.info(f"Resolved token {token_id[:16]}... → {question}")
                return question
    except Exception as e:
        logger.warning(f"Failed to resolve market name for {token_id[:16]}...: {e}")

    short = f"{token_id[:10]}…{token_id[-6:]}"
    _market_name_cache[token_id] = short
    if len(_market_name_cache) > 1000:
        _market_name_cache.pop(next(iter(_market_name_cache)))
    return short


# ─── Console display helpers ─────────────────────────────────────────────────


def format_trade_alert(trade: dict[str, Any]) -> Panel:
    """Format a trade into a colored Rich panel for console display."""
    side = trade.get("side", "UNKNOWN").upper()
    is_buy = side == "BUY"

    market_name = trade.get("market", trade.get("asset_id", "Unknown Market"))
    shares = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    usdc_size = shares * price
    outcome = trade.get("outcome", "")
    fill_type = trade.get("fill_type", "MAKER")

    trade_ts = trade.get("timestamp", 0)
    if trade_ts:
        try:
            trade_time = datetime.fromtimestamp(int(trade_ts)).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            trade_time = str(trade_ts)
    else:
        trade_time = "Unknown"

    color = "green" if is_buy else "red"
    direction_symbol = "📈" if is_buy else "📉"

    text = Text()
    text.append(f"{direction_symbol} ", style="bold")
    text.append(f"{side}\n", style=f"bold {color}")
    text.append("Market: ", style="dim")
    text.append(f"{market_name}\n", style="white")
    if outcome:
        text.append("Outcome: ", style="dim")
        text.append(f"{outcome}\n", style="magenta")
    text.append("Shares: ", style="dim")
    text.append(f"{shares:,.2f}\n", style="cyan")
    text.append("Price: ", style="dim")
    text.append(f"${price:.4f}\n", style="yellow")
    text.append("Total: ", style="dim")
    text.append(f"${usdc_size:,.2f} USDC", style=f"bold {color}")

    return Panel(
        text,
        title=f"[bold {color}]{fill_type} Trade Alert[/bold {color}]",
        subtitle=f"[dim]{trade_time}[/dim]",
        border_style=color,
    )


def log_trade(trade: dict[str, Any]) -> None:
    """Log trade details to file."""
    side = trade.get("side", "UNKNOWN").upper()
    market_name = trade.get("market", trade.get("asset_id", "Unknown"))
    shares = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    usdc_size = shares * price
    outcome = trade.get("outcome", "")
    fill_type = trade.get("fill_type", "MAKER")

    logger.info(
        f"🔔 {fill_type} Trade: {side} | Market: {market_name} | "
        f"Outcome: {outcome} | Shares: {shares:.2f} | "
        f"Price: ${price:.4f} | Total: ${usdc_size:.2f} USDC"
    )


# ─── Order fill tracking ─────────────────────────────────────────────────────

# Tracks order_id → last known size_matched, so we only alert on new fills
_order_fill_state: dict[str, float] = {}


def handle_order_fill(
    event: dict[str, Any],
    api_key: str,
) -> dict[str, Any] | None:
    """
    Process an order event from the User channel.

    The User channel sends events for OUR orders with these types:
      - PLACEMENT: order placed (size_matched = 0)
      - UPDATE:    order partially or fully filled (size_matched increases)

    We detect fills by comparing size_matched to the previous value.
    Since these are OUR orders on the User channel, any fill means
    we are the maker (limit order filled by someone else's market order).

    Returns a trade dict to alert on, or None.
    """
    # The event_type for order events is "order" (lowercase)
    event_type = event.get("event_type", "")
    if event_type != "order":
        return None

    # Verify this is our order
    owner = event.get("owner", "")
    if owner != api_key:
        logger.debug(f"Order event for different owner: {owner[:16]}...")
        return None

    order_id = event.get("id", "")
    order_type = event.get("type", "")  # PLACEMENT, UPDATE, CANCELLATION
    size_matched = float(event.get("size_matched", 0))
    original_size = float(event.get("original_size", 0))

    logger.debug(
        f"Order event: id={order_id[:20]}... type={order_type} "
        f"matched={size_matched}/{original_size}"
    )

    if order_type == "PLACEMENT":
        # Record initial state if not fully filled
        if size_matched < original_size:
            _order_fill_state[order_id] = size_matched

        if size_matched > 0:
            # Instant fill on placement — alert!
            logger.info(f"Order instantly filled on placement: {size_matched} shares")
            new_fill = size_matched
        else:
            return None

    elif order_type == "UPDATE":
        prev_matched = _order_fill_state.get(order_id, 0)
        new_fill = size_matched - prev_matched
        
        if size_matched >= original_size:
            _order_fill_state.pop(order_id, None)
        else:
            _order_fill_state[order_id] = size_matched

        if new_fill <= 0:
            logger.debug(f"No new fill for order {order_id[:20]}...")
            return None

        logger.info(
            f"🔔 ORDER FILLED: +{new_fill:.2f} shares "
            f"(total {size_matched}/{original_size})"
        )
    elif order_type == "CANCELLATION":
        _order_fill_state.pop(order_id, None)
        logger.debug(f"Order cancelled, removing tracker for {order_id[:20]}...")
        return None
    else:
        # Other types — skip
        logger.debug(f"Skipping order event type: {order_type}")
        return None

    # Build trade info for notification
    trade_info = {
        "id": order_id,
        "asset_id": event.get("asset_id", ""),
        "side": event.get("side", "UNKNOWN"),
        "size": str(new_fill),
        "price": event.get("price", "0"),
        "outcome": event.get("outcome", ""),
        "market": "",  # Will be resolved via API
        "status": "FILLED",
        "timestamp": event.get("created_at", ""),
        "fill_type": "MAKER",
    }

    return trade_info


# ─── WebSocket client ────────────────────────────────────────────────────────


async def listen_for_trades() -> None:
    """
    Main async loop: connect WS → authenticate → subscribe → listen → alert.
    Reconnects with exponential backoff on connection loss.
    """
    reconnect_delay = RECONNECT_DELAY

    # Initialize Telegram notifier
    notifier = TelegramNotifier.from_env()
    if notifier:
        console.print("[green]✓ Telegram notifications enabled[/green]")
    else:
        console.print(
            "[yellow]⚠ Telegram not configured - using terminal output only[/yellow]"
        )

    async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
        while True:
            try:
                # Build subscription message for the User channel.
                subscribe_msg = json.dumps({
                    "auth": {
                        "apiKey": POLYMARKET_API_KEY,
                        "secret": POLYMARKET_API_SECRET,
                        "passphrase": POLYMARKET_API_PASSPHRASE,
                    },
                    "type": "user",
                })

                console.print(f"[cyan]Connecting to WebSocket...[/cyan]")
                async with websockets.connect(
                    WS_URL,
                    ping_interval=PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    await ws.send(subscribe_msg)
                    console.print(
                        Panel(
                            f"[bold]Connected to WebSocket[/bold]\n"
                            f"[cyan]Subscribed to User Channel[/cyan]\n\n"
                            f"[dim]Listening for order fills...[/dim]",
                            title="[bold green]Active[/bold green]",
                            border_style="green",
                        )
                    )
                    logger.info("WebSocket connected — subscribed to User Channel")

                    reconnect_delay = RECONNECT_DELAY

                    async for raw_message in ws:
                        logger.debug(f"Raw WS: {raw_message[:500]}")
                        try:
                            message = json.loads(raw_message)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"Non-JSON message: {raw_message[:200]}"
                            )
                            continue

                        # Handle list of events (batch) or single event
                        events = (
                            message if isinstance(message, list) else [message]
                        )

                        for event in events:
                            event_type = event.get("event_type", "")

                            if event_type == "order":
                                trade = handle_order_fill(
                                    event, POLYMARKET_API_KEY
                                )
                                if trade:
                                    # Resolve market name
                                    asset_id = trade.get("asset_id", "")
                                    if asset_id:
                                        trade["market"] = (
                                            await resolve_market_name(
                                                asset_id, http_client
                                            )
                                        )

                                    log_trade(trade)

                                    if notifier:
                                        await notifier.send_trade_alert(
                                            trade, http_client
                                        )
                                        console.print(
                                            "[dim]✓ Sent Telegram alert[/dim]"
                                        )
                                    else:
                                        alert = format_trade_alert(trade)
                                        console.print(alert)

                            elif event_type == "trade":
                                logger.debug(
                                    f"Trade event (info only): "
                                    f"id={event.get('id', '?')[:16]}... "
                                    f"status={event.get('status', '?')}"
                                )
                            else:
                                logger.debug(
                                    f"Other event: type={event_type}"
                                )

            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
                console.print(
                    f"[yellow]⚠ Connection closed: {e}. "
                    f"Reconnecting in {reconnect_delay}s...[/yellow]"
                )
            except websockets.WebSocketException as e:
                logger.error(f"WebSocket error: {e}")
                console.print(
                    f"[red]✗ WebSocket error: {e}. "
                    f"Reconnecting in {reconnect_delay}s...[/red]"
                )
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                console.print(
                    f"[red]✗ Error: {e}. "
                    f"Reconnecting in {reconnect_delay}s...[/red]"
                )

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Main entry point — validate config, show startup banner, run async loop."""
    missing = []
    if not POLYMARKET_API_KEY:
        missing.append("POLYMARKET_API_KEY")
    if not POLYMARKET_API_SECRET:
        missing.append("POLYMARKET_API_SECRET")
    if not POLYMARKET_API_PASSPHRASE:
        missing.append("POLYMARKET_API_PASSPHRASE")
    if not POLYMARKET_WALLET:
        missing.append("POLYMARKET_WALLET")

    if missing:
        console.print(
            f"[red]✗ Error: Missing required environment variable(s): "
            f"{', '.join(missing)}[/red]"
        )
        console.print(
            "[yellow]Please set them in your .env file "
            "(see .env.example)[/yellow]"
        )
        sys.exit(1)

    console.print(
        Panel(
            "[bold cyan]Polymarket Real-Time Trade Alert System[/bold cyan]\n"
            "[dim]WebSocket · Order Fills · Telegram Alerts[/dim]",
            border_style="cyan",
        )
    )

    console.print(f"[dim]API Key: {POLYMARKET_API_KEY[:8]}...{POLYMARKET_API_KEY[-4:]}[/dim]")
    console.print(f"[dim]Wallet: {POLYMARKET_WALLET}[/dim]")
    console.print(f"[dim]Reconnect delay: {RECONNECT_DELAY}s[/dim]")
    console.print(f"[dim]Log file: trades.log[/dim]")
    console.print()

    try:
        asyncio.run(listen_for_trades())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down gracefully...[/yellow]")
        logger.info("Application stopped by user")


if __name__ == "__main__":
    main()
