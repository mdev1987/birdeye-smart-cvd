"""Keyless DexScreener client: pool ages, prices and enrichment data."""

from __future__ import annotations

from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


class DexScreenerError(RuntimeError):
    """Raised when DexScreener returns an error or unusable payload."""


class DexScreenerClient:
    """Minimal async client for the free, keyless DexScreener REST API.

    Rate limit is 300 RPM on pairs/tokens endpoints; a small default
    spacing keeps us far below it. Every failure raises DexScreenerError
    so callers can degrade gracefully (this source is enrichment only,
    never on the critical path).
    """

    def __init__(
        self,
        base_url: str = "https://api.dexscreener.com",
        *,
        min_request_interval: float = 0.3,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers={"accept": "application/json"},
        )
        self._limiter = AsyncRateLimiter(min_request_interval)

    async def __aenter__(self) -> "DexScreenerClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def token_pairs(self, address: str) -> list[dict[str, Any]]:
        """Return all known pools for a token on Solana.

        The endpoint returns a bare JSON array of Pair objects
        (``pairCreatedAt`` in Unix ms, ``priceUsd``, ``liquidity.usd``,
        ``fdv``/``marketCap``, ``txns``). A dict envelope with a
        ``pairs`` key is accepted defensively.
        """
        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await self._client.get(f"/token-pairs/v1/solana/{address}")
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise DexScreenerError(f"network error: {exc}") from exc
            self._limiter.mark()

            if response.status_code == 404:
                return []
            if response.status_code >= 400:
                raise DexScreenerError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise DexScreenerError("DexScreener returned invalid JSON") from exc

            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                pairs = payload.get("pairs", [])
                if isinstance(pairs, list):
                    return [item for item in pairs if isinstance(item, dict)]
                return []
            raise DexScreenerError(f"unexpected DexScreener payload: {type(payload)}")
