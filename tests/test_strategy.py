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
        self.assertEqual(state.trade_count, 2)
        self.assertEqual(state.total_volume_usd, 140.0)

    def test_removes_expired_trades(self):
        r = RollingCVD(900)
        r.add([TradePoint("a", 1000, "buy", 100.0)], now=1500)
        state = r.add([], now=2000)  # cutoff 1100, trade at 1000 expires
        self.assertEqual(state.buy_volume_usd, 0.0)
        self.assertEqual(len(r.points), 0)
        self.assertEqual(state.trade_count, 0)
        self.assertEqual(state.total_volume_usd, 0.0)

    def test_partial_expiry_updates_count(self):
        r = RollingCVD(900)
        r.add(
            [
                TradePoint("old", 1000, "buy", 50.0),
                TradePoint("new", 1500, "buy", 1500.0),
                TradePoint("new2", 1600, "sell", 1000.0),
            ],
            now=1600,
        )
        state = r.add([], now=2000)  # cutoff 1100: "old" expires
        self.assertEqual(state.trade_count, 2)
        self.assertEqual(state.total_volume_usd, 2500.0)

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
            # Healthy sample: $2200 volume over 12 trades, ratio 1.2.
            "cvd": CVDState(1200.0, 1000.0, trade_count=12),
            "min_smart_wallets": 2,
            "min_smart_buy_ratio": 0.6,
            "min_cvd_ratio": 1.2,
            "min_cvd_volume_usd": 2000.0,
            "min_cvd_trades": 10,
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
            entry_allowed(**self._args(cvd=CVDState(1000.0, 1000.0, trade_count=12)))
        )
        self.assertFalse(
            entry_allowed(**self._args(candidate=_cand(price_change_24h_pct=81.0)))
        )

    def test_thin_sample_rejected_despite_infinite_ratio(self):
        # "$2 buy / $0 sells": infinite ratio, but no information.
        from birdeye_smart_cvd.models import CVDState

        thin = CVDState(2.0, 0.0, trade_count=1)
        self.assertEqual(thin.buy_sell_ratio, float("inf"))
        self.assertFalse(entry_allowed(**self._args(cvd=thin)))

    def test_low_volume_blocks(self):
        from birdeye_smart_cvd.models import CVDState

        # Good ratio (1.5x) and enough trades, but only $300 volume.
        low_vol = CVDState(180.0, 120.0, trade_count=12)
        self.assertGreaterEqual(low_vol.buy_sell_ratio, 1.2)
        self.assertFalse(entry_allowed(**self._args(cvd=low_vol)))

    def test_few_trades_block(self):
        from birdeye_smart_cvd.models import CVDState

        # Good ratio and volume, but only 3 trades.
        few = CVDState(1500.0, 1000.0, trade_count=3)
        self.assertFalse(entry_allowed(**self._args(cvd=few)))

    def test_boundaries_pass(self):
        from birdeye_smart_cvd.models import CVDState

        exact = CVDState(1200.0, 1000.0, trade_count=10)
        self.assertTrue(entry_allowed(**self._args(cvd=exact)))


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


class TrendingRowTests(unittest.TestCase):
    def _args(self, **over):
        kw = {
            "min_market_cap_usd": 60_000,
            "max_market_cap_usd": 3_000_000,
            "min_liquidity_usd": 10_000,
            "max_price_change_24h": 80.0,
        }
        kw.update(over)
        return kw

    def test_in_band_row_passes(self):
        from birdeye_smart_cvd.strategy import trending_row_passes

        row = {"marketcap": 500_000, "liquidity": 50_000,
               "price24hChangePercent": 12.0}
        self.assertTrue(trending_row_passes(row, **self._args()))

    def test_fdv_fallback(self):
        from birdeye_smart_cvd.strategy import trending_row_passes

        row = {"fdv": 482_000, "liquidity": 20_000,
               "price24hChangePercent": -5.0}
        self.assertTrue(trending_row_passes(row, **self._args()))

    def test_each_gate_blocks(self):
        from birdeye_smart_cvd.strategy import trending_row_passes

        base = {"marketcap": 500_000, "liquidity": 50_000,
                "price24hChangePercent": 5.0}
        self.assertFalse(trending_row_passes(
            {**base, "marketcap": 5_000_000}, **self._args()))
        self.assertFalse(trending_row_passes(
            {**base, "liquidity": 500}, **self._args()))
        self.assertFalse(trending_row_passes(
            {**base, "price24hChangePercent": 81.0}, **self._args()))
        # No marketcap and no fdv: unplaceable in the band.
        self.assertFalse(trending_row_passes(
            {"liquidity": 50_000, "price24hChangePercent": 5.0},
            **self._args()))

    def test_zero_mc_rejected(self):
        from birdeye_smart_cvd.strategy import trending_row_passes

        row = {"marketcap": 0, "fdv": 0, "liquidity": 50_000,
               "price24hChangePercent": 1.0}
        self.assertFalse(trending_row_passes(row, **self._args()))


class StrategyNameTests(unittest.TestCase):
    def test_canonical_name_says_proxy(self):
        from birdeye_smart_cvd.strategy import STRATEGY_NAME

        self.assertIn("Proxy", STRATEGY_NAME)
        self.assertIn("CVD", STRATEGY_NAME)


class ChaseOkTests(unittest.TestCase):
    def test_allows_flat_and_dips(self):
        from birdeye_smart_cvd.strategy import chase_ok

        self.assertTrue(chase_ok(1.0, 1.0, 30.0))
        self.assertTrue(chase_ok(0.9, 1.0, 30.0))

    def test_blocks_breakout_chase(self):
        from birdeye_smart_cvd.strategy import chase_ok

        self.assertFalse(chase_ok(1.43, 1.0, 30.0))

    def test_boundary_passes(self):
        from birdeye_smart_cvd.strategy import chase_ok

        # 1.29-up on 1.0 is safely inside; exact 1.30 flirts with float
        # dust (0.30000000000000004), so the guard stays strict <=.
        self.assertTrue(chase_ok(1.29, 1.0, 30.0))
        self.assertFalse(chase_ok(1.31, 1.0, 30.0))

    def test_zero_disables(self):
        from birdeye_smart_cvd.strategy import chase_ok

        self.assertTrue(chase_ok(5.0, 1.0, 0))

    def test_missing_snapshot_fails_open(self):
        from birdeye_smart_cvd.strategy import chase_ok

        self.assertTrue(chase_ok(1.5, 0.0, 30.0))
        self.assertTrue(chase_ok(0.0, 1.0, 30.0))


def _settings_from_mock_env(**env):
    """Build Settings from a hermetic env (real .env never leaks into tests).

    ``Settings.from_env()`` calls ``load_dotenv()``, which would otherwise
    refill cleared vars from the developer's real .env file.
    """
    import os
    from unittest import mock

    from birdeye_smart_cvd import config as config_module
    from birdeye_smart_cvd.config import Settings

    base = {"BIRDEYE_API_KEY": "test-key"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True):
        with mock.patch.object(config_module, "load_dotenv", lambda *a, **k: False):
            return Settings.from_env()


class ScannerModeTests(unittest.TestCase):
    def _settings(self, **env):
        return _settings_from_mock_env(**env)

    def test_default_is_enriched(self):
        s = self._settings()
        self.assertEqual(s.scanner_mode, "enriched")
        self.assertTrue(s.use_risk_checks)
        self.assertTrue(s.use_enrichment)

    def test_core_disables_risk_and_enrichment(self):
        s = self._settings(SCANNER_MODE="core")
        self.assertFalse(s.use_risk_checks)
        self.assertFalse(s.use_enrichment)

    def test_risk_enables_risk_only(self):
        s = self._settings(SCANNER_MODE="RISK")
        self.assertEqual(s.scanner_mode, "risk")
        self.assertTrue(s.use_risk_checks)
        self.assertFalse(s.use_enrichment)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            self._settings(SCANNER_MODE="full")

    def test_cvd_gates_configurable(self):
        s = self._settings(CVD_MIN_VOLUME_USD="500", CVD_MIN_TRADES="3")
        self.assertEqual(s.cvd_min_volume_usd, 500.0)
        self.assertEqual(s.cvd_min_trades, 3)
        with self.assertRaises(ValueError):
            self._settings(CVD_MIN_TRADES="0")


class LifecycleConfigTests(unittest.TestCase):
    def _settings(self, **env):
        return _settings_from_mock_env(**env)

    def test_ladder_defaults(self):
        s = self._settings()
        self.assertEqual(s.take_profit_percent, 100.0)
        self.assertEqual(s.take_profit2_percent, 200.0)
        self.assertEqual((s.tp1_fraction, s.tp2_fraction), (0.5, 0.6))
        self.assertEqual((s.trail_arm_pct, s.trail_stop_pct), (50.0, 40.0))
        self.assertEqual(s.volume_death_quiet_polls, 6)
        self.assertEqual(s.entry_max_surge_pct, 30.0)

    def test_tp2_must_exceed_tp1(self):
        with self.assertRaises(ValueError):
            self._settings(TAKE_PROFIT2_PCT="40")
        with self.assertRaises(ValueError):
            self._settings(TP1_FRACTION="1.5")
        with self.assertRaises(ValueError):
            self._settings(TP2_FRACTION="0")

    def test_disables_allowed(self):
        s = self._settings(TRAIL_STOP_PCT="0", VOLUME_DEATH_QUIET_POLLS="0",
                           ENTRY_MAX_SURGE_PCT="0")
        self.assertEqual(s.trail_stop_pct, 0)
        self.assertEqual(s.volume_death_quiet_polls, 0)
        self.assertEqual(s.entry_max_surge_pct, 0)


class SimConfigTests(unittest.TestCase):
    def _settings(self, **env):
        return _settings_from_mock_env(**env)

    def test_sim_defaults(self):
        s = self._settings()
        self.assertTrue(s.sim_enabled)
        self.assertEqual(s.jupiter_base_url, "https://api.jup.ag")
        self.assertEqual(s.private_key, "")
        self.assertEqual(s.jupiter_order_timeout_s, 12.0)
        self.assertEqual(s.sim_slippage_bps, 300)
        self.assertEqual(s.sim_max_impact_pct, 5.0)
        self.assertFalse(s.sim_require_route)

    def test_sim_keys_read(self):
        s = self._settings(PRIVATE_KEY="abc123", JUPITER_API_KEY="jup_x",
                           SIM_REQUIRE_ROUTE="true")
        self.assertEqual(s.private_key, "abc123")
        self.assertEqual(s.jupiter_api_key, "jup_x")
        self.assertTrue(s.sim_require_route)

    def test_sim_validation(self):
        with self.assertRaises(ValueError):
            self._settings(SIM_SLIPPAGE_BPS="20000")
        with self.assertRaises(ValueError):
            self._settings(JUPITER_ORDER_TIMEOUT_S="0")


class TrendingConfigTests(unittest.TestCase):
    def _settings(self, **env):
        return _settings_from_mock_env(**env)

    def test_trending_defaults(self):
        s = self._settings()
        self.assertEqual(s.trending_page_size, 50)
        self.assertEqual(s.trending_max_pages, 2)
        self.assertEqual(s.trending_interval, "24h")
        self.assertEqual(s.candidate_limit, 100)

    def test_trending_validation(self):
        with self.assertRaises(ValueError):
            self._settings(TRENDING_PAGE_SIZE="51")
        with self.assertRaises(ValueError):
            self._settings(TRENDING_MAX_PAGES="0")
        with self.assertRaises(ValueError):
            self._settings(TRENDING_INTERVAL="7d")
        s = self._settings(TRENDING_INTERVAL="4H")
        self.assertEqual(s.trending_interval, "4h")


if __name__ == "__main__":
    unittest.main()
