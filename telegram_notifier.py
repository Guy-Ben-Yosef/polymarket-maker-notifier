"""
Telegram notifier: sends one message per fill to every configured chat,
in parallel, with retries and Telegram rate-limit handling.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from models import Fill, fmt_num

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
MAX_ATTEMPTS = 4
_MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape_md2(text: str) -> str:
    return "".join(f"\\{c}" if c in _MD2_SPECIAL else c for c in str(text))


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_ids: list[str] | tuple[str, ...], http: httpx.AsyncClient):
        self.chat_ids = list(chat_ids)
        self.http = http
        self._url = f"{TELEGRAM_API_URL}/bot{bot_token}/sendMessage"
        logger.info(f"Telegram notifier ready for {len(self.chat_ids)} recipient(s)")

    # ── formatting ─────────────────────────────────────────────────────

    @staticmethod
    def format_fill(f: Fill) -> str:
        is_long = f.side in ("BUY", "LONG")
        arrow = "📈" if is_long else "📉"
        venue = "Perps" if f.venue == "PERPS" else "Polymarket"
        unit = "contracts" if f.venue == "PERPS" else "shares"
        quote = "pUSD" if f.venue == "PERPS" else "USDC"

        status = f.fill_status
        headline = f"{arrow} {f.role} {f.side}"
        if status:
            headline += f" · {status} FILL"
        lines = [
            f"*{escape_md2(headline)}* · {escape_md2(venue)}",
            "",
            f"*Market:* {escape_md2(f.market)}",
        ]
        if f.outcome:
            lines.append(f"*Outcome:* {escape_md2(f.outcome)}")
        lines += [
            f"*Size:* {escape_md2(f'{fmt_num(f.size)} {unit}')}",
            f"*Price:* {escape_md2(fmt_num(f.price))}",
            f"*Total:* {escape_md2(f'{fmt_num(f.notional, 2)} {quote}')}",
        ]
        if f.progress:
            lines.append(f"*Order:* {escape_md2(f'{f.progress} {unit} filled ({status})')}")
        if f.fee:
            lines.append(f"*Fee:* {escape_md2(f'{f.fee:g} {f.fee_asset}'.strip())}")
        if f.flags:
            lines.append(f"*Flags:* {escape_md2(f.flags)}")
        lines += ["", f"_{escape_md2(f.time_str)}_"]
        return "\n".join(lines)

    # ── sending ────────────────────────────────────────────────────────

    async def send_fill(self, fill: Fill) -> bool:
        text = self.format_fill(fill)
        results = await asyncio.gather(
            *(self._send(cid, text) for cid in self.chat_ids), return_exceptions=True
        )
        ok = all(r is True for r in results)
        if not ok:
            logger.error(f"Telegram delivery incomplete for {fill.key}: {results}")
        return ok

    async def _send(self, chat_id: str, text: str) -> bool:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
        delay = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                r = await self.http.post(self._url, json=payload, timeout=15.0)
                if r.status_code == 429:
                    retry_after = 1.0
                    try:
                        retry_after = float(r.json().get("parameters", {}).get("retry_after", 1))
                    except Exception:
                        pass
                    logger.warning(f"Telegram rate-limited chat {chat_id}; retry in {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                if 400 <= r.status_code < 500:
                    # Permanent client error (bad chat id, bad markdown…). Don't retry.
                    logger.error(f"Telegram {r.status_code} for chat {chat_id}: {r.text[:300]}")
                    if "can't parse entities" in r.text:
                        # Fall back to plain text once.
                        plain = {"chat_id": chat_id, "text": _strip_md2(text)}
                        r2 = await self.http.post(self._url, json=plain, timeout=15.0)
                        return r2.status_code == 200
                    return False
                r.raise_for_status()
                body = r.json()
                if body.get("ok"):
                    return True
                logger.error(f"Telegram API error for chat {chat_id}: {body.get('description')}")
                return False
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning(
                    f"Telegram send failed for chat {chat_id} "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): {e}"
                )
            except Exception as e:
                logger.error(f"Unexpected Telegram error for chat {chat_id}: {e}", exc_info=True)
                return False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        return False


def _strip_md2(text: str) -> str:
    out = []
    skip = False
    for c in text:
        if skip:
            out.append(c)
            skip = False
        elif c == "\\":
            skip = True
        elif c in "*_":
            continue
        else:
            out.append(c)
    return "".join(out)
