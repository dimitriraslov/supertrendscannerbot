"""
Supertrend 1D Flip Scanner
Runs every 4 hours via GitHub Actions.
Sends Telegram alert when any ticker flips bull or bear on the daily.
"""

import os
import json
import time
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")

ATR_PERIOD  = 10
ST_FACTOR   = 3.0
OUTPUTSIZE  = 60   # 60 daily candles — plenty for Supertrend warmup
DELAY_SEC   = 8    # seconds between API calls — respects 8/min rate limit

STATE_FILE  = "state.json"

# ── Watchlist ─────────────────────────────────────────────────────────────────
def load_watchlist():
    with open("watchlist.json", "r") as f:
        return json.load(f)

# ── State (last known signal per ticker) ─────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Supertrend math ───────────────────────────────────────────────────────────
def calc_supertrend(highs, lows, closes):
    n = len(closes)
    tr  = [highs[0] - lows[0]]
    atr = [0.0] * n

    for i in range(1, n):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        ))

    # Wilder RMA
    seed = sum(tr[:ATR_PERIOD]) / ATR_PERIOD
    atr[ATR_PERIOD - 1] = seed
    for i in range(ATR_PERIOD, n):
        atr[i] = (atr[i-1] * (ATR_PERIOD - 1) + tr[i]) / ATR_PERIOD

    fu  = [0.0] * n   # final upper band
    fl  = [0.0] * n   # final lower band
    dir = [1]   * n   # 1 = bear, -1 = bull

    for i in range(ATR_PERIOD - 1, n):
        hl2 = (highs[i] + lows[i]) / 2
        bu  = hl2 + ST_FACTOR * atr[i]
        bl  = hl2 - ST_FACTOR * atr[i]

        if i == ATR_PERIOD - 1:
            fu[i] = bu
            fl[i] = bl
            dir[i] = -1 if closes[i] > bu else 1
            continue

        fu[i] = min(bu, fu[i-1]) if closes[i-1] <= fu[i-1] else bu
        fl[i] = max(bl, fl[i-1]) if closes[i-1] >= fl[i-1] else bl

        if dir[i-1] == 1:
            dir[i] = -1 if closes[i] > fu[i] else 1
        else:
            dir[i] =  1 if closes[i] < fl[i] else -1

    # Return current and previous direction
    return dir[-1], dir[-2]

# ── Fetch OHLC from Twelve Data ───────────────────────────────────────────────
def fetch_ohlc(symbol):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={requests.utils.quote(symbol)}"
        f"&interval=1day"
        f"&outputsize={OUTPUTSIZE}"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        d = r.json()
        if d.get("status") == "error":
            return None, d.get("message", "API error")
        if not d.get("values"):
            return None, "No data"

        vals   = list(reversed(d["values"]))  # oldest first
        highs  = [float(v["high"])  for v in vals]
        lows   = [float(v["low"])   for v in vals]
        closes = [float(v["close"]) for v in vals]
        price  = closes[-1]
        return (highs, lows, closes, price), None
    except Exception as e:
        return None, str(e)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("Telegram not configured — skipping")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram exception: {e}")

def format_alert(sym, name, narrative, old_dir, new_dir, price):
    arrow  = "🟢" if new_dir == "bull" else "🔴"
    action = "BULL" if new_dir == "bull" else "BEAR"
    from_s = "BEAR" if new_dir == "bull" else "BULL"
    now    = datetime.utcnow().strftime("%b %d, %Y · %H:%M UTC")

    return (
        f"{arrow} <b>SUPERTREND FLIP — {action}</b>\n"
        f"<b>{sym}</b> · {name}\n"
        f"1D Supertrend flipped {from_s} → {action}\n"
        f"Price: <b>${price:,.2f}</b>\n"
        f"Narrative: {narrative}\n\n"
        f"⏰ {now}"
    )

def format_summary(flips, errors, total):
    now = datetime.utcnow().strftime("%b %d, %Y · %H:%M UTC")
    if not flips:
        return (
            f"📊 <b>Supertrend Scan Complete</b>\n"
            f"No flips detected · {total} assets scanned\n"
            f"⏰ {now}"
        )
    lines = [f"📊 <b>Supertrend Scan — {len(flips)} flip(s)</b>", ""]
    for f in flips:
        arrow = "🟢" if f["new"] == "bull" else "🔴"
        lines.append(f"{arrow} {f['sym']} → {f['new'].upper()}")
    lines += ["", f"⏰ {now}"]
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"Starting scan — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    watchlist = load_watchlist()
    state     = load_state()
    flips     = []
    errors    = []
    total     = 0

    for narrative, assets in watchlist.items():
        for asset in assets:
            sym  = asset["sym"]
            name = asset["name"]
            td   = asset.get("td", sym)  # Twelve Data symbol (may differ e.g. BTC/USD)

            print(f"  Checking {sym}...", end=" ")

            data, err = fetch_ohlc(td)
            if err:
                print(f"ERROR: {err}")
                errors.append(f"{sym}: {err}")
                time.sleep(DELAY_SEC)
                continue

            highs, lows, closes, price = data
            total += 1

            try:
                cur_dir, prev_dir = calc_supertrend(highs, lows, closes)
                cur_sig  = "bull" if cur_dir  == -1 else "bear"
                prev_sig = "bull" if prev_dir == -1 else "bear"
            except Exception as e:
                print(f"CALC ERROR: {e}")
                errors.append(f"{sym}: calc error {e}")
                time.sleep(DELAY_SEC)
                continue

            # Check against stored state (not just previous bar)
            stored_sig = state.get(sym, {}).get("signal")

            print(f"{cur_sig.upper()} (prev bar: {prev_sig.upper()}, stored: {stored_sig or 'none'})")

            # Flip detected = current signal differs from last stored signal
            if stored_sig and stored_sig != cur_sig:
                print(f"    *** FLIP DETECTED: {stored_sig.upper()} → {cur_sig.upper()} ***")
                flips.append({
                    "sym": sym, "name": name, "narrative": narrative,
                    "old": stored_sig, "new": cur_sig, "price": price
                })
                # Send individual alert immediately
                msg = format_alert(sym, name, narrative, stored_sig, cur_sig, price)
                send_telegram(msg)

            # Update state
            state[sym] = {
                "signal": cur_sig,
                "price":  price,
                "updated": datetime.utcnow().isoformat()
            }

            time.sleep(DELAY_SEC)

    # Save updated state
    save_state(state)

    # Send summary (only if there were flips or errors)
    if flips or errors:
        summary = format_summary(flips, errors, total)
        send_telegram(summary)

    print(f"\nDone — {total} assets, {len(flips)} flips, {len(errors)} errors")

if __name__ == "__main__":
    main()
