"""Shared completion-anchored async rate limiter."""

from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Serialize requests and enforce a minimum gap between them.

    The gap is anchored at request *completion*: ``mark()`` must be called
    after each response (or failure) while the lock is held, so the server
    always sees a full quiet gap even when its own answers are slow.
    Start-anchored gaps collapse exactly when the server is struggling.
    """

    def __init__(self, min_interval: float = 1.0) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> "AsyncRateLimiter":
        """Hold the serial lock for a request attempt loop."""
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        """Release the serial lock."""
        self._lock.release()

    async def pace(self) -> None:
        """Wait until the next request may start. Call with lock held."""
        elapsed = time.monotonic() - self._last
        delay = self.min_interval - elapsed
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()

    def mark(self) -> None:
        """Stamp request completion. Call with lock held."""
        self._last = time.monotonic()
