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

    # Research mode: which pipeline stages run. Lets the core hypothesis
    # be tested before risk/enrichment influence the result.
    #   CORE     = Birdeye trending/overview/top-traders/trades + proxy + CVD
    #   RISK     = CORE + RugCheck + Helius
    #   ENRICHED = RISK + Jupiter + DexScreener + CabalSpy (full pipeline)
    # Telegram alerting stays orthogonal (fires in every mode when configured).
    scanner_mode: str = "enriched"

    discovery_interval_seconds: int = 300
    poll_interval_seconds: int = 30
    candidate_limit: int = 10
    max_watched_tokens: int = 3

    # Playbook universe: new/early, low-cap tokens with usable liquidity.
    min_market_cap_usd: float = 60_000
    max_market_cap_usd: float = 3_000_000
    min_liquidity_usd: float = 10_000
    max_token_age_hours: int = 24

    # Standard-tier top-trader tags are used as a Smart-Money Proxy
    # (free-tier stand-in for Birdeye's paid Smart Money feed).
    # Birdeye documents these Solana wallet_tags values: dev, bundler,
    # sniper, insider, smart_trader.
    top_traders_limit: int = 10
    smart_tags: tuple[str, ...] = ("smart_trader",)
    min_smart_wallets: int = 2
    min_smart_buy_ratio: float = 0.60

    # The playbook uses a 15m CVD confirmation.
    cvd_window_seconds: int = 900
    cvd_min_buy_sell_ratio: float = 1.20
    # Minimum CVD sample size: a "$2 buy / $0 sells" window has an infinite
    # ratio but no information. Research parameters — validate on history.
    cvd_min_volume_usd: float = 2000.0
    cvd_min_trades: int = 10
    max_price_change_24h_percent: float = 80.0

    # Simple signal lifecycle. These are bot additions, not rules quoted
    # directly from the Birdeye playbook.
    stop_loss_percent: float = 15.0
    take_profit_percent: float = 100.0
    max_hold_seconds: int = 2400
    # Partial take-profit ladder (playbook Early Meme risk rules: 50% at x2,
    # 30% at x3, 20% moonbag). TP1 banks TP1_FRACTION of the position at
    # +TAKE_PROFIT_PERCENT; TP2 banks TP2_FRACTION of the remainder at
    # +TAKE_PROFIT2_PERCENT; the rest rides to bearish/SL/TTL/volume exits.
    take_profit2_percent: float = 200.0
    tp1_fraction: float = 0.5
    tp2_fraction: float = 0.6
    # Trailing profit lock: once pnl >= TRAIL_ARM_PCT, exit the runner if
    # price falls TRAIL_STOP_PCT below its post-entry peak. Wide by design:
    # moonbag runners die to bearish CVD / volume, the trail only catches
    # genuine round-trips. 0 disables.
    trail_arm_pct: float = 50.0
    trail_stop_pct: float = 40.0
    # "Exit immediately if volume dies" (playbook): exit after this many
    # consecutive polls with zero new trades. 0 disables.
    volume_death_quiet_polls: int = 6
    # Entry chase guard (playbook: flat is the entry, never FOMO the
    # breakout): skip BUY when live price exceeds the discovery snapshot
    # by more than this. 0 disables.
    entry_max_surge_pct: float = 30.0
    # Consecutive bearish-CVD polls required before a paper exit fires.
    # Guards against single-poll whipsaw (1.3x -> 0.8x -> 1.4x).
    bearish_exit_confirmations: int = 2
    # Token-age gate. token_creation_info is documented for Lite/Starter
    # and above, NOT free Standard: when the endpoint is inaccessible the
    # age is "unknown". age_strict=False (default) allows unknown-age
    # tokens with a warning; True rejects them.
    enforce_token_age: bool = True
    age_strict: bool = False

    # Free keyless enrichment (DexScreener pairs, Jupiter price). These sit
    # outside the Birdeye CU budget with their own polite spacing.
    dexscreener_enabled: bool = True
    dexscreener_min_request_interval_seconds: float = 0.3
    jupiter_enabled: bool = True
    jupiter_min_request_interval_seconds: float = 0.5

    # RugCheck pre-entry veto (free summary endpoint, ~3 RPS tier).
    rugcheck_enabled: bool = True
    rugcheck_max_score: int = 50
    rugcheck_reject_danger: bool = True
    rugcheck_strict: bool = False
    rugcheck_min_request_interval_seconds: float = 0.4

    # Optional CabalSpy cluster confirmation. Inert unless a key is set;
    # advisory-only unless require_cluster is enabled, so an outage can
    # never silence entries by default.
    cabalspy_api_key: str = ""
    cabalspy_enabled: bool = True
    cabalspy_min_wallets: int = 3
    cabalspy_require_cluster: bool = False
    cabalspy_min_request_interval_seconds: float = 1.0

    # Helius RPC + transfers (holder concentration, on-chain buy proof).
    # Inert unless HELIUS_API_KEY is set. Concentration veto and buy-proof
    # gating both default OFF (advisory logs only) so fresh pump.fun-style
    # distributions and Helius outages can never silently kill signals.
    helius_api_key: str = ""
    helius_enabled: bool = True
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    helius_min_request_interval_seconds: float = 0.3
    helius_top10_max_pct: float = 60.0
    helius_reject_concentration: bool = False
    helius_verify_wallets: bool = True
    helius_require_confirmed: bool = False
    helius_verify_window_hours: int = 24
    helius_verify_limit: int = 10

    # Simulate-only Jupiter execution checks (quote + assemble + sign +
    # simulateTransaction, NEVER broadcast). Uses the Swap v2 /order API
    # (JUPITER_API_KEY, header x-api-key) and a throwaway PRIVATE_KEY that
    # only signs locally for the simulator. Inert unless PRIVATE_KEY is set;
    # advisory-only unless SIM_REQUIRE_ROUTE is enabled.
    sim_enabled: bool = True
    jupiter_api_key: str = ""
    jupiter_base_url: str = "https://api.jup.ag"
    private_key: str = ""
    jupiter_order_timeout_s: float = 12.0
    sim_slippage_bps: int = 300
    sim_max_impact_pct: float = 5.0
    sim_require_route: bool = False

    # Telegram paper alerts. Inert unless token + chat are both set.
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Paper portfolio: cash accounting for open/close alerts.
    paper_start_balance_usd: float = 1000.0
    paper_position_size_usd: float = 100.0
    max_open_positions: int = 3

    @property
    def use_risk_checks(self) -> bool:
        """True in RISK and ENRICHED modes (RugCheck + Helius)."""
        return self.scanner_mode in {"risk", "enriched"}

    @property
    def use_enrichment(self) -> bool:
        """True only in ENRICHED mode (Jupiter + DexScreener + CabalSpy)."""
        return self.scanner_mode == "enriched"

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
            scanner_mode=(_raw("SCANNER_MODE") or "enriched").lower(),
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
            cvd_min_volume_usd=_float("CVD_MIN_VOLUME_USD", 2000.0),
            cvd_min_trades=_int("CVD_MIN_TRADES", 10),
            max_price_change_24h_percent=_float("MAX_PRICE_CHANGE_24H_PERCENT", 80),
            stop_loss_percent=_float("STOP_LOSS_PERCENT", 15),
            take_profit_percent=_float("TAKE_PROFIT_PERCENT", 100),
            take_profit2_percent=_float("TAKE_PROFIT2_PCT", 200),
            tp1_fraction=_float("TP1_FRACTION", 0.5),
            tp2_fraction=_float("TP2_FRACTION", 0.6),
            trail_arm_pct=_float("TRAIL_ARM_PCT", 50),
            trail_stop_pct=_float("TRAIL_STOP_PCT", 40),
            volume_death_quiet_polls=_int("VOLUME_DEATH_QUIET_POLLS", 6),
            entry_max_surge_pct=_float("ENTRY_MAX_SURGE_PCT", 30),
            max_hold_seconds=_int("MAX_HOLD_SECONDS", 2400),
            bearish_exit_confirmations=_int("BEARISH_EXIT_CONFIRMATIONS", 2),
            enforce_token_age=_bool("ENFORCE_TOKEN_AGE", True),
            age_strict=_bool("AGE_STRICT", False),
            dexscreener_enabled=_bool("DEXSCREENER_ENABLED", True),
            dexscreener_min_request_interval_seconds=_float(
                "DEXSCREENER_MIN_REQUEST_INTERVAL_SECONDS", 0.3
            ),
            jupiter_enabled=_bool("JUPITER_ENABLED", True),
            jupiter_min_request_interval_seconds=_float(
                "JUPITER_MIN_REQUEST_INTERVAL_SECONDS", 0.5
            ),
            rugcheck_enabled=_bool("RUGCHECK_ENABLED", True),
            rugcheck_max_score=_int("RUGCHECK_MAX_SCORE", 50),
            rugcheck_reject_danger=_bool("RUGCHECK_REJECT_DANGER", True),
            rugcheck_strict=_bool("RUGCHECK_STRICT", False),
            rugcheck_min_request_interval_seconds=_float(
                "RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS", 0.4
            ),
            cabalspy_api_key=(_raw("CABALSPY_API_KEY") or ""),
            cabalspy_enabled=_bool("CABALSPY_ENABLED", True),
            cabalspy_min_wallets=_int("CABALSPY_MIN_WALLETS", 3),
            cabalspy_require_cluster=_bool("CABALSPY_REQUIRE_CLUSTER", False),
            cabalspy_min_request_interval_seconds=_float(
                "CABALSPY_MIN_REQUEST_INTERVAL_SECONDS", 1.0
            ),
            helius_api_key=(_raw("HELIUS_API_KEY") or ""),
            helius_enabled=_bool("HELIUS_ENABLED", True),
            helius_rpc_url=(
                _raw("HELIUS_RPC_URL") or _raw("HELIUS_RPC") or "https://mainnet.helius-rpc.com"
            ),
            helius_min_request_interval_seconds=_float(
                "HELIUS_MIN_REQUEST_INTERVAL_SECONDS", 0.3
            ),
            helius_top10_max_pct=_float("HELIUS_TOP10_MAX_PCT", 60.0),
            helius_reject_concentration=_bool("HELIUS_REJECT_CONCENTRATION", False),
            helius_verify_wallets=_bool("HELIUS_VERIFY_WALLETS", True),
            helius_require_confirmed=_bool("HELIUS_REQUIRE_CONFIRMED", False),
            helius_verify_window_hours=_int("HELIUS_VERIFY_WINDOW_HOURS", 24),
            helius_verify_limit=_int("HELIUS_VERIFY_LIMIT", 10),
            sim_enabled=_bool("SIM_ENABLED", True),
            jupiter_api_key=(_raw("JUPITER_API_KEY") or ""),
            jupiter_base_url=(
                _raw("JUPITER_BASE_URL") or "https://api.jup.ag"
            ),
            private_key=(_raw("PRIVATE_KEY") or ""),
            jupiter_order_timeout_s=_float("JUPITER_ORDER_TIMEOUT_S", 12),
            sim_slippage_bps=_int("SIM_SLIPPAGE_BPS", 300),
            sim_max_impact_pct=_float("SIM_MAX_IMPACT_PCT", 5.0),
            sim_require_route=_bool("SIM_REQUIRE_ROUTE", False),
            telegram_enabled=_bool("TELEGRAM_ENABLED", True),
            telegram_bot_token=(_raw("TELEGRAM_BOT_TOKEN") or ""),
            telegram_chat_id=(_raw("TELEGRAM_CHAT_ID") or ""),
            paper_start_balance_usd=_float("PAPER_START_BALANCE_USD", 1000.0),
            paper_position_size_usd=_float("PAPER_POSITION_SIZE_USD", 100.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
        )

        if settings.chain != "solana":
            raise ValueError("This strategy is intentionally configured for Solana")
        if settings.scanner_mode not in {"core", "risk", "enriched"}:
            raise ValueError("SCANNER_MODE must be one of: core, risk, enriched")
        if settings.cvd_min_volume_usd < 0:
            raise ValueError("CVD_MIN_VOLUME_USD must be >= 0")
        if settings.cvd_min_trades < 1:
            raise ValueError("CVD_MIN_TRADES must be >= 1")
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
        if settings.take_profit_percent <= 0:
            raise ValueError("TAKE_PROFIT_PERCENT must be > 0")
        if settings.take_profit2_percent <= settings.take_profit_percent:
            raise ValueError("TAKE_PROFIT2_PCT must be above TAKE_PROFIT_PERCENT")
        for name, frac in (
            ("TP1_FRACTION", settings.tp1_fraction),
            ("TP2_FRACTION", settings.tp2_fraction),
        ):
            if not 0 < frac < 1:
                raise ValueError(f"{name} must be between 0 and 1 (exclusive)")
        if settings.trail_arm_pct < 0:
            raise ValueError("TRAIL_ARM_PCT must be >= 0 (0 arms immediately)")
        if settings.trail_stop_pct < 0:
            raise ValueError("TRAIL_STOP_PCT must be >= 0 (0 disables the trail)")
        if settings.volume_death_quiet_polls < 0:
            raise ValueError("VOLUME_DEATH_QUIET_POLLS must be >= 0 (0 disables)")
        if settings.entry_max_surge_pct < 0:
            raise ValueError("ENTRY_MAX_SURGE_PCT must be >= 0 (0 disables)")
        if not 0 <= settings.rugcheck_max_score <= 100:
            raise ValueError("RUGCHECK_MAX_SCORE must be between 0 and 100")
        if settings.cabalspy_min_wallets < 1:
            raise ValueError("CABALSPY_MIN_WALLETS must be >= 1")
        if not 0 < settings.helius_top10_max_pct <= 100:
            raise ValueError("HELIUS_TOP10_MAX_PCT must be between 0 (exclusive) and 100")
        if settings.helius_verify_window_hours < 1:
            raise ValueError("HELIUS_VERIFY_WINDOW_HOURS must be >= 1")
        if not 1 <= settings.helius_verify_limit <= 100:
            raise ValueError("HELIUS_VERIFY_LIMIT must be between 1 and 100")
        if settings.jupiter_order_timeout_s < 1:
            raise ValueError("JUPITER_ORDER_TIMEOUT_S must be >= 1")
        if not 0 <= settings.sim_slippage_bps <= 10_000:
            raise ValueError("SIM_SLIPPAGE_BPS must be between 0 and 10000")
        if settings.sim_max_impact_pct < 0:
            raise ValueError("SIM_MAX_IMPACT_PCT must be >= 0")
        if settings.paper_start_balance_usd <= 0:
            raise ValueError("PAPER_START_BALANCE_USD must be > 0")
        if settings.paper_position_size_usd <= 0:
            raise ValueError("PAPER_POSITION_SIZE_USD must be > 0")
        if settings.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be >= 1")
        for name, interval in (
            ("DEXSCREENER_MIN_REQUEST_INTERVAL_SECONDS", settings.dexscreener_min_request_interval_seconds),
            ("JUPITER_MIN_REQUEST_INTERVAL_SECONDS", settings.jupiter_min_request_interval_seconds),
            ("RUGCHECK_MIN_REQUEST_INTERVAL_SECONDS", settings.rugcheck_min_request_interval_seconds),
            ("CABALSPY_MIN_REQUEST_INTERVAL_SECONDS", settings.cabalspy_min_request_interval_seconds),
            ("HELIUS_MIN_REQUEST_INTERVAL_SECONDS", settings.helius_min_request_interval_seconds),
        ):
            if interval < 0:
                raise ValueError(f"{name} must be >= 0")

        return settings
