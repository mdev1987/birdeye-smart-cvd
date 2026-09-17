"""Unit tests for HTTP clients (httpx MockTransport, no network)."""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from birdeye_smart_cvd.birdeye import BirdeyeClient  # noqa: E402
from birdeye_smart_cvd.cabalspy import CabalSpyClient  # noqa: E402
from birdeye_smart_cvd.dexscreener import DexScreenerClient  # noqa: E402
from birdeye_smart_cvd.jupiter import JupiterClient  # noqa: E402
from birdeye_smart_cvd.ratelimit import AsyncRateLimiter  # noqa: E402
from birdeye_smart_cvd.rugcheck import RugCheckClient  # noqa: E402


def _swap_transport(client, handler):
    """Point an existing client at a mock transport (white-box)."""
    base = str(client._client.base_url).rstrip("/")
    client._client = httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(handler)
    )


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_enforces_gap(self):
        limiter = AsyncRateLimiter(0.05)
        async with limiter:
            await limiter.pace()
            start = time.monotonic()
            limiter.mark()
            await limiter.pace()
            self.assertGreaterEqual(time.monotonic() - start, 0.04)

    async def test_zero_interval_no_wait(self):
        limiter = AsyncRateLimiter(0)
        async with limiter:
            await limiter.pace()
            limiter.mark()


class BirdeyeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_success_and_429_retry(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"success": True, "data": {"a": 1}})

        client = BirdeyeClient("http://x", "k", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            payload = await client.get("/p", {})
        finally:
            await client.close()
        self.assertEqual(payload, {"success": True, "data": {"a": 1}})
        self.assertEqual(len(calls), 2)

    async def test_get_raises_on_api_error(self):
        def handler(request):
            return httpx.Response(200, json={"success": False, "x": 1})

        client = BirdeyeClient("http://x", "k", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            with self.assertRaises(RuntimeError):
                await client.get("/p", {})
        finally:
            await client.close()


class DexScreenerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_list_and_404(self):
        def handler(request):
            if "MINT" in str(request.url):
                return httpx.Response(200, json=[{"pairCreatedAt": 5}, "junk"])
            return httpx.Response(404)

        client = DexScreenerClient("http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            self.assertEqual(await client.token_pairs("MINT"), [{"pairCreatedAt": 5}])
            self.assertEqual(await client.token_pairs("NOPE"), [])
        finally:
            await client.close()


class JupiterClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_mint_returns_empty(self):
        def handler(request):
            return httpx.Response(200, json={"OTHER": {"usdPrice": 1}})

        client = JupiterClient("http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            self.assertEqual(await client.price("MINT"), {})
        finally:
            await client.close()


class RugCheckClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_404_raises(self):
        def handler(request):
            return httpx.Response(404)

        client = RugCheckClient("http://x", min_request_interval=0)
        _swap_transport(client, handler)
        try:
            with self.assertRaises(RuntimeError):
                await client.summary("MINT")
        finally:
            await client.close()


class CabalSpyEnvelopeTests(unittest.TestCase):
    def test_signal_envelopes(self):
        items = [{"mint": "A"}]
        self.assertEqual(CabalSpyClient._signal_items(items), items)
        self.assertEqual(
            CabalSpyClient._signal_items({"data": items}), items
        )
        self.assertEqual(
            CabalSpyClient._signal_items({"data": {"signals": items}}), items
        )
        self.assertEqual(CabalSpyClient._signal_items({"data": {}}), [])
        self.assertEqual(CabalSpyClient._signal_items(None), [])


class KeyRedactionTests(unittest.TestCase):
    def test_helius_redact(self):
        from birdeye_smart_cvd.helius import _redact as h_redact

        scrubbed = h_redact("get https://x/?api-key=SECRET123 failed")
        self.assertNotIn("SECRET123", scrubbed)
        self.assertIn("api-key=***", scrubbed)

    def test_cabalspy_redact(self):
        from birdeye_smart_cvd.cabalspy import _redact as c_redact

        scrubbed = c_redact("get https://x/?a=1&api_key=SECRET123 failed")
        self.assertNotIn("SECRET123", scrubbed)
        self.assertIn("api_key=***", scrubbed)


if __name__ == "__main__":
    unittest.main()
