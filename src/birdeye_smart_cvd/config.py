"""Configuration for the Birdeye signal scanner."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _raw(name: str) -> str | None:
    """Read a raw env value, treating empty strings as unset."""
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _float(name: str, default: float) -> float:
    """Read a floating-point environment variable."""
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def _int(name: str, default: int) -> int:
    """Read an integer environment variable."""
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable (true/1/yes/on)."""
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    api_key: str
    chain: str = "solana"
    base_url: str = "https://public-api.birdeye.so"
    api_min_request_interval_seconds: float = 1.5

    discovery_interval_seconds: int = 300
    poll_interval_seconds: int = 30
    candidate_limit: int = 10
    max_watched_tokens: int = 3

    # Playbook universe: new/early, low-cap tokens with usable liquidity.
    min_market_cap_usd: float = 60_000
    max_market_cap_usd: float = 3_000_000
    min_liquidity_usd: float = 10_000
    max_token_age_hours: int = 24

    # Standard-tier top-trader tags are used as a smart-money proxy.
    # Birdeye documents these Solana wallet_tags values: dev, bundler,
    # sniper, insider, smart_trader.
    top_traders_limit: int = 10
    smart_tags: tuple[str, ...] = ("smart_trader",)
    min_smart_wallets: int = 2
    min_smart_buy_ratio: float = 0.60

    # The playbook uses a 15m CVD confirmation.
    cvd_window_seconds: int = 900
    cvd_min_buy_sell_ratio: float = 1.20
    max_price_change_24h_percent: float = 80.0

    # Simple signal lifecycle. These are bot additions, not rules quoted
    # directly from the Birdeye playbook.
    stop_loss_percent: float = 15.0
    take_profit_percent: float = 50.0
    max_hold_seconds: int = 2400
    # Consecutive bearish-CVD polls required before a paper exit fires.
    # Guards against single-poll whipsaw (1.3x -> 0.8x -> 1.4x).
    bearish_exit_confirmations: int = 2
    # Token-age gate. token_creation_info is documented for Lite/Starter
    # and above, NOT free Standard: when the endpoint is inaccessible the
    # age is "unknown". age_strict=False (default) allows unknown-age
    # tokens with a warning; True rejects them.
    enforce_token_age: bool = True
    age_strict: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables and validate them."""
        load_dotenv()
        api_key = (_raw("BIRDEYE_API_KEY") or "")
        if not api_key or api_key == "your_api_key_here":
            raise ValueError("BIRDEYE_API_KEY is missing; set it in .env")

        tags = tuple(
            tag.strip().lower()
            for tag in (os.getenv("SMART_TAGS", "smart_trader") or "smart_trader").split(",")
            if tag.strip()
        )
        if not tags:
            raise ValueError("SMART_TAGS must list at least one tag")

        settings = cls(
            api_key=api_key,
            chain=(_raw("CHAIN") or "solana").lower(),
            api_min_request_interval_seconds=_float("API_MIN_REQUEST_INTERVAL_SECONDS", 1.5),
            discovery_interval_seconds=_int("DISCOVERY_INTERVAL_SECONDS", 300),
            poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 30),
            candidate_limit=_int("CANDIDATE_LIMIT", 10),
            max_watched_tokens=_int("MAX_WATCHED_TOKENS", 3),
            min_market_cap_usd=_float("MIN_MARKET_CAP_USD", 60_000),
            max_market_cap_usd=_float("MAX_MARKET_CAP_USD", 3_000_000),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 10_000),
            max_token_age_hours=_int("MAX_TOKEN_AGE_HOURS", 24),
            top_traders_limit=_int("TOP_TRADERS_LIMIT", 10),
            smart_tags=tags,
            min_smart_wallets=_int("MIN_SMART_WALLETS", 2),
            min_smart_buy_ratio=_float("MIN_SMART_BUY_RATIO", 0.60),
            cvd_window_seconds=_int("CVD_WINDOW_SECONDS", 900),
            cvd_min_buy_sell_ratio=_float("CVD_MIN_BUY_SELL_RATIO", 1.20),
            max_price_change_24h_percent=_float("MAX_PRICE_CHANGE_24H_PERCENT", 80),
            stop_loss_percent=_float("STOP_LOSS_PERCENT", 15),
            take_profit_percent=_float("TAKE_PROFIT_PERCENT", 50),
            max_hold_seconds=_int("MAX_HOLD_SECONDS", 2400),
            bearish_exit_confirmations=_int("BEARISH_EXIT_CONFIRMATIONS", 2),
            enforce_token_age=_bool("ENFORCE_TOKEN_AGE", True),
            age_strict=_bool("AGE_STRICT", False),
        )

        if settings.chain != "solana":
            raise ValueError("This strategy is intentionally configured for Solana")
        if settings.api_min_request_interval_seconds < 1.0:
            raise ValueError("API_MIN_REQUEST_INTERVAL_SECONDS must be >= 1.0 for Standard 1 RPS")
        if settings.poll_interval_seconds < 5:
            raise ValueError("POLL_INTERVAL_SECONDS must be >= 5 for the free 1 RPS tier")
        if settings.min_market_cap_usd >= settings.max_market_cap_usd:
            raise ValueError("MIN_MARKET_CAP_USD must be below MAX_MARKET_CAP_USD")
        if settings.max_watched_tokens < 1:
            raise ValueError("MAX_WATCHED_TOKENS must be >= 1")
        if settings.bearish_exit_confirmations < 1:
            raise ValueError("BEARISH_EXIT_CONFIRMATIONS must be >= 1")
        if settings.max_token_age_hours < 1:
            raise ValueError("MAX_TOKEN_AGE_HOURS must be >= 1")

        return settings
