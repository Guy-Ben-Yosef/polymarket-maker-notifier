"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def fmt_num(x: float, decimals: int = 4) -> str:
    """Thousands separators, up to `decimals` places, trailing zeros removed."""
    s = f"{x:,.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class Fill:
    """One execution of one of our orders, on either venue."""

    venue: str          # "CLOB" or "PERPS"
    key: str            # unique key used for de-duplication
    role: str           # "MAKER" or "TAKER"
    side: str           # CLOB: BUY/SELL — Perps: LONG/SHORT
    market: str         # human-readable market name / instrument symbol
    outcome: str        # CLOB outcome ("Yes"/"Up"/…); "" for Perps
    size: float         # shares (CLOB) or contracts (Perps)
    price: float
    timestamp: float    # unix seconds
    fee: float = 0.0
    fee_asset: str = ""
    order_id: str = ""
    trade_id: str = ""
    asset_id: str = ""  # CLOB token id — used for name lookup
    flags: str = ""     # e.g. "LIQUIDATION", "ADL", "SETTLEMENT"
    filled_total: float | None = None    # cumulative filled on the order
    original_size: float | None = None   # the order's original size

    @property
    def notional(self) -> float:
        return self.size * self.price

    @property
    def fill_status(self) -> str:
        """'FULL', 'PARTIAL', or '' when the order size is unknown."""
        if self.filled_total is None or not self.original_size:
            return ""
        return "FULL" if self.filled_total >= self.original_size - 1e-9 else "PARTIAL"

    @property
    def progress(self) -> str:
        """e.g. '170/200', or '' when unknown."""
        if self.filled_total is None or not self.original_size:
            return ""
        return f"{fmt_num(self.filled_total)}/{fmt_num(self.original_size)}"

    @property
    def is_maker(self) -> bool:
        return self.role == "MAKER"

    @property
    def time_str(self) -> str:
        if not self.timestamp:
            return "Unknown"
        try:
            return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return str(self.timestamp)

    def summary(self) -> str:
        parts = [
            f"{self.venue} {self.role} {self.side}",
            f"market={self.market!r}",
        ]
        if self.outcome:
            parts.append(f"outcome={self.outcome}")
        parts.append(f"size={fmt_num(self.size)} @ {fmt_num(self.price)}")
        parts.append(f"notional={fmt_num(self.notional, 2)}")
        if self.progress:
            parts.append(f"order={self.progress} {self.fill_status}")
        if self.fee:
            parts.append(f"fee={self.fee:g} {self.fee_asset}".rstrip())
        if self.flags:
            parts.append(f"flags={self.flags}")
        return " | ".join(parts)
