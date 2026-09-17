"""Unit tests for paper accounting and Telegram messages (no network)."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from birdeye_smart_cvd.config import Settings  # noqa: E402
from birdeye_smart_cvd.models import TokenCandidate  # noqa: E402
from birdeye_smart_cvd.paper import PaperPortfolio  # noqa: E402
from birdeye_smart_cvd.scanner import Scanner  # noqa: E402
from birdeye_smart_cvd.strategy import RollingCVD  # noqa: E402
from birdeye_smart_cvd.telegram import (  # noqa: E402
    CloseAlert,
    OpenAlert,
    StartupAlert,
    TelegramNotifier,
    build_close_message,
    build_open_message,
    build_startup_message,
)

CA = "CbyTNf7UPzvewHh4Zp6umogM2RWahhmGRJWLJnPwpump"


class PortfolioTests(unittest.TestCase):
    def test_open_moves_cash(self):
        pf = PaperPortfolio(1000.0, 100.0, 3)
        result = pf.try_open(0)
        self.assertTrue(result.opened)
        self.assertEqual(result.notional_usd, 100.0)
        self.assertEqual((result.balance_before_usd, result.balance_after_usd), (1000.0, 900.0))
        self.assertEqual(pf.cash_usd, 900.0)

    def test_max_positions_gate(self):
        pf = PaperPortfolio(1000.0, 100.0, 2)
        self.assertTrue(pf.try_open(1).opened)
        rejected = pf.try_open(2)
        self.assertFalse(rejected.opened)
        self.assertIn("max open", rejected.reason)
        self.assertEqual(pf.cash_usd, 900.0)

    def test_insufficient_funds(self):
        pf = PaperPortfolio(50.0, 100.0, 3)
        rejected = pf.try_open(0)
        self.assertFalse(rejected.opened)
        self.assertIn("insufficient", rejected.reason)
        self.assertEqual(pf.cash_usd, 50.0)

    def test_close_win_and_loss(self):
        pf = PaperPortfolio(1000.0, 100.0, 3)
        pf.try_open(0)
        win = pf.close(1.0, 1.5, 100.0)
        self.assertAlmostEqual(win.pnl_usd, 50.0)
        self.assertAlmostEqual(win.pnl_pct, 50.0)
        self.assertEqual((win.cash_before_usd, win.cash_after_usd), (900.0, 1050.0))
        self.assertEqual((win.wins, win.losses), (1, 0))
        self.assertAlmostEqual(win.win_rate_pct, 100.0)
        pf.try_open(0)
        loss = pf.close(2.0, 1.0, 100.0)
        self.assertAlmostEqual(loss.pnl_usd, -50.0)
        self.assertEqual((loss.wins, loss.losses), (1, 1))
        self.assertAlmostEqual(loss.win_rate_pct, 50.0)
        self.assertAlmostEqual(pf.realized_pnl_usd, 0.0)

    def test_win_rate_none_before_first_close(self):
        pf = PaperPortfolio(1000.0, 100.0, 3)
        self.assertIsNone(pf.win_rate_pct)
        self.assertEqual(pf.closed_trades, 0)


def _open_alert(**over) -> OpenAlert:
    base = {
        "symbol": "BIKE",
        "name": "BIKE TYSON",
        "address": CA,
        "entry_price_usd": 0.003,
        "price_source": "trade",
        "notional_usd": 100.0,
        "balance_before_usd": 1000.0,
        "balance_after_usd": 900.0,
        "open_positions": 1,
        "max_open_positions": 3,
        "smart_wallets": 3,
        "smart_buy_pct": 67.0,
        "cvd_usd": 2961.0,
        "cvd_ratio": 2.0,
        "age_text": "5.2h/jupiter",
        "risk_text": "Score 1",
        "top10_text": "12.3% of supply",
        "extra_notes": ("🔗 Cluster: 4 tracked wallets",),
        "timestamp": 1789596000.0,
    }
    base.update(over)
    return OpenAlert(**base)


def _close_alert(**over) -> CloseAlert:
    base = {
        "symbol": "BIKE",
        "name": "BIKE TYSON",
        "address": CA,
        "reason": "Take-profit 🎯",
        "reason_icon": "🟢",
        "exit_price_usd": 0.0045,
        "entry_price_usd": 0.003,
        "pnl_usd": 50.0,
        "pnl_pct": 50.0,
        "hold_seconds": 3720.0,
        "cash_before_usd": 900.0,
        "cash_after_usd": 950.0,
        "realized_total_usd": 50.0,
        "wins": 1,
        "losses": 0,
        "win_rate_pct": 100.0,
        "open_positions": 0,
        "max_open_positions": 3,
        "timestamp": 1789596000.0,
    }
    base.update(over)
    return CloseAlert(**base)


class MessageTests(unittest.TestCase):
    def test_open_has_all_required_fields(self):
        msg = build_open_message(_open_alert())
        for needle in ("BIKE", "BIKE TYSON", CA, "0.003", "trade",
                       "$1,000.00", "$900.00", "1/3", "67%",
                       "5.2h/jupiter", "Score 1", "12.3%", "Cluster: 4"):
            self.assertIn(needle, msg)
        self.assertNotIn("...", msg)

    def test_close_has_all_required_fields(self):
        msg = build_close_message(_close_alert())
        for needle in ("BIKE", "BIKE TYSON", CA, "Take-profit",
                       "+$50.00", "+50.0%", "1h 2m",
                       "$900.00", "$950.00", "+$50.00",
                       "100.0% (1W-0L)", "0/3"):
            self.assertIn(needle, msg)
        self.assertNotIn("...", msg)

    def test_close_loss_and_empty_win_rate(self):
        msg = build_close_message(_close_alert(
            pnl_usd=-30.0, pnl_pct=-15.0, reason="Stop-loss 🛑",
            wins=0, losses=1, win_rate_pct=0.0,
        ))
        self.assertIn("-$30.00", msg)
        self.assertIn("0.0% (0W-1L)", msg)
        msg = build_close_message(_close_alert(win_rate_pct=None))
        self.assertIn("n/a (no closed trades)", msg)

    def test_startup_message(self):
        msg = build_startup_message(StartupAlert(1000.0, 100.0, 3, 3, ("🧠 x",)))
        self.assertIn("$1,000.00", msg)
        self.assertIn("3", msg)

    def test_markdownify_survives_adversarial_symbols(self):
        from telegramify_markdown import markdownify

        nasty = _open_alert(symbol="A_B*C[D]E~F`G", name="X_y $100 (soon!)",
                            address=CA)
        for builder, alert in ((build_open_message, nasty),
                               (build_close_message, _close_alert()),
                               (build_startup_message, StartupAlert())):
            out = markdownify(builder(alert))
            self.assertTrue(out.strip())
        # Full CA must survive conversion byte-for-byte inside code.
        self.assertIn(CA, markdownify(build_open_message(_open_alert())))


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_credentials(self):
        notifier = TelegramNotifier("", "")
        try:
            self.assertFalse(notifier.enabled)
            self.assertFalse(await notifier.send_open(_open_alert()))
            self.assertFalse(await notifier.send_close(_close_alert()))
            self.assertFalse(await notifier.send_startup(StartupAlert()))
        finally:
            await notifier.aclose()


class FakeBirdeye:
    """Canned smart-money + CVD flow that always passes entry gates."""

    def __init__(self):
        self.trades = None  # override per test; None = default flow

    async def top_traders(self, address, limit):
        return [
            {"owner": "W1", "tags": ["smart_trader"],
             "volumeBuyUSD": 70.0, "volumeSellUSD": 30.0},
            {"owner": "W2", "tags": ["smart_trader"],
             "volumeBuyUSD": 60.0, "volumeSellUSD": 40.0},
        ]

    async def token_trades(self, address, after_time=None, limit=100):
        if self.trades is not None:
            return self.trades
        now = int(time.time())
        leg = {"address": address, "price": 1.0}
        other = {"address": "So11111111111111111111111111111111111111112", "price": 100.0}
        return [
            {"side": "buy", "volume_usd": 500.0, "block_unix_time": now - 60,
             "tx_hash": "H1", "ins_index": 0, "inner_ins_index": 0,
             "from": leg, "to": other},
            {"side": "sell", "volume_usd": 100.0, "block_unix_time": now - 30,
             "tx_hash": "H2", "ins_index": 0, "inner_ins_index": 0,
             "from": other, "to": leg},
        ]


class FakeNotifier:
    """Records alerts instead of sending them."""

    enabled = True

    def __init__(self):
        self.sent = []

    async def send_open(self, alert):
        self.sent.append(("open", alert))
        return True

    async def send_close(self, alert):
        self.sent.append(("close", alert))
        return True

    async def send_startup(self, alert):
        self.sent.append(("startup", alert))
        return True

    async def close(self):
        pass

    async def aclose(self):
        pass


def _token(price=1.0, symbol="BIKE", name="BIKE TYSON", address=CA):
    return TokenCandidate(address, symbol, 100_000.0, 20_000.0, price, 5.0, name=name)


class ScannerAlertFlowTests(unittest.IsolatedAsyncioTestCase):
    def _scanner(self, fake, settings, notices):
        scanner = Scanner(fake, settings, notifier=notices)
        # Hermetic: no aux network in unit tests.
        scanner.dex = None
        scanner.jup = None
        scanner.rug = None
        scanner.cabal = None
        scanner.helius = None
        return scanner

    def _watch(self, scanner, token):
        # poll_token expects CVD state seeded by discover().
        scanner.cvd.setdefault(token.address, RollingCVD(900))

    async def test_buy_then_stoploss_alerts(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        fake = FakeBirdeye()
        scanner = self._scanner(fake, settings, notices)
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)
            self.assertIn(token.address, scanner.positions)
            kinds = [k for k, _ in notices.sent]
            self.assertEqual(kinds, ["open"])
            _kind, open_alert = notices.sent[0]
            self.assertEqual(open_alert.address, CA)
            self.assertEqual(open_alert.balance_before_usd, 1000.0)
            self.assertEqual(open_alert.balance_after_usd, 900.0)

            # No new trades and a crashed price: trade/jupiter/discovery
            # chain leaves the manual mark, stop-loss fires.
            fake.trades = []
            token.price_usd = 0.5  # -50% crashes through the stop-loss
            await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            kinds = [k for k, _ in notices.sent]
            self.assertEqual(kinds, ["open", "close"])
            _kind, close_alert = notices.sent[1]
            self.assertIn("Stop-loss", close_alert.reason)
            self.assertAlmostEqual(close_alert.pnl_pct, -50.0)
            self.assertEqual((close_alert.wins, close_alert.losses), (0, 1))
            rendered = build_close_message(close_alert)
            self.assertIn(CA, rendered)
            self.assertNotIn("...", rendered)
        finally:
            await scanner.aclose()

    async def test_max_positions_blocks_entry(self):
        settings = Settings(api_key="x", max_open_positions=1)
        notices = FakeNotifier()
        scanner = self._scanner(FakeBirdeye(), settings, notices)
        try:
            first = _token(price=1.0, symbol="AAA", address="AAA111")
            self._watch(scanner, first)
            await scanner.poll_token(first)
            second = _token(price=1.0, symbol="BBB", address="BBB222")
            self._watch(scanner, second)
            await scanner.poll_token(second)
            self.assertNotIn(second.address, scanner.positions)
            self.assertEqual([k for k, _ in notices.sent], ["open"])
        finally:
            await scanner.aclose()


if __name__ == "__main__":
    unittest.main()
