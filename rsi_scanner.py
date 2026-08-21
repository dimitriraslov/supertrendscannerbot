#!/usr/bin/env python3
"""
rsi_scanner.py — RSI Oversold Alert Scanner

Sends a Telegram alert when any watchlist asset crosses BELOW RSI 30 or RSI 20
on the Daily (1D) or Weekly (1W) timeframe.

Alert logic:
  - Crosses below 30: RSI was >= 30 on the previous closed bar, now < 30
  - Crosses below 20: RSI was >= 20 on the previous closed bar, now < 20
  - Only fires on CLOSED candles (same fix as the Supertrend scanner)
  - De-duplicated: same cross on same bar never fires twice

RSI formula matches ChrisMoody CM_Ultimate RSI / TradingView ta.rsi:
  Wilder RMA smoothing (same as Pine's rma())
  Default length: 14
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

from market_calendar import last_closed_index, is_crypto

# ── Config ────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")

RSI_PERIOD   = 14
OUTPUTSIZE   = 200      # enough warmup for RSI(14) to fully converge
MIN_BARS     = 30       # skip symbols with too little history
DELAY_SEC    = 8        # free plan: 8 requests/minute

# Thresholds to watch
LEVELS = [
    {"value": 30, "label": "Oversold",         "emoji": "🟡"},
    {"value": 20, "label": "Extreme Oversold",  "emoji": "🔴"},
]

# Timeframes to scan
TIMEFRAMES = [
    {"id": "1day",  "label": "1D", "interval": "1day"},
    {"id": "1week", "label": "1W", "interval": "1week"},
]

STATE_FILE = "rsi_state.json"
TD_URL     = "https://api.twelvedata.com/time_series"
LOCAL_TZ   = "America/Toronto"
LOCAL_TZ_LABEL = "ET"


# ── RSI calculation (Wilder RMA — matches Pine Script ta.rsi) ────────────────
def calc_rsi(closes, period=14):
    """
    Returns list of RSI values (same length as closes).
    Values are None during warmup period.
    Matches TradingView's ta.rsi() which uses Wilder RMA smoothing.
    """
    n = len(closes)
    if n < period + 1:
        return [None] * n

    gains  = [0.0]
    losses = [0.0]
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    rsi = [None] * n

    # Seed with simple average of first `period` bars
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    for i in range(period, n):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            rsi[i] = 100.0
        elif avg_gain == 0:
            rsi[i] = 0.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"!! {STATE_FILE} unreadable ({e}). Refusing to run.")
        sys.exit(1)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ── Data fetch ────────────────────────────────────────────────────────────────
def fetch_ohlc(td_symbol, interval):
    params = {
        "symbol":     td_symbol,
        "interval":   interval,
        "outputsize": OUTPUTSIZE,
        "order":      "ASC",
        "apikey":     TWELVE_DATA_KEY,
    }
    for attempt in range(1, 4):
        try:
            r = requests.get(TD_URL, params=params, timeout=25)
            if r.status_code == 429:
                wait = 20 * attempt
                print(f"[rate-limited, waiting {wait}s] ", end="", flush=True)
                time.sleep(wait)
                continue
            d = r.json()
            if isinstance(d, dict) and d.get("status") == "error":
                return None, None, d.get("message", "API error")
            vals = (d or {}).get("values")
            if not vals:
                return None, None, "no data"

            bars = []
            for v in vals:
                try:
                    bars.append({
                        "date": v["datetime"][:10],
                        "c":    float(v["close"]),
                    })
                except (KeyError, ValueError):
                    continue
            bars.sort(key=lambda b: b["date"])
            # de-duplicate dates
            dedup = {}
            for b in bars:
                dedup[b["date"]] = b
            bars = [dedup[k] for k in sorted(dedup)]
            return bars, (d.get("meta") or {}), None
        except Exception as e:
            if attempt < 3:
                time.sleep(4 * attempt)
    return None, None, "fetch failed"


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message, dry=False):
    if dry:
        print("\n--- telegram (dry-run) ---\n" + message + "\n--------------------------")
        return True
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT,
        "text":                     message,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                time.sleep(5 * attempt)
                continue
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
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
        return loc.strftime(f"%b %d, %Y · %I:%M %p {LOCAL_TZ_LABEL}")
    except Exception:
        return utc.strftime("%b %d, %Y · %H:%M UTC")


def fmt_price(p):
    if p is None:
        return "—"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.2f}"
    return f"${p:.4f}"


def format_alert(sym, name, narrative, tf_label, level, rsi_val, prev_rsi, price, bar_date):
    lvl = next((l for l in LEVELS if l["value"] == level), LEVELS[0])
    return (
        f"{lvl['emoji']} <b>RSI {lvl['label'].upper()} — {tf_label}</b>\n"
        f"<b>{sym}</b> · {name}\n"
        f"RSI crossed below <b>{level}</b> on {tf_label}\n"
        f"RSI: <b>{rsi_val:.1f}</b>  (prev bar: {prev_rsi:.1f})\n"
        f"Price: <b>{fmt_price(price)}</b>\n"
        f"Bar: {bar_date}\n"
        f"Narrative: {narrative}\n\n"
        f"⏰ {_stamp()}"
    )


def format_summary(alerts, errors, total):
    now = _stamp()
    if not alerts:
        return (
            f"📊 <b>RSI Scan Complete</b>\n"
            f"No RSI crosses detected · {total} symbols × 2 TFs scanned\n"
            f"⏰ {now}"
        )
    lines = [f"📊 <b>RSI Scan — {len(alerts)} cross(es)</b>", ""]
    for a in alerts:
        lvl = next((l for l in LEVELS if l["value"] == a["level"]), LEVELS[0])
        lines.append(
            f"{lvl['emoji']} {a['sym']} RSI<{a['level']} on {a['tf']}  "
            f"(RSI {a['rsi']:.1f})"
        )
    lines += ["", f"⏰ {now}"]
    return "\n".join(lines)


# ── Core evaluation ───────────────────────────────────────────────────────────
def evaluate(bars, td_symbol, meta, tf_id):
    """
    Returns list of triggered level dicts, or empty list.
    Each dict: {level, rsi_val, prev_rsi, price, bar_date}
    """
    dates  = [b["date"] for b in bars]
    closes = [b["c"]    for b in bars]

    idx = last_closed_index(dates, td_symbol, meta)
    if idx is None or idx < 1:
        return [], "no closed bar"

    closed_bars   = bars[:idx + 1]
    closed_closes = closes[:idx + 1]

    if len(closed_closes) < MIN_BARS:
        return [], f"only {len(closed_closes)} closed bars"

    rsi_vals = calc_rsi(closed_closes, RSI_PERIOD)

    cur_rsi  = rsi_vals[idx]
    prev_rsi = rsi_vals[idx - 1]

    if cur_rsi is None or prev_rsi is None:
        return [], "RSI not yet warmed up"

    bar_date = closed_bars[idx]["date"]
    price    = closed_closes[-1]

    triggered = []
    for lvl in LEVELS:
        threshold = lvl["value"]
        # Cross below: was >= threshold, now < threshold
        if prev_rsi >= threshold and cur_rsi < threshold:
            triggered.append({
                "level":    threshold,
                "label":    lvl["label"],
                "emoji":    lvl["emoji"],
                "rsi_val":  cur_rsi,
                "prev_rsi": prev_rsi,
                "price":    price,
                "bar_date": bar_date,
            })

    return triggered, None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print; no Telegram, no state write")
    ap.add_argument("--reseed", action="store_true",
                    help="adopt current RSI levels silently")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--timeframe", metavar="TF",
                    help="only scan this timeframe: 1day or 1week (default: both)")
    args = ap.parse_args()

    # Filter timeframes if --timeframe is specified
    global TIMEFRAMES
    if args.timeframe:
        TIMEFRAMES = [tf for tf in TIMEFRAMES if tf["id"] == args.timeframe]
        if not TIMEFRAMES:
            print(f"Unknown timeframe: {args.timeframe}. Use 1day or 1week.")
            return 1

    if not TWELVE_DATA_KEY:
        print("TWELVE_DATA_KEY is not set.")
        return 1

    with open("watchlist.json", "r") as f:
        watchlist = json.load(f)

    symbols = [(n, a) for n, assets in watchlist.items() for a in assets]
    started = datetime.now(timezone.utc)
    print(f"RSI Scan start {started.strftime('%Y-%m-%d %H:%M UTC')}"
          f"{'  [DRY RUN]' if args.dry_run else ''}")
    print(f"{len(symbols)} symbols × {len(TIMEFRAMES)} TFs = "
          f"{len(symbols) * len(TIMEFRAMES)} API calls this run\n")

    state   = load_state()
    alerts  = []
    errors  = []
    seeded  = []
    total   = 0

    try:
        for narrative, asset in symbols:
            sym  = asset["sym"]
            name = asset["name"]
            td   = asset.get("td", sym)

            for tf in TIMEFRAMES:
                tf_id    = tf["id"]
                tf_label = tf["label"]
                key      = f"{sym}:{tf_id}"

                print(f"  {sym:<8} {tf_label}", end=" ", flush=True)

                bars, meta, err = fetch_ohlc(td, tf["interval"])
                if err:
                    print(f"ERROR: {err}")
                    errors.append(f"{sym} {tf_label}: {err}")
                    time.sleep(DELAY_SEC)
                    continue

                total += 1
                triggered, why = evaluate(bars, td, meta, tf_id)

                if why:
                    print(f"SKIP: {why}")
                    time.sleep(DELAY_SEC)
                    continue

                prior = state.get(key, {})
                first_time = not prior

                if not triggered:
                    print("—")
                    # Update last seen RSI even when no cross
                    dates  = [b["date"] for b in bars]
                    closes = [b["c"]    for b in bars]
                    idx    = last_closed_index(dates, td, meta)
                    if idx is not None:
                        rsi_vals = calc_rsi(closes[:idx + 1], RSI_PERIOD)
                        cur_rsi  = rsi_vals[idx]
                        if cur_rsi is not None:
                            state[key] = {
                                **prior,
                                "last_rsi":  round(cur_rsi, 2),
                                "last_bar":  dates[idx],
                                "updated":   datetime.now(timezone.utc)
                                              .replace(microsecond=0).isoformat(),
                            }
                    time.sleep(DELAY_SEC)
                    continue

                for t in triggered:
                    alert_key = f"{t['level']}:{t['bar_date']}"
                    already   = prior.get("alerted", {})

                    if first_time or args.reseed:
                        print(f"  [seeded RSI<{t['level']} on {tf_label}]")
                        seeded.append(f"{sym} {tf_label} <{t['level']}")
                        state[key] = {
                            **prior,
                            "alerted": {**already, alert_key: True},
                            "last_rsi": round(t["rsi_val"], 2),
                            "last_bar": t["bar_date"],
                            "updated":  datetime.now(timezone.utc)
                                         .replace(microsecond=0).isoformat(),
                        }
                        continue

                    if already.get(alert_key):
                        print(f"  [already alerted RSI<{t['level']} {t['bar_date']}]")
                        continue

                    # Genuine new cross
                    print(f"  *** RSI<{t['level']} CROSS on {tf_label} "
                          f"(RSI {t['rsi_val']:.1f}) ***")
                    msg = format_alert(
                        sym, name, narrative, tf_label,
                        t["level"], t["rsi_val"], t["prev_rsi"],
                        t["price"], t["bar_date"]
                    )
                    if send_telegram(msg, dry=args.dry_run):
                        alerts.append({
                            "sym": sym, "tf": tf_label,
                            "level": t["level"], "rsi": t["rsi_val"],
                        })
                        state[key] = {
                            **prior,
                            "alerted": {**already, alert_key: True},
                            "last_rsi": round(t["rsi_val"], 2),
                            "last_bar": t["bar_date"],
                            "updated":  datetime.now(timezone.utc)
                                         .replace(microsecond=0).isoformat(),
                        }
                    else:
                        print(f"    (delivery failed; will retry next run)")

                time.sleep(DELAY_SEC)

    finally:
        if not args.dry_run:
            save_state(state)
            print(f"\nrsi_state.json written ({len(state)} entries)")

    if seeded:
        print(f"seeded (silent): {', '.join(seeded)}")

    if not args.no_summary and not args.reseed:
        if alerts or errors:
            send_telegram(
                format_summary(alerts, errors, total),
                dry=args.dry_run
            )

    took = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nDone in {took/60:.1f} min — {total} evaluated, "
          f"{len(alerts)} alerts, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
