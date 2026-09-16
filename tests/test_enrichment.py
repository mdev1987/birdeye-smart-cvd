"""Unit tests for multi-source enrichment helpers (no network)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from birdeye_smart_cvd.strategy import (  # noqa: E402
    enrichment_from_pair,
    jupiter_age_unix,
    jupiter_price_usd,
    pair_age_info,
    parse_cluster_for_token,
    parse_iso_unix,
    parse_jupiter_entry,
    rugcheck_verdict,
    select_best_pair,
)

SOL = "So11111111111111111111111111111111111111112"
TOK = "TokAddr123456789"


def _pair(created_ms=None, liq=1000.0, quote=SOL, base=TOK, price=0.5,
          mcap=100000.0, change=5.0, symbol="T"):
    p = {
        "baseToken": {"address": base, "symbol": symbol},
        "quoteToken": {"address": quote, "symbol": "SOL" if quote == SOL else "X"},
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "marketCap": mcap,
        "priceChange": {"h24": change},
    }
    if created_ms is not None:
        p["pairCreatedAt"] = created_ms
    return p


class IsoUnixTests(unittest.TestCase):
    def test_zulu(self):
        self.assertEqual(parse_iso_unix("2026-09-03T11:08:06Z"), 1788433686)

    def test_offset_and_naive(self):
        self.assertEqual(
            parse_iso_unix("2026-09-03T11:08:06+00:00"),
            parse_iso_unix("2026-09-03T11:08:06Z"),
        )
        self.assertEqual(
            parse_iso_unix("2026-09-03T11:08:06"),
            parse_iso_unix("2026-09-03T11:08:06Z"),
        )

    def test_passthrough_and_garbage(self):
        self.assertEqual(parse_iso_unix(1697044029), 1697044029)
        self.assertIsNone(parse_iso_unix(None))
        self.assertIsNone(parse_iso_unix(""))
        self.assertIsNone(parse_iso_unix("not-a-date"))
        self.assertIsNone(parse_iso_unix(-5))


class PairAgeTests(unittest.TestCase):
    def test_minimum_ms_and_seconds(self):
        pairs = [_pair(created_ms=1788452170000), _pair(created_ms=1789403449)]
        oldest, count = pair_age_info(pairs)
        self.assertEqual(oldest, 1788452170)
        self.assertEqual(count, 2)

    def test_ignores_missing_and_junk(self):
        pairs = [_pair(created_ms=None), {"nope": 1}, _pair(created_ms="junk")]
        oldest, count = pair_age_info(pairs)
        self.assertIsNone(oldest)
        self.assertEqual(count, 3)

    def test_empty(self):
        self.assertEqual(pair_age_info([]), (None, 0))


class BestPairTests(unittest.TestCase):
    def test_prefers_base_anchor_liquid(self):
        dust_quote = _pair(created_ms=1, liq=5.0, base="OTHER", quote=TOK)
        anchor = _pair(created_ms=2, liq=500.0)
        thin_anchor = _pair(created_ms=3, liq=10.0)
        best = select_best_pair([dust_quote, thin_anchor, anchor], TOK)
        self.assertIs(best, anchor)

    def test_no_match_returns_none(self):
        self.assertIsNone(select_best_pair([_pair(base="OTHER", quote="ELSE")], TOK))

    def test_without_address_hint_picks_liquid(self):
        best = select_best_pair([_pair(liq=1.0), _pair(liq=9.0)])
        self.assertEqual(best["liquidity"]["usd"], 9.0)


class EnrichmentFillTests(unittest.TestCase):
    def test_extracts_fields(self):
        enr = enrichment_from_pair(_pair())
        self.assertEqual(enr["price_usd"], 0.5)
        self.assertEqual(enr["liquidity_usd"], 1000.0)
        self.assertEqual(enr["market_cap_usd"], 100000.0)
        self.assertEqual(enr["market_cap_source"], "dex:marketCap")
        self.assertEqual(enr["price_change_24h_pct"], 5.0)

    def test_fdv_source_and_missing(self):
        p = _pair()
        del p["marketCap"]
        p["fdv"] = 777.0
        enr = enrichment_from_pair(p)
        self.assertEqual((enr["market_cap_usd"], enr["market_cap_source"]), (777.0, "dex:fdv"))
        enr = enrichment_from_pair({})
        self.assertEqual(enr["price_usd"], None)
        self.assertEqual(enr["market_cap_source"], "")


class JupiterParseTests(unittest.TestCase):
    ENTRY = {
        "createdAt": "2026-09-03T11:08:06Z",
        "liquidity": 386192.75,
        "usdPrice": 0.002837,
        "decimals": 6,
        "priceChange24h": -16.2,
    }

    def test_entry_age_price(self):
        payload = {TOK: self.ENTRY}
        entry = parse_jupiter_entry(payload, TOK)
        self.assertEqual(entry, self.ENTRY)
        self.assertEqual(jupiter_age_unix(entry), 1788433686)
        self.assertAlmostEqual(jupiter_price_usd(entry), 0.002837)

    def test_unknown_mint(self):
        entry = parse_jupiter_entry({}, TOK)
        self.assertEqual(entry, {})
        self.assertIsNone(jupiter_age_unix(entry))
        self.assertIsNone(jupiter_price_usd(entry))


class RugCheckVerdictTests(unittest.TestCase):
    def test_clean_passes(self):
        allowed, reasons, score = rugcheck_verdict(
            {"risks": [], "score_normalised": 1}, max_score=50, reject_danger=True
        )
        self.assertEqual((allowed, reasons, score), (True, [], 1))

    def test_warn_does_not_block(self):
        summary = {"risks": [{"name": "Low Liquidity", "level": "warn", "score": 771}],
                   "score_normalised": 24}
        allowed, _, score = rugcheck_verdict(summary, max_score=50, reject_danger=True)
        self.assertTrue(allowed)
        self.assertEqual(score, 24)

    def test_danger_blocks_when_required(self):
        summary = {"risks": [{"name": "Mint Authority enabled", "level": "danger"}],
                   "score_normalised": 10}
        allowed, reasons, _ = rugcheck_verdict(summary, max_score=50, reject_danger=True)
        self.assertFalse(allowed)
        self.assertTrue(any("Mint Authority" in r for r in reasons))

    def test_danger_allowed_when_not_rejected(self):
        summary = {"risks": [{"name": "X", "level": "DANGER"}], "score_normalised": 10}
        allowed, _, _ = rugcheck_verdict(summary, max_score=50, reject_danger=False)
        self.assertTrue(allowed)

    def test_high_score_blocks(self):
        allowed, reasons, score = rugcheck_verdict(
            {"risks": [], "score_normalised": 87}, max_score=50, reject_danger=True
        )
        self.assertFalse(allowed)
        self.assertIn("score 87 > max 50", reasons)
        self.assertEqual(score, 87)

    def test_missing_score_is_fail_open(self):
        allowed, _, score = rugcheck_verdict({"risks": []}, max_score=50, reject_danger=True)
        self.assertTrue(allowed)
        self.assertIsNone(score)


class ClusterParseTests(unittest.TestCase):
    def test_mint_key_variants(self):
        for signal in (
            {"mint": TOK, "wallets": 4},
            {"tokenAddress": TOK, "walletCount": 5},
            {"address": TOK, "count": 2},
        ):
            self.assertEqual(parse_cluster_for_token([signal], TOK), signal.get("wallets", signal.get("walletCount", signal.get("count"))))

    def test_case_insensitive_and_no_match(self):
        self.assertEqual(
            parse_cluster_for_token([{"mint": TOK.lower(), "wallets": [1, 2, 3]}], TOK), 3
        )
        self.assertIsNone(parse_cluster_for_token([{"mint": "OTHER", "wallets": 9}], TOK))
        self.assertIsNone(parse_cluster_for_token([], TOK))
        self.assertIsNone(parse_cluster_for_token([{"wallets": 9}], TOK))
        self.assertIsNone(parse_cluster_for_token([{"mint": TOK}], TOK))

    def test_envelope_shapes(self):
        # parse helper takes the list; envelopes are handled client-side,
        # but nested cluster objects resolve too.
        self.assertEqual(
            parse_cluster_for_token([{"mint": TOK, "cluster": {"wallets": 6}}], TOK), 6
        )


if __name__ == "__main__":
    unittest.main()
