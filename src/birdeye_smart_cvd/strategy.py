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


def parse_iso_unix(text: Any) -> int | None:
    """Parse an ISO-8601 timestamp (or plain Unix seconds) to Unix seconds."""
    if isinstance(text, (int, float)):
        stamp = int(text)
        return stamp if stamp > 0 else None
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.endswith(("Z", "z")):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = int(parsed.timestamp())
        return stamp if stamp > 0 else None
    except ValueError:
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
    return parse_iso_unix(_first_present(data, "liquidityAddedAt", "liquidity_added_at"))


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


# ---------------------------------------------------------------------------
# Multi-source enrichment: DexScreener ages, Jupiter birth/price, RugCheck
# verdicts and CabalSpy cluster matches. All pure functions; every network
# failure is handled by the caller (fail-open with a warning), never here.
# ---------------------------------------------------------------------------

_SOL_MINT = "so11111111111111111111111111111111111111112"
_USDC_MINT = "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"
_USDT_MINT = "es9vmfrzacerfjfrf4h2fyd4kcconky11mcce8benwnyb"
_STABLE_SYMBOLS = {"sol", "wsol", "usdc", "usdt"}


def _pair_leg_address(pair: dict[str, Any], leg: str) -> str:
    """Return the mint address of a DexScreener pair leg, if any."""
    node = pair.get(leg, {})
    if not isinstance(node, dict):
        return ""
    for key in ("address", "mint"):
        value = node.get(key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pair_leg_symbol(pair: dict[str, Any], leg: str) -> str:
    """Return the symbol of a DexScreener pair leg, lowercased."""
    node = pair.get(leg, {})
    if not isinstance(node, dict):
        return ""
    symbol = node.get("symbol", "")
    return str(symbol).strip().lower() if symbol else ""


def _pair_liquidity_usd(pair: dict[str, Any]) -> float:
    """Return DexScreener ``liquidity.usd`` as a float, else 0."""
    liquidity = pair.get("liquidity", {})
    if not isinstance(liquidity, dict):
        return 0.0
    return _parse_float(_first_present(liquidity, "usd")) or 0.0


def _is_anchor_quote(pair: dict[str, Any]) -> bool:
    """Return True when the quote leg is SOL/USDC/USDT (deep venues)."""
    quote_addr = _pair_leg_address(pair, "quoteToken") or _pair_leg_address(pair, "quote")
    if quote_addr.lower() in {_SOL_MINT, _USDC_MINT, _USDT_MINT}:
        return True
    return _pair_leg_symbol(pair, "quoteToken") in _STABLE_SYMBOLS


def select_best_pair(
    pairs: list[dict[str, Any]], token_address: str | None = None
) -> dict[str, Any] | None:
    """Pick the most trustworthy DexScreener pair for a token.

    Preference order: token as base leg, anchor quote (SOL/USDC/USDT),
    then highest USD liquidity. Dust meme-meme quote pairs sink to the
    bottom instead of polluting enrichment.
    """
    wanted = (token_address or "").strip().lower()
    scored: list[tuple[tuple[int, int, float], dict[str, Any]]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        base_addr = (_pair_leg_address(pair, "baseToken") or _pair_leg_address(pair, "base")).lower()
        quote_addr = (_pair_leg_address(pair, "quoteToken") or _pair_leg_address(pair, "quote")).lower()
        involves = not wanted or wanted in {base_addr, quote_addr}
        if not involves:
            continue
        is_base = bool(wanted) and base_addr == wanted
        scored.append(
            ((1 if is_base else 0, 1 if _is_anchor_quote(pair) else 0, _pair_liquidity_usd(pair)), pair)
        )
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def pair_age_info(pairs: list[dict[str, Any]]) -> tuple[int | None, int]:
    """Return (oldest pool Unix seconds or None, pair count).

    A pool can only be created after both of its tokens exist, so the
    minimum ``pairCreatedAt`` (Unix ms) is a *sound upper bound* on token
    age: if the oldest pool is older than the limit, the token is
    definitely too old (reject). If younger, the token *may* still be
    older (permissive admission, same as today's unknown-age path).
    """
    stamps: list[int] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        raw = _first_present(pair, "pairCreatedAt", "pair_created_at", "createdAt", "created_at")
        stamp_ms = _parse_int(raw)
        if not stamp_ms or stamp_ms <= 0:
            continue
        stamps.append(stamp_ms // 1000 if stamp_ms > 10**12 else stamp_ms)
    count = sum(1 for pair in pairs if isinstance(pair, dict))
    return (min(stamps) if stamps else None, count)


def enrichment_from_pair(pair: dict[str, Any]) -> dict[str, Any]:
    """Extract price/liquidity/market-cap from one DexScreener pair."""
    price = _parse_float(_first_present(pair, "priceUsd", "price_usd"))
    market_cap = _parse_float(
        _first_present(pair, "marketCap", "market_cap", "fdv", "FDV")
    )
    mc_source = ""
    for key in ("marketCap", "market_cap"):
        if _parse_float(pair.get(key)):
            mc_source = f"dex:{key}"
            break
    if not mc_source:
        for key in ("fdv", "FDV"):
            if _parse_float(pair.get(key)):
                mc_source = f"dex:{key}"
                break
    change = 0.0
    price_change = pair.get("priceChange", pair.get("price_change", {}))
    if isinstance(price_change, dict):
        change = _parse_float(_first_present(price_change, "h24")) or 0.0
    return {
        "price_usd": price,
        "liquidity_usd": _pair_liquidity_usd(pair),
        "market_cap_usd": market_cap,
        "market_cap_source": mc_source,
        "price_change_24h_pct": change,
    }


def parse_jupiter_entry(payload: dict[str, Any], mint: str) -> dict[str, Any]:
    """Return the Jupiter v3 price object for one mint, or {}."""
    entry = payload.get(mint, payload.get(mint.strip(), {}))
    return entry if isinstance(entry, dict) else {}


def jupiter_age_unix(entry: dict[str, Any]) -> int | None:
    """Extract token-level creation time from a Jupiter price object."""
    return parse_iso_unix(_first_present(entry, "createdAt", "created_at"))


def jupiter_price_usd(entry: dict[str, Any]) -> float | None:
    """Extract USD price from a Jupiter price object."""
    price = _parse_float(_first_present(entry, "usdPrice", "usd_price", "price"))
    return price if price and price > 0 else None


def rugcheck_verdict(
    summary: dict[str, Any],
    *,
    max_score: int,
    reject_danger: bool,
) -> tuple[bool, list[str], int | None]:
    """Judge a RugCheck summary: (allowed, reasons, score_normalised).

    Pure function — unknown/missing data never blocks here; the caller's
    ``RUGCHECK_STRICT`` flag decides what happens when no summary exists
    at all. Rejects on excessive normalized score or any ``danger``-level
    risk when ``reject_danger`` is set.
    """
    score = _parse_int(_first_present(summary, "score_normalised", "scoreNormalized"))
    reasons: list[str] = []
    allowed = True

    risks = summary.get("risks", [])
    dangers: list[str] = []
    if isinstance(risks, list):
        for risk in risks:
            if not isinstance(risk, dict):
                continue
            if str(risk.get("level", "")).lower() == "danger":
                dangers.append(str(risk.get("name", "unnamed risk")))
    if dangers and reject_danger:
        allowed = False
        reasons.append("danger risks: " + ", ".join(dangers[:5]))

    if score is not None and score > max_score:
        allowed = False
        reasons.append(f"score {score} > max {max_score}")

    return allowed, reasons, score


def parse_cluster_for_token(signals: list[Any], token_address: str) -> int | None:
    """Return the clustered wallet count for a token, or None.

    The signals envelope varies, so matches are attempted against every
    plausible mint key (exact then case-insensitive) and the wallet count
    against every plausible count key. None means "no usable signal",
    which the caller treats as advisory-absent, never as zero.
    """
    wanted = (token_address or "").strip()
    if not wanted:
        return None
    wanted_lower = wanted.lower()
    mint_keys = ("mint", "token", "tokenAddress", "token_address", "address", "id", "mintAddress", "mint_address")
    count_keys = ("wallets", "wallet_count", "walletCount", "count", "num_wallets", "numWallets", "walletCountTotal")

    def _count(item: Any) -> int | None:
        if isinstance(item, (int, float)):
            return int(item)
        if isinstance(item, list):
            return len(item)
        if isinstance(item, dict):
            for key in count_keys:
                value = item.get(key)
                if isinstance(value, (int, float)) and int(value) >= 0:
                    return int(value)
                if isinstance(value, list):
                    return len(value)
            nested = item.get("cluster", item.get("data", None))
            if nested is not item and nested is not None:
                return _count(nested)
        return None

    for signal in signals:
        if not isinstance(signal, dict):
            continue
        mint_value: Any = None
        for key in mint_keys:
            if signal.get(key) not in (None, ""):
                mint_value = signal.get(key)
                break
        if not isinstance(mint_value, str):
            continue
        if mint_value != wanted and mint_value.lower() != wanted_lower:
            continue
        found = _count(signal)
        if found is None:
            for key in ("cluster", "data", "info"):
                nested = signal.get(key)
                if isinstance(nested, dict):
                    found = _count(nested)
                    if found is not None:
                        break
        return found
    return None


# ---------------------------------------------------------------------------
# Helius: holder concentration and on-chain wallet-buy verification.
# ---------------------------------------------------------------------------

def _account_ui_amount(account: dict[str, Any]) -> float | None:
    """Extract a UI-adjusted token balance from a largest-accounts entry."""
    for key in ("uiAmount", "uiAmountString"):
        amount = _parse_float(account.get(key))
        if amount is not None and amount >= 0:
            return amount
    raw_amount = _parse_int(account.get("amount"))
    decimals = _parse_int(account.get("decimals"))
    if raw_amount is not None and decimals is not None and decimals >= 0:
        try:
            return raw_amount / (10**decimals)
        except (OverflowError, ZeroDivisionError):
            return None
    return None


def tagged_owners(rows: list[dict[str, Any]], smart_tags: tuple[str, ...]) -> list[str]:
    """Return owner addresses of rows carrying a wanted tag, deduped."""
    wanted = set(smart_tags)
    owners: list[str] = []
    for row in rows:
        if not _tags(row) & wanted:
            continue
        owner = row.get("owner", row.get("wallet", row.get("address", "")))
        if isinstance(owner, str) and owner.strip() and owner.strip() not in owners:
            owners.append(owner.strip())
    return owners


def top_holder_pct(
    accounts: list[dict[str, Any]], supply: float | None, *, top_n: int = 10
) -> tuple[float | None, float | None]:
    """Return (top1_pct, topN_pct) of supply held, or Nones when unknown.

    Needs total ``supply``; without it no percentage is computable and
    (None, None) is returned rather than a misleading number.
    """
    if not supply or supply <= 0 or top_n < 1:
        return None, None
    balances = sorted(
        (
            balance
            for account in accounts
            if isinstance(account, dict)
            for balance in [_account_ui_amount(account)]
            if balance is not None
        ),
        reverse=True,
    )
    if not balances:
        return None, None
    top1 = balances[0] / supply * 100.0
    topn = sum(balances[:top_n]) / supply * 100.0
    return top1, topn


def _transfer_timestamp(transfer: dict[str, Any]) -> int | None:
    """Extract a Unix timestamp from a Helius transfer row, if any."""
    return _parse_int(
        _first_present(transfer, "timestamp", "blockTime", "block_time", "blocktime")
    )


def count_wallet_buys(
    transfers: list[Any],
    wallet: str,
    mint: str,
    *,
    since_unix: int | None = None,
) -> tuple[int, float]:
    """Count inbound transfers of ``mint`` to ``wallet`` (optionally recent).

    The server already filters by wallet/mint/direction; this re-checks
    defensively (exact match first, case-insensitive fallback) and applies
    the recency window. Returns (count, total_ui_amount).
    """
    wanted_wallet = (wallet or "").strip()
    wanted_mint = (mint or "").strip()
    count = 0
    total = 0.0
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        to_value = str(
            _first_present(
                transfer, "toUserAccount", "to", "destination", "toAddress", "to_address"
            )
            or ""
        )
        mint_value = str(
            _first_present(transfer, "mint", "tokenMint", "mintAddress", "mint_address")
            or ""
        )
        if to_value != wanted_wallet and to_value.lower() != wanted_wallet.lower():
            continue
        if mint_value != wanted_mint and mint_value.lower() != wanted_mint.lower():
            continue
        if since_unix is not None:
            stamp = _transfer_timestamp(transfer)
            if stamp is None or stamp < since_unix:
                continue
        count += 1
        amount = _parse_float(
            _first_present(transfer, "tokenAmount", "amount", "uiAmount", "ui_amount")
        )
        if amount:
            total += amount
    return count, total
