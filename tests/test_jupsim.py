"""Unit tests for simulate-only Jupiter checks (mocked HTTP/RPC, no network).

A hard rule is enforced here: JupiterSim must never gain an execute/send
path. If someone adds one, test_no_broadcast_surface fails loudly.
"""

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base58  # noqa: E402
import httpx  # noqa: E402
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from birdeye_smart_cvd.jupsim import (  # noqa: E402
    JupiterSim,
    SimError,
    SimResult,
)

MINT = "Mint111111111111111111111111111111111111111"
KP = Keypair.from_seed(bytes([7]) * 32)
KEY_B58 = base58.b58encode(KP.secret() + bytes(KP.pubkey())).decode()


def _unsigned_tx_b64() -> str:
    ix = Instruction(
        Pubkey.new_unique(), b"\x00", [AccountMeta(KP.pubkey(), True, True)]
    )
    msg = MessageV0.try_compile(KP.pubkey(), [ix], [], Hash.new_unique())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


def _sim(handler, **kw):
    sim = JupiterSim("", "http://jup", KEY_B58, **kw)
    base = str(sim._client.base_url).rstrip("/")
    sim._client = httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(handler)
    )
    return sim


def _order_response(**over):
    body = {"outAmount": "2000000", "priceImpactPct": "0.5"}
    body.update(over)
    return body


class BuyQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_math_and_unfunded_taker_fallback(self):
        calls = []

        def handler(request):
            params = dict(request.url.params)
            calls.append(params)
            if "taker" in params:
                return httpx.Response(400, json={"error": "Insufficient funds for tx"})
            return httpx.Response(200, json=_order_response())

        sim = _sim(handler)
        try:
            res = await sim.check_buy(MINT, 100.0, out_decimals=6)
            self.assertTrue(res.route_ok)
            self.assertAlmostEqual(res.quoted_out_ui, 2.0)
            self.assertAlmostEqual(res.effective_price_usd, 50.0)
            self.assertAlmostEqual(res.impact_pct, 0.5)
            # Taker-less quote + with-taker assembly attempt.
            self.assertEqual(len(calls), 2)
            self.assertNotIn("taker", calls[0])
            # Empty throwaway wallet: route stands, sim skipped honestly.
            self.assertIsNone(res.sim_ok)
            self.assertIn("route only", res.reason)
        finally:
            await sim.close()

    async def test_full_sim_path_signs_and_simulates(self):
        seen = {}
        unsigned = _unsigned_tx_b64()

        def handler(request):
            if "taker" in request.url.params:
                return httpx.Response(
                    200, json=_order_response(transaction=unsigned)
                )
            return httpx.Response(200, json=_order_response())

        async def fake_rpc(method, params):
            self.assertEqual(method, "simulateTransaction")
            seen["payload"] = params[0]
            self.assertEqual(params[1]["encoding"], "base64")
            self.assertFalse(params[1]["sigVerify"])
            return {"value": {"err": None, "unitsConsumed": 41000, "logs": []}}

        sim = _sim(handler, rpc_simulate=fake_rpc)
        try:
            res = await sim.check_buy(MINT, 100.0, out_decimals=6)
            self.assertTrue(res.route_ok)
            self.assertTrue(res.sim_ok)
            self.assertEqual(res.units_consumed, 41000)
            # The simulated payload is really signed in our slot.
            signed = VersionedTransaction.from_bytes(
                base64.b64decode(seen["payload"])
            )
            self.assertNotEqual(bytes(signed.signatures[0]), bytes(Signature.default()))
            KP.pubkey()  # sanity: module keypair intact
            signed.verify_with_results()
        finally:
            await sim.close()

    async def test_no_route_is_fail_soft(self):
        def handler(request):
            return httpx.Response(400, json={"error": "Failed to get quotes"})

        sim = _sim(handler)
        try:
            res = await sim.check_buy(MINT, 100.0, out_decimals=6)
            self.assertFalse(res.route_ok)
            self.assertIn("no route", res.reason)
            self.assertIsNone(res.sim_ok)
        finally:
            await sim.close()

    async def test_no_key_gives_route_only(self):
        def handler(request):
            return httpx.Response(200, json=_order_response())

        sim = JupiterSim("", "http://jup", "")
        sim._client = httpx.AsyncClient(
            base_url="http://jup", transport=httpx.MockTransport(handler)
        )
        try:
            self.assertFalse(sim.has_key)
            res = await sim.check_buy(MINT, 100.0, out_decimals=6)
            self.assertTrue(res.route_ok)
            self.assertIsNone(res.sim_ok)
            self.assertIn("no PRIVATE_KEY", res.reason)
        finally:
            await sim.close()

    async def test_dust_and_unknown_decimals(self):
        sim = _sim(lambda request: httpx.Response(200, json=_order_response()))
        try:
            res = await sim.check_buy(MINT, 0.0, out_decimals=6)
            self.assertFalse(res.route_ok)
            res = await sim.check_buy(MINT, 100.0, out_decimals=None)
            self.assertFalse(res.route_ok)
            self.assertIn("decimals", res.reason)
        finally:
            await sim.close()


class SellQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_math(self):
        def handler(request):
            # 5.0 USDC out for 10 tokens -> $0.50 effective.
            return httpx.Response(
                200, json=_order_response(outAmount="5000000", priceImpactPct="1.25")
            )

        sim = _sim(handler)
        try:
            res = await sim.check_sell(MINT, 10.0, 6, usd_reference=0.55)
            self.assertTrue(res.route_ok)
            self.assertAlmostEqual(res.quoted_out_ui, 5.0)
            self.assertAlmostEqual(res.effective_price_usd, 0.5)
            self.assertAlmostEqual(res.impact_pct, 1.25)
        finally:
            await sim.close()


class SignTests(unittest.TestCase):
    def test_sign_without_key(self):
        sim = JupiterSim("", "http://x", "")
        with self.assertRaises(SimError):
            sim.sign("eA==")

    def test_sign_garbage(self):
        sim = JupiterSim("", "http://x", KEY_B58)
        with self.assertRaises(SimError):
            sim.sign("!!!not-base64!!!")


class SimulateTests(unittest.IsolatedAsyncioTestCase):
    async def test_sim_err_shapes(self):
        async def err_rpc(method, params):
            return {"value": {"err": "BlockhashNotFound", "unitsConsumed": 0,
                              "logs": ["log"]}}

        sim = _sim(lambda request: httpx.Response(200, json={}),
                   rpc_simulate=err_rpc)
        try:
            out = await sim.simulate("eA==")
            self.assertFalse(out["ok"])
            self.assertIn("BlockhashNotFound", out["reason"])
        finally:
            await sim.close()

    async def test_sim_rpc_outage(self):
        async def dead_rpc(method, params):
            raise TimeoutError("down")

        sim = _sim(lambda request: httpx.Response(200, json={}),
                   rpc_simulate=dead_rpc)
        try:
            out = await sim.simulate("eA==")
            self.assertFalse(out["ok"])
        finally:
            await sim.close()

    async def test_sim_without_rpc(self):
        sim = _sim(lambda request: httpx.Response(200, json={}))
        try:
            out = await sim.simulate("eA==")
            self.assertFalse(out["ok"])
        finally:
            await sim.close()


class NoteTests(unittest.TestCase):
    def test_notes(self):
        no_route = SimResult(side="buy", reason="no route: empty")
        self.assertIn("NO ROUTE", no_route.note(1.0))
        route_only = SimResult(side="buy", route_ok=True,
                               effective_price_usd=50.0, quoted_out_ui=2.0,
                               impact_pct=0.5, reason="route only (x)")
        text = route_only.note(50.25)
        self.assertIn("route OK", text)
        self.assertIn("vs signal", text)
        ok = SimResult(side="sell", route_ok=True, sim_ok=True,
                       effective_price_usd=0.5, units_consumed=41000)
        self.assertIn("sim ok", ok.note(0.55))
        bad = SimResult(side="sell", route_ok=True, sim_ok=False,
                        effective_price_usd=0.5, reason="sim err: X")
        self.assertIn("FAILED", bad.note(0.55))


class NoBroadcastTests(unittest.TestCase):
    def test_no_execute_or_send_path(self):
        for forbidden in ("execute", "send_transaction", "sendTransaction",
                          "broadcast", "send", "confirm"):
            self.assertFalse(
                hasattr(JupiterSim, forbidden),
                f"JupiterSim must never gain {forbidden} (simulate-only)",
            )


if __name__ == "__main__":
    unittest.main()
