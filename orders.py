"""
Order progress tracking: how much of an order's original size is filled.

Two sources are merged:
  * order snapshots from the venue (original size + matched so far), and
  * a running sum of the fills we have seen for the order.
The larger "matched" value wins, so progress is right whether the order
update arrives before or after the fill event.
"""

from __future__ import annotations

from collections import OrderedDict

MAX_ITEMS = 2000


class OrderTracker:
    def __init__(self) -> None:
        self._orders: OrderedDict[str, tuple[float, float]] = OrderedDict()  # id -> (original, matched)
        self._sums: OrderedDict[str, float] = OrderedDict()                   # id -> sum of our fills
        self._seen: OrderedDict[str, None] = OrderedDict()                    # fill keys counted

    @staticmethod
    def _trim(d: OrderedDict) -> None:
        while len(d) > MAX_ITEMS:
            d.popitem(last=False)

    def update(self, order_id: str, original: float, matched: float) -> None:
        if not order_id or original <= 0:
            return
        prev = self._orders.get(order_id)
        if prev and prev[1] > matched:
            matched = prev[1]  # never go backwards
        self._orders[order_id] = (original, matched)
        self._orders.move_to_end(order_id)
        self._trim(self._orders)

    def forget(self, order_id: str) -> None:
        self._orders.pop(order_id, None)
        self._sums.pop(order_id, None)

    def known(self, order_id: str) -> bool:
        return order_id in self._orders

    def record_fill(
        self, order_id: str, fill_key: str, amount: float
    ) -> tuple[float, float] | None:
        """
        Count `amount` towards `order_id` once per `fill_key`, then return
        (filled_total, original_size) if the order's size is known.
        """
        if not order_id:
            return None
        if fill_key not in self._seen:
            self._seen[fill_key] = None
            self._trim(self._seen)
            self._sums[order_id] = self._sums.get(order_id, 0.0) + amount
            self._sums.move_to_end(order_id)
            self._trim(self._sums)

        snap = self._orders.get(order_id)
        if not snap:
            return None
        original, matched = snap
        filled = max(matched, self._sums.get(order_id, 0.0))
        filled = min(filled, original)
        if filled >= original - 1e-9:
            # Fully filled — snapshot no longer needed after this report.
            self._orders[order_id] = (original, original)
        return filled, original
