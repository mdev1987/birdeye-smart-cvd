"""Unit tests for the pure strategy helpers (no network, no API keys)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from birdeye_smart_cvd.models import TokenCandidate  # noqa: E402
from birdeye_smart_cvd.strategy import (  # noqa: E402
    RollingCVD,
    TradePoint,
    entry_allowed,
    extract_trade_price,
    latest_trade_price,
    normalize_trade,
    parse_creation_unix,
    parse_market_cap,
    smart_money_stats,
)


def _cand(**over) -> TokenCandidate:
    base = {
        "address": "A",
        "symbol": "T",
        "market_cap_usd": 100_000,
        "liquidity_usd": 20_000,
        "price_usd": 1.0,
        "price_change_24h_pct": 5.0,
    }
    base.update(over)
    return TokenCandidate(**base)


class RollingCVDTests(unittest.TestCase):
    def test_adds_buy_sell_volume(self):
        r = RollingCVD(900)
        state = r.add(
            [
                TradePoint("a", 1000, "buy", 100.0),
                TradePoint("b", 1100, "sell", 40.0),
            ],
            now=1500,
        )
        self.assertEqual(state.cvd_usd, 60.0)
        self.assertAlmostEqual(state.buy_sell_ratio, 2.5)

    def test_removes_expired_trades(self):
        r = RollingCVD(900)
        r.add([TradePoint("a", 1000, "buy", 100.0)], now=1500)
        state = r.add([], now=2000)  # cutoff 1100, trade at 1000 expires
        self.assertEqual(state.buy_volume_usd, 0.0)
        self.assertEqual(len(r.points), 0)

    def test_no_double_count(self):
        r = RollingCVD(900)
        r.add([TradePoint("a", 1000, "buy", 100.0)], now=1500)
        state = r.add([TradePoint("a", 1000, "buy", 100.0)], now=1500)
        self.assertEqual(state.buy_volume_usd, 100.0)

    def test_seen_ids_bounded(self):
        r = RollingCVD(100)
        r.add([TradePoint("a", 1000, "buy", 10.0)], now=1050)
        r.add([], now=2000)  # everything expires
        self.assertEqual(r.state.seen_trade_ids, set())


class SmartMoneyTests(unittest.TestCase):
    def test_tag_normalization_and_volumes(self):
        rows = [
            {"tags": ["Smart_Trader"], "volumeBuyUSD": 60, "volumeSellUSD": 40},
            {"tag": "smart_trader", "volume_buy_usd": 10, "volume_sell_usd": 0},
            {"tags": ["random"], "volumeBuyUSD": 9999, "volumeSellUSD": 0},
            {"tags": [], "volumeBuyUSD": 5, "volumeSellUSD": 5},
        ]
        stats = smart_money_stats(rows, ("smart_trader",))
        self.assertEqual(stats.tagged_wallets, 2)
        self.assertEqual(stats.tagged_buy_volume_usd, 70)
        self.assertEqual(stats.tagged_sell_volume_usd, 40)

    def test_none_volumes_fall_through(self):
        rows = [{"tags": ["smart_trader"], "volumeBuyUSD": None, "volume_buy_usd": 25,
                 "volumeSellUSD": None, "volume_sell_usd": 5}]
        stats = smart_money_stats(rows, ("smart_trader",))
        self.assertEqual(stats.tagged_buy_volume_usd, 25)
        self.assertEqual(stats.tagged_sell_volume_usd, 5)


class EntryAllowedTests(unittest.TestCase):
    def _args(self, **over):
        from birdeye_smart_cvd.models import CVDState, SmartMoneyStats

        kw = {
            "candidate": _cand(),
            "smart": SmartMoneyStats(2, 60.0, 40.0),
            "cvd": CVDState(120.0, 100.0),
            "min_smart_wallets": 2,
            "min_smart_buy_ratio": 0.6,
            "min_cvd_ratio": 1.2,
            "max_price_change_24h": 80.0,
        }
        kw.update(over)
        return kw

    def test_all_gates_pass(self):
        self.assertTrue(entry_allowed(**self._args()))

    def test_each_gate_blocks(self):
        from birdeye_smart_cvd.models import CVDState, SmartMoneyStats

        self.assertFalse(
            entry_allowed(**self._args(smart=SmartMoneyStats(1, 90.0, 10.0)))
        )
        self.assertFalse(
            entry_allowed(**self._args(smart=SmartMoneyStats(2, 30.0, 70.0)))
        )
        self.assertFalse(
            entry_allowed(**self._args(cvd=CVDState(100.0, 100.0)))
        )
        self.assertFalse(
            entry_allowed(**self._args(candidate=_cand(price_change_24h_pct=81.0)))
        )


class NormalizeTradeTests(unittest.TestCase):
    def test_camel_case(self):
        row = {"side": "BUY", "volumeUsd": 10, "blockUnixTime": 123,
               "txHash": "h", "insIndex": 0, "innerInsIndex": 1}
        p = normalize_trade(row)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.trade_id, "h:0:1")
        self.assertEqual((p.timestamp, p.side, p.volume_usd), (123, "buy", 10.0))

    def test_snake_case(self):
        row = {"side": "sell", "volume_usd": 5, "block_unix_time": 200,
               "tx_hash": "s", "ins_index": 2, "inner_ins_index": 3}
        p = normalize_trade(row, "tok")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.trade_id, "s:2:3")

    def test_none_fallback(self):
        row = {"side": "buy", "volumeUsd": None, "volume_usd": 7,
               "blockUnixTime": None, "block_unix_time": 300,
               "txHash": None, "tx_hash": "h2", "insIndex": 1,
               "innerInsIndex": 0}
        p = normalize_trade(row)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual((p.volume_usd, p.timestamp), (7.0, 300))

    def test_null_inner_index_fallback_id(self):
        r1 = {"side": "buy", "volume_usd": 10, "block_unix_time": 500,
              "tx_hash": "h", "ins_index": 2, "inner_ins_index": None}
        r2 = {"side": "sell", "volume_usd": 20, "block_unix_time": 500,
              "tx_hash": "h", "ins_index": 2, "inner_ins_index": None}
        p1 = normalize_trade(r1)
        p2 = normalize_trade(r2)
        self.assertIsNotNone(p1)
        self.assertIsNotNone(p2)
        assert p1 is not None and p2 is not None
        self.assertNotEqual(p1.trade_id, p2.trade_id)

    def test_rejects_bad_rows(self):
        self.assertIsNone(normalize_trade({"side": "add", "volume_usd": 1,
                                           "block_unix_time": 1, "tx_hash": "x"}))
        self.assertIsNone(normalize_trade({"side": "buy", "block_unix_time": 1,
                                           "tx_hash": "x"}))
        self.assertIsNone(normalize_trade({"side": "buy", "volume_usd": 1,
                                           "block_unix_time": 1}))


class TradePriceTests(unittest.TestCase):
    ADDR = "TokAddr123"

    def test_prefers_matching_leg(self):
        row = {"from": {"address": self.ADDR, "price": 0.5},
               "to": {"address": "So111", "price": 100.0}}
        self.assertEqual(extract_trade_price(row, self.ADDR), 0.5)

    def test_first_leg_without_address_hint(self):
        row = {"from": {"address": "X", "price": 0.25},
               "to": {"address": "Y", "price": 50.0}}
        self.assertEqual(extract_trade_price(row), 0.25)

    def test_latest_trade_price_picks_newest(self):
        trades = [
            TradePoint("a", 100, "buy", 10.0, 1.0),
            TradePoint("b", 200, "buy", 10.0, 2.0),
            TradePoint("c", 300, "buy", 10.0, None),
        ]
        price, stamp = latest_trade_price(trades)
        self.assertEqual((price, stamp), (2.0, 200))

    def test_latest_trade_price_none_when_missing(self):
        price, stamp = latest_trade_price([TradePoint("a", 1, "buy", 1.0)])
        self.assertIsNone(price)
        self.assertIsNone(stamp)


class MarketCapTests(unittest.TestCase):
    def test_marketcap_priority_then_fdv(self):
        v, s = parse_market_cap({"marketCap": 100, "fdv": 999})
        self.assertEqual((v, s), (100, "marketCap"))
        v, s = parse_market_cap({"marketcap": 55})
        self.assertEqual((v, s), (55, "marketcap"))
        v, s = parse_market_cap({"marketCap": 0, "fdv": 482000})
        self.assertEqual((v, s), (482000, "fdv"))
        v, s = parse_market_cap({"FDV": 7})
        self.assertEqual((v, s), (7, "FDV"))
        v, s = parse_market_cap({})
        self.assertEqual((v, s), (0.0, ""))


class CreationUnixTests(unittest.TestCase):
    def test_direct_fields(self):
        self.assertEqual(parse_creation_unix({"blockUnixTime": 1697044029}), 1697044029)
        self.assertEqual(parse_creation_unix({"block_unix_time": 100}), 100)

    def test_iso_liquidity_added_at(self):
        stamp = parse_creation_unix({"liquidityAddedAt": "2024-09-18T17:59:23"})
        self.assertIsInstance(stamp, int)
        self.assertGreater(stamp, 0)

    def test_missing(self):
        self.assertIsNone(parse_creation_unix({}))


if __name__ == "__main__":
    unittest.main()
