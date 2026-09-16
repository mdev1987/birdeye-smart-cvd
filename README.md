# Birdeye Smart Money + CVD Scanner

Signal-only Solana scanner built around the attached Birdeye playbook, using endpoints documented as available on the free Standard package.

## Strategy

```text
Trending candidates (Birdeye)
    -> market-cap/FDV + liquidity + momentum filter
    -> DexScreener enrichment (best pair fills overview gaps)
    -> <24h age gate: Jupiter createdAt -> DexScreener oldest pool
       -> Birdeye creation_info (strict mode) -> unknown
    -> RugCheck veto (score / danger risks)
    -> Top Traders tagged-wallet smart-money proxy
    -> recent token trades
    -> rolling 15m CVD (+ CabalSpy cluster confirmation, optional)
    -> BUY SIGNAL
    -> paper exit on confirmed bearish CVD / TP / SL / TTL
    -> EXIT DROPPED_FROM_UNIVERSE when a holding leaves discovery
```

The playbook's dedicated Smart Money Token List is intentionally **not** used because Birdeye currently documents that endpoint as Starter+.

## Data sources

| Source | Cost | Used for | Degrades to |
|---|---|---|---|
| Birdeye Standard | CU-metered, 1 RPS | trending, overview, top traders, trades (core path) | poll skipped on error |
| DexScreener | free, keyless, 300 RPM | pool ages (`pairCreatedAt`), overview-gap filling | skipped with warning |
| Jupiter lite Price v3 | free, keyless | token-level `createdAt` age oracle, poll price fallback | skipped with warning |
| RugCheck summary | free (~3 RPS) | pre-entry veto (`score_normalised`, `danger` risks) | allowed as `rug:unknown` (strict mode rejects) |
| CabalSpy | key required, inert without one | advisory cluster confirmation | skipped with info log |
| Helius RPC + transfers | key required, inert without one | holder concentration (`top10%`), on-chain buy proof per tagged wallet | skipped with info log; veto/require gates default OFF |

Every auxiliary source fails open (warn + continue) except under its explicit `*_STRICT` / `REQUIRE` flag. The scanner never sends transactions.

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
- Execution price chain: newest trade leg price (`price[trade]`) → Jupiter quote (`price[jupiter]`) → discovery snapshot (`price[discovery]`); the source is always logged.
- Token age priority: Jupiter token-level `createdAt` → DexScreener oldest-pool `pairCreatedAt` (a pool cannot predate its tokens, so an old oldest-pool safely rejects) → Birdeye `token_creation_info` (Lite/Starter+, strict mode only) → `unknown` (allowed with warning unless `AGE_STRICT=true`). On free Standard the paid call is now skipped entirely.
- Bearish-CVD exits require `BEARISH_EXIT_CONFIRMATIONS` (default 2) consecutive polls to avoid single-poll whipsaw.
- `TXNs > 100` is deliberately not part of this strategy: it belongs to the broader Trending / Early Meme playbooks, not the Smart Money + CVD workflow.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```


## Rate limiting

The client serializes requests (including 429 backoff) behind a single lock and waits 1.5 seconds between requests by default, anchored at response completion so slow answers can't compress the gap. Birdeye Standard is documented at 1 RPS, so the scanner intentionally does not use concurrent API calls. The 1.5s value comes from live runs: at start-anchored 1.15s Birdeye 429'd every second request (phase-locked just under its real window); completion-anchored 1.5s reduced that to the occasional retry, each recovered transparently. Tune with `API_MIN_REQUEST_INTERVAL_SECONDS` (minimum 1.0, enforced).
