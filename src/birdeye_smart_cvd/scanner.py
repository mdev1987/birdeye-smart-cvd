"""Orchestrate discovery, enrichment and signal generation."""

from __future__ import annotations

import asyncio
import logging
import time

from .birdeye import BirdeyeClient, BirdeyeError
from .config import Settings
from .models import PositionState, TokenCandidate
from .strategy import (
    RollingCVD,
    entry_allowed,
    latest_trade_price,
    normalize_trade,
    parse_creation_unix,
    parse_market_cap,
    smart_money_stats,
)

log = logging.getLogger(__name__)


class Scanner:
    """Signal-only scanner that deliberately performs no trades."""

    def __init__(self, client: BirdeyeClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.cvd: dict[str, RollingCVD] = {}
        self.positions: dict[str, PositionState] = {}
        self.last_discovery = 0.0
        self.watched: dict[str, TokenCandidate] = {}
        # Cache of token creation timestamps (Unix seconds) or None when
        # the endpoint returned no usable time. Avoids re-paying the 30 CU
        # creation_info cost for the same address every discovery cycle.
        self._creation_cache: dict[str, int | None] = {}
        # Set once a 401/403 proves token_creation_info is above our tier
        # (Standard). Further calls would just burn rate limit.
        self._creation_unsupported = False

    async def _creation_unix(self, address: str) -> int | None:
        """Return the cached or freshly fetched creation timestamp."""
        if address in self._creation_cache:
            return self._creation_cache[address]
        if self._creation_unsupported:
            return None
        try:
            info = await self.client.token_creation_info(address)
        except BirdeyeError as exc:
            message = str(exc)
            if "401" in message or "403" in message or "Forbidden" in message or "Unauthorized" in message:
                self._creation_unsupported = True
                log.warning(
                    "token_creation_info not accessible on this tier; "
                    "age gate degrades to unknown (set AGE_STRICT=1 to reject)"
                )
                return None
            log.warning("creation info failed %s: %s", address[:8], exc)
            return None
        stamp = parse_creation_unix(info)
        self._creation_cache[address] = stamp
        return stamp

    def _apply_age_gate(self, candidate: TokenCandidate, creation_unix: int | None) -> bool:
        """Attach age metadata and return True when the token may be kept."""
        if creation_unix:
            candidate.age_hours = (time.time() - creation_unix) / 3600.0
            candidate.age_source = "creation_info"
            if (
                self.settings.enforce_token_age
                and candidate.age_hours > self.settings.max_token_age_hours
            ):
                return False
            return True
        candidate.age_hours = None
        candidate.age_source = "unknown"
        if self.settings.enforce_token_age and self.settings.age_strict:
            return False
        return True

    async def discover(self) -> None:
        """Discover a small candidate universe from Birdeye Trending."""
        rows = await self.client.trending(self.settings.candidate_limit)
        candidates: dict[str, TokenCandidate] = {}

        for row in rows:
            address = str(row.get("address", row.get("token", "")))
            if not address:
                continue

            try:
                overview = await self.client.token_overview(address)
            except BirdeyeError as exc:
                log.warning("overview failed %s: %s", address[:8], exc)
                continue

            candidate = self._candidate_from_overview(address, overview)
            if candidate is None:
                continue
            if candidate.liquidity_usd < self.settings.min_liquidity_usd:
                continue
            if not self.settings.min_market_cap_usd <= candidate.market_cap_usd <= self.settings.max_market_cap_usd:
                continue
            if candidate.price_change_24h_pct > self.settings.max_price_change_24h_percent:
                continue
            if self.settings.enforce_token_age:
                creation_unix = await self._creation_unix(address)
                if not self._apply_age_gate(candidate, creation_unix):
                    log.info(
                        "skip %-10s age=%.1fh exceeds %dh",
                        candidate.symbol,
                        candidate.age_hours or -1,
                        self.settings.max_token_age_hours,
                    )
                    continue

            candidates[address] = candidate
            if len(candidates) >= self.settings.max_watched_tokens:
                break

        self._retire_dropped(candidates)
        self.watched = candidates
        for address in self.watched:
            self.cvd.setdefault(address, RollingCVD(self.settings.cvd_window_seconds))

        self.last_discovery = time.time()
        log.info("discovered %d candidates", len(self.watched))
        for token in self.watched.values():
            age_text = (
                f"{token.age_hours:.1f}h/{token.age_source}"
                if token.age_hours is not None
                else "unknown-age"
            )
            log.info(
                "candidate %-10s MC=$%s [%s] LP=$%s 24h=%+.1f%% age=%s %s",
                token.symbol,
                f"{token.market_cap_usd:,.0f}",
                token.market_cap_source or "?",
                f"{token.liquidity_usd:,.0f}",
                token.price_change_24h_pct,
                age_text,
                token.address,
            )

    def _retire_dropped(self, candidates: dict[str, TokenCandidate]) -> None:
        """Exit positions and drop CVD state for tokens leaving the universe.

        Without this, a position opened on a token that later disappears
        from discovery would sit in ``self.positions`` forever: it is never
        polled again, so it can never hit TP/SL/CVD/TTL.
        """
        for address in list(self.positions):
            if address not in candidates:
                position = self.positions.pop(address)
                try:
                    pnl_pct = (self.watched[address].price_usd / position.entry_price_usd - 1) * 100
                except (KeyError, ZeroDivisionError):
                    pnl_pct = float("nan")
                log.warning(
                    "EXIT %-10s DROPPED_FROM_UNIVERSE pnl=%+.1f%%",
                    position.symbol,
                    pnl_pct,
                )
        for address in list(self.cvd):
            if address not in candidates:
                del self.cvd[address]

    @staticmethod
    def _candidate_from_overview(address: str, data: dict) -> TokenCandidate | None:
        """Map a Birdeye overview response to a candidate model."""
        try:
            symbol = str(data.get("symbol", "?"))
            market_cap, mc_source = parse_market_cap(data)
            liquidity_raw = data.get("liquidity", data.get("liquidityUsd", 0))
            liquidity = float(liquidity_raw or 0)
            price = float(data.get("price", 0) or 0)
            change = float(
                data.get(
                    "priceChange24hPercent",
                    data.get("price_change_24h_percent", data.get("price24hChangePercent", 0)),
                )
                or 0
            )
        except (TypeError, ValueError):
            return None

        if market_cap <= 0 or liquidity <= 0 or price <= 0:
            return None
        return TokenCandidate(
            address,
            symbol,
            market_cap,
            liquidity,
            price,
            change,
            market_cap_source=mc_source,
        )

    async def poll_token(self, token: TokenCandidate) -> None:
        """Update one token and emit an entry/exit signal when appropriate."""
        try:
            trader_rows = await self.client.top_traders(token.address, self.settings.top_traders_limit)

            # Bootstrap the rolling window once; later polls are incremental.
            last_timestamp = self.cvd[token.address].state.last_trade_timestamp
            after_time = (
                last_timestamp
                if last_timestamp > 0
                else int(time.time()) - self.settings.cvd_window_seconds
            )
            trade_rows = await self.client.token_trades(
                token.address,
                after_time=after_time,
                limit=100,
            )
        except BirdeyeError as exc:
            log.warning("poll failed %-10s: %s", token.symbol, exc)
            return

        smart = smart_money_stats(trader_rows, self.settings.smart_tags)
        points = [normalize_trade(row, token.address) for row in trade_rows]
        normalized = [point for point in points if point is not None]
        state = self.cvd[token.address].add(normalized)

        # The newest normalized trade price is a fresher execution reference
        # than the discovery snapshot, without spending another API request.
        # Fall back to the discovery price only when no trade carries one.
        trade_price, _ = latest_trade_price(normalized)
        if trade_price is not None:
            token.price_usd = trade_price
            price_source = "trade"
        else:
            price_source = "discovery"

        log.info(
            "watch %-10s smart=%d buy%%=%.0f CVD=$%+.0f buy/sell=%.2fx price[%s]=$%.8f",
            token.symbol,
            smart.tagged_wallets,
            smart.buy_ratio * 100,
            state.cvd_usd,
            state.buy_sell_ratio,
            price_source,
            token.price_usd,
        )

        now = time.time()
        position = self.positions.get(token.address)
        if position:
            position.symbol = token.symbol
            pnl_pct = (token.price_usd / position.entry_price_usd - 1) * 100
            age = now - position.entry_time

            if pnl_pct <= -self.settings.stop_loss_percent:
                log.warning("EXIT %-10s stop-loss pnl=%+.1f%%", token.symbol, pnl_pct)
                self.positions.pop(token.address, None)
            elif pnl_pct >= self.settings.take_profit_percent:
                log.info("EXIT %-10s take-profit pnl=%+.1f%%", token.symbol, pnl_pct)
                self.positions.pop(token.address, None)
            elif state.buy_sell_ratio < 1.0:
                position.bearish_streak += 1
                if position.bearish_streak >= self.settings.bearish_exit_confirmations:
                    log.info(
                        "EXIT %-10s bearish CVD pnl=%+.1f%% streak=%d/%d",
                        token.symbol,
                        pnl_pct,
                        position.bearish_streak,
                        self.settings.bearish_exit_confirmations,
                    )
                    self.positions.pop(token.address, None)
                else:
                    log.info(
                        "bearish CVD %-10s pnl=%+.1f%% streak=%d/%d (holding)",
                        token.symbol,
                        pnl_pct,
                        position.bearish_streak,
                        self.settings.bearish_exit_confirmations,
                    )
            elif age >= self.settings.max_hold_seconds:
                log.info("EXIT %-10s TTL pnl=%+.1f%%", token.symbol, pnl_pct)
                self.positions.pop(token.address, None)
            else:
                position.bearish_streak = 0
            return

        if entry_allowed(
            token,
            smart,
            state,
            min_smart_wallets=self.settings.min_smart_wallets,
            min_smart_buy_ratio=self.settings.min_smart_buy_ratio,
            min_cvd_ratio=self.settings.cvd_min_buy_sell_ratio,
            max_price_change_24h=self.settings.max_price_change_24h_percent,
        ):
            self.positions[token.address] = PositionState(token.price_usd, now, token.symbol)
            log.warning(
                "BUY SIGNAL %-10s price=$%.8f smart=%d smart_buy%%=%.0f CVD=$%+.0f ratio=%.2fx",
                token.symbol,
                token.price_usd,
                smart.tagged_wallets,
                smart.buy_ratio * 100,
                state.cvd_usd,
                state.buy_sell_ratio,
            )

    async def run(self) -> None:
        """Run discovery and monitoring until interrupted."""
        while True:
            now = time.time()
            if now - self.last_discovery >= self.settings.discovery_interval_seconds:
                try:
                    await self.discover()
                except BirdeyeError as exc:
                    log.error("discovery failed: %s", exc)

            # Sequential polling is intentional: Standard is documented at
            # 1 request/sec, so we avoid concurrent bursts.
            for token in list(self.watched.values()):
                await self.poll_token(token)

            await asyncio.sleep(self.settings.poll_interval_seconds)
