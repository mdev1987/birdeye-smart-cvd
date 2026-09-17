"""Paper portfolio: cash accounting, realized PnL and win-rate tracking.

Cash model: opening a position moves ``notional`` out of cash (a paper
buy); closing returns ``notional * exit / entry`` to cash (a paper sell).
No leverage, no fees, no real transactions — research bookkeeping only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class OpenResult:
    """Outcome of a paper open attempt."""

    opened: bool
    reason: str = ""
    notional_usd: float = 0.0
    balance_before_usd: float = 0.0
    balance_after_usd: float = 0.0


@dataclass(slots=True)
class CloseResult:
    """Outcome of a paper close."""

    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    cash_before_usd: float = 0.0
    cash_after_usd: float = 0.0
    realized_total_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float | None = None


class PaperPortfolio:
    """Track paper cash, realized PnL and win rate across signals."""

    def __init__(
        self,
        start_balance_usd: float,
        position_size_usd: float,
        max_open_positions: int,
    ) -> None:
        self.start_balance_usd = start_balance_usd
        self.position_size_usd = position_size_usd
        self.max_open_positions = max_open_positions
        self.cash_usd = start_balance_usd
        self.realized_pnl_usd = 0.0
        self.wins = 0
        self.losses = 0

    @property
    def win_rate_pct(self) -> float | None:
        """Win rate over closed positions, or None before the first close."""
        total = self.wins + self.losses
        return (self.wins / total * 100.0) if total else None

    @property
    def closed_trades(self) -> int:
        """Number of closed paper positions."""
        return self.wins + self.losses

    def try_open(self, open_positions: int) -> OpenResult:
        """Reserve cash for one paper position, if allowed."""
        if open_positions >= self.max_open_positions:
            return OpenResult(
                opened=False,
                reason=f"max open positions reached ({open_positions}/{self.max_open_positions})",
                balance_before_usd=self.cash_usd,
                balance_after_usd=self.cash_usd,
            )
        notional = self.position_size_usd
        if self.cash_usd < notional:
            return OpenResult(
                opened=False,
                reason=(
                    f"insufficient paper funds "
                    f"(cash ${self.cash_usd:,.2f} < size ${notional:,.2f})"
                ),
                balance_before_usd=self.cash_usd,
                balance_after_usd=self.cash_usd,
            )
        before = self.cash_usd
        self.cash_usd -= notional
        return OpenResult(
            opened=True,
            notional_usd=notional,
            balance_before_usd=before,
            balance_after_usd=self.cash_usd,
        )

    def close(self, entry_price_usd: float, exit_price_usd: float, notional_usd: float,
              *, win_override: bool | None = None) -> CloseResult:
        """Settle one paper position and update realized stats.

        ``win_override`` forces the win/loss verdict: partial take-profits
        settle through :meth:`close_partial` (never counted), and the final
        runner close passes the verdict computed over partials + runner
        combined, so one scaled exit counts as one trade.
        """
        cash_before = self.cash_usd
        if entry_price_usd > 0 and notional_usd > 0:
            proceeds = notional_usd * exit_price_usd / entry_price_usd
        else:
            proceeds = notional_usd
        pnl = proceeds - notional_usd
        pnl_pct = (exit_price_usd / entry_price_usd - 1.0) * 100.0 if entry_price_usd > 0 else 0.0
        self.cash_usd += proceeds
        self.realized_pnl_usd += pnl
        won = (pnl >= 0) if win_override is None else win_override
        if won:
            self.wins += 1
        else:
            self.losses += 1
        return CloseResult(
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            cash_before_usd=cash_before,
            cash_after_usd=self.cash_usd,
            realized_total_usd=self.realized_pnl_usd,
            wins=self.wins,
            losses=self.losses,
            win_rate_pct=self.win_rate_pct,
        )

    def close_partial(self, entry_price_usd: float, exit_price_usd: float,
                      notional_usd: float) -> CloseResult:
        """Settle one scale-out slice: cash + realized move, no win/loss.

        Win rate is judged once, on the whole position at final close
        (see ``win_override``), so ladder rungs never inflate the count.
        """
        cash_before = self.cash_usd
        if entry_price_usd > 0 and notional_usd > 0:
            proceeds = notional_usd * exit_price_usd / entry_price_usd
        else:
            proceeds = notional_usd
        pnl = proceeds - notional_usd
        pnl_pct = (exit_price_usd / entry_price_usd - 1.0) * 100.0 if entry_price_usd > 0 else 0.0
        self.cash_usd += proceeds
        self.realized_pnl_usd += pnl
        return CloseResult(
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            cash_before_usd=cash_before,
            cash_after_usd=self.cash_usd,
            realized_total_usd=self.realized_pnl_usd,
            wins=self.wins,
            losses=self.losses,
            win_rate_pct=self.win_rate_pct,
        )
