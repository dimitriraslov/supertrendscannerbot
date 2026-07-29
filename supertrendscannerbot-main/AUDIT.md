# Audit: why the SuperTrend alerts were late / inaccurate

**Verdict in one line:** the scanner was reading the **still-forming daily
candle**, so it fired on intraday wicks that later un-happened, corrupted its
own `state.json` in the process, and then announced the *recovery* from that
corruption as a brand-new flip — which is why an alert saying "fresh BULL"
could land on a chart that had been bullish for months.

Everything below is measured, not assumed. Every hypothesis was tested against
real daily OHLC, including the two that turned out to be **wrong**.

---

## 1. Reproducing the symptom

Data: 5 years of daily OHLC for 58 of your watchlist symbols, plus 730 days of
hourly bars for 22 of them. The hourly bars let me rebuild what the
still-forming daily candle actually looked like at each moment, so I could
replay the original `scanner.py` exactly as GitHub Actions ran it — 6 scans a
day, live candle included — and compare every alert against the finished chart.

**Replay of the original bot** (22 tickers, ~2 years, 6 scans/day):

| Measure | Result |
|---|---|
| True flips that actually happened on a closed bar | 479 |
| Alerts the bot sent | **724** |
| True flips it caught | 473 (98.7%) |
| True flips it missed | 6 (1.3%) |
| **False alerts (no flip ever happened)** | **222 — 30.7% of everything it sent** |
| **Alerts fired on an unfinished candle** | **714 — 98.6%** |
| Delay when it was right | median 0 bars, max 1 bar |

Two things jump out.

First, **the bot was not slow.** Median lag was zero bars. It was firing
*early* — before the candle existed — which is a different disease with the same
taste.

Second, **nearly a third of the alerts were for flips that never occurred.**

## 2. Measuring your exact complaint

Your report was specific: the message says *fresh* flip, but the chart has been
in that trend for ages. So I measured precisely that — for every alert, how old
was the trend the bot was announcing?

| | |
|---|---|
| Alerts that were genuinely fresh | 604 / 724 (83.4%) |
| **Alerts announcing a trend that was already established** | **120 / 724 (16.6%)** |
| How stale those were | **median 23 trading days**, mean 32.3, worst **146** |

Distribution of the stale ones: 14 were 6–10 days old, 33 were 11–20 days,
41 were 21–50 days, and **25 were over 50 days old**.

Worked examples straight out of the replay:

| Symbol | Alert date | Message said | Reality |
|---|---|---|---|
| AVGO | 2025-11-07 | fresh BULL | BULL for **146 days** |
| MRNA | 2024-12-09 | fresh BEAR | BEAR for **131 days** |
| NVDA | 2025-09-02 | fresh BULL | BULL for **98 days** |
| HOOD | 2025-09-05 | fresh BULL | BULL for **91 days** |
| ARKK | 2024-12-30 | fresh BEAR | BEAR for **91 days** |
| TSM | 2026-07-15 | fresh BULL | BULL for **66 days** |

Every single one was tagged *fired on live candle*. That is the fingerprint.

I also counted **263 round-trip alert pairs** — the bot flipped a symbol, then
un-flipped it days later, while the chart's trend never changed once.

## 3. The mechanism

```
14:30  intraday wick pierces the SuperTrend band
       -> scanner computes BEAR on the unfinished candle
       -> ALERT SENT
       -> state.json overwritten to "bear"          <-- the damage
16:00  candle closes back above the band; the real trend was BULL all along
       -> chart never flipped. No flip exists.
next   scanner computes BULL (correct), sees stored "bear", concludes
run    "it flipped!" -> ALERT: "fresh BULL"
       -> but the chart has been BULL for weeks or months
```

The alert isn't late. It is **an echo of the bot's own earlier mistake**. The
second message is the bot recovering from self-inflicted state corruption, and
it has no way to know that, because of the second defect:

```python
if stored_sig and stored_sig != cur_sig:   # original scanner.py
```

Flip detection was a **string comparison against stored state**. That test
cannot distinguish "this flipped today" from "my stored state is stale."
Anything that desynchronises state — a dropped Actions run, a failed
`git push`, an API error, the repaint above — is indistinguishable from a fresh
flip. The original bot never computed *which bar* flipped or *how many bars ago*,
so it was structurally incapable of telling a same-day flip from a 146-day-old
one.

## 4. Hypotheses that were WRONG

Worth stating plainly, because these are the obvious suspects and both failed.

**"The 60-bar history is too short."** SuperTrend is path-dependent, so a short
window is a real concern. Measured across 58 tickers × 400 days (23,200
ticker-days), a 60-bar window disagreed with a fully-converged reference on
**0.48%** of days — 111 cases. 39 of 58 tickers were *perfect*. Bad on a few
(PLD 9.00%, WELL 4.25%, ASTS 3.50%, IONQ 2.50%) but NVDA was 1.00%. This is a
real defect, but far too small to explain 222 false alerts. **Not the cause.**

**"Alerts arrive late because GitHub Actions is delayed."** Measured lag on
correct alerts was a median of **0 bars**. Scheduler drift is real and was
visible in your committed `state.json` (a 04:00 UTC cron whose timestamps all
landed at 06:55–07:10 UTC, ~3 hours late), and GitHub does
[drop scheduled runs entirely](https://github.com/orgs/community/discussions/158356)
with [delays from minutes to hours](https://stackoverflow.com/questions/79534419/reliability-issues-with-github-actions-with-cron-based-schedule).
But drift makes alerts *arrive late*, it does not make them *describe an old
trend as new*. It is an amplifier, not the cause.

## 5. Isolating the cause

I rebuilt the bot four ways and replayed identical data through each, changing
one thing at a time. "Stale" = announced as fresh but the trend was already
established. "Wrong direction" = the alert's direction disagreed with the last
closed bar.

| Variant | Alerts | Stale | Stale % | Wrong direction | Median stale age |
|---|---|---|---|---|---|
| **A** — as shipped | 382 | 66 | 17.3% | **299 (78%)** | 21 d |
| **B** — closed candles only | 262 | 8 | 3.1% | **10** | 10 d |
| **C** — bar-level flip identity only | 298 | 4 | 1.3% | **287** | 28 d |
| **D** — both fixes | 249 | **3** | **1.2%** | **2** | 2 d |

Reduction in stale "fresh flip" alerts vs baseline: B **−88%**, C **−94%**,
D **−95%**.

This is the decisive table, and it says something subtle:

- **Waiting for the candle to close is the cure** (variant B): wrong-direction
  alerts collapse from 299 to 10, a **97% reduction**, from that single change.
- **Bar-level flip identity alone is a trap** (variant C): it makes the *stale*
  number look great (66 → 4) while leaving **287 wrong-direction alerts**. It
  relabels the symptom without treating the disease, because every flip is still
  dated to today's forming candle and therefore always *looks* fresh.
- Only **both together** (variant D) drive both columns to near zero.

So: **live-candle repaint is the primary cause; flip-identity-by-string-diff is
what converted each repaint into a second, confidently-wrong "fresh flip"
message.** Neither fix is sufficient alone.

## 6. Ranked root causes

| # | Defect | Measured impact |
|---|---|---|
| 1 | **Unfinished daily candle fed to the indicator** | 98.6% of alerts; removing it cuts wrong-direction alerts 97% |
| 2 | **Flip detected by `stored_sig != cur_sig`** | Turns every desync into a "fresh flip"; 263 round-trips; no bars-since-flip ever computed |
| 3 | **60-bar history** | 0.48% direction error; **745 flip-date wobbles** per 250 days across 57 tickers |
| 4 | Scanning 6×/day | A daily indicator changes once per day; 4 of 6 runs landed mid-session |
| 5 | API headroom | 116 × 6 = **696 of 800** daily credits (87%) — one retry storm from starvation |
| 6 | `save_state()` only at the end of the loop, non-atomic | A crash or timeout loses every update, re-arming defect #2 |
| 7 | `git push` with no pull/rebase or retry | Push races silently discard state |
| 8 | Cron on round hours; no `concurrency` guard | Maximises scheduler drift and allows overlapping runs to clobber state |

Note on #5: Twelve Data's free tier
[includes real-time US equities](https://twelvedata.com/pricing) at
[8 credits/min and 800/day](https://support.twelvedata.com/en/articles/5194820-api-credits-limits).
That is *why* the `interval=1day` request returned a live, unfinished candle
during the session. The data was never wrong — it was being used wrong.

## 7. The fix, and proof it works

1. **Closed candles only.** `market_calendar.last_closed_index()` drops any bar
   that is still forming — equities need the session closed plus a 45-minute
   settlement buffer; crypto's current UTC day is never trusted. This removes
   repaint by construction rather than by heuristic.
2. **A flip is identified by its bar date**, and de-duplicated on
   `alerted_flip_date`. A dropped, delayed, duplicated or retried run can no
   longer re-fire an old flip or invent a new one.
3. **Freshness is stated, never implied.** Every alert carries the flip bar's
   date and its age. Anything older than 1 bar ships as a clearly-labelled
   `CATCH-UP`, never as fresh. If the bot is behind, it says so.
4. **400 bars of history** instead of 60 — still 1 API credit.
5. **Three runs/day, after the close**, at off-round minutes. Credits drop from
   696 to **348 of 800**.
6. Atomic state writes, a corrupt-state guard that refuses to run rather than
   mass-alert, silent first-run seeding, a staleness watchdog, retry/backoff on
   429s, `concurrency` group, and `git pull --rebase` with push retries.

**Validation.** Same data, same 6-scans-a-day grid, and **25% of runs randomly
deleted** to simulate GitHub dropping them:

| Measure | Original | Fixed |
|---|---|---|
| True flips in period | 479 | 479 |
| Alerts sent | 724 | **482** |
| True flips caught | 473 (98.7%) | **479 (100.0%)** |
| Missed | 6 | **0** |
| False alerts | **222 (30.7%)** | **0** |
| Duplicate alerts for one flip | 263 round-trips | **0** |
| Wrong-direction alerts | 299 of 382 in ablation | **0** |
| Fired on an unfinished candle | 714 (98.6%) | **0** |

Zero missed flips even with a quarter of runs deleted, because a missed run no
longer means a missed flip — the next run sees the flip is still unannounced and
sends it, labelled with its true age.

**On the residual.** A first pass of this validation showed 6 surplus alerts on
PLTR and ARKK. Rather than wave them off, I traced them: my simulator rebuilt
each daily close by aggregating hourly bars, which **omits the closing-auction
print**, leaving its closes off by 1–5 cents (PLTR 2024-04-01: official close
22.86, rebuilt 22.85). Those 6 alerts sat within pennies of the band, and the
penny error flipped the comparison. Re-running with official closes for settled
bars: **0 duplicates, 0 wrong-direction.** The window length was separately
cleared — at 400 bars, PLTR, ARKK and NVDA show 0.00% direction mismatch and
zero flip-date differences vs a converged reference over 850 days.

## 8. History length, chosen not guessed

57 tickers × 250 days. "Flip-date wobble" = the scanner reports a *different*
flip bar for the same ongoing trend between runs — each one a duplicate alert
that de-duplication cannot catch, so it must be zero.

| Window | Direction mismatch | Flip-date wobbles |
|---|---|---|
| 60 (original) | 0.281% | **745** |
| 120 | 0.000% | 136 |
| 250 | 0.000% | **0** |
| 400 (**shipped**) | 0.000% | **0** |
| 600 | 0.000% | 0 |

250 bars is the point of convergence; 400 ships as margin, at no extra credit
cost.

## 9. How to check it yourself

```bash
python scanner.py --verify NVDA
```

Prints the last closed bar, the current trend, the exact date the trend began,
and the last 12 flips. Compare those dates against your TradingView chart with
ATR 10 / factor 3.0 — they should match bar for bar.

```bash
python scanner.py --dry-run    # full scan, no Telegram, no state write
python selftest.py             # indicator + candle-close invariants
```

`selftest.py` runs in CI before every scan, so if any of the invariants behind
this audit ever break, the workflow fails loudly instead of quietly messaging
you something wrong.

---

### Reproducibility

All experiments used free Yahoo Finance daily/hourly OHLC. Direction convention
follows Pine Script: `-1` = BULL, `+1` = BEAR. Reference SuperTrend is computed
over full available history with Wilder RMA ATR, matching TradingView's
`ta.supertrend`. Ablation ran on 10 representative tickers; the replay and
validation on the 22 with hourly coverage; window tests on all 57 with
sufficient history.
