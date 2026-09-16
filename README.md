# Birdeye Smart Money + CVD Scanner

Signal-only Solana scanner built around the attached Birdeye playbook, using endpoints documented as available on the free Standard package.

## Strategy

```text
Trending candidates
    -> market-cap/FDV + liquidity + momentum filter
    -> <24h age gate (creation_info; best-effort on Standard)
    -> Top Traders tagged-wallet smart-money proxy
    -> recent token trades
    -> rolling 15m CVD
    -> BUY SIGNAL
    -> paper exit on confirmed bearish CVD / TP / SL / TTL
    -> EXIT DROPPED_FROM_UNIVERSE when a holding leaves discovery
```

The playbook's dedicated Smart Money Token List is intentionally **not** used because Birdeye currently documents that endpoint as Starter+.

## Install

```bash
uv sync
cp .env.example .env
# set BIRDEYE_API_KEY in .env
```

## Run

```bash
uv run birdeye-scanner
```

No transaction is sent. `PositionState` is local paper state only.

## Notes

- The published Standard package is free, includes 30,000 CU, and is limited to 1 RPS.
- This scanner therefore uses sequential REST polling and a small watchlist.
- The CVD is calculated from Birdeye token trade `buy` / `sell` records; it is not a TradingView indicator request.
- Smart-money participation is a proxy derived from tagged top traders (`smart_trader` by default; Birdeye documents `dev, bundler, sniper, insider, smart_trader`), not Birdeye's paid Smart Money Token List.
- Market cap accepts `marketCap`/`marketcap` first, then falls back to `fdv`/`FDV` for early tokens; the source is logged (`MC=$482K [fdv]`).
- Execution price comes from the newest normalized trade's `from`/`to` leg price (`price[trade]`); the discovery snapshot (`price[discovery]`) is only a fallback.
- Token age uses `token_creation_info` (`blockUnixTime`), cached per address. That endpoint is documented for Lite/Starter+, not free Standard: on Standard the age is `unknown` and allowed with a warning unless `AGE_STRICT=true`.
- Bearish-CVD exits require `BEARISH_EXIT_CONFIRMATIONS` (default 2) consecutive polls to avoid single-poll whipsaw.
- `TXNs > 100` is deliberately not part of this strategy: it belongs to the broader Trending / Early Meme playbooks, not the Smart Money + CVD workflow.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```


## Rate limiting

The client serializes requests (including 429 backoff) behind a single lock and waits 1.5 seconds between requests by default, anchored at response completion so slow answers can't compress the gap. Birdeye Standard is documented at 1 RPS, so the scanner intentionally does not use concurrent API calls. The 1.5s value comes from live runs: at start-anchored 1.15s Birdeye 429'd every second request (phase-locked just under its real window); completion-anchored 1.5s reduced that to the occasional retry, each recovered transparently. Tune with `API_MIN_REQUEST_INTERVAL_SECONDS` (minimum 1.0, enforced).
