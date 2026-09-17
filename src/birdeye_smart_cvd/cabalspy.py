"""Optional CabalSpy client: cluster confirmation for paper entries.

Disabled unless ``CABALSPY_API_KEY`` is set. Even when enabled it is
advisory by default (``CABALSPY_REQUIRE_CLUSTER=false``): cluster info is
logged alongside signals but never blocks an entry unless explicitly
required, so a CabalSpy outage can never silence the scanner.
"""

from __future__ import annotations

from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


def _redact(text: str) -> str:
    """Scrub the query-string API key so errors are safe for daemon logs."""
    import re

    return re.sub(r"api_key=[^&\s'\"]*", "api_key=***", text)


class CabalSpyError(RuntimeError):
    """Raised when CabalSpy returns an error or unusable payload."""


class CabalSpyClient:
    """Minimal async client for two verified CabalSpy REST endpoints.

    - ``GET /v1/signals`` (cluster snapshot): ``blockchain``, ``type``,
      ``mode=cluster``, ``min_wallets``, ``hours``, ``api_key``.
    - ``GET /v1/wallets/lookup``: ``address``, ``api_key`` → ``data``
      with ``found``, ``name``, ``type``, ``blockchain``.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.cabalspy.xyz",
        *,
        min_request_interval: float = 1.0,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers={"accept": "application/json"},
        )
        self._limiter = AsyncRateLimiter(min_request_interval)
        self._lookup_cache: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "CabalSpyClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        """Perform one paced GET with the API key attached."""
        query = dict(params)
        query["api_key"] = self._api_key
        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await self._client.get(path, params=query)
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise CabalSpyError(f"network error: {_redact(str(exc))}") from exc
            self._limiter.mark()

            if response.status_code in (401, 403):
                raise CabalSpyError(
                    f"HTTP {response.status_code}: key missing/invalid or credits exhausted"
                )
            if response.status_code >= 400:
                raise CabalSpyError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise CabalSpyError("CabalSpy returned invalid JSON") from exc

    @staticmethod
    def _signal_items(payload: Any) -> list[Any]:
        """Extract the signal list from several plausible envelopes."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data", [])
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("signals", "items", "clusters"):
                    items = data.get(key, [])
                    if isinstance(items, list):
                        return items
        return []

    async def cluster_signals(
        self,
        *,
        wallet_type: str = "kol",
        min_wallets: int = 3,
        hours: int = 6,
        limit: int = 50,
    ) -> list[Any]:
        """Return the current cluster snapshot (one request per poll)."""
        payload = await self._get(
            "/v1/signals",
            {
                "blockchain": "solana",
                "type": wallet_type,
                "mode": "cluster",
                "min_wallets": min_wallets,
                "hours": hours,
                "limit": limit,
            },
        )
        return self._signal_items(payload)

    async def lookup_wallet(self, address: str) -> dict[str, Any]:
        """Return the lookup ``data`` dict for one wallet (cached)."""
        cached = self._lookup_cache.get(address)
        if cached is not None:
            return cached
        payload = await self._get("/v1/wallets/lookup", {"address": address})
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        result = data if isinstance(data, dict) else {}
        self._lookup_cache[address] = result
        return result
