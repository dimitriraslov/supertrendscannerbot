# v4 validation and changes

## Implemented

- Unified candle fetch and indicator evaluation for daily and weekly timeframes.
- Independent pullback / regular price-RSI divergence evaluation, using same-timeframe Supertrend and timestamp-aligned RSI evidence.
- Shared persistent event ledger for Telegram and dashboard; both threshold crossings retained; pending deliveries retried; first-run legacy migration.
- Weekly-specific candle gates, DST conversion, preserved weekly records, weekly-only command without daily repeat.
- One workflow writer; removed old RSI cron; UTC daily crypto coverage; meaningful commit failures and recovery artifacts.
- Duplicate watchlist symbols fetched once while preserving all themes.
- Candle-based stale detection, explicit missing/failed data, strict corrupt-state failure, no writes in dry-run/verify.
- Dashboard v4 signal desk, filters, delivery labels, price/RSI history, divergence pivot markers, unique asset counts, and refresh state preservation.
- Market-cap figures labelled as historical estimates; unknown symbols no longer default to mid-cap.

## Checks run locally

- Original 20 Supertrend / daily-candle invariant checks: passed. Corrected the old test fixtures to use actual New York timezone objects instead of labelling UTC wall times as local time.
- 34 scanner regression/integration tests: passed. Covers weekly boundaries and DST, crypto close, independent buy/sell pullbacks, confirmed bullish/bearish price-RSI divergence, zone filters, multi-threshold persistence, retries, missed-bar catch-up, stale suppression, migration, weekly retention, weekly-only requests, dry-run isolation, corrupt state, Telegram response validation, provider OHLC validation, rate-limit retry, and key redaction.
- Dashboard logic tests run without a browser: passed. Tests data loading, filtering, evidence output, chart/pivot markup, preserved filters, unknown caps, refresh failure, escaping, legacy deduplication and staleness.
- Python and JavaScript syntax checks: passed.

## Not verified live

No live market API requests, Telegram messages, GitHub uploads or deployed-site changes were made. Market-data entitlements and symbol mappings require the user's first live scan. There is no claim of independently verified TradingView parity or profitable signals.

Full browser rendering and mobile interaction tests were prepared in the dashboard package but could not run here: a headless browser could not launch in the sandbox, and browser security policy blocked the local-file preview. Dashboard logic tests passed, but visual layout remains unverified.

## Operational limits

The close gate covers ordinary US/Canadian equity hours and UTC crypto weeks; holidays use conservative cutoffs rather than a full exchange calendar. Confirm provider weekly aggregation conventions against your chosen charts. New listings with fewer than 120 closed candles are explicitly marked insufficient history. Cap tiers remain the user's historical estimates, labelled as such. Pending delivery can duplicate after an ambiguous network response or lost GitHub state commit; restore the workflow artifact after a failed push. Missed-bar recovery is limited to 60 candles.
