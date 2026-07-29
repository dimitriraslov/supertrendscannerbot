# SuperTrend 1D Scanner Bot

Scans a watchlist of equities, ETFs and crypto for **daily SuperTrend flips**
(ATR 10, factor 3.0) and sends Telegram alerts. Runs free on GitHub Actions.

**This is v2.** v1 sent alerts that were late or plain wrong — roughly a third of
them were for flips that never happened, and 1 in 6 announced a "fresh" flip on
a trend that was already weeks old. The cause was found, isolated and fixed.
The full measured write-up is in **[AUDIT.md](AUDIT.md)**.

## What changed

| | v1 | v2 |
|---|---|---|
| Candle used | still-forming daily candle | **only closed candles** |
| Flip detection | `stored_signal != current_signal` | **flip's bar date**, de-duplicated |
| Freshness | assumed | **stated**: flip date + age on every alert |
| History | 60 bars | 400 bars |
| Schedule | 6×/day, mid-session | 3×/day, after the close |
| API credits | 696 / 800 (87%) | **348 / 800** |
| State writes | non-atomic, end of run | atomic, `finally`-block |
| Dropped run | can cause a false "fresh flip" | caught next run, labelled catch-up |

Measured on identical data with 25% of runs randomly dropped: **100% of real
flips caught, 0 missed, 0 false alerts, 0 duplicates** (v1: 98.7% caught, 222
false alerts, 263 round-trips).

## Files

```
scanner.py            main scanner + Telegram delivery
supertrend.py         Pine-parity SuperTrend (Wilder RMA ATR)
market_calendar.py    decides whether a bar has actually closed
selftest.py           invariants; runs in CI before every scan
watchlist.json        your symbols, grouped by narrative
state.json            last known trend per symbol (committed by the workflow)
requirements.txt
AUDIT.md              why v1 was wrong, and the proof v2 isn't
.github/workflows/scanner.yml
```

## Setup

1. Push this repo to GitHub.
2. **Settings → Secrets and variables → Actions**, add three repository secrets:

   | Secret | Where to get it |
   |---|---|
   | `TWELVE_DATA_KEY` | [twelvedata.com](https://twelvedata.com/) — free tier is enough |
   | `TELEGRAM_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
   | `TELEGRAM_CHAT` | message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id` |

3. **Settings → Actions → General → Workflow permissions** → select
   **Read and write permissions**. The workflow commits `state.json` back to the
   repo; without this it cannot remember what it already sent.
4. **Actions** tab → *Supertrend 1D Scanner* → **Run workflow**.

### The first run is silent — that is correct

`state.json` ships empty. On the first run every symbol is adopted silently,
because a trend that started before the bot was watching is by definition not a
fresh flip. Announcing 115 "flips" on day one is exactly the bug v2 exists to
prevent. You will start getting alerts from the second run onward, as flips
actually occur.

Upgrading from v1 and keeping your old `state.json` works too — v1 records are
detected and migrated silently for the same reason.

## Alerts

A real flip:

```
🟢 SUPERTREND FLIP — BULL
NVDA · NVIDIA
1D SuperTrend BEAR → BULL
Flip bar: 2026-07-28 (last closed daily candle)
Close: $197.01   ST line: $214.15
Narrative: AI / Semiconductors
Data through: 2026-07-28 · 399 bars · ATR10×3.0
```

A flip the bot is behind on — clearly labelled, never sold as fresh:

```
🕓 CATCH-UP — BEAR (not a new flip)
NVDA · NVIDIA
1D SuperTrend BULL → BEAR
Flip bar: 2026-06-05 — 35 trading days ago
⚠️ Already in BEAR since then. Reported now because this scanner had not
   recorded it yet.
```

If you see a catch-up, the bot missed runs — the alert is still honest about it.

## Verifying against your chart

```bash
python scanner.py --verify NVDA
```

Prints the last closed bar, current trend, the date the trend began, and the
last 12 flips. Set TradingView to SuperTrend with **ATR 10, factor 3.0** on the
**1D** chart; the flip dates should match bar for bar. If they don't, that is a
bug worth reporting — not something to talk yourself out of.

Other flags:

```bash
python scanner.py --dry-run      # full scan, no Telegram, no state write
python scanner.py --reseed       # adopt all current trends silently
python scanner.py --no-summary   # skip the end-of-run summary message
python selftest.py               # indicator + candle-close invariants
```

## Editing the watchlist

`watchlist.json` maps a narrative name to a list of symbols:

```json
{
  "AI / Semiconductors": [
    { "sym": "NVDA", "name": "NVIDIA", "td": "NVDA" }
  ],
  "Base Crypto": [
    { "sym": "BTC", "name": "Bitcoin", "td": "BTC/USD" }
  ]
}
```

`td` is the Twelve Data symbol (crypto uses `BASE/QUOTE`). Keep total symbols
× 3 runs/day under your daily credit limit — at 800 credits/day the ceiling is
about 260 symbols.

## Schedule

Runs at 22:23, 01:07 and 13:43 UTC. Chosen so the US close is settled in both
EST and EDT, so crypto's UTC day has rolled over, and so a dropped overnight run
gets a second chance in the morning. Minutes are deliberately off the hour —
GitHub delays and sometimes drops jobs scheduled on round slots. Duplicate runs
are harmless: alerts are de-duplicated on the flip bar's date.

Note that GitHub **disables scheduled workflows on public repos after 60 days of
repository inactivity**. If alerts stop entirely, check that first.

## Notes

- Direction convention follows Pine Script: `-1` = BULL, `+1` = BEAR.
- A symbol with fewer than 120 bars of history is skipped rather than guessed at.
- If `state.json` is corrupt, the scanner **exits without sending anything**
  rather than treating every symbol as a fresh flip.
- Alerts are only marked as sent after Telegram confirms delivery, so a network
  failure means a retry next run, not a lost signal.
