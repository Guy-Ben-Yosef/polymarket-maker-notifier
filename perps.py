"""
Polymarket Perps — private WebSocket `fills` channel.

Auth uses a proxy signer created with POST /v1/account/proxy
(see generate_perps_creds.py). Each fill carries `taker: bool`;
`taker == false` means we were the maker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from config import Settings
from console import Console
from models import Fill
from orders import OrderTracker
from wsbase import AuthFailed, OnFill, WSListener

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.perpetuals.polymarket.com/v1/ws"
REST_URL = "https://api.perpetuals.polymarket.com"

PING_INTERVAL = 25.0   # docs: ping ~every 25 s
STALE_AFTER = 65.0     # docs: treat as stale after ~65 s of silence
INSTRUMENT_REFRESH = 3600.0

AUTH_ID = 1
SUB_ID = 2
PING_ID = 0
PING_FRAME = json.dumps({"id": PING_ID, "req": "post", "op": {"type": "ping"}})


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


class PerpsListener(WSListener):
    name = "PERPS"
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
        self._instruments: dict[int, str] = {}
        self._instruments_loaded_at = 0.0
        self._last_sq: int | None = None
        self._backfill_from: float | None = last_ts
        self.orders = OrderTracker()

    # ── session ────────────────────────────────────────────────────────

    async def _session(self, ws: Any) -> None:
        s = self.settings
        await ws.send(
            json.dumps(
                {
                    "id": AUTH_ID,
                    "req": "post",
                    "op": {
                        "type": "auth",
                        "args": {
                            "proxy": s.perps_proxy_address,
                            "secret": s.perps_proxy_secret,
                        },
                    },
                }
            )
        )
        # Wait for the auth acknowledgement before subscribing.
        ack = await self._await_response(ws, AUTH_ID)
        data = ack.get("data")
        if not isinstance(data, dict) or data.get("status") != "ok":
            err = data.get("error") if isinstance(data, dict) else None
            raise AuthFailed(str(err or ack))

        await ws.send(json.dumps({"id": SUB_ID, "req": "sub", "chs": ["fills", "orders"]}))
        self.console.panel(
            "[bold]Connected[/bold]\n[dim]Listening on the private fills channel…[/dim]",
            title="[bold green]Perps active[/bold green]",
            style="green",
        )
        logger.info("[PERPS] authenticated; subscribed to fills")
        self._last_sq = None

        ka = asyncio.create_task(self.keepalive(ws, PING_FRAME, PING_INTERVAL))
        try:
            while True:
                raw = await self.recv(ws)
                self.mark_healthy()
                msg = self.parse_json(raw)
                if not isinstance(msg, dict):
                    logger.debug(f"[PERPS] unexpected frame: {raw[:120]}")
                    continue
                await self._handle_message(msg)
        finally:
            ka.cancel()

    async def _await_response(self, ws: Any, req_id: int, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await self.recv(ws)
            msg = self.parse_json(raw)
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg
            if isinstance(msg, dict):
                await self._handle_message(msg)
        raise AuthFailed(f"no response to request id={req_id} within {timeout}s")

    # ── message handling ───────────────────────────────────────────────

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        if "id" in msg and "ch" not in msg:
            rid = msg.get("id")
            data = msg.get("data")
            if rid == PING_ID:
                return
            # Responses may carry a dict ({"status": ...}) or a list
            # (e.g. the channels now subscribed). Treat only an explicit
            # non-ok status as a failure.
            status = data.get("status") if isinstance(data, dict) else None
            if rid == SUB_ID:
                if status not in (None, "ok"):
                    logger.error(f"[PERPS] subscribe failed: {data}")
                else:
                    logger.info(f"[PERPS] subscribe ack: {data}")
                return
            logger.debug(f"[PERPS] response id={rid}: {data}")
            return

        ch = str(msg.get("ch", ""))
        self._check_sequence(ch, msg.get("sq"))

        data = msg.get("data")
        records = data if isinstance(data, list) else [data]

        if ch == "fills":
            for rec in records:
                if isinstance(rec, dict):
                    fill = await self.parse_fill(rec)
                    if fill:
                        await self._attach_progress(fill)
                        await self.on_fill(fill)
        elif ch == "orders":
            for rec in records:
                if isinstance(rec, dict):
                    self._update_order(rec)
        elif ch == "resync":
            logger.warning("[PERPS] server requested resync — running backfill")
            await self._backfill()
        else:
            logger.debug(f"[PERPS] message on channel {ch!r}")

    def _check_sequence(self, ch: str, sq: Any) -> None:
        if ch != "fills" or not isinstance(sq, int):
            return
        if self._last_sq is not None and sq > self._last_sq + 1:
            logger.warning(
                f"[PERPS] sequence gap on fills: {self._last_sq} → {sq}; "
                "will backfill on next connect"
            )
            # Schedule a backfill without blocking the stream.
            asyncio.get_running_loop().create_task(self._backfill())
        self._last_sq = sq

    async def parse_fill(self, rec: dict[str, Any]) -> Fill | None:
        """Accepts both the WS short-name schema and the REST long-name schema."""
        tid = str(_first(rec, "tid", "trade_id", default=""))
        oid = str(_first(rec, "oid", "order_id", default=""))
        iid_raw = _first(rec, "iid", "instrument_id")
        try:
            iid = int(iid_raw)
        except (TypeError, ValueError):
            iid = -1
        side = str(rec.get("side", "")).upper() or "?"
        price = _f(_first(rec, "p", "price"))
        qty = _f(_first(rec, "qty", "quantity"))
        taker = bool(_first(rec, "taker", default=True))
        fee = _f(rec.get("fee"))
        fee_asset = str(_first(rec, "fea", "fee_asset", default="") or "")
        ts_raw = _f(_first(rec, "ts", "timestamp"))
        ts = ts_raw / 1000 if ts_raw > 1e11 else (ts_raw or time.time())

        flags = []
        if _first(rec, "liq", "liquidation", default=False):
            flags.append("LIQUIDATION")
        if rec.get("adl"):
            flags.append("ADL")
        if rec.get("settlement"):
            flags.append("SETTLEMENT")

        if not tid and not oid:
            logger.debug(f"[PERPS] fill without ids: {rec}")
            return None

        return Fill(
            venue="PERPS",
            key=f"perps:{tid}:{oid}",
            role="TAKER" if taker else "MAKER",
            side=side,
            market=await self.instrument_name(iid),
            outcome="",
            size=qty,
            price=price,
            timestamp=ts,
            fee=fee,
            fee_asset=fee_asset,
            order_id=oid,
            trade_id=tid,
            flags=",".join(flags),
        )

    # ── order progress ─────────────────────────────────────────────────

    def _update_order(self, rec: dict[str, Any]) -> None:
        """Accepts the WS `orders` schema (oid/qty/fill) or REST long names."""
        oid = str(_first(rec, "oid", "order_id", default=""))
        original = _f(_first(rec, "qty", "quantity", "size"))
        filled = _f(_first(rec, "fill", "filled", "filled_quantity", "filled_size"))
        if oid and original > 0:
            self.orders.update(oid, original, filled)
            logger.debug(f"[PERPS] order {oid}: {filled}/{original} {rec.get('status', '')}")

    async def _attach_progress(self, fill: Fill) -> None:
        if not fill.order_id:
            return
        res = self.orders.record_fill(fill.order_id, fill.key, fill.size)
        if res is None:
            rec = await self._fetch_order(fill.order_id)
            if rec:
                self._update_order(rec)
                res = self.orders.record_fill(fill.order_id, fill.key, fill.size)
        if res:
            fill.filled_total, fill.original_size = res

    async def _fetch_order(self, oid: str) -> dict[str, Any] | None:
        try:
            r = await asyncio.wait_for(
                self.http.get(
                    f"{REST_URL}/v1/account/orders",
                    params={"order_id": oid},
                    headers=self._auth_headers(),
                ),
                timeout=self.settings.name_lookup_timeout + 1,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            logger.info(f"[PERPS] order lookup failed for {oid}: {e}")
            return None
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None

    # ── instruments ────────────────────────────────────────────────────

    async def instrument_name(self, iid: int) -> str:
        stale = time.monotonic() - self._instruments_loaded_at > INSTRUMENT_REFRESH
        if iid not in self._instruments or stale:
            await self._load_instruments()
        return self._instruments.get(iid, f"instrument #{iid}")

    async def _load_instruments(self) -> None:
        try:
            r = await asyncio.wait_for(
                self.http.get(f"{REST_URL}/v1/info/instruments"),
                timeout=self.settings.name_lookup_timeout + 3,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"[PERPS] could not load instruments: {e}")
            self._instruments_loaded_at = time.monotonic()  # avoid hammering
            return
        items = data if isinstance(data, list) else data.get("data", [])
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                iid = int(it.get("instrument_id"))
            except (TypeError, ValueError):
                continue
            name = it.get("display_symbol") or it.get("symbol") or f"instrument #{iid}"
            self._instruments[iid] = str(name)
        self._instruments_loaded_at = time.monotonic()
        logger.info(f"[PERPS] loaded {len(self._instruments)} instruments")

    # ── backfill ───────────────────────────────────────────────────────

    async def _on_connect(self) -> None:
        if not self._instruments:
            await self._load_instruments()
        await self._backfill()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "polymarket-proxy": self.settings.perps_proxy_address,
            "polymarket-secret": self.settings.perps_proxy_secret,
        }

    async def _backfill(self) -> None:
        if self._backfill_from is None:
            if self.settings.backfill_window <= 0:
                return
            self._backfill_from = time.time() - self.settings.backfill_window
        start_ms = int((self._backfill_from - 60) * 1000)
        end_ms = int(time.time() * 1000)
        params: dict[str, Any] = {
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
        }
        records: list[dict[str, Any]] = []
        try:
            for _ in range(10):  # page cap
                r = await self.http.get(
                    f"{REST_URL}/v1/account/fills",
                    params=params,
                    headers=self._auth_headers(),
                )
                r.raise_for_status()
                body = r.json()
                page = body.get("data", []) if isinstance(body, dict) else body
                records.extend(p for p in page if isinstance(p, dict))
                cursor = body.get("cursor") or body.get("next_cursor") if isinstance(body, dict) else None
                if not (isinstance(body, dict) and body.get("more") and cursor):
                    break
                params["cursor"] = cursor
        except Exception as e:
            logger.warning(f"[PERPS] backfill failed: {e}")
            return

        for rec in records:
            fill = await self.parse_fill(rec)
            if fill:
                await self._attach_progress(fill)
                await self.on_fill(fill)
        logger.info(f"[PERPS] backfill checked {len(records)} fills since {start_ms}")
        self._backfill_from = time.time()
