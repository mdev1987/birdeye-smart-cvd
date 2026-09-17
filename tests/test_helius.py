"""Unit tests for Helius helpers and client (mocked HTTP, no network)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from birdeye_smart_cvd.helius import HeliusClient  # noqa: E402
from birdeye_smart_cvd.strategy import (  # noqa: E402
    count_wallet_buys,
    tagged_owners,
    top_holder_pct,
)

WALLET = "Wallet1111111111111111111111111111111111111"
MINT = "Mint111111111111111111111111111111111111111"


def _swap_transport(client, handler):
    base = str(client._client.base_url).rstrip("/")
    client._client = httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(handler)
    )


class TopHolderPctTests(unittest.TestCase):
    def test_basic_math(self):
        accounts = [
            {"uiAmount": 500.0},
            {"uiAmountString": "300"},
            {"amount": "100000000", "decimals": 6},
            {"uiAmount": 50.0},
        ]
        top1, top10 = top_holder_pct(accounts, 1000.0)
        self.assertAlmostEqual(top1, 50.0)
        self.assertAlmostEqual(top10, 95.0)

    def test_top_n_window(self):
        accounts = [{"uiAmount": float(v)} for v in (50, 40, 30, 20, 10)]
        _, top2 = top_holder_pct(accounts, 1000.0, top_n=2)
        self.assertAlmostEqual(top2, 9.0)

    def test_unknown_without_supply(self):
        self.assertEqual(top_holder_pct([{"uiAmount": 5}], None), (None, None))
        self.assertEqual(top_holder_pct([{"uiAmount": 5}], 0), (None, None))
        self.assertEqual(top_holder_pct([], 100.0), (None, None))
        self.assertEqual(top_holder_pct([{"nope": 1}], 100.0), (None, None))


class TaggedOwnersTests(unittest.TestCase):
    def test_dedup_and_tag_filter(self):
        rows = [
            {"owner": WALLET, "tags": ["smart_trader"]},
            {"owner": WALLET, "tags": ["smart_trader"]},
            {"owner": "Other1", "tags": ["random"]},
            {"owner": "Other2", "tag": "SMART_TRADER"},
            {"tags": ["smart_trader"]},
        ]
        self.assertEqual(
            tagged_owners(rows, ("smart_trader",)), [WALLET, "Other2"]
        )


class CountWalletBuysTests(unittest.TestCase):
    def _tx(self, to=WALLET, mint=MINT, ts=2000, amount=1.5):
        return {
            "toUserAccount": to,
            "mint": mint,
            "timestamp": ts,
            "tokenAmount": amount,
        }

    def test_counts_and_sums(self):
        txs = [self._tx(), self._tx(amount=2.5), self._tx(to="Else"), self._tx(mint="Other")]
        count, total = count_wallet_buys(txs, WALLET, MINT)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(total, 4.0)

    def test_window_and_case(self):
        txs = [self._tx(ts=100), self._tx(ts=300)]
        self.assertEqual(count_wallet_buys(txs, WALLET, MINT, since_unix=200)[0], 1)
        self.assertEqual(count_wallet_buys(txs, WALLET.lower(), MINT.lower())[0], 2)
        self.assertEqual(count_wallet_buys(["junk", None], WALLET, MINT), (0, 0.0))


class HeliusClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_envelope_and_errors(self):
        def handler(request):
            import json

            body = json.loads(request.content.decode())
            if body["method"] == "getTokenSupply":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1,
                               "result": {"value": {"uiAmountString": "1000"}}}
                )
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32602, "message": "bad"}})

        client = HeliusClient("k", "http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            self.assertEqual(await client.token_supply(MINT), 1000.0)
            with self.assertRaises(RuntimeError):
                await client.largest_accounts(MINT)
        finally:
            await client.close()

    async def test_inbound_transfers_cached(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "result": {"data": [{"a": 1}], "paginationToken": None}})

        client = HeliusClient("k", "http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            first = await client.inbound_transfers(WALLET, MINT)
            second = await client.inbound_transfers(WALLET, MINT)
            self.assertEqual(first, [{"a": 1}])
            self.assertEqual(second, [{"a": 1}])
            self.assertEqual(len(calls), 1)
        finally:
            await client.close()

    async def test_inbound_transfers_since_filter(self):
        import json

        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content.decode()))
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1,
                           "result": {"data": [], "paginationToken": None}})

        client = HeliusClient("k", "http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            await client.inbound_transfers(WALLET, MINT, since_unix=1700000000)
            params = bodies[-1]["params"]
            self.assertEqual(
                params["filters"], {"blockTime": {"gte": 1700000000}}
            )
            client2 = HeliusClient("k", "http://x", min_request_interval=0)
            _swap_transport(client2, handler)
            try:
                await client2.inbound_transfers(WALLET, MINT)
                self.assertNotIn("filters", bodies[-1]["params"])
            finally:
                await client2.close()
        finally:
            await client.close()

    async def test_api_key_appended_unless_present(self):
        client = HeliusClient("k", "https://mainnet.helius-rpc.com", min_request_interval=0)
        try:
            self.assertIn("api-key=k", str(client._client.base_url))
        finally:
            await client.close()
        prekeyed = HeliusClient("k", "https://x.io/?api-key=zzz", min_request_interval=0)
        try:
            self.assertNotIn("api-key=k", str(prekeyed._client.base_url))
        finally:
            await prekeyed.close()


if __name__ == "__main__":
    unittest.main()
