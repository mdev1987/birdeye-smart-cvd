"""Command-line entry point for the Birdeye Smart Money + CVD scanner."""

from __future__ import annotations

import asyncio
import logging
import sys

from .birdeye import BirdeyeClient
from .config import Settings
from .scanner import Scanner


def configure_logging() -> None:
    """Configure readable timestamped console logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def run() -> None:
    """Create dependencies and run the scanner until Ctrl-C."""
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logging.getLogger(__name__).error("configuration error: %s", exc)
        raise SystemExit(2) from exc
    async with BirdeyeClient(
        settings.base_url,
        settings.api_key,
        settings.chain,
        min_request_interval=settings.api_min_request_interval_seconds,
    ) as client:
        await Scanner(client, settings).run()


def main() -> None:
    """CLI wrapper with clean shutdown on Ctrl-C."""
    configure_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("stopped")
    except SystemExit as exc:
        sys.exit(exc.code)


if __name__ == "__main__":
    main()
