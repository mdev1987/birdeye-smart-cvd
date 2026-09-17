"""Unit tests for paper accounting and Telegram messages (no network)."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from birdeye_smart_cvd.config import Settings  # noqa: E402
from birdeye_smart_cvd.jupsim import SimResult  # noqa: E402
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

    def test_close_partial_moves_cash_without_counting(self):
        pf = PaperPortfolio(1000.0, 100.0, 3)
        pf.try_open(0)
        part = pf.close_partial(1.0, 1.5, 50.0)
        self.assertAlmostEqual(part.pnl_usd, 25.0)
        self.assertAlmostEqual(part.pnl_pct, 50.0)
        self.assertEqual((pf.wins, pf.losses), (0, 0))
        self.assertIsNone(pf.win_rate_pct)
        self.assertAlmostEqual(pf.realized_pnl_usd, 25.0)

    def test_close_win_override_judges_whole_position(self):
        pf = PaperPortfolio(1000.0, 100.0, 3)
        pf.try_open(0)
        pf.close_partial(1.0, 1.5, 50.0)  # +25 banked
        # Runner slice loses, but the whole position won overall.
        final = pf.close(1.0, 0.9, 50.0, win_override=True)
        self.assertAlmostEqual(final.pnl_usd, -5.0)
        self.assertEqual((pf.wins, pf.losses), (1, 0))
        self.assertAlmostEqual(pf.realized_pnl_usd, 20.0)

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
        # Healthy CVD sample: 8 x $300 buys ($2400) + 4 x $150 sells ($600)
        # = $3000 volume over 12 trades at 4.0x ratio, clearing the
        # default CVD_MIN_VOLUME_USD=2000 / CVD_MIN_TRADES=10 floors.
        rows = []
        for i in range(8):
            rows.append(
                {"side": "buy", "volume_usd": 300.0, "block_unix_time": now - 120 + i,
                 "tx_hash": f"BUY{i}", "ins_index": 0, "inner_ins_index": i,
                 "from": leg, "to": other}
            )
        for i in range(4):
            rows.append(
                {"side": "sell", "volume_usd": 150.0, "block_unix_time": now - 60 + i,
                 "tx_hash": f"SELL{i}", "ins_index": 0, "inner_ins_index": i,
                 "from": other, "to": leg}
            )
        return rows


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

    async def test_tp_ladder_scales_out_then_runner_wins(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        fake = FakeBirdeye()
        scanner = self._scanner(fake, settings, notices)
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)  # entry @ 1.0
            self.assertIn(token.address, scanner.positions)

            fake.trades = []
            token.price_usd = 1.6  # +60% -> TP1 banks half
            await scanner.poll_token(token)
            position = scanner.positions.get(token.address)
            self.assertIsNotNone(position)
            assert position is not None
            self.assertTrue(position.tp1_done)
            self.assertAlmostEqual(position.remaining_notional_usd, 50.0)
            self.assertAlmostEqual(position.realized_pnl_usd, 30.0)
            # Partial never counts as a trade.
            self.assertEqual((scanner.portfolio.wins, scanner.portfolio.losses), (0, 0))
            kinds = [k for k, _ in notices.sent]
            self.assertEqual(kinds, ["open", "close"])
            self.assertIn("TP1", notices.sent[1][1].reason)

            token.price_usd = 2.1  # +110% -> TP2 banks half of remainder
            await scanner.poll_token(token)
            position = scanner.positions.get(token.address)
            assert position is not None
            self.assertTrue(position.tp2_done)
            self.assertAlmostEqual(position.remaining_notional_usd, 25.0)
            self.assertAlmostEqual(position.realized_pnl_usd, 57.5)

            token.price_usd = 0.5  # -50% -> stop-loss on the runner
            await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            # Banked +30 +27.5 outweighs the -12.5 runner: one win overall.
            self.assertEqual((scanner.portfolio.wins, scanner.portfolio.losses), (1, 0))
            self.assertAlmostEqual(scanner.portfolio.realized_pnl_usd, 45.0)
        finally:
            await scanner.aclose()

    async def test_trailing_stop_locks_peak_profit(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        fake = FakeBirdeye()
        scanner = self._scanner(fake, settings, notices)
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)  # entry @ 1.0

            fake.trades = []
            token.price_usd = 1.3  # +30%: arms trail, sets peak
            await scanner.poll_token(token)
            self.assertIn(token.address, scanner.positions)

            token.price_usd = 0.9  # -10% pnl but -31% from peak -> trail fires
            await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            _kind, close_alert = notices.sent[-1]
            self.assertIn("Trailing", close_alert.reason)
            self.assertAlmostEqual(close_alert.pnl_pct, -10.0)
        finally:
            await scanner.aclose()

    async def test_volume_death_exits_quiet_position(self):
        settings = Settings(api_key="x", volume_death_quiet_polls=3)
        notices = FakeNotifier()
        fake = FakeBirdeye()
        scanner = self._scanner(fake, settings, notices)
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)  # entry @ 1.0

            fake.trades = []
            for _ in range(3):
                await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            _kind, close_alert = notices.sent[-1]
            self.assertIn("Volume died", close_alert.reason)
        finally:
            await scanner.aclose()

    async def test_chase_guard_skips_runaway_entry(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        scanner = self._scanner(FakeBirdeye(), settings, notices)
        try:
            token = _token()
            token.discovery_price_usd = 0.7  # live 1.0 = +43% past discovery
            self._watch(scanner, token)
            await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            self.assertEqual(notices.sent, [])

            token2 = _token(symbol="OK", address="OK222")
            token2.discovery_price_usd = 0.8  # +25% <= 30% guard: allowed
            self._watch(scanner, token2)
            await scanner.poll_token(token2)
            self.assertIn(token2.address, scanner.positions)
        finally:
            await scanner.aclose()


class FakeSim:
    """Stub JupiterSim: canned route, records calls, never touches network."""

    def __init__(self, route_ok=True):
        self.calls = []
        self.route_ok = route_ok

    async def check_buy(self, mint, notional_usd, *, out_decimals):
        self.calls.append(("buy", mint, notional_usd))
        if not self.route_ok:
            return SimResult(side="buy", reason="no route: empty")
        return SimResult(side="buy", route_ok=True, sim_ok=True,
                         effective_price_usd=1.0, quoted_out_ui=100.0,
                         impact_pct=0.4, units_consumed=41000,
                         reason="simulated, not broadcast")

    async def check_sell(self, mint, qty_ui, decimals, ref=0.0):
        self.calls.append(("sell", mint, qty_ui))
        if not self.route_ok:
            return SimResult(side="sell", reason="no route: empty")
        return SimResult(side="sell", route_ok=True, sim_ok=True,
                         effective_price_usd=0.5, quoted_out_ui=50.0,
                         impact_pct=0.6, units_consumed=38000,
                         reason="simulated, not broadcast")


class SimWiringTests(unittest.IsolatedAsyncioTestCase):
    def _scanner(self, fake, settings, notices):
        scanner = Scanner(fake, settings, notifier=notices)
        scanner.dex = None
        scanner.jup = None
        scanner.rug = None
        scanner.cabal = None
        scanner.helius = None
        return scanner

    def _watch(self, scanner, token):
        scanner.cvd.setdefault(token.address, RollingCVD(900))

    async def test_buy_note_attached_to_open_alert(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        scanner = self._scanner(FakeBirdeye(), settings, notices)
        scanner.jupsim = FakeSim()
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)
            self.assertIn(token.address, scanner.positions)
            self.assertEqual(scanner.jupsim.calls[0][0], "buy")
            _kind, open_alert = notices.sent[0]
            sim_notes = [n for n in open_alert.extra_notes if "Sim buy" in n]
            self.assertEqual(len(sim_notes), 1)
            self.assertIn("route OK", sim_notes[0])
        finally:
            await scanner.aclose()

    async def test_require_route_blocks_routeless_entry(self):
        settings = Settings(api_key="x", sim_require_route=True)
        notices = FakeNotifier()
        scanner = self._scanner(FakeBirdeye(), settings, notices)
        scanner.jupsim = FakeSim(route_ok=False)
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)
            self.assertNotIn(token.address, scanner.positions)
            self.assertEqual(notices.sent, [])
        finally:
            await scanner.aclose()

    async def test_sell_note_attached_to_close_alert(self):
        settings = Settings(api_key="x")
        notices = FakeNotifier()
        fake = FakeBirdeye()
        scanner = self._scanner(fake, settings, notices)
        scanner.jupsim = FakeSim()

        async def six_decimals(address):
            return 6

        scanner._decimals = six_decimals
        try:
            token = _token()
            self._watch(scanner, token)
            await scanner.poll_token(token)
            fake.trades = []
            token.price_usd = 0.5
            await scanner.poll_token(token)
            _kind, close_alert = notices.sent[-1]
            self.assertIn("Stop-loss", close_alert.reason)
            self.assertIn("Sim sell", close_alert.sim_text)
            self.assertIn("route OK", close_alert.sim_text)
            kinds = [c[0] for c in scanner.jupsim.calls]
            self.assertIn("buy", kinds)
            self.assertIn("sell", kinds)
        finally:
            await scanner.aclose()


if __name__ == "__main__":
    unittest.main()
