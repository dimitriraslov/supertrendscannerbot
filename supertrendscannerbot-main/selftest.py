#!/usr/bin/env python3
"""
selftest.py — runs in CI before every scan.

These are the invariants whose violation produced the bad alerts in v1. If any
of them break, the workflow fails loudly instead of quietly messaging you
nonsense.
"""
import sys
from datetime import datetime, timedelta, timezone

from supertrend import supertrend, last_flip, all_flips, label, BULL, BEAR
from market_calendar import last_closed_index, is_crypto

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAIL.append(name)


def synth(n=300, seed=11):
    """Deterministic pseudo-random walk with trends, no numpy needed."""
    s, price, bars = seed, 100.0, []
    for i in range(n):
        s = (1103515245 * s + 12345) % (2 ** 31)
        r = (s / 2 ** 31) - 0.5
        drift = 0.35 if (i // 60) % 2 == 0 else -0.35
        price = max(1.0, price + drift + r * 2.5)
        s = (1103515245 * s + 12345) % (2 ** 31)
        w = abs((s / 2 ** 31)) * 1.5 + 0.2
        bars.append((price + w, price - w, price))
    return [b[0] for b in bars], [b[1] for b in bars], [b[2] for b in bars]


print("SuperTrend scanner self-test")
print("-" * 46)

H, L, C = synth()
st = supertrend(H, L, C, 10, 3.0)
d = st["dirs"]

# --- ATR is a proper Wilder RMA -----------------------------------------------
tr = [H[0] - L[0]] + [max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1]))
                      for i in range(1, len(C))]
exp_seed = sum(tr[:10]) / 10
check("ATR seeds with SMA of first 10 TRs", abs(st["atr"][9] - exp_seed) < 1e-9)
exp10 = (exp_seed * 9 + tr[10]) / 10
check("ATR recurses as Wilder RMA", abs(st["atr"][10] - exp10) < 1e-9)
check("ATR is warm-up-masked before the seed", st["atr"][8] is None)

# --- direction semantics -------------------------------------------------------
check("direction only ever BULL or BEAR",
      all(x in (BULL, BEAR) for x in d if x is not None))
check("indicator actually produces flips on trending data",
      len(all_flips(d)) >= 2, f"(got {len(all_flips(d))})")

# A flip to BULL requires the close to be above the final upper band.
ok = True
for i, nd in all_flips(d):
    if nd == BULL and not (C[i] > st["upper"][i] - 1e-9):
        ok = False
    if nd == BEAR and not (C[i] < st["lower"][i] + 1e-9):
        ok = False
check("every flip is a genuine band breach", ok)

# The trailing line must not loosen against the trend inside a leg.
ok = True
for i in range(11, len(C)):
    if d[i] == d[i-1] == BULL and st["trend"][i] < st["trend"][i-1] - 1e-9:
        ok = False
    if d[i] == d[i-1] == BEAR and st["trend"][i] > st["trend"][i-1] + 1e-9:
        ok = False
check("trailing stop ratchets, never loosens mid-trend", ok)

# --- last_flip agrees with all_flips ------------------------------------------
fi, fd = last_flip(d)
af = all_flips(d)
check("last_flip matches the final entry of all_flips",
      af and fi == af[-1][0] and fd == af[-1][1])
check("last_flip direction equals current direction", fd == d[-1])

# never-flipped series must report None rather than a bogus index
flat = supertrend([10 + i*0.01 for i in range(200)],
                  [9 + i*0.01 for i in range(200)],
                  [9.5 + i*0.01 for i in range(200)], 10, 3.0)
fi2, _ = last_flip(flat["dirs"])
check("a never-flipping series reports no flip index",
      fi2 is None or flat["dirs"][fi2] != flat["dirs"][fi2 - 1])

# --- window stability (EXP 6): >=250 bars must be flip-date stable ------------
wobble = 0
prev = None
for t in range(len(C) - 30, len(C)):
    w = slice(t - 250 + 1, t + 1)
    sub = supertrend(H[w], L[w], C[w], 10, 3.0)
    j, _ = last_flip(sub["dirs"])
    abs_idx = (t - 250 + 1 + j) if j is not None else None
    if prev is not None and abs_idx != prev and d[t] == d[t-1]:
        wobble += 1
    prev = abs_idx
check("250-bar window reports a stable flip bar", wobble == 0,
      f"(wobbles={wobble})")

# --- candle-close gate: THE core fix ------------------------------------------
print("-" * 46)
dates = ["2026-07-24", "2026-07-27", "2026-07-28", "2026-07-29"]
eq_meta = {"exchange_timezone": "America/New_York"}

midday = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)     # 12:00 local ET
check("equity: today's bar EXCLUDED during the session",
      last_closed_index(dates, "NVDA", eq_meta, now=midday) == 2)

after = datetime(2026, 7, 29, 16, 50, tzinfo=timezone.utc)     # 16:50 local ET
check("equity: today's bar INCLUDED after close + settle buffer",
      last_closed_index(dates, "NVDA", eq_meta, now=after) == 3)

edge = datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc)      # inside buffer
check("equity: bar still excluded inside the settle buffer",
      last_closed_index(dates, "NVDA", eq_meta, now=edge) == 2)

cr_meta = {"exchange_timezone": "UTC", "type": "Digital Currency"}
anytime = datetime(2026, 7, 29, 23, 59, tzinfo=timezone.utc)
check("crypto: today's UTC bar is never treated as closed",
      last_closed_index(dates, "BTC/USD", cr_meta, now=anytime) == 2)

check("crypto detection via symbol", is_crypto("BTC/USD"))
check("crypto detection via meta type", is_crypto("BTCUSD", cr_meta))
check("equity not misdetected as crypto", not is_crypto("NVDA", eq_meta))

check("empty date list is handled", last_closed_index([], "NVDA", eq_meta) is None)
check("a single forming bar yields no closed bar",
      last_closed_index(["2026-07-29"], "NVDA", eq_meta, now=midday) is None)

print("-" * 46)
if FAIL:
    print(f"{len(FAIL)} CHECK(S) FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("All checks passed.")
