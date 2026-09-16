"""Pure strategy logic: no network calls and no trade execution."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .models import CVDState, SmartMoneyStats, TokenCandidate


@dataclass(slots=True)
class TradePoint:
    """One buy/sell trade normalized to USD volume and Unix time."""

    trade_id: str
    timestamp: int
    side: str
    volume_usd: float
    price_usd: float | None = None


class RollingCVD:
    """Maintain a time-windowed CVD without double-counting trades."""

    def __init__(self, window_seconds: int) -> None:
        self.window_seconds = window_seconds
        self.points: deque[TradePoint] = deque()
        self.state = CVDState()

    def add(self, trades: list[TradePoint], now: int | None = None) -> CVDState:
        """Add trades and discard observations outside the rolling window."""
        now = int(time.time()) if now is None else now
        for trade in trades:
            if trade.trade_id in self.state.seen_trade_ids:
                continue
            self.state.seen_trade_ids.add(trade.trade_id)
            self.points.append(trade)
            self.state.last_trade_timestamp = max(self.state.last_trade_timestamp, trade.timestamp)

        cutoff = now - self.window_seconds
        while self.points and self.points[0].timestamp < cutoff:
            self.points.popleft()

        # Keep the deduplication set bounded. Older IDs cannot re-enter the
        # active window after they have aged out.
        active_ids = {point.trade_id for point in self.points}
        self.state.seen_trade_ids.intersection_update(active_ids)

        buy = sum(point.volume_usd for point in self.points if point.side == "buy")
        sell = sum(point.volume_usd for point in self.points if point.side == "sell")
        self.state.buy_volume_usd = buy
        self.state.sell_volume_usd = sell
        return self.state


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    """Return the first value that is present and not None, else None.

    Unlike ``row.get(a, row.get(b))`` this falls through when the first
    key exists with an explicit ``null``/``None`` value, which Birdeye
    responses commonly include.
    """
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
        # Also accept a missing key whose fallback may exist; a present
        # None must not block later keys.
    return None


def _parse_float(value: Any) -> float | None:
    """Parse a float, returning None for missing/invalid values."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    """Parse an int timestamp, returning None for missing/invalid values."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_market_cap(data: dict[str, Any]) -> tuple[float, str]:
    """Extract a market-cap/FDV value and record which field supplied it.

    Priority is true market cap first (``marketCap`` from token_overview,
    ``marketcap`` lowercase from token_trending), then fully-diluted
    valuation (``fdv``/``FDV``) as a documented fallback for early tokens
    that report no circulating market cap yet.
    """
    for key in ("marketCap", "market_cap", "marketcap"):
        value = _parse_float(data.get(key))
        if value and value > 0:
            return value, key
    for key in ("fdv", "FDV", "Fdv", "fullyDilutedValuation", "fully_diluted_valuation"):
        value = _parse_float(data.get(key))
        if value and value > 0:
            return value, key
    return 0.0, ""


def parse_creation_unix(data: dict[str, Any]) -> int | None:
    """Extract a token creation timestamp (Unix seconds) when present."""
    direct = _parse_int(
        _first_present(
            data,
            "blockUnixTime",
            "block_unix_time",
            "createdUnixTime",
            "created_unix_time",
            "creationTime",
            "creation_time",
            "listedAt",
            "listed_at",
        )
    )
    if direct:
        return direct
    # /defi/v2/tokens/new_listing reports ISO strings like
    # "2024-09-18T17:59:23" in liquidityAddedAt.
    iso_raw = _first_present(data, "liquidityAddedAt", "liquidity_added_at")
    if isinstance(iso_raw, str) and iso_raw.strip():
        text = iso_raw.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(text)
            epoch = datetime(1970, 1, 1, tzinfo=parsed.tzinfo)
            stamp = int((parsed - epoch).total_seconds())
            return stamp if stamp > 0 else None
        except ValueError:
            return None
    return None


def _tags(row: dict[str, Any]) -> set[str]:
    """Normalize the trader tag field returned by Birdeye."""
    raw = row.get("tags", row.get("tag", []))
    if isinstance(raw, str):
        return {raw.lower()}
    return {str(item).lower() for item in (raw or [])}


def smart_money_stats(rows: list[dict[str, Any]], smart_tags: tuple[str, ...]) -> SmartMoneyStats:
    """Estimate smart-money flow from tagged top traders.

    This is a proxy rather than Birdeye's dedicated Smart Money API.
    """
    wanted = set(smart_tags)
    stats = SmartMoneyStats()

    for row in rows:
        if not _tags(row) & wanted:
            continue
        stats.tagged_wallets += 1
        buy = _parse_float(_first_present(row, "volumeBuyUSD", "volume_buy_usd")) or 0.0
        sell = _parse_float(_first_present(row, "volumeSellUSD", "volume_sell_usd")) or 0.0
        stats.tagged_buy_volume_usd += buy
        stats.tagged_sell_volume_usd += sell

    return stats


def extract_trade_price(row: dict[str, Any], token_address: str | None = None) -> float | None:
    """Extract the USD price of the traded token from a V3 trade row.

    The V3 ``/defi/v3/token/txs`` response carries no top-level ``price``
    field. The per-leg USD price lives in the ``from``/``to`` objects
    (``from.price`` / ``to.price``) or in ``base_price``/``quote_price``.
    When ``token_address`` is known, the leg whose address matches the
    token is preferred; otherwise the first usable leg price wins.
    """
    direct = _parse_float(
        _first_present(
            row,
            "price",
            "priceUsd",
            "price_usd",
            "base_price",
            "basePrice",
            "quote_price",
            "quotePrice",
        )
    )
    if direct and direct > 0:
        return direct

    legs: list[dict[str, Any]] = []
    for key in ("from", "to", "base", "quote"):
        leg = row.get(key)
        if isinstance(leg, dict):
            legs.append(leg)

    if token_address and legs:
        wanted = token_address.strip().lower()
        for leg in legs:
            leg_addr = leg.get("address", leg.get("mint", ""))
            if isinstance(leg_addr, str) and leg_addr.strip().lower() == wanted:
                price = _parse_float(_first_present(leg, "price", "priceUsd", "price_usd"))
                if price and price > 0:
                    return price

    for leg in legs:
        price = _parse_float(_first_present(leg, "price", "priceUsd", "price_usd"))
        if price and price > 0:
            return price
    return None


def normalize_trade(row: dict[str, Any], token_address: str | None = None) -> TradePoint | None:
    """Convert a Birdeye token-trade row into a CVD observation."""
    side = str(row.get("side", "")).lower()
    if side not in {"buy", "sell"}:
        return None

    # V3 responses use snake_case (volume_usd, block_unix_time, tx_hash);
    # older examples use camelCase. _first_present falls through on
    # explicit nulls instead of stopping at the first key.
    volume = _parse_float(_first_present(row, "volumeUsd", "volume_usd"))
    timestamp = _parse_int(
        _first_present(row, "blockUnixTime", "block_unix_time", "unix_time", "blockTime", "block_time")
    )

    tx_hash = _first_present(row, "txHash", "tx_hash", "signature", "txHashStr") or ""
    tx_hash = str(tx_hash)
    ins_index = _first_present(row, "insIndex", "ins_index")
    inner_index = _first_present(row, "innerInsIndex", "inner_ins_index")

    if not tx_hash or timestamp is None or volume is None:
        return None

    # inner_ins_index is nullable in the V3 schema; when it is missing we
    # add side/timestamp/volume discriminators so two swaps sharing one
    # instruction do not collapse into a single deduplication ID.
    if inner_index is None or (isinstance(inner_index, str) and not inner_index.strip()):
        trade_id = f"{tx_hash}:{ins_index if ins_index is not None else ''}:{side}:{timestamp}:{volume:.12g}"
    else:
        trade_id = f"{tx_hash}:{ins_index if ins_index is not None else ''}:{inner_index}"

    try:
        volume_f = float(volume)
    except (TypeError, ValueError):
        return None

    return TradePoint(
        trade_id=trade_id,
        timestamp=int(timestamp),
        side=side,
        volume_usd=volume_f,
        price_usd=extract_trade_price(row, token_address),
    )


def latest_trade_price(trades: list[TradePoint]) -> tuple[float | None, int | None]:
    """Return the price of the newest trade that carries one, if any."""
    best: TradePoint | None = None
    for trade in trades:
        if trade.price_usd is None or trade.price_usd <= 0:
            continue
        if best is None or trade.timestamp > best.timestamp:
            best = trade
    if best is None:
        return None, None
    return best.price_usd, best.timestamp


def entry_allowed(
    candidate: TokenCandidate,
    smart: SmartMoneyStats,
    cvd: CVDState,
    *,
    min_smart_wallets: int,
    min_smart_buy_ratio: float,
    min_cvd_ratio: float,
    max_price_change_24h: float,
) -> bool:
    """Return True when the candidate passes all entry confirmation gates."""
    return (
        smart.tagged_wallets >= min_smart_wallets
        and smart.buy_ratio >= min_smart_buy_ratio
        and cvd.buy_sell_ratio >= min_cvd_ratio
        and candidate.price_change_24h_pct <= max_price_change_24h
    )
