"""Telegram open/close alerts: markdown builders + PTB sender.

Messages are authored as plain Markdown (with icons), converted with
``telegramify-markdown`` (``markdownify`` → MarkdownV2) and sent with
``python-telegram-bot``. Sending is fail-open: any Telegram failure is
logged and never disturbs the scan loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def usd(value: float) -> str:
    """Format a dollar amount with sign for PnL-style values."""
    if value < 0:
        return f"-${abs(value):,.2f}"
    if value > 0:
        return f"+${value:,.2f}"
    return "$0.00"


def cash(value: float) -> str:
    """Format a cash balance (no sign)."""
    return f"${value:,.2f}"


def pct(value: float) -> str:
    """Format a percentage with sign."""
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.1f}%"


def duration(seconds: float) -> str:
    """Format a hold time as '2h 14m', '3m 20s' or '45s'."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def utc_stamp(epoch: float | None = None) -> str:
    """Format an epoch as 'YYYY-MM-DD HH:MM:SS UTC'."""
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(), tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def win_rate_text(win_rate_pct: float | None, wins: int, losses: int) -> str:
    """Format win rate as '63.6% (7W-4L)' or 'n/a (no closed trades)'."""
    if win_rate_pct is None:
        return "n/a (no closed trades)"
    return f"{win_rate_pct:.1f}% ({wins}W-{losses}L)"


@dataclass(slots=True)
class OpenAlert:
    """Everything the OPEN message needs (all plain values)."""

    symbol: str
    name: str
    address: str
    entry_price_usd: float
    price_source: str = "discovery"
    notional_usd: float = 0.0
    balance_before_usd: float = 0.0
    balance_after_usd: float = 0.0
    open_positions: int = 1
    max_open_positions: int = 3
    smart_wallets: int = 0
    smart_buy_pct: float = 0.0
    cvd_usd: float = 0.0
    cvd_ratio: float = 0.0
    age_text: str = "unknown-age"
    risk_text: str = ""
    top10_text: str = ""
    extra_notes: tuple[str, ...] = ()
    timestamp: float = 0.0


@dataclass(slots=True)
class CloseAlert:
    """Everything the CLOSE message needs (all plain values)."""

    symbol: str
    name: str
    address: str
    reason: str
    reason_icon: str = "🔴"
    exit_price_usd: float = 0.0
    entry_price_usd: float = 0.0
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    hold_seconds: float = 0.0
    cash_before_usd: float = 0.0
    cash_after_usd: float = 0.0
    realized_total_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float | None = None
    open_positions: int = 0
    max_open_positions: int = 3
    timestamp: float = 0.0
    # Advisory simulate-only execution check ("" when unavailable).
    sim_text: str = ""


@dataclass(slots=True)
class StartupAlert:
    """Scanner boot summary."""

    start_balance_usd: float = 0.0
    position_size_usd: float = 0.0
    max_open_positions: int = 3
    max_watched_tokens: int = 3
    detail_lines: tuple[str, ...] = ()


def build_open_message(alert: OpenAlert) -> str:
    """Render the OPEN alert as raw Markdown (full CA, never truncated)."""
    lines = [
        f"🟢 **PAPER OPEN — {alert.symbol}**",
        "",
        f"🪙 Name: {alert.name}",
        f"💱 Symbol: {alert.symbol}",
        "📋 CA:",
        "```",
        alert.address,
        "```",
        f"💵 Entry: ${alert.entry_price_usd:.8f} [{alert.price_source}]",
        f"💰 Size: {cash(alert.notional_usd)}",
        f"🏦 Balance: {cash(alert.balance_before_usd)} → {cash(alert.balance_after_usd)}",
        f"📂 Positions: {alert.open_positions}/{alert.max_open_positions}",
        f"🧠 Smart-proxy: {alert.smart_wallets} wallets, buys {alert.smart_buy_pct:.0f}%",
        f"📈 CVD: ${alert.cvd_usd:+,.0f} ({alert.cvd_ratio:.2f}x)",
        f"🕰 Age: {alert.age_text}",
    ]
    if alert.risk_text:
        lines.append(f"🛡 Risk: {alert.risk_text}")
    if alert.top10_text:
        lines.append(f"🐋 Holders: {alert.top10_text}")
    lines.extend(alert.extra_notes)
    lines.append(f"🕒 {utc_stamp(alert.timestamp)}")
    return "\n".join(lines)


def build_close_message(alert: CloseAlert) -> str:
    """Render the CLOSE alert as raw Markdown (full CA, never truncated)."""
    lines = [
        f"{alert.reason_icon} **PAPER CLOSE — {alert.symbol} ({alert.reason})**",
        "",
        f"🪙 Name: {alert.name}",
        f"💱 Symbol: {alert.symbol}",
        "📋 CA:",
        "```",
        alert.address,
        "```",
        f"💵 Exit: ${alert.exit_price_usd:.8f} (entry ${alert.entry_price_usd:.8f})",
        f"📊 PnL: {usd(alert.pnl_usd)} ({pct(alert.pnl_pct)})",
        f"⏳ Held: {duration(alert.hold_seconds)}",
        f"🏦 Balance: {cash(alert.cash_before_usd)} → {cash(alert.cash_after_usd)}",
        f"💼 Realized total: {usd(alert.realized_total_usd)}",
        f"🏆 Win rate: {win_rate_text(alert.win_rate_pct, alert.wins, alert.losses)}",
        f"📂 Positions: {alert.open_positions}/{alert.max_open_positions}",
    ]
    if alert.sim_text:
        lines.append(alert.sim_text)
    lines.append(f"🕒 {utc_stamp(alert.timestamp)}")
    return "\n".join(lines)


def build_startup_message(alert: StartupAlert) -> str:
    """Render the boot summary as raw Markdown."""
    lines = [
        "🤖 **Scanner started — paper mode**",
        "",
        f"💰 Start balance: {cash(alert.start_balance_usd)}",
        f"💰 Position size: {cash(alert.position_size_usd)}",
        f"📂 Max positions: {alert.max_open_positions}",
        f"👀 Max watched: {alert.max_watched_tokens}",
    ]
    lines.extend(alert.detail_lines)
    lines.append(f"🕒 {utc_stamp()}")
    return "\n".join(lines)


def _parse_chat_id(raw: str) -> int | str:
    """Accept numeric chat IDs and @channel names."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


class TelegramNotifier:
    """Send Markdown alerts via python-telegram-bot. Fail-open always."""

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        *,
        enabled: bool = True,
        max_retry_wait_seconds: float = 60.0,
    ) -> None:
        self._enabled = bool(enabled and bot_token and chat_id)
        self._chat_id: int | str = _parse_chat_id(chat_id) if chat_id else ""
        self._max_retry_wait = max(1.0, max_retry_wait_seconds)
        self._bot = None
        if self._enabled:
            from telegram import Bot

            self._bot = Bot(token=bot_token)
        elif enabled:
            log.info("Telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable alerts")

    @property
    def enabled(self) -> bool:
        """Whether alerts will actually be sent."""
        return self._enabled and self._bot is not None

    async def aclose(self) -> None:
        """Shut down the bot session."""
        if self._bot is not None:
            try:
                await self._bot.shutdown()
            except Exception:  # noqa: BLE001 - shutdown path only
                pass

    async def close(self) -> None:
        """Alias for aclose (uniform shutdown with the HTTP clients)."""
        await self.aclose()

    async def send_markdown(self, markdown_text: str) -> bool:
        """Convert Markdown and send it. Returns True on success."""
        if not self.enabled or self._bot is None:
            return False
        try:
            from telegramify_markdown import markdownify

            payload = markdownify(markdown_text)
        except Exception as exc:  # noqa: BLE001 - fall back to plain text
            log.warning("telegramify failed, sending plain text: %s", exc)
            payload = None
        try:
            if payload is None:
                await self._bot.send_message(chat_id=self._chat_id, text=markdown_text)
            else:
                from telegram.constants import ParseMode

                await self._bot.send_message(
                    chat_id=self._chat_id, text=payload, parse_mode=ParseMode.MARKDOWN_V2
                )
            return True
        except Exception as exc:  # noqa: BLE001 - network/API failures
            wait = self._retry_wait(exc)
            if wait is not None:
                log.warning("telegram rate-limited, retrying once in %.0fs", wait)
                await asyncio.sleep(wait)
                try:
                    if payload is None:
                        await self._bot.send_message(chat_id=self._chat_id, text=markdown_text)
                    else:
                        from telegram.constants import ParseMode

                        await self._bot.send_message(
                            chat_id=self._chat_id,
                            text=payload,
                            parse_mode=ParseMode.MARKDOWN_V2,
                        )
                    return True
                except Exception as retry_exc:  # noqa: BLE001
                    log.warning("telegram resend failed: %s", retry_exc)
                    return False
            log.warning("telegram send failed: %s", exc)
            return False

    def _retry_wait(self, exc: Exception) -> float | None:
        """Return seconds to wait for rate limits, else None."""
        try:
            from telegram.error import RetryAfter

            if isinstance(exc, RetryAfter):
                return min(float(exc.retry_after), self._max_retry_wait)
        except ImportError:
            pass
        return None

    async def send_open(self, alert: OpenAlert) -> bool:
        """Build and send the OPEN message."""
        return await self.send_markdown(build_open_message(alert))

    async def send_close(self, alert: CloseAlert) -> bool:
        """Build and send the CLOSE message."""
        return await self.send_markdown(build_close_message(alert))

    async def send_startup(self, alert: StartupAlert) -> bool:
        """Build and send the boot summary."""
        return await self.send_markdown(build_startup_message(alert))


# Re-exported for tests that prefer not to import telegramify directly.
__all__ = [
    "CloseAlert",
    "OpenAlert",
    "StartupAlert",
    "TelegramNotifier",
    "build_close_message",
    "build_open_message",
    "build_startup_message",
    "cash",
    "duration",
    "pct",
    "usd",
    "utc_stamp",
    "win_rate_text",
]
