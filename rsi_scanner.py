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



# ── RSI Divergence Detection ──────────────────────────────────────────────────
# Divergence detection parameters — based on professional TradingView standard
PIVOT_LEFT    = 5    # bars to LEFT of pivot required to confirm swing (TradingView standard)
PIVOT_RIGHT   = 5    # bars to RIGHT of pivot required to confirm swing
MIN_PIVOT_GAP = 5    # minimum bars between two swing points (avoid fake close divergences)
MAX_PIVOT_GAP = 50   # maximum bars between two swing points (5-50 is professional standard)
FRESH_BARS    = 5    # most recent swing must be within this many bars of last close bar
MIN_RSI_DIFF  = 3.0  # minimum RSI difference between swings (3pts filters noise better than 2)
RSI_BULL_ZONE = 40   # bullish divergence: both RSI lows must be BELOW this (oversold zone)
RSI_BEAR_ZONE = 60   # bearish divergence: both RSI highs must be ABOVE this (overbought zone)
LOOKBACK      = 60   # total bars to search back for swing points

def find_swings(values, left=PIVOT_LEFT, right=PIVOT_RIGHT, lookback=LOOKBACK):
    """
    Find swing highs and lows using professional pivot detection.
    Standard: 5 bars left, 5 bars right (TradingView RSI divergence standard).

    A swing low:  values[i] is strictly less than ALL values in [i-left:i]
                  AND strictly less than ALL values in [i+1:i+right+1]
    A swing high: same logic but strictly greater.

    Uses CLOSING prices only — mixing wick-based lows on price with
    close-based RSI lows manufactures false divergences.

    Returns list of (index, value, type) sorted oldest to newest.
    """
    length = len(values)
    start  = max(left, length - lookback - right)
    swings = []
    for i in range(start, length - right):
        v = values[i]
        if v is None:
            continue
        left_bars  = values[max(0, i - left):i]
        right_bars = values[i + 1:i + right + 1]
        if len(left_bars) < left or len(right_bars) < right:
            continue
        if any(x is None for x in left_bars + right_bars):
            continue
        if v < min(left_bars) and v < min(right_bars):
            swings.append((i, v, "low"))
        elif v > max(left_bars) and v > max(right_bars):
            swings.append((i, v, "high"))
    return swings


def detect_divergence(closes, rsi_vals, idx):
    """
    Detect regular bullish or bearish RSI divergence.

    Uses professional TradingView standard parameters:
      - 5 bars left / 5 bars right for pivot confirmation
      - 5-50 bars between pivot points (not too close, not too stale)
      - RSI zone filters: bullish only below 40, bearish only above 60
      - Minimum 3 RSI points difference to filter noise
      - Freshness: most recent pivot within 5 bars of last closed bar

    Closes only — no wick mixing.

    BULLISH divergence (reversal signal — buy opportunity):
      Price: lower low (2nd swing low < 1st swing low)
      RSI:   higher low (RSI at 2nd swing > RSI at 1st swing)
      Zone:  both RSI values below 40 (genuine oversold)

    BEARISH divergence (reversal signal — sell opportunity):
      Price: higher high (2nd swing high > 1st swing high)
      RSI:   lower high  (RSI at 2nd swing < RSI at 1st swing)
      Zone:  both RSI values above 60 (genuine overbought)

    Returns list of divergence dicts.
    """
    min_bars_needed = PIVOT_LEFT + PIVOT_RIGHT + MIN_PIVOT_GAP + 5
    if idx < min_bars_needed:
        return []

    closed_closes = closes[:idx + 1]
    closed_rsi    = [r for r in rsi_vals[:idx + 1]]

    # Detect swings using 5L/5R professional standard
    price_swings = find_swings(closed_closes)
    rsi_swings   = find_swings(closed_rsi)

    results = []

    # ── BULLISH DIVERGENCE: lower price lows + higher RSI lows ───────────────
    price_lows = [(i, v) for i, v, t in price_swings if t == "low"]
    rsi_lows   = [(i, v) for i, v, t in rsi_swings   if t == "low"]

    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        pl2_i, pl2_v = price_lows[-1]   # most recent price swing low
        pl1_i, pl1_v = price_lows[-2]   # previous price swing low

        pivot_gap = pl2_i - pl1_i
        fresh     = (idx - pl2_i) <= FRESH_BARS

        if MIN_PIVOT_GAP <= pivot_gap <= MAX_PIVOT_GAP and fresh:
            # Find RSI swing lows closest to each price swing low (within 5 bars)
            rl2 = next(((i,v) for i,v in reversed(rsi_lows) if abs(i-pl2_i)<=5), None)
            rl1 = next(((i,v) for i,v in reversed(rsi_lows)
                        if abs(i-pl1_i)<=5 and i < (rl2[0] if rl2 else idx)), None)

            if rl2 and rl1:
                price_lower_low  = pl2_v < pl1_v          # price: lower low ✓
                rsi_higher_low   = rl2[1] > rl1[1]        # RSI: higher low ✓
                rsi_diff         = rl2[1] - rl1[1]
                both_in_zone     = rl2[1] < RSI_BULL_ZONE and rl1[1] < RSI_BULL_ZONE
                strong_enough    = rsi_diff >= MIN_RSI_DIFF

                if price_lower_low and rsi_higher_low and both_in_zone and strong_enough:
                    strength = "STRONG" if rsi_diff >= 6 else "MODERATE"
                    results.append({
                        "type":         "bullish",
                        "strength":     strength,
                        "rsi_diff":     round(rsi_diff, 1),
                        "price_low1":   round(pl1_v, 4),
                        "price_low2":   round(pl2_v, 4),
                        "rsi_low1":     round(rl1[1], 1),
                        "rsi_low2":     round(rl2[1], 1),
                        "pivot_gap":    pivot_gap,
                        "bar_date_idx": pl2_i,
                    })

    # ── BEARISH DIVERGENCE: higher price highs + lower RSI highs ─────────────
    price_highs = [(i, v) for i, v, t in price_swings if t == "high"]
    rsi_highs   = [(i, v) for i, v, t in rsi_swings   if t == "high"]

    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        ph2_i, ph2_v = price_highs[-1]
        ph1_i, ph1_v = price_highs[-2]

        pivot_gap = ph2_i - ph1_i
        fresh     = (idx - ph2_i) <= FRESH_BARS

        if MIN_PIVOT_GAP <= pivot_gap <= MAX_PIVOT_GAP and fresh:
            rh2 = next(((i,v) for i,v in reversed(rsi_highs) if abs(i-ph2_i)<=5), None)
            rh1 = next(((i,v) for i,v in reversed(rsi_highs)
                        if abs(i-ph1_i)<=5 and i < (rh2[0] if rh2 else idx)), None)

            if rh2 and rh1:
                price_higher_high = ph2_v > ph1_v
                rsi_lower_high    = rh2[1] < rh1[1]
                rsi_diff          = rh1[1] - rh2[1]
                both_in_zone      = rh2[1] > RSI_BEAR_ZONE and rh1[1] > RSI_BEAR_ZONE
                strong_enough     = rsi_diff >= MIN_RSI_DIFF

                if price_higher_high and rsi_lower_high and both_in_zone and strong_enough:
                    strength = "STRONG" if rsi_diff >= 6 else "MODERATE"
                    results.append({
                        "type":          "bearish",
                        "strength":      strength,
                        "rsi_diff":      round(rsi_diff, 1),
                        "price_high1":   round(ph1_v, 4),
                        "price_high2":   round(ph2_v, 4),
                        "rsi_high1":     round(rh1[1], 1),
                        "rsi_high2":     round(rh2[1], 1),
                        "pivot_gap":     pivot_gap,
                        "bar_date_idx":  ph2_i,
                    })

    return results

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # State file corrupted — likely from overlapping git pushes.
        # Self-repair: reset and reseed silently instead of crashing.
        print(f"!! {STATE_FILE} unreadable ({e}).")
        print("!! Auto-repairing: resetting to {} and reseeding silently.")
        print("!! No RSI alerts will fire this run.")
        try:
            import shutil
            shutil.copy(STATE_FILE, STATE_FILE + ".corrupt")
        except Exception:
            pass
        try:
            with open(STATE_FILE, "w") as f:
                f.write("{}")
        except Exception:
            pass
        return {}


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



def format_div_alert(sym, name, narrative, tf_label, div, price, bar_date):
    if div["type"] == "bullish":
        return (
            f"📈 <b>BULLISH DIVERGENCE — {tf_label}</b>\n"
            f"<b>{sym}</b> · {name}\n"
            f"Price: lower low  ({fmt_price(div['price_low1'])} → {fmt_price(div['price_low2'])})\n"
            f"RSI:   higher low ({div['rsi_low1']} → {div['rsi_low2']}) ↑{div['rsi_diff']}pts\n"
            f"Strength: <b>{div['strength']}</b>\n"
            f"Current price: {fmt_price(price)} · Bar: {bar_date}\n"
            f"Narrative: {narrative}\n\n"
            f"⏰ {_stamp()}"
        )
    else:
        return (
            f"📉 <b>BEARISH DIVERGENCE — {tf_label}</b>\n"
            f"<b>{sym}</b> · {name}\n"
            f"Price: higher high ({fmt_price(div['price_high1'])} → {fmt_price(div['price_high2'])})\n"
            f"RSI:   lower high  ({div['rsi_high1']} → {div['rsi_high2']}) ↓{div['rsi_diff']}pts\n"
            f"Strength: <b>{div['strength']}</b>\n"
            f"Current price: {fmt_price(price)} · Bar: {bar_date}\n"
            f"Narrative: {narrative}\n\n"
            f"⏰ {_stamp()}"
        )

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

                # ── Divergence detection (uses same fetched bars, 0 extra credits) ──
                div_key_prefix = f"{sym}:{tf_id}:div"
                dates_all  = [b["date"] for b in bars]
                closes_all = [b["c"]    for b in bars]
                idx_all    = last_closed_index(dates_all, td, meta)
                if idx_all is not None and idx_all >= SWING_N * 2 + 5:
                    rsi_all = calc_rsi(closes_all[:idx_all + 1], RSI_PERIOD)
                    divs    = detect_divergence(closes_all, rsi_all, idx_all)
                    for div in divs:
                        bar_date = dates_all[div["bar_date_idx"]] if div["bar_date_idx"] < len(dates_all) else dates_all[idx_all]
                        div_alert_key = f"{div['type']}:{bar_date}"
                        div_state_key = div_key_prefix
                        div_prior     = state.get(div_state_key, {})
                        already_div   = div_prior.get("alerted", {})

                        if first_time or args.reseed:
                            state[div_state_key] = {**div_prior, "alerted": {**already_div, div_alert_key: True}}
                            continue

                        if already_div.get(div_alert_key):
                            continue

                        # Genuine new divergence — alert once
                        cur_price = closes_all[idx_all]
                        print(f"  *** {div['type'].upper()} DIVERGENCE {div['strength']} on {tf_label} ***")
                        msg = format_div_alert(sym, name, narrative, tf_label, div, cur_price, bar_date)
                        if send_telegram(msg, dry=args.dry_run):
                            alerts.append({"sym": sym, "tf": tf_label, "level": f"DIV_{div['type'].upper()}", "rsi": 0})
                            state[div_state_key] = {**div_prior, "alerted": {**already_div, div_alert_key: True},
                                                    "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}

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
