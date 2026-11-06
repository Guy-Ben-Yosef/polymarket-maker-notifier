"""
Persistent de-duplication state.

Keeps a bounded set of fill keys we already alerted on, plus the timestamp
of the newest fill per venue (used to backfill after a restart).
Saved atomically to a small JSON file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import OrderedDict

logger = logging.getLogger(__name__)

MAX_KEYS = 5000


class FillState:
    def __init__(self, path: str):
        self.path = path
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.last_ts: dict[str, float] = {}  # venue -> unix seconds
        self._load()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read state file {self.path}: {e}")
            return

        for k in data.get("seen", [])[-MAX_KEYS:]:
            self._seen[str(k)] = None
        for venue, ts in (data.get("last_ts") or {}).items():
            try:
                self.last_ts[venue] = float(ts)
            except (TypeError, ValueError):
                pass
        logger.info(
            f"Loaded state: {len(self._seen)} seen fills, last_ts={self.last_ts}"
        )

    def save(self) -> None:
        payload = {"seen": list(self._seen.keys()), "last_ts": self.last_ts}
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"Could not write state file {self.path}: {e}")

    # ── API ────────────────────────────────────────────────────────────

    def is_seen(self, key: str) -> bool:
        return key in self._seen

    def mark(self, key: str, venue: str, ts: float) -> None:
        """Record a fill. Returns nothing; call save() afterwards."""
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > MAX_KEYS:
            self._seen.popitem(last=False)
        if ts and ts > self.last_ts.get(venue, 0):
            self.last_ts[venue] = ts
