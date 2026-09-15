"""Conservative close gates for US/Canadian equities and UTC crypto.

Weekly bars are assigned to their Monday-start week. Equity weeks are trusted
Friday 16:45 exchange time; crypto weeks on the next Monday 00:45 UTC.
Holidays/half days may delay acceptance, never advance it. This is not a full
international exchange calendar; configure session_close for other markets.
"""
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

SETTLE_BUFFER_MIN = 45


def is_crypto(td_symbol, meta=None):
    meta = meta or {}
    if 'asset_type' in meta:
        return meta['asset_type'] == 'crypto'
    return '/' in (td_symbol or '') or any(s in str(meta.get('type', '')).lower() for s in ('digital', 'crypto'))


def exchange_tz(td_symbol, meta=None):
    if is_crypto(td_symbol, meta):
        return 'UTC'
    name = (meta or {}).get('exchange_timezone', 'America/New_York')
    try:
        ZoneInfo(name)
    except (KeyError, ValueError):
        name = 'America/New_York'
    return name


def close_at(bar_date, td_symbol, meta=None, timeframe='1day'):
    meta = meta or {}
    day = date.fromisoformat(bar_date[:10])
    tz = ZoneInfo(exchange_tz(td_symbol, meta))
    crypto = is_crypto(td_symbol, meta)
    if timeframe == '1week':
        monday = day - timedelta(days=day.weekday())
        day = monday + timedelta(days=7 if crypto else 4)
    elif timeframe == '1day':
        if crypto:
            day += timedelta(days=1)
    else:
        raise ValueError('Unsupported timeframe: ' + timeframe)
    h, m = (0, 0) if crypto else tuple(map(int, meta.get('session_close', '16:00').split(':')))
    return datetime.combine(day, time(h, m), tz) + timedelta(minutes=SETTLE_BUFFER_MIN)


def last_closed_index(dates, td_symbol, meta=None, now=None, timeframe='1day'):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(exchange_tz(td_symbol, meta)))
    for i in range(len(dates) - 1, -1, -1):
        if close_at(dates[i], td_symbol, meta, timeframe) <= local:
            return i
    return None


def closed_index(dates, td_symbol, meta=None, now=None, timeframe='1day'):
    """Production entry: now is an actual instant, converted to exchange time."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(exchange_tz(td_symbol, meta)))
    return last_closed_index(dates, td_symbol, meta, local, timeframe)


def latest_week_key(td_symbol, meta=None, now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(exchange_tz(td_symbol, meta)))
    monday = local.date() - timedelta(days=local.weekday())
    if close_at(monday.isoformat(), td_symbol, meta, '1week') > local:
        monday -= timedelta(days=7)
    return monday.isoformat()


def stale_bar(bar_date, td_symbol, meta=None, timeframe='1day', now=None):
    if not bar_date:
        return True
    now = now or datetime.now(timezone.utc)
    allowance = 2 if is_crypto(td_symbol, meta) else 4
    if timeframe == '1week':
        allowance = 8
    return now - close_at(bar_date, td_symbol, meta, timeframe) > timedelta(days=allowance)


def describe(td_symbol, meta=None):
    return exchange_tz(td_symbol, meta)
