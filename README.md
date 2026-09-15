# Supertrend Scanner v4.0.0

One scanner calculates Supertrend, RSI threshold crosses, trend pullbacks and regular RSI/price divergences on closed daily and weekly candles. The same saved signal events feed Telegram and Dashboard v4.

## Install / upgrade

1. Upload this folder's **contents** to the root of your scanner repository. Replace the existing files, including **both** files in `.github/workflows/`. The replacement RSI workflow removes its old schedules; all signals now run in `Supertrend Scanner v4`.
2. **Keep your current `state.json`, `rsi_state.json` and `dashboard_data.json`.** They are deliberately not included in this package. On the first v4 run, legacy daily alert history is imported into the new `scanner_state.json`. Later runs use that file. Do not overwrite or reset it during future code uploads.
3. Keep your GitHub repository secrets: `TWELVE_DATA_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT`. No new accounts or secrets are needed.
4. In Actions, manually run **Supertrend Scanner v4**, leaving `timeframe` as `auto`. This fills daily data and the latest fully closed weekly data. Once it finishes, check the state/snapshot commit and Telegram summary.
5. Upload the Dashboard v4 files to your dashboard repository. If your scanner branch is not `main`, change `dataUrl` in its `config.js`.

Upload files into the existing repository root, not into an extra nested folder. The old nested `supertrendscannerbot-main/` copy is unused and can be removed to avoid confusion. Old `AUDIT.md` performance claims are replaced by the current validation report.

## Signal rules

- **Supertrend:** ATR(10), factor 3.0, Wilder smoothing, 400 requested candles and at least 120 closed candles. Initial historical trends are silently adopted. Later flips are identified by their candle dates.
- **RSI oversold:** RSI(14) crosses from at/above 30 to below 30, or from at/above 20 to below 20. Both crossings are retained if they happen together.
- **Pullback buy:** Supertrend is bullish on both the previous and current candle, and RSI crosses from above 50 to at/below 50.
- **Pullback sell:** Supertrend is bearish on both candles, and RSI crosses from below 50 to at/above 50. A trend flip on the same candle does not count as an established-trend pullback.
- **Regular bullish divergence:** consecutive confirmed **closing-price** pivot lows form a lower low, while RSI measured on those exact same dates forms a higher low. Both RSI readings must be below 40 and differ by at least 3 points.
- **Regular bearish divergence:** consecutive confirmed closing-price pivot highs form a higher high, while RSI on the same dates forms a lower high. Both RSI readings must be above 60 and differ by at least 3 points.
- **Pivot confirmation:** 5 candles on each side; pivots 5–50 candles apart. A divergence is detected on the fifth closed candle after its second pivot. It is not known at the pivot itself. “Strong” means at least 6 RSI points of separation; otherwise “moderate.” These labels are rule thresholds, not backtested probabilities.

The prior code independently matched nearby RSI pivots to price pivots. V4 instead compares RSI **at the price pivots**, so the two series use identical dates. Hidden divergences and wick-based pivots are not enabled.

All conditions run independently from the same fetched candles. Daily pullbacks use daily Supertrend; weekly pullbacks use weekly Supertrend. No extra requests are made per indicator.

## Signal visibility and alert delivery

- Recent setups mean age **0 or 1 closed candles** since the signal (or divergence confirmation), separately for daily and weekly timeframes.
- Older events remain in the saved history for up to 90 candles. Failed deliveries remain pending even when older.
- Missed scans replay up to 60 candles after the last processed candle. Older alerts are labelled `CATCH-UP`; this is a bounded recovery window, not unlimited historical replay.
- On first installation, the latest two candles are inspected for pullbacks, divergences and oversold crosses. Qualifying setups can alert immediately. Historical Supertrend flips are adopted silently. Existing daily Supertrend/RSI alert markers are respected during migration.
- Telegram confirms delivery only when its response contains `ok: true`. Pending events are written before delivery; confirmation is saved after each successful send. Failed sends retry on the next run, including weekly signals between weekly scans.
- A request that Telegram accepts but whose response is lost can still be duplicated on retry. Likewise, if GitHub cannot persist state, recover the workflow artifact before rerunning. External messaging and Git commits cannot provide a single atomic transaction.
- `--reseed` silently adopts current events and cancels pending alerts for the selected timeframes; use it only if that is intended.

## Schedule and candle boundaries

The unified workflow runs at **22:23 UTC Monday–Friday**, plus **01:07 UTC every day** for crypto rollover and backup/retries. Weekly requests are made only when the newest completed week is not already stored (or when explicitly requested).

- US/Canadian equities: daily candle after 16:45 exchange time, including daylight-saving changes. Weekly candle after Friday 16:45.
- Crypto: daily candle after the next day's 00:45 UTC; weekly candle after the next Monday's 00:45 UTC.
- Weekly timestamps are interpreted as the Monday-start calendar week containing the provider's date, so a holiday Tuesday start belongs to the same week.
- Holidays and half days use conservative regular closing cutoffs. The gate may wait longer after an early close; it is not a full international holiday/session calendar. `asset_type`, `exchange_timezone` and `session_close` can be supplied per watchlist asset for other regular sessions.
- A successful request does not prove fresh prices: stale detection uses the **candle's close time**, not when the request ran. Tolerances are 4 days for equities, 2 for crypto, and 8 after a weekly close. These tolerate normal weekends and holidays; they are not exchange-session guarantees.

168 unique symbols means 168 requests for daily-only, and up to 336 when all weekly data also needs filling. A normal weekday with an overnight backup makes 336 base requests; a weekly refresh adds up to 168. Retries and manual runs add more. No subscription quota or provider entitlement is assumed. At 8 seconds between requests, a full daily pass takes at least about 22 minutes.

Both workflows no longer compete to write state. The workflow uses one concurrency group, commits to the branch it ran on, retains state artifacts for 14 days, and fails visibly if pushing state fails. Scheduled GitHub workflows run from the repository's default branch.

## Watchlist

The original themes and entries are preserved. Duplicate symbols (PRCT and ISRG) are fetched once and retain all their themes in the dashboard. A conflicting `td` mapping for the same symbol is rejected.

The supplied snapshot had old NKLA prices and missing JBT / MOGA data (`MOGA` was mapped to `MOG/A`). V4 exposes these as data issues rather than inventing a quote or substituting an unverified ticker. Adjust their `td` values to symbols supported by your Twelve Data account or remove entries you no longer track. Corrupt state is never silently reset.

## Commands

```sh
python scanner.py                             # daily + weekly when due
python scanner.py --timeframe 1day             # daily only, all indicators
python scanner.py --weekly                     # weekly only, no duplicate daily pass
python scanner.py --dry-run                    # fetch and print, no state writes or Telegram
python scanner.py --verify NVDA --timeframe 1day # inspect one asset, no writes or messages
python scanner.py --reseed --timeframe 1day     # silently adopt / cancel pending daily alerts
python selftest.py
python -m unittest discover -s tests -v
```

`rsi_scanner.py` remains a compatibility entry point into the unified scanner. Do not schedule it separately. Run from the repository root. Python 3.11+ is required; no external Python packages are needed.

## Dashboard contract

`dashboard_data.json` schema 4 has unique assets with `narratives` and `timeframes.1day` / `timeframes.1week` records. Each record contains trend, RSI, actual candle date/close time, update time, data health, events, and up to 90 price/RSI history points. Events contain stable IDs, evidence, candle age, direction, and Telegram delivery state. Legacy flat daily/weekly keys remain exported for the old dashboard during rollout.

## References and validation

Provider data parameters: [Twelve Data documentation](https://twelvedata.com/docs). Scheduler and concurrency behavior: [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

See `AUDIT.md` for tests actually performed and remaining verification limits. The historical backtest/parity claims in earlier project files were not independently reproduced.
