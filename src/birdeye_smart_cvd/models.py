"""Typed models used by the scanner."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TokenCandidate:
    """A token that passed the initial discovery filter."""

    address: str
    symbol: str
    market_cap_usd: float
    liquidity_usd: float
    price_usd: float
    price_change_24h_pct: float
    # Display name from the overview (may be "?" when unavailable).
    name: str = "?"
    # Which overview field the market-cap value came from
    # (e.g. "marketCap", "marketcap", "fdv"). Empty when unknown.
    market_cap_source: str = ""
    # Token age in hours when known, otherwise None.
    age_hours: float | None = None
    # Where the age came from: "jupiter", "dexscreener", "creation_info".
    age_source: str = "unknown"
    # RugCheck normalized score when checked, otherwise None.
    risk_score: int | None = None
    # Short risk/veto annotations (e.g. "rug:blocked score=87").
    risk_flags: tuple[str, ...] = ()
    # Helius holder concentration: top-10 share of supply when known.
    top10_holder_pct: float | None = None
    # Where the current price_usd came from: discovery/trade/jupiter/dex.
    price_source: str = "discovery"


@dataclass(slots=True)
class SmartMoneyStats:
    """A conservative proxy for smart-money participation."""

    tagged_wallets: int = 0
    tagged_buy_volume_usd: float = 0.0
    tagged_sell_volume_usd: float = 0.0

    @property
    def buy_ratio(self) -> float:
        """Return tagged buy flow as a fraction of tagged total flow."""
        total = self.tagged_buy_volume_usd + self.tagged_sell_volume_usd
        return self.tagged_buy_volume_usd / total if total else 0.0


@dataclass(slots=True)
class CVDState:
    """Rolling trade-pressure state for one token."""

    buy_volume_usd: float = 0.0
    sell_volume_usd: float = 0.0
    seen_trade_ids: set[str] = field(default_factory=set)
    last_trade_timestamp: int = 0

    @property
    def cvd_usd(self) -> float:
        """Return cumulative buy volume minus sell volume."""
        return self.buy_volume_usd - self.sell_volume_usd

    @property
    def buy_sell_ratio(self) -> float:
        """Return the buy/sell ratio, or infinity when no sells exist."""
        if self.sell_volume_usd <= 0:
            return float("inf") if self.buy_volume_usd > 0 else 0.0
        return self.buy_volume_usd / self.sell_volume_usd


@dataclass(slots=True)
class PositionState:
    """Local paper-position state used to generate exits."""

    entry_price_usd: float
    entry_time: float
    symbol: str = "?"
    # Consecutive polls with bearish CVD; reset on non-bearish polls.
    bearish_streak: int = 0
    # Paper accounting snapshot taken at open.
    name: str = "?"
    notional_usd: float = 0.0
    balance_before_open_usd: float = 0.0
    balance_after_open_usd: float = 0.0
