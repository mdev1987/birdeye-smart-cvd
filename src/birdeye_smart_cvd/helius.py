"""Helius client: holder concentration + on-chain wallet-buy verification.

Uses standard JSON-RPC plus Helius-native ``getTransfersByAddress``
(wallet owner + mint + direction filter, server-side). Key-metered but
generous; every failure raises HeliusError so callers degrade gracefully.
"""

from __future__ import annotations

from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


def _redact(text: str) -> str:
    """Scrub an embedded Helius key so errors are safe for daemon logs."""
    import re

    return re.sub(r"api-key=[^&\s'\"]*", "api-key=***", text)


class HeliusError(RuntimeError):
    """Raised when Helius returns an error or unusable payload."""


class HeliusClient:
    """Minimal async Helius client for the scanner's two read paths.

    - ``getTokenLargestAccounts`` / ``getTokenSupply``: holder
      concentration at discovery (2 cheap standard RPC calls).
    - ``getTransfersByAddress``: inbound transfers of a mint to a wallet,
      i.e. on-chain proof a tagged smart wallet actually bought the
      token (bounded: tagged wallets only, cached per session).
    """

    def __init__(
        self,
        api_key: str,
        rpc_url: str = "https://mainnet.helius-rpc.com",
        *,
        min_request_interval: float = 0.3,
        timeout: float = 15.0,
    ) -> None:
        base = rpc_url.rstrip("/")
        if "api-key" not in base and api_key:
            base = f"{base}/?api-key={api_key}"
        self._client = httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(timeout),
            headers={"accept": "application/json", "content-type": "application/json"},
        )
        self._limiter = AsyncRateLimiter(min_request_interval)
        self._verify_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        # Mint decimals never change: cache successes forever to avoid
        # re-paying an RPC call per signal on the same token.
        self._decimals_cache: dict[str, int] = {}

    async def __aenter__(self) -> "HeliusClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def rpc(self, method: str, params: Any) -> Any:
        """Perform one paced JSON-RPC call, returning ``result``."""
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await self._client.post("", json=body)
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise HeliusError(f"network error: {_redact(str(exc))}") from exc
            self._limiter.mark()

            if response.status_code == 401:
                raise HeliusError("HTTP 401: Helius API key missing/invalid")
            if response.status_code == 403:
                raise HeliusError("HTTP 403: Helius key forbidden or out of credits")
            if response.status_code == 429:
                raise HeliusError("HTTP 429: Helius rate limit exceeded")
            if response.status_code >= 400:
                raise HeliusError(
                    f"HTTP {response.status_code}: {_redact(response.text[:300])}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise HeliusError("Helius returned invalid JSON") from exc

            if not isinstance(payload, dict):
                raise HeliusError(f"unexpected Helius payload: {type(payload)}")
            if payload.get("error"):
                raise HeliusError(f"Helius RPC error: {_redact(str(payload['error']))}")
            return payload.get("result")

    async def token_decimals(self, mint: str, commitment: str = "confirmed") -> int | None:
        """Return a mint's decimals via parsed getAccountInfo, cached forever."""
        cached = self._decimals_cache.get(mint)
        if cached is not None:
            return cached
        result = await self.rpc(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed", "commitment": commitment}],
        )
        decimals: int | None = None
        if isinstance(result, dict):
            value = result.get("value", {})
            data = value.get("data", {}) if isinstance(value, dict) else {}
            parsed = data.get("parsed", {}) if isinstance(data, dict) else {}
            info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
            raw = info.get("decimals") if isinstance(info, dict) else None
            if isinstance(raw, int) and 0 <= raw <= 18:
                decimals = raw
        if decimals is not None:
            self._decimals_cache[mint] = decimals
        return decimals

    async def largest_accounts(self, mint: str, commitment: str = "confirmed") -> list[dict[str, Any]]:
        """Return largest token accounts for a mint (may be empty)."""
        result = await self.rpc(
            "getTokenLargestAccounts",
            [mint, {"commitment": commitment}],
        )
        accounts = (result or {}).get("value", []) if isinstance(result, dict) else []
        return [a for a in accounts if isinstance(a, dict)]

    async def token_supply(self, mint: str, commitment: str = "confirmed") -> float | None:
        """Return UI-adjusted total supply for a mint, if reported."""
        result = await self.rpc(
            "getTokenSupply", [mint, {"commitment": commitment}]
        )
        if not isinstance(result, dict):
            return None
        value = result.get("value", {})
        if not isinstance(value, dict):
            return None
        for key in ("uiAmount", "uiAmountString"):
            raw = value.get(key)
            if raw is None:
                continue
            try:
                supply = float(raw)
            except (TypeError, ValueError):
                continue
            if supply > 0:
                return supply
        return None

    async def inbound_transfers(
        self,
        wallet: str,
        mint: str,
        *,
        limit: int = 10,
        commitment: str = "confirmed",
        since_unix: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent inbound transfers of ``mint`` to ``wallet``.

        Results are cached per (wallet, mint) for the session: buys only
        accumulate, so a cached hit stays valid as advisory confirmation.
        When ``since_unix`` is given it is pushed server-side as
        ``filters.blockTime.gte`` (per Helius docs) so stale rows never
        cross the wire; callers still filter client-side as a backstop.
        """
        cache_key = (wallet, mint)
        cached = self._verify_cache.get(cache_key)
        if cached is not None:
            return cached
        options: dict[str, Any] = {
            "address": wallet,
            "mint": mint,
            "direction": "in",
            "limit": min(max(limit, 1), 100),
            "commitment": commitment,
            "sortOrder": "desc",
        }
        if since_unix is not None and since_unix > 0:
            options["filters"] = {"blockTime": {"gte": int(since_unix)}}
        result = await self.rpc("getTransfersByAddress", options)
        items: list[dict[str, Any]] = []
        if isinstance(result, dict):
            data = result.get("data", result.get("transfers", []))
            items = [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        elif isinstance(result, list):
            items = [t for t in result if isinstance(t, dict)]
        self._verify_cache[cache_key] = items
        return items
