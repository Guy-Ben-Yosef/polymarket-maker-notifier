"""
Common WebSocket listener plumbing: reconnect loop with exponential
backoff, application-level keepalive, and a stale-connection watchdog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from config import Settings
from console import Console
from models import Fill

logger = logging.getLogger(__name__)

OnFill = Callable[[Fill], Awaitable[None]]


class StaleConnection(Exception):
    """No frame received within the watchdog window."""


class AuthFailed(Exception):
    """The venue rejected our credentials."""


class WSListener:
    name: str = "WS"
    ws_url: str = ""
    stale_after: float = 60.0   # seconds without any frame -> reconnect

    def __init__(self, settings: Settings, console: Console, on_fill: OnFill):
        self.settings = settings
        self.console = console
        self.on_fill = on_fill
        self._healthy = False

    # ── to override ────────────────────────────────────────────────────

    async def _session(self, ws: Any) -> None:
        """Authenticate, subscribe and consume frames until the socket closes."""
        raise NotImplementedError

    async def _on_connect(self) -> None:
        """Optional hook run before each connection attempt (e.g. backfill)."""

    # ── helpers for subclasses ─────────────────────────────────────────

    def mark_healthy(self) -> None:
        self._healthy = True

    async def recv(self, ws: Any) -> str:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after)
        except asyncio.TimeoutError:
            raise StaleConnection(
                f"no frame for {self.stale_after:.0f}s"
            ) from None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if self.settings.log_raw_events:
            logger.debug(f"[{self.name}] raw: {raw}")
        return raw

    @staticmethod
    def parse_json(raw: str) -> Any | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def keepalive(self, ws: Any, frame: str, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # socket gone — main loop will notice
            logger.debug(f"[{self.name}] keepalive stopped: {e}")

    # ── main loop ──────────────────────────────────────────────────────

    async def run(self) -> None:
        delay = self.settings.reconnect_delay
        while True:
            self._healthy = False
            try:
                await self._on_connect()
                self.console.print(f"[cyan]{self.name}: connecting…[/cyan]")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=1024,
                ) as ws:
                    await self._session(ws)
                logger.info(f"[{self.name}] connection closed cleanly")
            except asyncio.CancelledError:
                raise
            except AuthFailed as e:
                logger.error(f"[{self.name}] authentication failed: {e}")
                self.console.print(f"[red]✗ {self.name}: auth failed: {e}[/red]")
                # Credentials are wrong or expired; back off hard.
                delay = self.settings.max_reconnect_delay
            except (
                websockets.ConnectionClosed,
                websockets.WebSocketException,
                StaleConnection,
                OSError,
            ) as e:
                logger.warning(f"[{self.name}] connection lost: {e!r}")
                self.console.print(
                    f"[yellow]⚠ {self.name}: connection lost ({e}). "
                    f"Reconnecting in {delay}s…[/yellow]"
                )
            except Exception as e:
                logger.error(f"[{self.name}] unexpected error: {e}", exc_info=True)
                self.console.print(
                    f"[red]✗ {self.name}: error: {e}. Reconnecting in {delay}s…[/red]"
                )

            await asyncio.sleep(delay)
            if self._healthy:
                delay = self.settings.reconnect_delay
            else:
                delay = min(delay * 2, self.settings.max_reconnect_delay)
