"""Keyless Jupiter Price API client: USD price + token creation time."""

from __future__ import annotations

from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


class JupiterError(RuntimeError):
    """Raised when Jupiter returns an error or unusable payload."""


class JupiterClient:
    """Minimal async client for Jupiter's free lite Price API v3.

    ``GET /price/v3?ids={mint}`` returns per-mint ``usdPrice``,
    ``liquidity``, ``priceChange24h`` and — most valuable here —
    ``createdAt`` (ISO-8601 token creation time), all without a key.
    Enrichment only: every failure raises JupiterError for graceful
    degradation by the caller.
    """

    def __init__(
        self,
        base_url: str = "https://lite-api.jup.ag",
        *,
        min_request_interval: float = 0.5,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers={"accept": "application/json"},
        )
        self._limiter = AsyncRateLimiter(min_request_interval)

    async def __aenter__(self) -> "JupiterClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def price(self, address: str) -> dict[str, Any]:
        """Return the v3 price object for one mint, or {} when unknown."""
        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await self._client.get("/price/v3", params={"ids": address})
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise JupiterError(f"network error: {exc}") from exc
            self._limiter.mark()

            if response.status_code == 404:
                return {}
            if response.status_code >= 400:
                raise JupiterError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise JupiterError("Jupiter returned invalid JSON") from exc

            if not isinstance(payload, dict):
                raise JupiterError(f"unexpected Jupiter payload: {type(payload)}")
            entry = payload.get(address, {})
            return entry if isinstance(entry, dict) else {}
