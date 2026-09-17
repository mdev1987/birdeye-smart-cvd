"""Simulate-only Jupiter execution checks: quote, assemble, sign, simulate.

Pipeline per signal (buy or sell):

```text
/order (taker-less)   -> route exists? outAmount? priceImpactPct?
/order (taker=throwaway) -> assembled v0 transaction (no funds needed to build)
/sign locally            -> throwaway PRIVATE_KEY, machine-local only
simulateTransaction RPC  -> err? unitsConsumed? (Helius/Shyft standard RPC)
```

This module **never broadcasts**: there is intentionally no execute/send
path (guarded by a regression test asserting the methods do not exist).
The throwaway key only signs locally so the simulator accepts the payload;
nothing that moves funds ever leaves the machine. Every failure degrades
to an advisory ``SimResult`` — the paper signal flow never depends on it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from .ratelimit import AsyncRateLimiter

log = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6


class SimError(RuntimeError):
    """Raised when a simulation step cannot proceed (caught by callers)."""


@dataclass(slots=True)
class SimResult:
    """Advisory outcome of one buy/sell execution-feasibility check."""

    side: str  # "buy" | "sell"
    route_ok: bool = False
    sim_ok: bool | None = None  # None = simulation skipped/not attempted
    effective_price_usd: float | None = None
    quoted_out_ui: float | None = None
    impact_pct: float | None = None
    units_consumed: int | None = None
    reason: str = ""

    def note(self, signal_price_usd: float = 0.0) -> str:
        """One-line human summary for logs and Telegram extra notes."""
        if not self.route_ok:
            return f"🧪 Sim {self.side}: NO ROUTE ({self.reason})"
        delta = ""
        if (
            self.effective_price_usd
            and self.effective_price_usd > 0
            and signal_price_usd > 0
        ):
            move = (self.effective_price_usd / signal_price_usd - 1.0) * 100.0
            # For sells a lower fill is the adverse direction; keep the raw
            # signed delta so readers always see fill-vs-signal honestly.
            delta = f" ({move:+.1f}% vs signal)"
        impact = f" impact {self.impact_pct:.2f}%" if self.impact_pct is not None else ""
        if self.sim_ok is None:
            return (
                f"🧪 Sim {self.side}: route OK"
                f" fill ${self.effective_price_usd:.8f}{delta}{impact}"
                f" [{self.reason}]"
            )
        sim = (
            f"sim ok {self.units_consumed or 0} CU"
            if self.sim_ok
            else f"sim FAILED ({self.reason})"
        )
        return (
            f"🧪 Sim {self.side}: route OK"
            f" fill ${self.effective_price_usd:.8f}{delta}{impact} {sim}"
        )


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class JupiterSim:
    """Quote + simulate only. No execute/send method exists on purpose."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.jup.ag",
        private_key_b58: str = "",
        *,
        rpc_simulate: Callable[[str, Any], Awaitable[Any]] | None = None,
        min_request_interval: float = 1.0,
        order_timeout: float = 12.0,
        slippage_bps: int = 300,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"accept": "application/json"}
        if api_key:
            self._headers["x-api-key"] = api_key
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._limiter = AsyncRateLimiter(min_request_interval)
        self._order_timeout = max(2.0, order_timeout)
        self._slippage_bps = slippage_bps
        self._rpc_simulate = rpc_simulate
        self._keypair: Keypair | None = None
        if private_key_b58 and private_key_b58.strip():
            try:
                self._keypair = Keypair.from_base58_string(private_key_b58.strip())
            except Exception:  # noqa: BLE001 - degraded mode, caller logs once
                self._keypair = None

    @property
    def has_key(self) -> bool:
        """Whether a usable throwaway signing key is loaded (never logged)."""
        return self._keypair is not None

    @property
    def taker(self) -> str | None:
        """Throwaway taker pubkey for tx assembly, or None without a key."""
        return str(self._keypair.pubkey()) if self._keypair is not None else None

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _order(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        *,
        taker: str | None = None,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        """GET /swap/v2/order. Raises SimError with a short reason on failure."""
        params: dict[str, str] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
        }
        # Omit slippageBps on buys so Jupiter applies RTSE (ultra routing);
        # sells keep explicit slippage (execution certainty dominates).
        if slippage_bps is not None:
            params["slippageBps"] = str(slippage_bps)
        if taker is not None:
            params["taker"] = taker

        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await asyncio.wait_for(
                    self._client.get(
                        f"{self._base}/swap/v2/order",
                        params=params,
                        headers=self._headers,
                    ),
                    timeout=self._order_timeout,
                )
            except (TimeoutError, httpx.TimeoutException) as exc:
                self._limiter.mark()
                raise SimError(f"order timeout: {exc}") from exc
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise SimError(f"order network error: {exc}") from exc
            self._limiter.mark()

        if response.status_code == 429:
            raise SimError("order rate-limited (429)")
        if response.status_code != 200:
            raise SimError(self._http_reason(response))
        try:
            data = response.json()
        except ValueError as exc:
            raise SimError("order invalid JSON") from exc
        if not isinstance(data, dict):
            raise SimError("order unexpected payload")
        err_text = str(data.get("errorMessage") or data.get("error") or "")
        if err_text and not data.get("outAmount"):
            raise SimError(self._classify_error_text(err_text))
        if not data.get("outAmount"):
            raise SimError("no route: empty outAmount")
        return data

    @staticmethod
    def _classify_error_text(err_text: str) -> str:
        """Map a Jupiter error string to a short stable reason."""
        lowered = err_text.lower()
        if "insufficient funds" in lowered or "insufficientfunds" in lowered:
            return f"unfunded taker: {err_text[:120]}"
        return f"no route: {err_text[:120]}"

    @classmethod
    def _http_reason(cls, response: httpx.Response) -> str:
        """Extract a short reason from a non-200 order response."""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            err_text = str(payload.get("errorMessage") or payload.get("error") or "")
            if err_text:
                return cls._classify_error_text(err_text)
        return f"order HTTP {response.status_code}: {response.text[:160]}"

    def sign(self, tx_b64: str) -> str:
        """Sign an assembled v0 tx with the throwaway key; return base64."""
        if self._keypair is None:
            raise SimError("no signing key (set PRIVATE_KEY)")
        try:
            raw = base64.b64decode(tx_b64)
            tx = VersionedTransaction.from_bytes(raw)
            # v0 message payload: 0x80 version byte + serialized MessageV0.
            versioned_message = b"\x80" + bytes(tx.message)
            signature = self._keypair.sign_message(versioned_message)
            sigs = list(tx.signatures)
            required = tx.message.header.num_required_signatures
            signer_keys = tx.message.account_keys[:required]
            try:
                slot = signer_keys.index(self._keypair.pubkey())
            except ValueError:
                slot = 0
            sigs[slot] = signature
            signed = VersionedTransaction.populate(tx.message, sigs)
            return base64.b64encode(bytes(signed)).decode()
        except SimError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed tx from API
            raise SimError(f"sign failed: {exc}") from exc

    async def simulate(self, signed_b64: str) -> dict[str, Any]:
        """Run standard simulateTransaction RPC. Never broadcasts."""
        if self._rpc_simulate is None:
            return {"ok": False, "reason": "no RPC configured"}
        try:
            result = await self._rpc_simulate(
                "simulateTransaction",
                [
                    signed_b64,
                    {
                        "encoding": "base64",
                        "replaceRecentBlockhash": True,
                        "sigVerify": False,
                    },
                ],
            )
        except Exception as exc:  # noqa: BLE001 - RPC outage, advisory only
            return {"ok": False, "reason": f"rpc error: {exc}"}
        value = result.get("value", {}) if isinstance(result, dict) else {}
        if not isinstance(value, dict):
            return {"ok": False, "reason": "unexpected sim payload"}
        err = value.get("err")
        logs = value.get("logs") or []
        units = value.get("unitsConsumed") or 0
        if err:
            return {"ok": False, "reason": f"sim err: {err}",
                    "logs": logs, "units": units}
        return {"ok": True, "units": units, "logs": logs}

    async def check_buy(
        self, mint: str, usd_notional: float, *, out_decimals: int | None
    ) -> SimResult:
        """Quote + simulate a USDC->token buy of ``usd_notional``."""
        res = SimResult(side="buy")
        if usd_notional <= 0:
            res.reason = "dust size"
            return res
        if out_decimals is None:
            res.reason = "unknown token decimals"
            return res
        amount_raw = int(usd_notional * (10**USDC_DECIMALS))
        if amount_raw <= 0:
            res.reason = "dust size"
            return res
        try:
            # Taker-less quote (RTSE): route + impact, no transaction.
            quote = await self._order(USDC_MINT, mint, amount_raw)
        except SimError as exc:
            res.reason = str(exc)
            return res
        if not self._fill_quote(res, quote, usd_notional, out_decimals, is_buy=True):
            return res
        await self._try_simulate(res, USDC_MINT, mint, amount_raw)
        return res

    async def check_sell(
        self, mint: str, qty_ui: float, token_decimals: int,
        usd_reference: float = 0.0,
    ) -> SimResult:
        """Quote + simulate a token->USDC sell of ``qty_ui`` tokens."""
        res = SimResult(side="sell")
        if qty_ui <= 0:
            res.reason = "dust size"
            return res
        amount_raw = int(qty_ui * (10**token_decimals))
        if amount_raw <= 0:
            res.reason = "dust size"
            return res
        try:
            quote = await self._order(
                mint, USDC_MINT, amount_raw, slippage_bps=self._slippage_bps
            )
        except SimError as exc:
            res.reason = str(exc)
            return res
        if not self._fill_quote(res, quote, usd_reference, USDC_DECIMALS, is_buy=False,
                                qty_ui=qty_ui):
            return res
        await self._try_simulate(res, mint, USDC_MINT, amount_raw,
                                 slippage_bps=self._slippage_bps)
        return res

    def _fill_quote(
        self,
        res: SimResult,
        quote: dict[str, Any],
        usd_reference: float,
        out_decimals: int,
        *,
        is_buy: bool,
        qty_ui: float = 0.0,
    ) -> bool:
        """Fill route fields from a taker-less quote. False when unusable."""
        out_raw = _parse_float(quote.get("outAmount"))
        if not out_raw or out_raw <= 0:
            res.reason = "empty outAmount"
            return False
        out_ui = out_raw / (10**out_decimals)
        if out_ui <= 0:
            res.reason = "empty outAmount"
            return False
        res.route_ok = True
        res.quoted_out_ui = out_ui
        impact = _parse_float(quote.get("priceImpactPct"))
        res.impact_pct = impact
        if is_buy:
            res.effective_price_usd = usd_reference / out_ui if usd_reference > 0 else None
        else:
            res.effective_price_usd = out_ui / qty_ui if qty_ui > 0 else None
        return True

    async def _try_simulate(
        self,
        res: SimResult,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        *,
        slippage_bps: int | None = None,
    ) -> None:
        """Assemble with the throwaway taker, sign, simulate. Advisory only."""
        if self._keypair is None or self.taker is None:
            res.reason = "route only (no PRIVATE_KEY for sim)"
            return
        try:
            order = await self._order(
                input_mint, output_mint, amount_raw,
                taker=self.taker, slippage_bps=slippage_bps,
            )
        except SimError as exc:
            # Empty throwaway wallet: Jupiter refuses assembly but the
            # taker-less route above already answered the trading question.
            res.reason = f"route only ({exc})"
            return
        tx_b64 = order.get("transaction")
        if not isinstance(tx_b64, str) or not tx_b64:
            res.reason = "route only (no assembled tx)"
            return
        if self._rpc_simulate is None:
            res.reason = "route only (no RPC for sim)"
            return
        try:
            signed = self.sign(tx_b64)
        except SimError as exc:
            res.reason = f"route only ({exc})"
            return
        sim = await self.simulate(signed)
        if sim.get("ok"):
            res.sim_ok = True
            res.units_consumed = int(sim.get("units") or 0)
            res.reason = "simulated, not broadcast"
        else:
            res.sim_ok = False
            res.reason = str(sim.get("reason", "sim failed"))
