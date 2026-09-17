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
    # Discovery-time price snapshot. Never overwritten by poll updates;
    # the entry chase guard compares the live price against this.
    discovery_price_usd: float = 0.0


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
    # Number of trades currently inside the rolling window. Tracked
    # explicitly (rather than len(seen_trade_ids)) so the entry gate can
    # reject thin samples like "$2 buy / $0 sells" even when the ratio
    # looks infinite.
    trade_count: int = 0

    @property
    def cvd_usd(self) -> float:
        """Return cumulative buy volume minus sell volume."""
        return self.buy_volume_usd - self.sell_volume_usd

    @property
    def total_volume_usd(self) -> float:
        """Return buy + sell volume inside the window."""
        return self.buy_volume_usd + self.sell_volume_usd

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
    # --- Profit-protection state (playbook: scale out, don't round-trip) ---
    # Notional still exposed after partial take-profits.
    remaining_notional_usd: float = 0.0
    # Highest price seen since entry; anchors the trailing stop.
    peak_price_usd: float = 0.0
    # Realized paper PnL banked by partial take-profits (final win/loss is
    # judged on partials + runner combined, not on the runner slice alone).
    realized_pnl_usd: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    # Latched once pnl first reaches TRAIL_ARM_PCT; stays armed even if
    # price fades (that fade is exactly what the trail must catch).
    trail_armed: bool = False
    # Consecutive polls with zero *new* trades. Playbook: "exit immediately
    # if volume dies". Reset on any poll that advances the CVD tape.
    quiet_polls: int = 0
    # Tagged smart-wallet count when the position opened; the runner log
    # shows accumulation/attrition (e.g. "smart 2->4") as context.
    smart_at_entry: int = 0
