"""
Polymarket Maker Fill Notifier

Streams your own fills from the Polymarket CLOB (prediction markets) and
Polymarket Perps over authenticated WebSockets, and sends a Telegram alert
for every fill where you were the MAKER.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from logging.handlers import RotatingFileHandler

import httpx

from clob import ClobListener
from config import Settings
from console import Console
from models import Fill
from perps import PerpsListener
from state import FillState
from telegram_notifier import TelegramNotifier

logger = logging.getLogger("notifier")

CashSource = Callable[[], Awaitable[tuple[float, str] | None]]
CASH_LOOKUP_TIMEOUT = 8.0  # seconds; the alert goes out without cash after this


def setup_logging(s: Settings) -> None:
    level = getattr(logging, s.log_level, logging.INFO)
    handler: logging.Handler
    if s.log_max_bytes > 0:
        handler = RotatingFileHandler(
            s.log_file,
            maxBytes=s.log_max_bytes,
            backupCount=s.log_backup_count,
            encoding="utf-8",
        )
    else:
        handler = logging.FileHandler(s.log_file, encoding="utf-8")
    # Warnings and errors also go to stderr so hosted log viewers (Railway)
    # show them without access to the log file.
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[handler, stderr],
    )
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Pipeline:
    """De-duplicates fills, logs them, and dispatches maker alerts."""

    def __init__(
        self,
        state: FillState,
        console: Console,
        notifier: TelegramNotifier | None,
    ):
        self.state = state
        self.console = console
        self.notifier = notifier
        # venue -> coroutine returning (free cash, asset) or None
        self.cash_sources: dict[str, CashSource] = {}
        self._tasks: set[asyncio.Task] = set()

    async def on_fill(self, fill: Fill) -> None:
        if self.state.is_seen(fill.key):
            logger.debug(f"duplicate fill ignored: {fill.key}")
            return
        self.state.mark(fill.key, fill.venue, fill.timestamp)
        self.state.save()

        logger.info(f"🔔 {fill.summary()}")
        if not fill.is_maker:
            self.console.print(f"[dim]· taker fill (no alert): {fill.summary()}[/dim]")
            return

        # Fire-and-forget so the WebSocket loop is never blocked by REST or Telegram.
        task = asyncio.create_task(self._alert(fill))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _alert(self, fill: Fill) -> None:
        await self._attach_cash(fill)
        self.console.fill(fill)
        if not self.notifier:
            return
        ok = await self.notifier.send_fill(fill)
        self.console.print(
            "[dim]✓ Telegram alert sent[/dim]" if ok else "[red]✗ Telegram alert failed[/red]"
        )

    async def _attach_cash(self, fill: Fill) -> None:
        source = self.cash_sources.get(fill.venue)
        if source is None:
            return
        try:
            res = await asyncio.wait_for(source(), timeout=CASH_LOOKUP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"{fill.venue} cash lookup timed out; alert sent without cash")
            return
        except Exception as e:
            logger.warning(f"{fill.venue} cash lookup failed: {e}")
            return
        if res:
            fill.cash, fill.cash_asset = res

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


async def run(s: Settings, console: Console) -> None:
    state = FillState(s.state_file)

    async with httpx.AsyncClient(verify=s.ssl_verify, timeout=20.0) as http:
        notifier = (
            TelegramNotifier(s.telegram_bot_token, s.telegram_chat_ids, http)
            if s.telegram_enabled
            else None
        )
        console.print(
            "[green]✓ Telegram notifications enabled[/green]"
            if notifier
            else "[yellow]⚠ Telegram not configured — terminal output only[/yellow]"
        )
        if not s.ssl_verify:
            console.print("[yellow]⚠ SSL verification is DISABLED (SSL_VERIFY=false)[/yellow]")

        pipeline = Pipeline(state, console, notifier)
        listeners: list[ClobListener | PerpsListener] = []
        if s.clob_enabled:
            listeners.append(
                ClobListener(s, console, pipeline.on_fill, http, state.last_ts.get("CLOB"))
            )
        if s.perps_enabled:
            listeners.append(
                PerpsListener(s, console, pipeline.on_fill, http, state.last_ts.get("PERPS"))
            )
        for l in listeners:
            pipeline.cash_sources[l.name] = l.free_cash

        try:
            await asyncio.gather(*(l.run() for l in listeners))
        finally:
            await pipeline.drain()


def main() -> None:
    s = Settings.load()
    console = Console()

    if s.missing:
        console.print(f"[red]✗ Missing configuration: {', '.join(s.missing)}[/red]")
        console.print("[yellow]Set the variables in your .env file (see .env.example)[/yellow]")
        sys.exit(1)

    setup_logging(s)

    venues = []
    if s.clob_enabled:
        venues.append("CLOB")
    if s.perps_enabled:
        venues.append("Perps")

    console.panel(
        "[bold cyan]Polymarket Maker Fill Notifier[/bold cyan]\n"
        f"[dim]Venues: {', '.join(venues)} · maker-only alerts · Telegram[/dim]",
        style="cyan",
    )
    if s.clob_enabled:
        console.print(f"[dim]CLOB API key: {s.clob_api_key[:8]}…{s.clob_api_key[-4:]}[/dim]")
        if s.clob_wallet:
            console.print(f"[dim]CLOB wallet: {s.clob_wallet}[/dim]")
    if s.perps_enabled:
        console.print(f"[dim]Perps proxy: {s.perps_proxy_address}[/dim]")
    console.print(f"[dim]Log: {s.log_file} ({s.log_level}) · State: {s.state_file}[/dim]")
    console.print()

    try:
        asyncio.run(run(s, console))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down…[/yellow]")
        logger.info("stopped by user")


if __name__ == "__main__":
    main()
