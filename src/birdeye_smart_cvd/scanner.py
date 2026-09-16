"""Orchestrate discovery, enrichment and signal generation."""

from __future__ import annotations

import asyncio
import logging
import time

from .birdeye import BirdeyeClient, BirdeyeError
from .cabalspy import CabalSpyClient, CabalSpyError
from .config import Settings
from .dexscreener import DexScreenerClient, DexScreenerError
from .jupiter import JupiterClient, JupiterError
from .models import PositionState, TokenCandidate
from .rugcheck import RugCheckClient, RugCheckError
from .strategy import (
    RollingCVD,
    enrichment_from_pair,
    entry_allowed,
    jupiter_age_unix,
    jupiter_price_usd,
    latest_trade_price,
    normalize_trade,
    pair_age_info,
    parse_cluster_for_token,
    parse_creation_unix,
    parse_market_cap,
    rugcheck_verdict,
    select_best_pair,
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

        # Free keyless enrichment clients. Each degrades independently:
        # a failure here only loses an enrichment source, never a signal.
        self.dex = (
            DexScreenerClient(
                min_request_interval=settings.dexscreener_min_request_interval_seconds
            )
            if settings.dexscreener_enabled
            else None
        )
        self.jup = (
            JupiterClient(
                min_request_interval=settings.jupiter_min_request_interval_seconds
            )
            if settings.jupiter_enabled
            else None
        )
        self.rug = (
            RugCheckClient(
                min_request_interval=settings.rugcheck_min_request_interval_seconds
            )
            if settings.rugcheck_enabled
            else None
        )
        if settings.cabalspy_enabled and settings.cabalspy_api_key:
            self.cabal: CabalSpyClient | None = CabalSpyClient(
                settings.cabalspy_api_key,
                min_request_interval=settings.cabalspy_min_request_interval_seconds,
            )
            log.info("CabalSpy cluster confirmation enabled")
        else:
            self.cabal = None
            if settings.cabalspy_enabled:
                log.info("CabalSpy disabled: set CABALSPY_API_KEY to enable cluster confirmation")

    async def aclose(self) -> None:
        """Close auxiliary HTTP clients (Birdeye client is owned by main)."""
        for aux in (self.dex, self.jup, self.rug, self.cabal):
            if aux is not None:
                try:
                    await aux.close()
                except Exception:  # noqa: BLE001 - shutdown path only
                    pass

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

    def _apply_age_gate(
        self, candidate: TokenCandidate, age_hours: float | None, source: str
    ) -> bool:
        """Attach age metadata and return True when the token may be kept."""
        candidate.age_hours = age_hours
        candidate.age_source = source
        if age_hours is None:
            return not (self.settings.enforce_token_age and self.settings.age_strict)
        if (
            self.settings.enforce_token_age
            and age_hours > self.settings.max_token_age_hours
        ):
            return False
        return True

    @staticmethod
    def _fmt_age(candidate: TokenCandidate) -> str:
        """Format token age for logs."""
        if candidate.age_hours is None:
            return "unknown-age"
        return f"{candidate.age_hours:.1f}h/{candidate.age_source}"

    async def _dex_pairs(self, address: str) -> list[dict] | None:
        """Return DexScreener pairs, or None when unavailable/disabled."""
        if self.dex is None:
            return None
        try:
            return await self.dex.token_pairs(address)
        except DexScreenerError as exc:
            log.warning("dexscreener failed %s: %s", address[:8], exc)
            return None

    async def _jupiter_entry(self, address: str) -> dict | None:
        """Return the Jupiter price object, or None when unavailable."""
        if self.jup is None:
            return None
        try:
            entry = await self.jup.price(address)
            return entry or None
        except JupiterError as exc:
            log.warning("jupiter failed %s: %s", address[:8], exc)
            return None

    async def _resolve_age(
        self,
        address: str,
        dex_pairs: list[dict] | None,
        jup_entry: dict | None,
    ) -> tuple[float | None, str]:
        """Resolve token age in hours via free oracles first.

        Priority: Jupiter token-level ``createdAt`` (strongest, mint time)
        → DexScreener oldest-pool time (sound upper bound: a pool cannot
        predate its tokens, so an old oldest-pool safely rejects) →
        Birdeye ``creation_info`` (paid tiers, strict mode only) →
        unknown. On free Standard the paid call is skipped entirely unless
        strict mode demands it.
        """
        now = time.time()
        if jup_entry:
            stamp = jupiter_age_unix(jup_entry)
            if stamp:
                return (now - stamp) / 3600.0, "jupiter"
        if dex_pairs:
            oldest, _count = pair_age_info(dex_pairs)
            if oldest:
                return (now - oldest) / 3600.0, "dexscreener"
        if self.settings.age_strict:
            creation_unix = await self._creation_unix(address)
            if creation_unix:
                return (now - creation_unix) / 3600.0, "creation_info"
        return None, "unknown"

    async def _rugcheck_ok(self, candidate: TokenCandidate) -> bool:
        """Apply the RugCheck pre-entry veto. Fail-open unless strict."""
        if self.rug is None:
            return True
        try:
            summary = await self.rug.summary(candidate.address)
        except RugCheckError as exc:
            if self.settings.rugcheck_strict:
                log.info("skip %-10s RugCheck unknown (strict): %s", candidate.symbol, exc)
                return False
            log.warning("rug unknown %-10s (allowing): %s", candidate.symbol, exc)
            candidate.risk_flags = ("rug:unknown",)
            return True
        allowed, reasons, score = rugcheck_verdict(
            summary,
            max_score=self.settings.rugcheck_max_score,
            reject_danger=self.settings.rugcheck_reject_danger,
        )
        candidate.risk_score = score
        if allowed:
            candidate.risk_flags = (f"rug:score={score}",) if score is not None else ()
            return True
        candidate.risk_flags = tuple(["rug:blocked"] + reasons[:2])
        log.info(
            "skip %-10s RugCheck veto score=%s [%s]",
            candidate.symbol,
            score,
            "; ".join(reasons),
        )
        return False

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

            # Free keyless enrichment (never billed, fail-open). Fetched
            # up front: age resolution and overview-gap filling both need it.
            dex_pairs = await self._dex_pairs(address)
            jup_entry = await self._jupiter_entry(address)

            candidate = self._candidate_from_overview(address, overview)
            if candidate is None and dex_pairs:
                # Birdeye overview incomplete: try the DexScreener best
                # pair as a fallback so one missing field does not drop
                # an otherwise valid early token.
                candidate = self._candidate_from_dex(address, overview, dex_pairs)
                if candidate is not None:
                    log.info(
                        "enriched %-10s from DexScreener (Birdeye overview incomplete)",
                        candidate.symbol,
                    )
            if candidate is None:
                continue
            if candidate.liquidity_usd < self.settings.min_liquidity_usd:
                continue
            if not self.settings.min_market_cap_usd <= candidate.market_cap_usd <= self.settings.max_market_cap_usd:
                continue
            if candidate.price_change_24h_pct > self.settings.max_price_change_24h_percent:
                continue
            if self.settings.enforce_token_age:
                age_hours, age_source = await self._resolve_age(address, dex_pairs, jup_entry)
                if not self._apply_age_gate(candidate, age_hours, age_source):
                    log.info(
                        "skip %-10s age=%s exceeds %dh",
                        candidate.symbol,
                        self._fmt_age(candidate),
                        self.settings.max_token_age_hours,
                    )
                    continue
            if self.settings.rugcheck_enabled:
                if not await self._rugcheck_ok(candidate):
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
            risk_text = f" risk={token.risk_score}" if token.risk_score is not None else ""
            log.info(
                "candidate %-10s MC=$%s [%s] LP=$%s 24h=%+.1f%% age=%s%s %s",
                token.symbol,
                f"{token.market_cap_usd:,.0f}",
                token.market_cap_source or "?",
                f"{token.liquidity_usd:,.0f}",
                token.price_change_24h_pct,
                self._fmt_age(token),
                risk_text,
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

    @staticmethod
    def _candidate_from_dex(
        address: str, overview: dict, dex_pairs: list[dict]
    ) -> TokenCandidate | None:
        """Build a candidate from the DexScreener best pair as fallback."""
        best = select_best_pair(dex_pairs, address)
        if best is None:
            return None
        enriched = enrichment_from_pair(best)
        market_cap = enriched["market_cap_usd"] or 0.0
        liquidity = enriched["liquidity_usd"] or 0.0
        price = enriched["price_usd"] or 0.0
        if market_cap <= 0 or liquidity <= 0 or price <= 0:
            return None
        base_token = best.get("baseToken", {})
        symbol = str(
            overview.get("symbol", "")
            or (base_token.get("symbol", "") if isinstance(base_token, dict) else "")
            or "?"
        )
        try:
            change = float(enriched["price_change_24h_pct"] or 0.0)
        except (TypeError, ValueError):
            change = 0.0
        return TokenCandidate(
            address,
            symbol,
            market_cap,
            liquidity,
            price,
            change,
            market_cap_source=enriched["market_cap_source"] or "dex",
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

        # Execution price chain: newest normalized trade price first
        # (no extra request), then free Jupiter quote, then discovery
        # snapshot as last resort. The source is logged, never hidden.
        trade_price, _ = latest_trade_price(normalized)
        if trade_price is not None:
            token.price_usd = trade_price
            token.price_source = "trade"
        else:
            jup_price = await self._jupiter_price_usd(token.address)
            if jup_price is not None:
                token.price_usd = jup_price
                token.price_source = "jupiter"
            else:
                token.price_source = "discovery"

        # Optional CabalSpy cluster confirmation (advisory unless required).
        cluster_wallets: int | None = None
        if self.cabal is not None:
            try:
                signals = await self.cabal.cluster_signals(
                    min_wallets=self.settings.cabalspy_min_wallets
                )
                cluster_wallets = parse_cluster_for_token(signals, token.address)
            except CabalSpyError as exc:
                log.warning("cabal cluster failed %-10s: %s", token.symbol, exc)

        log.info(
            "watch %-10s smart=%d buy%%=%.0f CVD=$%+.0f buy/sell=%.2fx price[%s]=$%.8f%s",
            token.symbol,
            smart.tagged_wallets,
            smart.buy_ratio * 100,
            state.cvd_usd,
            state.buy_sell_ratio,
            token.price_source,
            token.price_usd,
            f" cluster={cluster_wallets}" if cluster_wallets is not None else "",
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
            if self.settings.cabalspy_require_cluster and self.cabal is not None:
                if cluster_wallets is None or cluster_wallets < self.settings.cabalspy_min_wallets:
                    log.info(
                        "skip BUY %-10s no CabalSpy cluster (need %d, required)",
                        token.symbol,
                        self.settings.cabalspy_min_wallets,
                    )
                    return
            self.positions[token.address] = PositionState(token.price_usd, now, token.symbol)
            log.warning(
                "BUY SIGNAL %-10s price[%s]=$%.8f smart=%d smart_buy%%=%.0f CVD=$%+.0f ratio=%.2fx%s",
                token.symbol,
                token.price_source,
                token.price_usd,
                smart.tagged_wallets,
                smart.buy_ratio * 100,
                state.cvd_usd,
                state.buy_sell_ratio,
                f" cluster={cluster_wallets}" if cluster_wallets is not None else "",
            )

    async def _jupiter_price_usd(self, address: str) -> float | None:
        """Return a fresh Jupiter quote, or None when unavailable."""
        entry = await self._jupiter_entry(address)
        if not entry:
            return None
        return jupiter_price_usd(entry)

    async def run(self) -> None:
        """Run discovery and monitoring until interrupted."""
        while True:
            now = time.time()
            if now - self.last_discovery >= self.settings.discovery_interval_seconds:
                try:
                    await self.discover()
                except BirdeyeError as exc:
                    log.error("discovery failed: %s", exc)
                except Exception as exc:  # noqa: BLE001 - loop must survive aux bugs
                    log.error("discovery failed unexpectedly: %s", exc, exc_info=True)

            # Sequential polling is intentional: Standard is documented at
            # 1 request/sec, so we avoid concurrent bursts.
            for token in list(self.watched.values()):
                await self.poll_token(token)

            await asyncio.sleep(self.settings.poll_interval_seconds)
