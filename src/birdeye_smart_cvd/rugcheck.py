"""Keyless RugCheck client: token risk summaries as a pre-entry veto."""

from __future__ import annotations

from typing import Any

import httpx

from .ratelimit import AsyncRateLimiter


class RugCheckError(RuntimeError):
    """Raised when RugCheck returns an error or unusable payload."""


class RugCheckClient:
    """Minimal async client for RugCheck's free summary endpoint.

    ``GET /v1/tokens/{mint}/report/summary`` returns ``score_normalised``,
    a ``risks`` list of ``{name, value, description, score, level}`` items
    (``level`` is ``warn``/``danger``) and ``lpLockedPct``. Free tier is
    ~3 RPS on cached reports; our discovery-only usage stays far below.
    A 404 (never scanned) raises RugCheckError so the caller can treat
    the token as risk-unknown rather than risky.
    """

    def __init__(
        self,
        base_url: str = "https://api.rugcheck.xyz",
        *,
        min_request_interval: float = 0.4,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout),
            headers={"accept": "application/json"},
        )
        self._limiter = AsyncRateLimiter(min_request_interval)

    async def __aenter__(self) -> "RugCheckClient":
        """Enter the async client context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Close the underlying HTTP connection pool."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def summary(self, address: str) -> dict[str, Any]:
        """Return the risk summary for one mint."""
        async with self._limiter:
            await self._limiter.pace()
            try:
                response = await self._client.get(f"/v1/tokens/{address}/report/summary")
            except httpx.HTTPError as exc:
                self._limiter.mark()
                raise RugCheckError(f"network error: {exc}") from exc
            self._limiter.mark()

            if response.status_code == 404:
                raise RugCheckError("no RugCheck report for token (never scanned)")
            if response.status_code >= 400:
                raise RugCheckError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise RugCheckError("RugCheck returned invalid JSON") from exc

            if not isinstance(payload, dict):
                raise RugCheckError(f"unexpected RugCheck payload: {type(payload)}")
            return payload
