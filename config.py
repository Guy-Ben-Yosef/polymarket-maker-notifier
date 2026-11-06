"""
Configuration loaded from environment variables (.env).

All settings are read once at startup into an immutable Settings object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── Polymarket CLOB (prediction markets) ─────────────────────────────
    clob_api_key: str
    clob_api_secret: str
    clob_api_passphrase: str
    # Signer (EOA) address that derived the API key. Optional.
    # Used to (a) match maker orders by address and (b) backfill trades
    # from the REST API after a reconnect.
    clob_wallet: str

    # ── Polymarket Perps ─────────────────────────────────────────────────
    perps_proxy_address: str
    perps_proxy_secret: str

    # ── Telegram ─────────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_chat_ids: tuple[str, ...]

    # ── Behaviour ────────────────────────────────────────────────────────
    reconnect_delay: int = 5
    max_reconnect_delay: int = 60
    ssl_verify: bool = True
    # Max seconds to wait for a market-name lookup before alerting anyway.
    name_lookup_timeout: float = 2.0
    # Backfill window (seconds) used on first connect when no state exists.
    backfill_window: int = 6 * 3600
    state_file: str = "state.json"

    # ── Logging ──────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = "trades.log"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3
    # Write every raw WebSocket frame to the log at DEBUG level.
    log_raw_events: bool = False

    # ── Derived ──────────────────────────────────────────────────────────
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clob_enabled(self) -> bool:
        return bool(
            self.clob_api_key and self.clob_api_secret and self.clob_api_passphrase
        )

    @property
    def perps_enabled(self) -> bool:
        return bool(self.perps_proxy_address and self.perps_proxy_secret)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_ids)

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        chat_ids = tuple(
            c.strip()
            for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
            if c.strip()
        )

        s = cls(
            clob_api_key=os.getenv("POLYMARKET_API_KEY", "").strip(),
            clob_api_secret=os.getenv("POLYMARKET_API_SECRET", "").strip(),
            clob_api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE", "").strip(),
            clob_wallet=os.getenv("POLYMARKET_WALLET", "").strip(),
            perps_proxy_address=os.getenv("PERPS_PROXY_ADDRESS", "").strip(),
            perps_proxy_secret=os.getenv("PERPS_PROXY_SECRET", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_ids=chat_ids,
            reconnect_delay=max(1, _env_int("RECONNECT_DELAY", 5)),
            max_reconnect_delay=max(5, _env_int("MAX_RECONNECT_DELAY", 60)),
            ssl_verify=_env_bool("SSL_VERIFY", True),
            name_lookup_timeout=_env_float("NAME_LOOKUP_TIMEOUT", 2.0),
            backfill_window=max(0, _env_int("BACKFILL_WINDOW", 6 * 3600)),
            state_file=os.getenv("STATE_FILE", "state.json").strip() or "state.json",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            log_file=os.getenv("LOG_FILE", "trades.log").strip() or "trades.log",
            log_max_bytes=max(0, _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024)),
            log_backup_count=max(0, _env_int("LOG_BACKUP_COUNT", 3)),
            log_raw_events=_env_bool("LOG_RAW_EVENTS", False),
        )

        missing: list[str] = []
        if not s.clob_enabled and not s.perps_enabled:
            missing.append(
                "POLYMARKET_API_KEY/SECRET/PASSPHRASE (CLOB) "
                "or PERPS_PROXY_ADDRESS/PERPS_PROXY_SECRET (Perps)"
            )
        return cls(**{**s.__dict__, "missing": tuple(missing)})
