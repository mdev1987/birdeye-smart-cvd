"""Small asynchronous Birdeye client with Standard-tier rate limiting."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


class BirdeyeError(RuntimeError):
    """Raised when Birdeye returns an unsuccessful API response."""


def is_cu_exhausted(exc: BaseException) -> bool:
    """True when an error means the API key is out of compute units.

    Birdeye answers quota exhaustion with HTTP 400
    ``{"success":false,"message":"Compute units usage limit exceeded"}``,
    which surfaces here as ``HTTP 400: ...``. Matching is substring-based
    so client-side wrapping never hides it.
    """
    return "compute units usage limit exceeded" in str(exc).lower()


class BirdeyeClient:
    """Async HTTP client with authentication and a strict request rate limit.

    Birdeye's free Standard package is documented at 1 request per second.
    The client therefore serializes all requests (including 429 backoff
    sleeps) behind a single lock and spaces them by ``min_request_interval``.
    Spacing is anchored at request *completion*: after each response the
    timestamp resets, so the server always sees a full quiet gap even when
    responses themselves are slow. A short retry is also used for HTTP 429.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        chain: str = "solana",
        *,
        min_request_interval: float = 1.5,
        max_429_retries: int = 2,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0),
            headers={
                "X-API-KEY": api_key,
                "x-chain": chain,
                "accept": "application/json",
            },
        )
        self._min_request_interval = max(1.0, min_request_interval)
        self._max_429_retries = max(0, max_429_retries)
        self._limiter = AsyncRateLimiter(self._min_request_interval)

    async def __aenter__(self) -> "BirdeyeClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Perform a rate-limited GET request with limited 429 retries."""
        # The whole attempt loop holds the lock so request spacing and
        # 429 backoff stay globally serialized even under concurrent use.
        async with self._limiter:
            for attempt in range(self._max_429_retries + 1):
                await self._limiter.pace()

                try:
                    response = await self._client.get(path, params=params)
                except httpx.HTTPError as exc:
                    self._limiter.mark()
                    raise BirdeyeError(f"network error: {exc}") from exc
                self._limiter.mark()

                if response.status_code == 429:
                    if attempt >= self._max_429_retries:
                        raise BirdeyeError("HTTP 429: Birdeye rate limit exceeded")

                    # Prefer the server-provided backoff when available. The
                    # client still keeps its global 1+ RPS spacing for the retry.
                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_seconds = float(retry_after) if retry_after else self._min_request_interval
                    except ValueError:
                        retry_seconds = self._min_request_interval

                    await asyncio.sleep(max(self._min_request_interval, retry_seconds))
                    continue

                if response.status_code >= 400:
                    raise BirdeyeError(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise BirdeyeError("Birdeye returned invalid JSON") from exc

                if not payload.get("success", True):
                    raise BirdeyeError(f"Birdeye returned success=false: {payload}")
                return payload

        raise BirdeyeError("request failed unexpectedly")

    @staticmethod
    def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract list-style ``data`` responses defensively."""
        data = payload.get("data", [])
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("items", data.get("tokens", []))
            return items if isinstance(items, list) else []
        return []

    async def trending(
        self,
        limit: int,
        *,
        offset: int = 0,
        sort_by: str = "rank",
        interval: str = "1h",
    ) -> list[dict[str, Any]]:
        """Return one page of the ranked Solana trending-token list.

        The ranking holds ~1000 tokens (see ``total``); small caps live
        deeper than offset 0, so callers paginate. Cost is 25 CU per
        request regardless of ``limit`` (max 50) — deep pages are the
        cheapest depth available. Rows natively carry marketcap/fdv,
        liquidity and 24h change for pre-filtering before any overview.
        """
        payload = await self.get(
            "/defi/token_trending",
            {
                "sort_by": sort_by,
                "sort_type": "asc",
                "interval": interval,
                "limit": min(max(limit, 1), 50),
                "offset": max(offset, 0),
            },
        )
        return self._items(payload)

    async def token_overview(self, address: str) -> dict[str, Any]:
        """Return current token price, liquidity and market metrics."""
        payload = await self.get(
            "/defi/token_overview",
            {"address": address, "frames": "1h,24h"},
        )
        return payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

    async def top_traders(self, address: str, limit: int) -> list[dict[str, Any]]:
        """Return the most active traders for a token."""
        payload = await self.get(
            "/defi/v2/tokens/top_traders",
            {
                "address": address,
                "time_frame": "24h",
                "sort_by": "volume",
                "sort_type": "desc",
                "limit": min(limit, 10),
            },
        )
        return self._items(payload)

    async def token_creation_info(self, address: str) -> dict[str, Any]:
        """Return mint-creation context (blockUnixTime) for a token.

        Note: Birdeye documents this endpoint for Lite/Starter and above,
        not the free Standard package, so callers must tolerate HTTP
        401/403 and treat age as unknown in that case.
        """
        payload = await self.get(
            "/defi/token_creation_info",
            {"address": address},
        )
        return payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}

    async def new_listing(
        self,
        limit: int = 20,
        *,
        meme_platform_enabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recently listed tokens (Standard tier, 20 CU/request).

        Per the spec this is fresh-launch monitoring: rows carry address,
        symbol/decimals, venue ``source``, ISO ``liquidityAddedAt`` and
        ``liquidity`` — but no marketcap or momentum, so callers pre-gate
        on liquidity + listing age and let the overview stage judge size.
        ``meme_platform_enabled`` adds pump.fun-style venues (Solana only).
        """
        params: dict[str, Any] = {"limit": min(max(limit, 1), 20)}
        if meme_platform_enabled:
            params["meme_platform_enabled"] = True
        payload = await self.get("/defi/v2/tokens/new_listing", params)
        return self._items(payload)

    async def token_trades(
        self,
        address: str,
        *,
        after_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return recent token swaps, newest first.

        The V3 endpoint only supports ``sort_type=desc``; incremental
        polling is done by passing ``after_time`` to the server and then
        filtering/deduping client-side in the rolling CVD window.
        """
        params: dict[str, Any] = {
            "address": address,
            "offset": 0,
            "limit": min(limit, 100),
            "sort_by": "block_unix_time",
            "sort_type": "desc",
            "tx_type": "swap",
        }
        if after_time is not None:
            params["after_time"] = after_time

        payload = await self.get("/defi/v3/token/txs", params)
        return self._items(payload)
