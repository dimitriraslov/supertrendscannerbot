"""
supertrend.py — TradingView-equivalent SuperTrend, with explicit flip metadata.

Why this module exists separately:
  The original scanner returned only (current_dir, previous_dir) and decided
  "is this a flip?" by diffing against a stored string in state.json. That made
  it impossible to know HOW OLD a trend was, so any state desync surfaced as a
  brand-new "fresh flip" alert. This module returns the full direction series
  plus the index/date of the last direction change, so freshness is a measured
  fact rather than an assumption.

Parity notes (matches Pine Script ta.supertrend):
  * ATR uses Wilder RMA seeded with the SMA of the first `period` true ranges.
  * Band memory:  upper = (basic < prev) or (close[1] > prev) ? basic : prev
                  lower = (basic > prev) or (close[1] < prev) ? basic : prev
  * Direction is decided against whichever band the trailing stop currently
    sits on — not against a bare `dir[i-1]` test.
  * Direction seeds to 1 (downtrend) on the first computable bar, exactly as
    Pine does, and converges within a few dozen bars. We feed it far more
    history than that (see MIN_BARS / TARGET_BARS in scanner.py).
"""

BULL = -1   # Pine convention: direction -1 == uptrend
BEAR = 1    # direction +1 == downtrend


def supertrend(highs, lows, closes, period=10, factor=3.0):
    """
    Compute SuperTrend over the whole series.

    Returns dict with:
      dirs   : list[int|None]  direction per bar (BULL/BEAR), None during warmup
      trend  : list[float|None] the trailing SuperTrend line
      upper  : list[float|None] final upper band
      lower  : list[float|None] final lower band
      atr    : list[float|None]
    """
    n = len(closes)
    if n < period + 1:
        raise ValueError(f"need at least {period + 1} bars, got {n}")

    # --- True Range ---
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))

    # --- Wilder RMA (== Pine ta.atr) ---
    atr = [None] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    upper = [None] * n
    lower = [None] * n
    dirs = [None] * n
    trend = [None] * n

    for i in range(period - 1, n):
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_u = hl2 + factor * atr[i]
        basic_l = hl2 - factor * atr[i]

        if i == period - 1:
            upper[i], lower[i] = basic_u, basic_l
            dirs[i] = BEAR          # Pine seeds direction = 1
            trend[i] = upper[i]
            continue

        pu, pl = upper[i - 1], lower[i - 1]
        upper[i] = basic_u if (basic_u < pu or closes[i - 1] > pu) else pu
        lower[i] = basic_l if (basic_l > pl or closes[i - 1] < pl) else pl

        # Which band was the trailing stop on last bar?
        if trend[i - 1] == pu:
            dirs[i] = BULL if closes[i] > upper[i] else BEAR
        else:
            dirs[i] = BEAR if closes[i] < lower[i] else BULL

        trend[i] = upper[i] if dirs[i] == BEAR else lower[i]

    return {"dirs": dirs, "trend": trend, "upper": upper,
            "lower": lower, "atr": atr}


def last_flip(dirs):
    """
    Index of the most recent direction change.

    Returns (flip_index, direction) or (None, current_direction) when the
    direction has never changed across the supplied history.
    """
    idxs = [i for i, d in enumerate(dirs) if d is not None]
    if len(idxs) < 2:
        return None, (dirs[idxs[-1]] if idxs else None)
    for i in range(idxs[-1], idxs[0], -1):
        if dirs[i] != dirs[i - 1]:
            return i, dirs[i]
    return None, dirs[idxs[-1]]


def all_flips(dirs):
    """Every direction change as (index, new_direction) — used by --verify."""
    out = []
    prev = None
    for i, d in enumerate(dirs):
        if d is None:
            continue
        if prev is not None and d != prev:
            out.append((i, d))
        prev = d
    return out


def label(direction):
    return "bull" if direction == BULL else "bear"
