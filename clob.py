"""
Polymarket CLOB (prediction markets) — authenticated User channel.

Maker detection uses `trade` events, which carry `trader_side`
(MAKER / TAKER) and the list of `maker_orders` that were matched.

Important: on a MAKER-side trade event, the top-level `side`, `price`,
`asset_id` and `outcome` describe the *taker's* order. Our own values
are in the `maker_orders` entry whose `owner` is our API key.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from config import Settings
from console import Console
from models import Fill
from orders import OrderTracker
from wsbase import OnFill, WSListener

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
REST_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

PING_FRAME = "PING"
PING_INTERVAL = 10.0   # docs: send text "PING" every 10 s
STALE_AFTER = 45.0

_TRADE_TYPES = {"TRADE"}
_ORDER_TYPES = {"PLACEMENT", "UPDATE", "CANCELLATION"}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(event: dict[str, Any]) -> float:
    """Best-effort unix-seconds timestamp from a trade payload."""
    for key, scale in (("match_time", 1), ("last_update", 1), ("timestamp", 1000)):
        v = _f(event.get(key))
        if v > 0:
            return v / scale if v > 1e11 else v
    return time.time()


class ClobListener(WSListener):
    name = "CLOB"
    ws_url = WS_URL
    stale_after = STALE_AFTER

    def __init__(
        self,
        settings: Settings,
        console: Console,
        on_fill: OnFill,
        http: httpx.AsyncClient,
        last_ts: float | None,
    ):
        super().__init__(settings, console, on_fill)
        self.http = http
        self._api_key = settings.clob_api_key.lower()
        self._wallet = settings.clob_wallet.lower()
        self._names: dict[str, str] = {}
        self._backfill_from: float | None = last_ts
        self._backfill_warned = False
        self.orders = OrderTracker()

    # ── session ────────────────────────────────────────────────────────

    async def _session(self, ws: Any) -> None:
        s = self.settings
        await ws.send(
            self._json(
                {
                    "auth": {
                        "apiKey": s.clob_api_key,
                        "secret": s.clob_api_secret,
                        "passphrase": s.clob_api_passphrase,
                    },
                    "type": "user",
                }
            )
        )
        self.console.panel(
            "[bold]Connected[/bold]\n[dim]Listening for fills on the User channel…[/dim]",
            title="[bold green]CLOB active[/bold green]",
            style="green",
        )
        logger.info("[CLOB] connected and subscribed to user channel")

        ka = asyncio.create_task(self.keepalive(ws, PING_FRAME, PING_INTERVAL))
        try:
            while True:
                raw = await self.recv(ws)
                self.mark_healthy()
                if raw == "PONG":
                    continue
                msg = self.parse_json(raw)
                if msg is None:
                    logger.debug(f"[CLOB] non-JSON frame: {raw[:120]}")
                    continue
                for event in msg if isinstance(msg, list) else [msg]:
                    if isinstance(event, dict):
                        await self._handle_event(event)
        finally:
            ka.cancel()

    @staticmethod
    def _json(obj: Any) -> str:
        return json.dumps(obj)

    # ── event handling ─────────────────────────────────────────────────

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("event_type", "")).lower()
        sub = str(event.get("type", "")).upper()

        if etype == "trade" or sub in _TRADE_TYPES:
            for fill in self.parse_trade(event):
                await self._enrich(fill)
                await self.on_fill(fill)
        elif etype == "order" or sub in _ORDER_TYPES:
            order_id = str(event.get("id", ""))
            logger.debug(
                f"[CLOB] order {sub}: id={order_id[:18]}… "
                f"matched={event.get('size_matched')}/{event.get('original_size')} "
                f"status={event.get('status')}"
            )
            if sub == "CANCELLATION":
                self.orders.forget(order_id)
            else:
                self.orders.update(
                    order_id, _f(event.get("original_size")), _f(event.get("size_matched"))
                )
        else:
            logger.debug(f"[CLOB] other event: {etype or sub or '?'}")

    def _is_ours(self, obj: dict[str, Any]) -> bool:
        owner = str(obj.get("owner", "")).lower()
        if owner and owner == self._api_key:
            return True
        addr = str(obj.get("maker_address", "")).lower()
        return bool(self._wallet) and addr == self._wallet

    def parse_trade(self, event: dict[str, Any]) -> list[Fill]:
        """
        Convert a `trade` payload (WS or REST) into zero or more Fills.
        The same trade is delivered several times as its status progresses
        (MATCHED → MINED → CONFIRMED); de-duplication happens downstream
        via Fill.key.
        """
        trade_id = str(event.get("id", ""))
        status = str(event.get("status", "")).upper()
        trader_side = str(event.get("trader_side") or "").upper()
        makers = [m for m in (event.get("maker_orders") or []) if isinstance(m, dict)]
        ours = [m for m in makers if self._is_ours(m)]
        condition_id = str(event.get("market", ""))
        ts = _ts(event)

        if status == "FAILED":
            logger.warning(f"[CLOB] trade {trade_id} FAILED on-chain")

        # Decide our role.
        if trader_side == "MAKER":
            role = "MAKER"
        elif trader_side == "TAKER":
            role = "TAKER"
        elif ours and not self._is_ours(event):
            role = "MAKER"
        else:
            role = "TAKER"

        fills: list[Fill] = []
        if role == "MAKER":
            if not ours:
                logger.warning(
                    f"[CLOB] MAKER trade {trade_id} but no maker_orders entry "
                    "matched our API key/wallet — using top-level fields"
                )
                ours = [
                    {
                        "order_id": "",
                        "asset_id": event.get("asset_id", ""),
                        "outcome": event.get("outcome", ""),
                        "side": event.get("side", ""),
                        "price": event.get("price", 0),
                        "matched_amount": event.get("size", 0),
                    }
                ]
            for m in ours:
                order_id = str(m.get("order_id", ""))
                fills.append(
                    Fill(
                        venue="CLOB",
                        key=f"clob:{trade_id}:{order_id or 'maker'}",
                        role="MAKER",
                        side=str(m.get("side", "")).upper() or "?",
                        market=condition_id,
                        outcome=str(m.get("outcome", "")),
                        size=_f(m.get("matched_amount")),
                        price=_f(m.get("price")),
                        timestamp=ts,
                        order_id=order_id,
                        trade_id=trade_id,
                        asset_id=str(m.get("asset_id", "")),
                    )
                )
        else:
            fills.append(
                Fill(
                    venue="CLOB",
                    key=f"clob:{trade_id}:taker",
                    role="TAKER",
                    side=str(event.get("side", "")).upper() or "?",
                    market=condition_id,
                    outcome=str(event.get("outcome", "")),
                    size=_f(event.get("size")),
                    price=_f(event.get("price")),
                    timestamp=ts,
                    order_id=str(event.get("taker_order_id", "")),
                    trade_id=trade_id,
                    asset_id=str(event.get("asset_id", "")),
                )
            )
        return fills

    # ── enrichment: market name + order progress ───────────────────────

    async def _enrich(self, fill: Fill) -> None:
        if fill.asset_id:
            fill.market = await self.resolve_name(fill.asset_id)
        await self._attach_progress(fill)

    async def _attach_progress(self, fill: Fill) -> None:
        if not fill.order_id:
            return
        res = self.orders.record_fill(fill.order_id, fill.key, fill.size)
        if res is None and self._wallet:
            snap = await self._fetch_order(fill.order_id)
            if snap:
                self.orders.update(fill.order_id, *snap)
                res = self.orders.record_fill(fill.order_id, fill.key, fill.size)
        if res:
            fill.filled_total, fill.original_size = res

    async def _fetch_order(self, order_id: str) -> tuple[float, float] | None:
        """Return (original_size, size_matched) for one of our orders via REST."""
        path = f"/data/order/{order_id}"
        try:
            r = await asyncio.wait_for(
                self.http.get(f"{REST_URL}{path}", headers=self._l2_headers("GET", path)),
                timeout=self.settings.name_lookup_timeout + 1,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.info(f"[CLOB] order lookup failed for {order_id[:18]}…: {e}")
            return None
        if not isinstance(data, dict):
            return None
        original = _f(data.get("original_size"))
        matched = _f(data.get("size_matched"))
        return (original, matched) if original > 0 else None

    # ── market names ───────────────────────────────────────────────────

    async def resolve_name(self, token_id: str) -> str:
        cached = self._names.get(token_id)
        if cached:
            return cached
        short = f"{token_id[:10]}…{token_id[-6:]}" if len(token_id) > 20 else token_id
        try:
            name = await asyncio.wait_for(
                self._fetch_name(token_id), timeout=self.settings.name_lookup_timeout
            )
        except asyncio.TimeoutError:
            logger.info(f"[CLOB] name lookup timed out for {short}; alerting anyway")
            return short
        except Exception as e:
            logger.warning(f"[CLOB] name lookup failed for {short}: {e}")
            return short
        if name:
            self._names[token_id] = name
            if len(self._names) > 2000:
                self._names.pop(next(iter(self._names)))
            return name
        return short

    async def _fetch_name(self, token_id: str) -> str:
        r = await self.http.get(
            f"{GAMMA_URL}/markets", params={"clob_token_ids": token_id}
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return str(data[0].get("question") or "")
        return ""

    # ── backfill after (re)connect ─────────────────────────────────────

    async def _on_connect(self) -> None:
        if self._backfill_from is None:
            if self.settings.backfill_window <= 0:
                return
            self._backfill_from = time.time() - self.settings.backfill_window
        if not self._wallet:
            if not self._backfill_warned:
                logger.info(
                    "[CLOB] POLYMARKET_WALLET not set — skipping REST backfill "
                    "(fills during downtime will be missed)"
                )
                self._backfill_warned = True
            return
        since = int(self._backfill_from) - 60
        try:
            trades = await self._fetch_trades(after=since)
        except Exception as e:
            logger.warning(f"[CLOB] backfill failed: {e}")
            return
        count = 0
        for t in trades:
            for fill in self.parse_trade(t):
                await self._enrich(fill)
                await self.on_fill(fill)
                count += 1
        logger.info(f"[CLOB] backfill checked {len(trades)} trades since {since}")
        self._backfill_from = time.time()

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        s = self.settings
        ts = str(int(time.time()))
        secret = base64.urlsafe_b64decode(s.clob_api_secret)
        msg = f"{ts}{method}{path}{body}".encode()
        sig = base64.urlsafe_b64encode(
            hmac.new(secret, msg, hashlib.sha256).digest()
        ).decode()
        return {
            "POLY_ADDRESS": s.clob_wallet,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": s.clob_api_key,
            "POLY_PASSPHRASE": s.clob_api_passphrase,
        }

    async def _fetch_trades(self, after: int) -> list[dict[str, Any]]:
        path = "/data/trades"
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(20):  # hard cap on pages
            params: dict[str, Any] = {"after": after}
            if cursor:
                params["next_cursor"] = cursor
            r = await self.http.get(
                f"{REST_URL}{path}", params=params, headers=self._l2_headers("GET", path)
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
                break
            out.extend(d for d in data.get("data", []) if isinstance(d, dict))
            cursor = str(data.get("next_cursor") or "")
            if not cursor or cursor == "LTE=":
                break
        return out
