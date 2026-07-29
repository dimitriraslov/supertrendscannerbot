"""
market_calendar.py — decide whether a daily candle is actually CLOSED.

This is the single most important fix in the rewrite. Twelve Data's
`/time_series?interval=1day` returns the CURRENT, STILL-FORMING day as the most
recent value while the market is open (the free plan includes real-time US
equity, forex and crypto data). The original scanner fed that live candle
straight into SuperTrend, so the indicator repainted intraday: a wick could push
price through a band, fire an alert, and then close back on the original side.

A daily SuperTrend value is only final once the daily bar has closed. Everything
here exists to guarantee we never evaluate an unfinished bar.
"""

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _HAVE_TZ = True
except Exception:                                     # pragma: no cover
    _HAVE_TZ = False

# Minutes to wait after the official close before trusting today's bar.
# Absorbs late/consolidated prints that can still nudge the day's high/low.
SETTLE_BUFFER_MIN = 45

# Regular-session close, local exchange time.
EQUITY_CLOSE = (16, 0)

_FALLBACK_OFFSETS = {          # used only if zoneinfo is unavailable
    "America/New_York": -4,
    "America/Toronto": -4,
    "UTC": 0,
}


def _now_in(tzname):
    if _HAVE_TZ:
        try:
            return datetime.now(ZoneInfo(tzname))
        except Exception:
            pass
    off = _FALLBACK_OFFSETS.get(tzname, 0)
    return datetime.now(timezone.utc) + timedelta(hours=off)


def is_crypto(td_symbol, meta=None):
    if "/" in (td_symbol or ""):
        return True
    t = ((meta or {}).get("type") or "").lower()
    return "digital" in t or "crypto" in t


def exchange_tz(td_symbol, meta=None):
    """Prefer the timezone the provider reports; fall back sensibly."""
    tz = (meta or {}).get("exchange_timezone")
    if tz and (not _HAVE_TZ or _tz_ok(tz)):
        return tz
    return "UTC" if is_crypto(td_symbol, meta) else "America/New_York"


def _tz_ok(tz):
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def last_closed_index(dates, td_symbol, meta=None, now=None):
    """
    Given ascending 'YYYY-MM-DD' bar dates, return the index of the newest bar
    that is definitely CLOSED, or None if none qualify.

    Rules
    -----
    crypto (24/7)  : the bar dated 'today in UTC' is always still forming.
    equities/ETFs  : a bar dated before today (exchange tz) is closed. Today's
                     bar counts as closed only once we are past
                     16:00 + SETTLE_BUFFER_MIN local time. The generous buffer
                     also covers 13:00 half-day closes.
    """
    if not dates:
        return None

    crypto = is_crypto(td_symbol, meta)
    tzname = "UTC" if crypto else exchange_tz(td_symbol, meta)
    local = now or _now_in(tzname)
    today = local.strftime("%Y-%m-%d")

    if crypto:
        session_done = False        # UTC day only completes at 00:00 next day
    else:
        cutoff = local.replace(hour=EQUITY_CLOSE[0], minute=EQUITY_CLOSE[1],
                               second=0, microsecond=0) \
                 + timedelta(minutes=SETTLE_BUFFER_MIN)
        session_done = local >= cutoff

    for i in range(len(dates) - 1, -1, -1):
        d = dates[i]
        if d < today:
            return i
        if d == today and session_done:
            return i
        # d == today and session still running -> skip this forming bar
        # d  > today (stale clock / provider quirk) -> skip
    return None


def describe(td_symbol, meta=None):
    tzname = "UTC" if is_crypto(td_symbol, meta) else exchange_tz(td_symbol, meta)
    return f"{tzname}{' (24/7)' if is_crypto(td_symbol, meta) else ''}"
