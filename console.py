"""
Console output. Uses `rich` when installed and stdout is a terminal,
otherwise falls back to plain print(). `rich` is therefore optional.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from models import Fill, fmt_num

try:  # pragma: no cover - import guard
    from rich.console import Console as _RichConsole
    from rich.panel import Panel
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

_TAG_RE = re.compile(r"\[/?[a-zA-Z0-9 _#]*\]")


class Console:
    def __init__(self) -> None:
        self._rich = _RichConsole() if (_HAS_RICH and sys.stdout.isatty()) else None

    def print(self, markup: str = "") -> None:
        if self._rich:
            self._rich.print(markup)
        else:
            print(_TAG_RE.sub("", markup), flush=True)

    def panel(self, body: str, title: str = "", style: str = "cyan") -> None:
        if self._rich:
            self._rich.print(Panel(body, title=title, border_style=style))
        else:
            clean_title = _TAG_RE.sub("", title)
            clean_body = _TAG_RE.sub("", body)
            print(f"== {clean_title} ==" if clean_title else "====", flush=True)
            print(clean_body, flush=True)
            print("====", flush=True)

    def fill(self, f: Fill) -> None:
        is_long = f.side in ("BUY", "LONG")
        color = "green" if is_long else "red"
        symbol = "📈" if is_long else "📉"

        if not self._rich:
            print(f"🔔 {symbol} {f.summary()} | {f.time_str}", flush=True)
            return

        text: Any = Text()
        text.append(f"{symbol} ", style="bold")
        text.append(f"{f.side}\n", style=f"bold {color}")
        text.append("Market: ", style="dim")
        text.append(f"{f.market}\n", style="white")
        if f.outcome:
            text.append("Outcome: ", style="dim")
            text.append(f"{f.outcome}\n", style="magenta")
        text.append("Size: ", style="dim")
        text.append(f"{fmt_num(f.size)}\n", style="cyan")
        text.append("Price: ", style="dim")
        text.append(f"{fmt_num(f.price)}\n", style="yellow")
        text.append("Total: ", style="dim")
        text.append(f"{fmt_num(f.notional, 2)}", style=f"bold {color}")
        if f.fee:
            text.append(f"\nFee: {f.fee:g} {f.fee_asset}", style="dim")
        if f.flags:
            text.append(f"\n{f.flags}", style="bold yellow")

        self._rich.print(
            Panel(
                text,
                title=f"[bold {color}]{f.venue} {f.role} fill[/bold {color}]",
                subtitle=f"[dim]{f.time_str}[/dim]",
                border_style=color,
            )
        )
