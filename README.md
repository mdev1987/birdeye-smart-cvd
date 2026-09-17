# Birdeye Smart-Money Proxy + 15m CVD Scanner

Signal-only Solana scanner built around the attached Birdeye playbook, using endpoints documented as available on the free Standard package.

> Naming: the smart-money leg is a free-tier **proxy** from tagged top
> traders, not Birdeye's paid Smart Money feed. The name says "Proxy"
> everywhere (code, logs, alerts, docs) so backtests never confuse the two.

## Scanner modes (`SCANNER_MODE`)

```text
CORE      Birdeye trending/overview/top-traders/trades + proxy + CVD
RISK      CORE + RugCheck + Helius
ENRICHED  RISK + Jupiter + DexScreener + CabalSpy   (default, full pipeline)
```

Run `CORE` first to test whether the raw strategy has edge before risk
filters and enrichment influence the result. Telegram alerting is
orthogonal and fires in every mode when configured.

## Discovery: paged trending, not top-10

Per the Birdeye spec, `/defi/token_trending` holds ~1000 ranked tokens at
25 CU per request *whatever the limit*, and each row already carries
`marketcap`/`fdv`, `liquidity` and `price24hChangePercent`. So discovery
pages through the ranking (`TRENDING_PAGE_SIZE=50`, `TRENDING_MAX_PAGES=2`,
`TRENDING_INTERVAL=24h` per the Early Meme timeframe), pre-filters rows on
their native fields, and only survivors pay for overview + enrichment —
stopping early once the watchlist is full. Reading only offset 0–10 sees
mega-caps exclusively; the in-band tokens live deeper in the ranking.

## Strategy

```text
Trending candidates (Birdeye)
    -> market-cap/FDV + liquidity + momentum filter
    -> DexScreener enrichment (best pair fills overview gaps)
    -> <24h age gate: Jupiter createdAt -> DexScreener oldest pool
       -> Birdeye creation_info (strict mode) -> unknown
    -> RugCheck veto (score / danger risks)
    -> Top Traders tagged-wallet Smart-Money Proxy
    -> recent token trades
    -> rolling 15m CVD (ratio ≥ threshold AND volume ≥ floor AND trades ≥ floor)
    -> chase guard: live price must be within +ENTRY_MAX_SURGE_PCT of discovery
    -> BUY SIGNAL
    -> paper exits: SL | TP1/TP2 partials (runner rides) | trailing stop |
       confirmed bearish CVD | volume-died | TTL
    -> EXIT DROPPED_FROM_UNIVERSE when a holding leaves discovery
```

Profit protection follows the playbook Early Meme risk rules (50% at x2,
30% at x3, 20% moonbag): TP1 banks half at +100%, TP2 banks 60% of the
remainder at +200%, a wide trailing stop (armed +50%, trails 40% from
peak, latched) only catches genuine round-trips while bearish CVD and
volume-death exits do the real work, and positions die immediately when
the tape goes quiet (`VOLUME_DEATH_QUIET_POLLS` polls with zero new
trades). Paper size is 1% (`PAPER_POSITION_SIZE_USD=10` on 1000). One
scaled exit counts as one trade in win rate; partials only move cash +
realized.
Every entry, TP1 scale-out, and final exit also carries a 🧪 Jupiter
simulate-only note (route OK? executable fill vs signal? impact?
simulate CU?) — quote + assemble + local throwaway sign + simulate,
never broadcast.
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
| Helius RPC + transfers | key required, inert without one | holder concentration (`top10%`), on-chain buy proof per tagged wallet, mint decimals, `simulateTransaction` RPC | skipped with info log; veto/require gates default OFF |
| Jupiter Swap v2 | key + throwaway sim key, ENRICHED only | simulate-only execution check per entry/exit (quote → assemble → local sign → simulate, **never broadcast**) | advisory `🧪 Sim …` note; `SIM_REQUIRE_ROUTE=true` skips routeless entries |

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

## Telegram alerts

Open/close/startup alerts go out via `python-telegram-bot`, rendered with
`telegramify-markdown` (MarkdownV2, icons, full contract address in a code
block — never truncated).

```bash
# in .env
TELEGRAM_BOT_TOKEN=123456:ABC...   # from @BotFather
TELEGRAM_CHAT_ID=123456789          # message the bot, then getUpdates
```

OPEN 🟢 reports entry price + source, size, balance before → after, open
positions, smart/CVD stats, age, risk, cluster/Helius notes. CLOSE
(🛑/🔶/🔷/🪝/📉/💀/⏱/🗑) reports exit price, PnL $ + %, hold time, balance
before → after, realized total, win rate and open positions. Partial
take-profits arrive as CLOSE alerts marked `Partial TP1/TP2` while the
runner stays open. Without both vars the notifier stays inert (logged)
and the scanner runs normally.

Paper accounting (`paper.py`): cash starts at `PAPER_START_BALANCE_USD`,
each open reserves `PAPER_POSITION_SIZE_USD`, closes settle
`size × exit/entry`. `MAX_OPEN_POSITIONS` (default 3) and insufficient
funds both veto entries with a log line.

## Run under supervision (`oxfile.toml`)

```bash
oxmgr validate ./oxfile.toml
oxmgr apply ./oxfile.toml
oxmgr logs birdeye-cvd -f
```

The bundled `oxfile.toml` runs `uv run birdeye-scanner` as `birdeye-cvd`
with crash-loop protection (5 crashes / 5 min → error state, no hot-loop
on a bad `.env`), a log-freshness health check (no output for 6 min →
restart), `uv sync --frozen` gating reloads, and `PYTHONUNBUFFERED=1`
so piped logs stay real-time. It supervises **this** project only — the
previous file pointed at an unrelated bot (`ave_signal_trade`).

## Security

- `.env` is gitignored and never logged (Helius/CabalSpy error paths scrub
  keys). `PRIVATE_KEY` must be a **throwaway** keypair: it only signs
  locally so `simulateTransaction` accepts the payload — no funds needed,
  and `jupsim.py` has no execute/send path by construction (regression
  test fails the build if one is added). Never paste `.env` into chats,
  tickets, or commits.
- `JUPITER_API_KEY` authenticates Swap v2 `/order` (header); the Lite price
  client stays keyless. `RUGCHECK_API_KEY` is inert (keyless client).
  `HELIUS_RPC` / `HELIUS_TXS` / `HELIUS_TX_HISTORY` are legacy duplicates
  of `HELIUS_RPC_URL`. `SHYFT_*` / `DBOTX_API_KEY` are reserved for future
  execution work and are not read by any code path today.

## Doc triage (why some `doc/` files change no code)

- Playbook + CVD/Smart-Money guides → TP ladder, trailing lock,
  volume-death exit, chase guard, smart-trend context (implemented above).
- DexScreener API → pair-selection/age usage already matches the schema;
  300 RPM headroom confirmed, no change needed.
- Helius docs → `getTransfersByAddress` now pushes `filters.blockTime.gte`
  server-side (client-side re-check retained as backstop).
- Helius docs → `getTransfersByAddress` now pushes `filters.blockTime.gte`
  server-side (client-side re-check retained as backstop); mint decimals via
  parsed `getAccountInfo` (cached forever) for sim sizing.
- Sibling `ave_signal_trade/src/jupiter_trade.py` (house pattern) → Swap v2
  `/order` shape, taker-less RTSE quoting on buys, explicit slippage on
  sells, v0 `0x80 || message` signing with solders, `simulateTransaction`
  envelope. Reused for the simulate-only path; execution/retry/PumpAPI
  machinery deliberately left behind.
- DBot / Shyft / PumpAPI docs describe *live execution* (trailing-stop
  tasks, `send_txn`, PumpSwap routing). Still unwired: nothing broadcasts,
  so there is nothing to pre-flight, schedule, or route — wiring them
  would add key-management risk for zero research benefit.

## Notes

- The published Standard package is free, includes 30,000 CU, and is limited to 1 RPS.
- This scanner therefore uses sequential REST polling and a small watchlist.
- The CVD is calculated from Birdeye token trade `buy` / `sell` records; it is not a TradingView indicator request.
- Smart-money participation is a **proxy** derived from tagged top traders (`smart_trader` by default; Birdeye documents `dev, bundler, sniper, insider, smart_trader`), not Birdeye's paid Smart Money Token List.
- CVD entry needs all three: buy/sell ratio (`CVD_MIN_BUY_SELL_RATIO`, default 1.20x) **plus** window volume (`CVD_MIN_VOLUME_USD`, default $2,000) **plus** trade count (`CVD_MIN_TRADES`, default 10). Thin "$2 buy / $0 sells" windows have an infinite ratio but fail the sample-size floors. Tune these on historical data.
- Market cap accepts `marketCap`/`marketcap` first, then falls back to `fdv`/`FDV` for early tokens; the source is logged (`MC=$482K [fdv]`).
- Execution price chain: newest trade leg price (`price[trade]`) → Jupiter quote (`price[jupiter]`) → discovery snapshot (`price[discovery]`); the source is always logged.
- Token age priority: Jupiter token-level `createdAt` → DexScreener oldest-pool `pairCreatedAt` (a pool cannot predate its tokens, so an old oldest-pool safely rejects) → Birdeye `token_creation_info` (Lite/Starter+, strict mode only) → `unknown` (allowed with warning unless `AGE_STRICT=true`). On free Standard the paid call is now skipped entirely.
- Bearish-CVD exits require `BEARISH_EXIT_CONFIRMATIONS` (default 2) consecutive polls to avoid single-poll whipsaw.
- `TXNs > 100` is deliberately not part of this strategy: it belongs to the broader Trending / Early Meme playbooks, not the Smart-Money Proxy + 15m CVD workflow.

## Tests

```bash
uv run python -m unittest discover -s tests -v
```


## Rate limiting

The client serializes requests (including 429 backoff) behind a single lock and waits 1.5 seconds between requests by default, anchored at response completion so slow answers can't compress the gap. Birdeye Standard is documented at 1 RPS, so the scanner intentionally does not use concurrent API calls. The 1.5s value comes from live runs: at start-anchored 1.15s Birdeye 429'd every second request (phase-locked just under its real window); completion-anchored 1.5s reduced that to the occasional retry, each recovered transparently. Tune with `API_MIN_REQUEST_INTERVAL_SECONDS` (minimum 1.0, enforced).
