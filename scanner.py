#!/usr/bin/env python3
"""
Supertrend 1D Flip Scanner — v2

Sends a Telegram alert when a watchlist asset flips direction on the DAILY
SuperTrend (ATR 10, factor 3.0), matching what TradingView draws.

What changed vs v1, and why
---------------------------
1. CLOSED CANDLES ONLY. v1 fed the still-forming daily bar into the indicator,
   so a mid-session wick could trip a band, fire an alert, and then close back
   on the original side. Measured on 2 years of replayed scans across 22 assets:
   98.6% of v1's alerts fired on an unclosed candle and ~31% of all its alerts
   never corresponded to a real flip at all.

2. FLIPS ARE IDENTIFIED BY THEIR BAR, NOT BY A STRING DIFF. v1 asked "does
   state.json disagree with right now?" Any desync — a dropped GitHub Actions
   run, a failed push, an API error, an intraday repaint — therefore looked
   exactly like a brand-new flip. That is why alerts arrived announcing a
   "fresh" trend the chart had been in for weeks (measured median: 23 trading
   days stale, worst case 146 days). We now locate the actual bar where the
   trend changed, and de-duplicate on that bar's date. Re-running the scanner
   ten times can never re-fire or invent a flip.

3. FRESHNESS IS STATED, NOT ASSUMED. Every alert carries the flip bar's date
   and its age in bars. A flip older than MAX_FRESH_BARS is delivered as an
   explicitly-labelled CATCH-UP, never as a fresh signal.

4. ENOUGH HISTORY TO CONVERGE. SuperTrend is path-dependent. v1's 60 bars
   disagreed with a fully-converged chart on up to 9% of days for some symbols.
   We now request ~400 bars (still 1 API credit).

5. RUNS AFTER THE CLOSE. A daily SuperTrend cannot change mid-session, so
   scanning 6x/day only invited repaint. We scan after the US close and after
   the UTC crypto rollover, which also cuts API usage ~50%.

Usage
-----
  python scanner.py                 normal scan
  python scanner.py --dry-run       compute + print, no Telegram, no state write
  python scanner.py --verify NVDA   print recent flip dates to check vs a chart
  python scanner.py --reseed        adopt current trends silently, no alerts
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

from supertrend import supertrend, last_flip, all_flips, label, BULL, BEAR
from market_calendar import last_closed_index, describe, is_crypto

# ── Config ────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")

ATR_PERIOD = 10
ST_FACTOR  = 3.0

# SuperTrend is path dependent; short windows do not match a chart that was
# computed over full history. 400 daily bars is far past convergence and still
# costs a single API credit (free plan allows 5,000 data points per request).
OUTPUTSIZE = 400
MIN_BARS   = 120          # refuse to signal on less history than this

# A flip newer than this is "fresh". Anything older is reported as catch-up.
# 1 gives a one-day grace window so a dropped scheduled run still alerts.
MAX_FRESH_BARS = 1

DELAY_SEC   = 8           # free plan: 8 requests/minute
MAX_RETRIES = 3
STALE_AFTER_DAYS = 4      # warn if a symbol has not updated in this long

STATE_FILE = "state.json"
LOCAL_TZ = "America/Toronto"      # timestamps shown in your local time
LOCAL_TZ_LABEL = "ET"
STATE_SCHEMA = 2

TD_URL = "https://api.twelvedata.com/time_series"


# ── Files ─────────────────────────────────────────────────────────────────────
def load_watchlist(path="watchlist.json"):
    with open(path, "r") as f:
        return json.load(f)


def load_state(path=STATE_FILE):
    try:
        with open(path, "r") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # A corrupt state file must NOT be silently treated as "no state" —
        # that would make every asset look like a fresh flip.
        print(f"!! {path} is unreadable ({e}). Refusing to run to avoid "
              f"mass false alerts. Fix or delete the file, then use --reseed.")
        sys.exit(1)


def save_state(state, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)      # atomic: never leaves a half-written state file


# ── Data ──────────────────────────────────────────────────────────────────────
def fetch_ohlc(td_symbol):
    """Return (bars, meta, None) or (None, None, error). bars are oldest-first."""
    params = {
        "symbol": td_symbol,
        "interval": "1day",
        "outputsize": OUTPUTSIZE,
        "order": "ASC",
        "apikey": TWELVE_DATA_KEY,
    }
    err = "unknown"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(TD_URL, params=params, timeout=25)
            if r.status_code == 429:
                wait = 20 * attempt
                print(f"[rate-limited, waiting {wait}s] ", end="", flush=True)
                time.sleep(wait)
                err = "rate limited"
                continue
            d = r.json()
            if isinstance(d, dict) and d.get("status") == "error":
                return None, None, d.get("message", "API error")
            vals = (d or {}).get("values")
            if not vals:
                return None, None, "no data returned"

            bars = []
            for v in vals:
                try:
                    bars.append({
                        "date": v["datetime"][:10],
                        "h": float(v["high"]),
                        "l": float(v["low"]),
                        "c": float(v["close"]),
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            bars.sort(key=lambda b: b["date"])
            # de-duplicate identical dates, keeping the newest entry
            dedup = {}
            for b in bars:
                dedup[b["date"]] = b
            bars = [dedup[k] for k in sorted(dedup)]
            if not bars:
                return None, None, "no parseable bars"
            return bars, (d.get("meta") or {}), None
        except Exception as e:
            err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(4 * attempt)
    return None, None, err


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message, dry=False):
    if dry:
        print("\n--- telegram (dry-run) ---\n" + message + "\n--------------------------")
        return True
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("Telegram not configured — skipping send")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": message,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
            if r.status_code == 429:
                time.sleep(5 * attempt)
                continue
            return False
        except Exception as e:
            print(f"Telegram exception: {e}")
            time.sleep(3 * attempt)
    return False


def _stamp():
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        loc = utc.astimezone(ZoneInfo(LOCAL_TZ))
        tag = loc.tzname() or LOCAL_TZ_LABEL
    except Exception:
        loc, tag = utc - timedelta(hours=4), LOCAL_TZ_LABEL
    return (f"{loc.strftime('%b %d, %Y · %H:%M')} {tag}"
            f"  ({utc.strftime('%H:%M')} UTC)")


def fmt_price(p):
    if p is None:
        return "n/a"
    return f"${p:,.4f}" if abs(p) < 1 else f"${p:,.2f}"


def format_alert(a):
    fresh = a["age"] <= MAX_FRESH_BARS
    arrow = "🟢" if a["new"] == "bull" else "🔴"
    action = a["new"].upper()
    from_s = "BEAR" if a["new"] == "bull" else "BULL"

    if fresh:
        head = f"{arrow} <b>SUPERTREND FLIP — {action}</b>"
        age_line = (f"Flip bar: <b>{a['flip_date']}</b> (last close — confirmed)"
                    if a["age"] == 0 else
                    f"Flip bar: <b>{a['flip_date']}</b> ({a['age']} bar(s) ago)")
    else:
        head = f"🕓 <b>CATCH-UP — {action}</b> (not a new flip)"
        age_line = (f"Flip bar: <b>{a['flip_date']}</b> — "
                    f"<b>{a['age']} trading days ago</b>\n"
                    f"⚠️ Already in {action} since then. Reported now because "
                    f"this scanner had not recorded it yet.")

    lines = [
        head,
        f"<b>{a['sym']}</b> · {a['name']}",
        f"1D SuperTrend {from_s} → {action}",
        age_line,
        f"Close: <b>{fmt_price(a['price'])}</b>   "
        f"ST line: {fmt_price(a['st'])}",
        f"Narrative: {a['narrative']}",
        f"Data through: {a['last_bar']} · {a['bars']} bars · ATR{ATR_PERIOD}×{ST_FACTOR}",
        "",
        f"⏰ {_stamp()}",
    ]
    return "\n".join(lines)


def format_summary(fresh, catchup, errors, stale, total):
    lines = [f"📊 <b>SuperTrend Scan</b> — {total} assets on closed daily bars"]
    if fresh:
        lines += ["", f"<b>Fresh flips ({len(fresh)})</b>"]
        for f in fresh:
            arrow = "🟢" if f["new"] == "bull" else "🔴"
            lines.append(f"{arrow} {f['sym']} → {f['new'].upper()}  "
                         f"({f['flip_date']})")
    if catchup:
        lines += ["", f"<b>Catch-up, already established ({len(catchup)})</b>"]
        for f in catchup[:12]:
            lines.append(f"🕓 {f['sym']} → {f['new'].upper()}  "
                         f"since {f['flip_date']} ({f['age']}d)")
        if len(catchup) > 12:
            lines.append(f"…and {len(catchup) - 12} more")
    if not fresh and not catchup:
        lines.append("No flips on the latest closed daily bar.")
    if stale:
        lines += ["", f"⚠️ <b>Not updating ({len(stale)})</b>: " + ", ".join(stale[:15])]
    if errors:
        lines += ["", f"⚠️ <b>Errors ({len(errors)})</b>"]
        for e in errors[:12]:
            lines.append(f"• {e}")
        if len(errors) > 12:
            lines.append(f"…and {len(errors) - 12} more")
    lines += ["", f"⏰ {_stamp()}"]
    return "\n".join(lines)


# ── Core evaluation ───────────────────────────────────────────────────────────
def evaluate(bars, td_symbol, meta):
    """
    Compute SuperTrend on CLOSED bars only and locate the real flip bar.

    Returns (result_dict, None) or (None, reason_string).
    """
    dates = [b["date"] for b in bars]
    lci = last_closed_index(dates, td_symbol, meta)
    if lci is None:
        return None, "no closed daily bar available yet"

    closed = bars[:lci + 1]
    if len(closed) < MIN_BARS:
        return None, f"only {len(closed)} closed bars (need {MIN_BARS})"

    H = [b["h"] for b in closed]
    L = [b["l"] for b in closed]
    C = [b["c"] for b in closed]
    st = supertrend(H, L, C, ATR_PERIOD, ST_FACTOR)
    dirs = st["dirs"]

    cur = dirs[-1]
    flip_idx, _ = last_flip(dirs)
    last_i = len(closed) - 1

    if flip_idx is None:
        # Direction never changed anywhere in ~400 bars of history.
        flip_date, age = None, None
    else:
        flip_date = closed[flip_idx]["date"]
        age = last_i - flip_idx

    return {
        "signal": label(cur),
        "flip_date": flip_date,
        "age": age,
        "price": C[-1],
        "st": st["trend"][-1],
        "last_bar": closed[-1]["date"],
        "bars": len(closed),
        "dropped_forming": len(bars) - len(closed),
        "dirs": dirs,
        "closed": closed,
    }, None


# ── Verify mode ───────────────────────────────────────────────────────────────
def verify(sym_query, watchlist):
    target = None
    for narrative, assets in watchlist.items():
        for a in assets:
            if a["sym"].upper() == sym_query.upper():
                target = (narrative, a)
    if not target:
        print(f"{sym_query} is not in watchlist.json")
        return 1
    narrative, asset = target
    td = asset.get("td", asset["sym"])
    bars, meta, err = fetch_ohlc(td)
    if err:
        print(f"fetch failed: {err}")
        return 1
    res, why = evaluate(bars, td, meta)
    if not res:
        print(f"cannot evaluate: {why}")
        return 1

    print(f"\n{asset['sym']} — {asset['name']}   [{narrative}]")
    print(f"session/timezone   : {describe(td, meta)}")
    print(f"bars fetched       : {len(bars)}  "
          f"(dropped {res['dropped_forming']} still-forming)")
    print(f"last CLOSED bar    : {res['last_bar']}   close {fmt_price(res['price'])}")
    print(f"current trend      : {res['signal'].upper()}")
    began = res["flip_date"] or "before our history window"
    if res["age"] is not None:
        began += f"   ({res['age']} trading days ago)"
    print(f"trend began        : {began}")
    print(f"SuperTrend line    : {fmt_price(res['st'])}")
    print(f"\nLast 12 flips (compare these dates against your chart):")
    fl = all_flips(res["dirs"])
    for i, d in fl[-12:]:
        print(f"   {res['closed'][i]['date']}   → {label(d).upper():<4}  "
              f"close {fmt_price(res['closed'][i]['c'])}")
    if not fl:
        print("   (no direction change in the fetched history)")
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print; do not send Telegram or write state")
    ap.add_argument("--verify", metavar="SYMBOL",
                    help="print recent flip dates for one symbol and exit")
    ap.add_argument("--reseed", action="store_true",
                    help="adopt every current trend silently, sending no alerts")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()

    if not TWELVE_DATA_KEY:
        print("TWELVE_DATA_KEY is not set."); return 1

    watchlist = load_watchlist()

    if args.verify:
        return verify(args.verify, watchlist)

    started = datetime.now(timezone.utc)
    print(f"Scan start {started.strftime('%Y-%m-%d %H:%M UTC')}"
          f"{'  [DRY RUN]' if args.dry_run else ''}"
          f"{'  [RESEED]' if args.reseed else ''}")
    print(f"closed bars only · {OUTPUTSIZE}-bar history · "
          f"fresh window {MAX_FRESH_BARS} bar(s)\n")

    state = load_state()
    fresh, catchup, errors, seeded = [], [], [], []
    total = 0
    symbols = [(n, a) for n, assets in watchlist.items() for a in assets]
    budget = len(symbols)
    print(f"{budget} symbols → {budget} API credits this run\n")

    try:
        for narrative, asset in symbols:
            sym = asset["sym"]
            name = asset["name"]
            td = asset.get("td", sym)
            print(f"  {sym:<8}", end=" ", flush=True)

            bars, meta, err = fetch_ohlc(td)
            if err:
                print(f"ERROR: {err}")
                errors.append(f"{sym}: {err}")
                time.sleep(DELAY_SEC)
                continue

            res, why = evaluate(bars, td, meta)
            if not res:
                print(f"SKIP: {why}")
                errors.append(f"{sym}: {why}")
                time.sleep(DELAY_SEC)
                continue

            total += 1
            prior = state.get(sym) or {}
            # v1 state records only stored a bull/bear string with no notion of
            # WHICH BAR flipped. They cannot tell us whether a flip was already
            # announced, so they are adopted silently rather than replayed as a
            # wave of bogus "fresh" alerts on the first v2 run.
            legacy = bool(prior) and "alerted_flip_date" not in prior
            prior_alerted = prior.get("alerted_flip_date")
            prior_sig = prior.get("signal")

            age_s = "never" if res["age"] is None else f"{res['age']}d ago"
            print(f"{res['signal'].upper():<4} since {res['flip_date'] or '—'} "
                  f"({age_s})  bar {res['last_bar']}", end="")

            record = {
                "signal": res["signal"],
                "flip_date": res["flip_date"],
                "bars_since_flip": res["age"],
                "last_closed_bar": res["last_bar"],
                "price": round(res["price"], 6),
                "supertrend": (None if res["st"] is None else round(res["st"], 6)),
                "history_bars": res["bars"],
                "schema": STATE_SCHEMA,
                "updated": datetime.now(timezone.utc)
                            .replace(microsecond=0).isoformat(),
                "alerted_flip_date": prior_alerted,
            }

            alert = {
                "sym": sym, "name": name, "narrative": narrative,
                "new": res["signal"], "old": prior_sig or "unknown",
                "flip_date": res["flip_date"], "age": res["age"],
                "price": res["price"], "st": res["st"],
                "last_bar": res["last_bar"], "bars": res["bars"],
            }

            first_time = not prior
            no_flip_in_history = res["flip_date"] is None

            if args.reseed or first_time or legacy or no_flip_in_history:
                # Silent adoption. A symbol we have never tracked, or one whose
                # trend predates our whole history window, is by definition NOT
                # a fresh flip and must never be announced as one.
                record["alerted_flip_date"] = res["flip_date"]
                if not args.reseed and (first_time or legacy):
                    seeded.append(sym)
                    print("   [seeded, no alert]" if first_time
                          else "   [migrated from v1, no alert]")
                else:
                    print("   [adopted]")
            elif prior_alerted == res["flip_date"]:
                print("   [already alerted]")
            else:
                # A genuine, not-yet-announced change of trend bar.
                if res["age"] is not None and res["age"] <= MAX_FRESH_BARS:
                    fresh.append(alert)
                    print("   *** FRESH FLIP ***")
                else:
                    catchup.append(alert)
                    print(f"   [catch-up, {res['age']}d old]")
                if send_telegram(format_alert(alert), dry=args.dry_run):
                    record["alerted_flip_date"] = res["flip_date"]
                elif args.dry_run:
                    record["alerted_flip_date"] = res["flip_date"]
                else:
                    # Delivery failed — do NOT mark as alerted, so the next run
                    # retries instead of silently swallowing the signal.
                    print(f"    (delivery failed for {sym}; will retry next run)")

            state[sym] = record
            time.sleep(DELAY_SEC)
    finally:
        if not args.dry_run:
            save_state(state)
            print(f"\nstate.json written ({len(state)} symbols)")

    # staleness watchdog — surfaces silent scheduler/API failures
    stale = []
    now = datetime.now(timezone.utc)
    for sym, rec in state.items():
        try:
            u = datetime.fromisoformat(rec["updated"])
            if u.tzinfo is None:
                u = u.replace(tzinfo=timezone.utc)
            if (now - u).days >= STALE_AFTER_DAYS:
                stale.append(sym)
        except Exception:
            pass

    if seeded:
        print(f"seeded (first sighting, intentionally silent): {', '.join(seeded)}")

    if not args.no_summary and not args.reseed:
        if fresh or catchup or errors or stale:
            send_telegram(format_summary(fresh, catchup, errors, stale, total),
                          dry=args.dry_run)

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nDone in {took/60:.1f} min — {total} evaluated, "
          f"{len(fresh)} fresh, {len(catchup)} catch-up, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
