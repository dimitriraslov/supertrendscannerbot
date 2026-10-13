#!/usr/bin/env python3
"""Supertrend scanner v4: one candle stream, one event ledger, two outputs."""
import argparse
import copy
import csv
import html
import io
import json
import math
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import urllib.request
import urllib.parse
import urllib.error
from market_calendar import closed_index, close_at, is_crypto, latest_week_key, stale_bar
from signals import calculate, events_at
from supertrend import last_flip, label

VERSION = '4.0.0'
STATE_FILE = 'scanner_state.json'
PRIORITY_FILE = 'scan_priority.json'
MIN_BARS = 120
HISTORY = 400
FRESH_BARS = 1
TD_ATTEMPTS = 6
FALLBACK_GAP = 1.5
# Bases quoted in USD by Yahoo (-USD) and Stooq (no separator). Twelve Data uses BASE/USD.
CRYPTO_BASES = {'BTC', 'ETH', 'SOL', 'BNB', 'ONDO', 'LINK', 'TAO'}
_fallback_last = [0.0]


def http_json(url, params=None, payload=None, timeout=30):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'SupertrendScanner/4'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = {'message': 'HTTP ' + str(exc.code)}
        return exc.code, body


def _header_map(headers):
    return {str(k).lower(): v for k, v in (headers.items() if headers is not None else [])}


def http_json_ex(url, params=None, payload=None, timeout=30):
    """http_json plus response headers, so credit limits can be read from the plan."""
    if params:
        url += '?' + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json', 'User-Agent': 'SupertrendScanner/4'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status, headers = response.status, _header_map(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status, headers = exc.code, _header_map(exc.headers)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {'message': 'HTTP ' + str(status)}
    return status, body, headers


def http_bytes(url, tries=5, timeout=20):
    """GET bytes with the fallback reader's throttle and backoff (Yahoo, then Stooq)."""
    err = None
    for k in range(tries):
        wait = FALLBACK_GAP - (time.monotonic() - _fallback_last[0])
        if wait > 0:
            time.sleep(wait)
        _fallback_last[0] = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            err = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except OSError as exc:
            err = exc
        time.sleep(min(60, 2 * 2 ** k))
    raise RuntimeError(f'gave up after {tries} tries: {err}')


def yahoo_symbol(td_symbol):
    symbol = td_symbol or ''
    if '/' in symbol:
        base, quote = symbol.split('/', 1)
        if quote.upper() in ('USD', 'USDT'):
            return base.upper() + '-USD'
        return symbol.replace('/', '-')
    if symbol.upper() in CRYPTO_BASES:
        return symbol.upper() + '-USD'
    return symbol


def stooq_symbol(td_symbol):
    symbol = td_symbol or ''
    if '/' in symbol:
        base, quote = symbol.split('/', 1)
        return (base + quote).lower()
    if symbol.upper() in CRYPTO_BASES:
        return symbol.lower() + 'usd'
    return symbol.lower() + '.us'


def _as_response(result):
    if isinstance(result, tuple) and len(result) >= 3:
        status, data, headers = result[0], result[1], result[2]
    elif isinstance(result, tuple) and len(result) == 2:
        status, data, headers = result[0], result[1], {}
    else:
        raise ValueError('Unexpected market data response')
    return status, data, _header_map(headers)


def _clean_bars(bars, skip_invalid=False):
    dedup = {}
    for bar in bars:
        invalid = not all(math.isfinite(bar[k]) for k in ('h', 'l', 'c')) or not 0 < bar['l'] <= bar['c'] <= bar['h']
        if invalid:
            if skip_invalid:
                continue
            raise ValueError('Invalid OHLC values returned')
        datetime.fromisoformat(bar['date'])
        dedup[bar['date']] = bar
    if not dedup:
        raise ValueError('No price data returned')
    return [dedup[d] for d in sorted(dedup)]


def _finalize_bars(bars, td_symbol, meta, timeframe):
    """Drop the forming bar, then keep the same window Twelve Data is asked for."""
    now = datetime.now(timezone.utc)
    idx = closed_index([b['date'] for b in bars], td_symbol, meta, now, timeframe)
    if idx is None:
        raise RuntimeError('No closed candle available')
    closed = bars[:idx + 1]
    if len(closed) > HISTORY:
        closed = closed[-HISTORY:]
    if len(closed) < MIN_BARS:
        raise RuntimeError(f'only {len(closed)} closed bars')
    return closed


def _limit_kind(status, data):
    """'minute', 'daily', or None. Twelve Data uses 429 for both credit windows."""
    message = str(data.get('message', '')).lower() if isinstance(data, dict) else ''
    code = data.get('code', status) if isinstance(data, dict) else status
    limited = status == 429 or code == 429 or code == '429' or ('credit' in message and ('limit' in message or 'run out' in message))
    if not limited:
        return None
    if 'day' in message or 'daily' in message:
        return 'daily'
    return 'minute'


def _header_int(headers, name):
    raw = headers.get(name)
    if raw is None or raw == '':
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def load_scan_priority(path=PRIORITY_FILE):
    if not Path(path).exists():
        return {'held': [], 'watch': [], 'symbols': {}}
    data = read_json(path)
    symbols = data.get('symbols')
    return {'held': [str(s) for s in data.get('held') or []],
            'watch': [str(s) for s in data.get('watch') or []],
            'symbols': symbols if isinstance(symbols, dict) else {}}


def _asset_from_spec(sym, spec):
    asset = {'sym': sym, 'name': spec.get('name', sym), 'td': spec.get('td', sym),
             'narratives': list(spec.get('narratives') or ['Held'])}
    for key in ('asset_type', 'exchange_timezone', 'session_close'):
        if key in spec:
            asset[key] = spec[key]
    return asset


def order_assets(assets, priority=None):
    """Held names, then the watch list, then every other watchlist symbol."""
    priority = priority or {'held': [], 'watch': [], 'symbols': {}}
    by_sym = {asset['sym']: asset for asset in assets}
    extras = priority.get('symbols') or {}
    ordered, seen = [], set()

    def add(sym):
        if sym in seen:
            return
        asset = by_sym.get(sym)
        if asset is None and sym in extras and isinstance(extras[sym], dict):
            asset = _asset_from_spec(sym, extras[sym])
        if asset is None:
            return
        ordered.append(asset)
        seen.add(sym)

    for sym in list(priority.get('held') or []) + list(priority.get('watch') or []):
        add(sym)
    for asset in assets:
        if asset['sym'] not in seen:
            ordered.append(asset)
            seen.add(asset['sym'])
    return ordered


def stamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path, default=None):
    if not Path(path).exists():
        return copy.deepcopy(default if default is not None else {})
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError('expected a JSON object')
        return value
    except (ValueError, OSError) as exc:
        raise RuntimeError(f'{path} is unreadable; restore it before scanning. State was not reset.') from exc


def write_json(path, data):
    tmp = str(path) + '.tmp'
    Path(tmp).write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    os.replace(tmp, path)


def load_watchlist(path='watchlist.json'):
    grouped = read_json(path)
    unique = {}
    for narrative, assets in grouped.items():
        for original in assets:
            a = dict(original)
            sym = a['sym']
            a.setdefault('td', sym)
            if sym in unique:
                if unique[sym]['td'] != a['td']:
                    raise ValueError(f'Conflicting data symbols for {sym}')
                if narrative not in unique[sym]['narratives']:
                    unique[sym]['narratives'].append(narrative)
            else:
                a['narratives'] = [narrative]
                unique[sym] = a
    return list(unique.values())


def load_state():
    if Path(STATE_FILE).exists():
        state = read_json(STATE_FILE)
        if state.get('schema') != 4 or not isinstance(state.get('assets'), dict):
            raise RuntimeError('Unsupported scanner_state.json schema; refusing to reset alerts.')
        return state
    old, old_rsi = read_json('state.json'), read_json('rsi_state.json')
    assets = {}
    for sym, rec in old.items():
        if not isinstance(rec, dict):
            continue
        daily = dict(signal=rec.get('signal'), flip_date=rec.get('flip_date'),
                     bars_since_flip=rec.get('bars_since_flip'), last_bar=rec.get('last_closed_bar'),
                     price=rec.get('price'), supertrend=rec.get('supertrend'), rsi=rec.get('rsi'),
                     updated=rec.get('updated'), events=[], status='unverified',
                     legacy_alerted_flip=rec.get('alerted_flip_date'),
                     legacy_rsi_alerted=old_rsi.get(sym + ':1day', {}).get('alerted', {}))
        assets[sym] = {'1day': daily}
        # Old weekly records used a daily close gate: do not trust them as final.
    return dict(schema=4, version=VERSION, assets=assets)


class DataClient:
    def __init__(self, key, delay=8):
        self.key, self.delay, self.last_request = key, delay, None
        self.calls = 0
        self.minute_limit = None
        self.credits_left = None
        self.credit_cost = 1
        self.daily_exhausted = False
        self._limit_minute = None
        self._minute_limit_hits = 0

    def _redact(self, message):
        return str(message).replace(self.key, '[redacted]') if self.key else str(message)

    def _seconds_until_minute_reset(self):
        # Twelve Data restores the per-minute quota at the start of the next minute.
        return (60 - (time.time() % 60)) + 1.0

    def _note_headers(self, headers):
        cost = _header_int(headers, 'api-credits-request')
        if cost and cost > 0:
            self.credit_cost = cost
        used = _header_int(headers, 'api-credits-used')
        left = _header_int(headers, 'api-credits-left')
        if used is not None and left is not None and used + left > 0:
            self.minute_limit = used + left
            self.credits_left = left

    def _gap(self):
        if self.delay == 0:
            return 0.0
        gap = float(self.delay)
        if self.minute_limit:
            gap = 60.0 * (self.credit_cost or 1) / self.minute_limit
        if self.credits_left is not None and (self.credit_cost or 1) > self.credits_left:
            gap = max(gap, self._seconds_until_minute_reset())
        return gap

    def _pace(self):
        gap = self._gap()
        if self.last_request is not None and gap > 0:
            remain = gap - (time.monotonic() - self.last_request)
            if remain > 0:
                time.sleep(remain)
        self.last_request = time.monotonic()

    def _backoff(self, attempt):
        # Longer than the old 20s/40s linear waits, and at least until the minute window resets.
        return max(min(180.0, 30.0 * (2 ** attempt)), self._seconds_until_minute_reset())

    def _block_twelve(self):
        if not self.daily_exhausted:
            self.daily_exhausted = True
            print('Twelve Data credit limit will not reset this run; falling back to Yahoo and Stooq.')

    def fetch(self, symbol, timeframe):
        primary = 'Market data rate limit' if self.daily_exhausted else None
        if not self.daily_exhausted:
            try:
                return self._fetch_twelve(symbol, timeframe)
            except (RuntimeError, ValueError) as exc:
                primary = str(exc)
        return self._fallback(symbol, timeframe, primary or 'Market data error')

    def _fetch_twelve(self, symbol, timeframe):
        error = 'Request failed'
        for attempt in range(TD_ATTEMPTS):
            self._pace()
            self.calls += 1
            try:
                status, data, headers = _as_response(http_json_ex('https://api.twelvedata.com/time_series', params={
                    'symbol': symbol, 'interval': timeframe, 'outputsize': HISTORY,
                    'order': 'ASC', 'apikey': self.key}, timeout=30))
                self._note_headers(headers)
                if not isinstance(data, dict):
                    raise ValueError('Unexpected market data response')
                kind = _limit_kind(status, data)
                if kind == 'daily':
                    self._block_twelve()
                    raise RuntimeError('Market data rate limit')
                if kind == 'minute':
                    error = 'Market data rate limit'
                    minute = int(time.time() // 60)
                    crossed = self._limit_minute is not None and minute > self._limit_minute
                    self._limit_minute = minute
                    self._minute_limit_hits += 1
                    # A limit that survives a full minute reset is the daily cap, not the per-minute window.
                    if crossed and self._minute_limit_hits >= 3:
                        self._block_twelve()
                        raise RuntimeError('Market data rate limit')
                    if attempt + 1 >= TD_ATTEMPTS:
                        break
                    time.sleep(self._backoff(attempt))
                    continue
                if status != 200 or data.get('status') == 'error':
                    raise RuntimeError(self._redact(data.get('message', 'Market data error')))
                bars = []
                for value in data.get('values', []):
                    bars.append(dict(date=value['datetime'][:10], h=float(value['high']),
                                     l=float(value['low']), c=float(value['close'])))
                bars = _clean_bars(bars)
                self._limit_minute = None
                self._minute_limit_hits = 0
                return bars, {**(data.get('meta') or {}), 'source': 'twelvedata'}
            except OSError:
                error = 'Market data network request failed'
                if attempt + 1 >= TD_ATTEMPTS:
                    break
                time.sleep(self._backoff(attempt))
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(self._redact(exc)) from exc
        raise RuntimeError(self._redact(error))

    def _fallback(self, symbol, timeframe, primary):
        errors = []
        for name, fn in (('yahoo', self._yahoo), ('stooq', self._stooq)):
            try:
                bars, meta = fn(symbol, timeframe)
                meta = dict(meta or {})
                meta['source'] = name
                return bars, meta
            except Exception as exc:
                errors.append(f'{name}: {exc}')
        detail = '; '.join(errors)
        raise RuntimeError(self._redact(f'{primary} | fallback failed ({detail})'))

    def _yahoo(self, symbol, timeframe):
        interval = '1d' if timeframe == '1day' else '1wk'
        rng = '2y' if timeframe == '1day' else '10y'
        ysym = urllib.parse.quote(yahoo_symbol(symbol), safe='-')
        payload = json.loads(http_bytes(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{ysym}?range={rng}&interval={interval}'))
        chart = payload.get('chart') or {}
        result = chart.get('result') or []
        if not result:
            description = ((chart.get('error') or {}).get('description')) or 'yahoo: no data'
            raise RuntimeError(description)
        row = result[0]
        quote = ((row.get('indicators') or {}).get('quote') or [{}])[0]
        tzname = (row.get('meta') or {}).get('exchangeTimezoneName') or 'America/New_York'
        try:
            tz = ZoneInfo(tzname)
        except (KeyError, ValueError):
            tzname, tz = 'America/New_York', ZoneInfo('America/New_York')
        bars = []
        for bar_ts, high, low, close in zip(row.get('timestamp') or [], quote.get('high') or [],
                                            quote.get('low') or [], quote.get('close') or []):
            if None in (high, low, close):
                continue
            bars.append(dict(date=datetime.fromtimestamp(bar_ts, tz).date().isoformat(),
                             h=float(high), l=float(low), c=float(close)))
        meta = {'exchange_timezone': tzname, 'source': 'yahoo'}
        return _finalize_bars(_clean_bars(bars, skip_invalid=True), symbol, meta, timeframe), meta

    def _stooq(self, symbol, timeframe):
        interval = 'd' if timeframe == '1day' else 'w'
        raw = http_bytes(f'https://stooq.com/q/d/l/?s={urllib.parse.quote(stooq_symbol(symbol))}&i={interval}')
        text = raw.decode('utf-8', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
        if text.lstrip().startswith('<'):
            raise RuntimeError('stooq: no data')
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or 'Close' not in rows[0]:
            raise RuntimeError('stooq: no data')
        bars = [dict(date=row['Date'][:10], h=float(row['High']), l=float(row['Low']), c=float(row['Close']))
                for row in rows]
        meta = {'source': 'stooq'}
        return _finalize_bars(_clean_bars(bars, skip_invalid=True), symbol, meta, timeframe), meta


def event_id(sym, tf, event):
    parts = [sym, tf, event['kind'], event['direction'], str(event.get('level', '')), event['bar_date']]
    if event['kind'] == 'divergence':
        parts += [event['pivot1_date'], event['pivot2_date']]
    return '|'.join(parts)


def evaluate(bars, asset, tf, prior=None, meta=None, now=None, reseed=False):
    now = now or datetime.now(timezone.utc)
    prior = copy.deepcopy(prior or {})
    meta = {**(meta or {}), **{k: asset[k] for k in ('asset_type', 'exchange_timezone', 'session_close') if k in asset}}
    idx = closed_index([b['date'] for b in bars], asset['td'], meta, now, tf)
    if idx is None:
        raise ValueError('No closed candle available')
    bars = bars[:idx + 1]
    if len(bars) < MIN_BARS:
        raise ValueError(f'Only {len(bars)} closed candles; need {MIN_BARS}')
    closes, rsi, st = calculate(bars)
    fi, _ = last_flip(st['dirs'])
    n = len(bars) - 1
    old_events = {e['id']: e for e in prior.get('events', [])}
    stale = stale_bar(bars[-1]['date'], asset['td'], meta, tf, now)
    record = {**prior, 'signal': label(st['dirs'][-1]),
              'flip_date': bars[fi]['date'] if fi is not None else None,
              'bars_since_flip': n - fi if fi is not None else None,
              'last_bar': bars[-1]['date'], 'price': closes[-1],
              'supertrend': st['trend'][-1], 'rsi': rsi[-1],
              'updated': now.isoformat(), 'closed_at': close_at(bars[-1]['date'], asset['td'], meta, tf).isoformat(),
              'status': 'stale' if stale else 'ok', 'error': None, 'source': meta.get('source'), 'meta': meta,
              'history_bars': len(bars), 'events': list(old_events.values()),
              'series': [dict(date=b['date'], close=b['c'], rsi=rsi[j], supertrend=st['trend'][j])
                         for j, b in enumerate(bars) if j >= len(bars) - 90]}
    if stale:
        for e in record['events']:
            e['active'] = False
            e['stale_data'] = True
        return record
    # Replay missed bars (up to 60). Also inspect the last two to discover newly
    # installed features; event IDs prevent repeats on unchanged data.
    watermark = prior.get('processed_bar') or prior.get('last_bar')
    start = max(1, n - 1)
    if watermark:
        unseen = next((i for i, b in enumerate(bars) if b['date'] > watermark), n)
        start = max(1, n - 59, min(start, unseen))
    for i in range(start, n + 1):
        for event in events_at(closes, rsi, st['dirs'], i):
            e = {**event, 'sym': asset['sym'], 'timeframe': tf, 'bar_date': bars[i]['date'],
                 'price': closes[i], 'supertrend': st['trend'][i], 'rsi': rsi[i],
                 'detected_at': now.isoformat(), 'delivery': 'pending'}
            if e['kind'] == 'divergence':
                e['pivot1_date'] = bars[e.pop('pivot1_index')]['date']
                e['pivot2_date'] = bars[e.pop('pivot2_index')]['date']
            e['id'] = event_id(asset['sym'], tf, e)
            # Preserve pre-upgrade Telegram deduplication records.
            if e['kind'] == 'flip' and (not prior.get('signal') or e['bar_date'] <= (prior.get('legacy_alerted_flip') or '')):
                e['delivery'] = 'seeded'
            if e['kind'] == 'oversold' and prior.get('legacy_rsi_alerted', {}).get(f"{e['level']}:{e['bar_date']}"):
                e['delivery'] = 'sent'
            if reseed:
                e['delivery'] = 'seeded'
            if e['id'] not in old_events:
                old_events[e['id']] = e
    # If upgrading after a long gap, preserve the original latest-flip catch-up.
    if fi is not None and fi < start and prior and record['flip_date'] > (prior.get('processed_bar') or prior.get('legacy_alerted_flip') or record['flip_date']):
        e = dict(kind='flip', direction=record['signal'], sym=asset['sym'], timeframe=tf,
                 bar_date=record['flip_date'], price=closes[fi], supertrend=st['trend'][fi], rsi=rsi[fi],
                 detected_at=now.isoformat(), delivery='seeded' if reseed else 'pending')
        e['id'] = event_id(asset['sym'], tf, e)
        old_events.setdefault(e['id'], e)
    positions = {b['date']: i for i, b in enumerate(bars)}
    events = []
    for e in old_events.values():
        e.pop('stale_data', None)
        e['age_bars'] = n - positions[e['bar_date']] if e['bar_date'] in positions else None
        e['active'] = e['age_bars'] is not None and e['age_bars'] <= FRESH_BARS
        if reseed and e['delivery'] == 'pending':
            e['delivery'] = 'seeded'
        if e['delivery'] == 'pending' or (e['age_bars'] is not None and e['age_bars'] <= 90):
            events.append(e)
    record['events'] = sorted(events, key=lambda e: (e['bar_date'], e['id']))
    record['processed_bar'] = bars[-1]['date']
    return record


def format_alert(asset, event):
    esc = lambda v: html.escape(str(v))
    money = lambda v: f'${v:,.4f}' if abs(v) < 1 else f'${v:,.2f}'
    tf = '1D' if event['timeframe'] == '1day' else '1W'
    titles = {'flip': 'SUPERTREND FLIP', 'pullback': 'PULLBACK SETUP', 'divergence': 'RSI / PRICE DIVERGENCE', 'oversold': 'RSI OVERSOLD CROSS'}
    age = event.get('age_bars')
    catchup = 'CATCH-UP · ' if event.get('stale_data') or age is None or age > FRESH_BARS else ''
    lines = [f"<b>{catchup}{titles[event['kind']]} — {esc(event['direction'].upper())} · {tf}</b>",
             f"<b>{esc(asset['sym'])}</b> · {esc(asset['name'])}"]
    if event['kind'] == 'divergence':
        lines += [f"Price closes: {money(event['price1'])} → {money(event['price2'])}",
                  f"RSI at those pivots: {event['rsi1']:.1f} → {event['rsi2']:.1f}",
                  f"Pivots: {event['pivot1_date']} → {event['pivot2_date']}",
                  'Confirmed after 5 closed candles to the right of the second pivot.',
                  f"Strength: {event['strength']}"]
    elif event['kind'] in ('pullback', 'oversold'):
        lines.append(f"RSI: {event['previous_rsi']:.1f} → {event['rsi']:.1f}")
        if event['kind'] == 'pullback':
            lines.append('RSI crossed 50 within an established ' + ('bullish' if event['direction'] == 'buy' else 'bearish') + ' Supertrend.')
        else:
            lines.append(f"Crossed below {event['level']}.")
    lines += [f"Signal candle: {event['bar_date']} · Age: {age if age is not None else 'unknown'} {tf} bars",
              f"Close at signal: {money(event['price'])}",
              f"Supertrend at signal: {money(event['supertrend'])}",
              'Themes: ' + esc(', '.join(asset['narratives']))]
    return '\n'.join(lines)


def send_telegram(message, dry=False):
    if dry:
        print('[DRY RUN] ' + message)
        return True
    token, chat = os.getenv('TELEGRAM_TOKEN'), os.getenv('TELEGRAM_CHAT')
    if not token or not chat:
        return False
    for attempt in range(3):
        try:
            status, result = http_json(f'https://api.telegram.org/bot{token}/sendMessage', payload={
                'chat_id': chat, 'text': message, 'parse_mode': 'HTML', 'disable_web_page_preview': True}, timeout=25)
            if not isinstance(result, dict):
                return False
            if status == 200 and result.get('ok') is True:
                return True
            if status == 429:
                time.sleep(min(60, result.get('parameters', {}).get('retry_after', 5 * (attempt + 1))))
            elif status < 500:
                return False
        except (OSError, ValueError):
            pass
        time.sleep(2 * (attempt + 1))
    return False


def deliver(asset, rec, dry=False, persist=None):
    sent = failed = 0
    for event in rec.get('events', []):
        if event['delivery'] != 'pending':
            continue
        if send_telegram(format_alert(asset, event), dry):
            if not dry:
                event.update(delivery='sent', sent_at=stamp())
                if persist:
                    persist()
            sent += 1
        else:
            failed += 1
    return sent, failed


def export_dashboard(state, assets, path='dashboard_data.json', now=None):
    now = now or datetime.now(timezone.utc)
    rows = []
    for asset in assets:
        row = {**asset, 'narrative': asset['narratives'][0], 'timeframes': {}}
        for tf in ('1day', '1week'):
            rec = copy.deepcopy(state['assets'].get(asset['sym'], {}).get(tf, {}))
            rec.pop('legacy_alerted_flip', None)
            rec.pop('legacy_rsi_alerted', None)
            if not rec.get('last_bar'):
                rec['status'] = 'error' if rec.get('error') else 'missing'
            elif stale_bar(rec['last_bar'], asset['td'], rec.get('meta', asset), tf, now):
                rec['status'] = 'stale'
            for e in rec.get('events', []):
                e['active'] = rec['status'] == 'ok' and e.get('age_bars') is not None and e['age_bars'] <= FRESH_BARS
            row['timeframes'][tf] = rec
        # Legacy daily/weekly keys keep the old dashboard usable during upload.
        for tf, suffix in (('1day', ''), ('1week', '_1w')):
            r = row['timeframes'][tf]
            for key in ('signal', 'flip_date', 'bars_since_flip', 'last_bar', 'price', 'supertrend', 'rsi', 'updated', 'source'):
                row[key + suffix] = r.get(key)
            for direction in ('buy', 'sell'):
                row[f'pb_{direction}_{"1d" if tf == "1day" else "1w"}'] = any(
                    e.get('active') and e['kind'] == 'pullback' and e['direction'] == direction for e in r.get('events', []))
        rows.append(row)
    out = dict(schema=4, version=VERSION, generated_at=now.isoformat(), assets=rows,
               last_run=state.get('last_run', {}), rules=dict(atr_period=10, factor=3, rsi_period=14,
               pivot_left=5, pivot_right=5, min_rsi_difference=3, active_bars=FRESH_BARS))
    write_json(path, out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--reseed', action='store_true')
    ap.add_argument('--no-summary', action='store_true')
    ap.add_argument('--weekly', action='store_true', help='Weekly only; does not repeat daily scans')
    ap.add_argument('--timeframe', choices=['auto', '1day', '1week'], default='auto')
    ap.add_argument('--verify', metavar='SYMBOL')
    args = ap.parse_args(argv)
    key = os.getenv('TWELVE_DATA_KEY')
    if not key:
        print('TWELVE_DATA_KEY is not set.')
        return 1
    if not args.verify and not args.dry_run and not args.reseed and (not os.getenv('TELEGRAM_TOKEN') or not os.getenv('TELEGRAM_CHAT')):
        print('Telegram secrets missing. Configure TELEGRAM_TOKEN and TELEGRAM_CHAT, or use --dry-run.')
        return 1
    assets, state = order_assets(load_watchlist(), load_scan_priority()), load_state()
    if args.verify:
        assets = [a for a in assets if a['sym'].upper() == args.verify.upper()]
        if not assets:
            print('Symbol is not in watchlist.')
            return 1
    client = DataClient(key)
    summary = dict(started_at=stamp(), evaluated=0, errors=0, sent=0, delivery_failed=0, sources={})
    mode = '1week' if args.weekly else args.timeframe
    persist = lambda: write_json(STATE_FILE, state)
    try:
        for asset in assets:
            sym = asset['sym']
            records = state['assets'].setdefault(sym, {})
            tfs = ['1day', '1week'] if mode == 'auto' else [mode]
            for tf in tfs:
                prior = records.get(tf, {})
                now = datetime.now(timezone.utc)
                meta = {**asset, **prior.get('meta', {})}
                if tf == '1week' and mode == 'auto' and prior.get('last_bar') and prior.get('status') == 'ok' and prior['last_bar'] >= latest_week_key(asset['td'], meta, now):
                    if not args.reseed and not args.verify:
                        sent, failed = deliver(asset, prior, args.dry_run, None if args.dry_run else persist)
                        summary['sent'] += sent
                        summary['delivery_failed'] += failed
                    continue
                try:
                    bars, meta = client.fetch(asset['td'], tf)
                    rec = evaluate(bars, asset, tf, prior, meta, now, args.reseed)
                    records[tf] = rec
                    summary['evaluated'] += 1
                    if rec.get('source'):
                        summary['sources'][rec['source']] = summary['sources'].get(rec['source'], 0) + 1
                    if rec['status'] == 'stale':
                        summary['errors'] += 1
                    print(f"{sym} {tf}: {rec['signal']} · RSI {rec['rsi']:.1f} · candle {rec['last_bar']} · {rec['status']} · {rec.get('source')}")
                    if args.verify:
                        print(json.dumps(rec, indent=2))
                        continue
                except (RuntimeError, ValueError) as exc:
                    summary['errors'] += 1
                    rec = records.setdefault(tf, prior)
                    rec.update(status='error', error=str(exc), checked_at=stamp())
                    for e in rec.get('events', []):
                        e['active'] = False
                        e['stale_data'] = True
                    print(f'{sym} {tf}: {exc}')
                if not args.dry_run and not args.verify:
                    persist()  # Persist pending events before attempting delivery.
                if not args.reseed and not args.verify:
                    sent, failed = deliver(asset, rec, args.dry_run, None if args.dry_run else persist)
                    summary['sent'] += sent
                    summary['delivery_failed'] += failed
    finally:
        summary.update(finished_at=stamp(), api_requests=client.calls, twelve_data_blocked=client.daily_exhausted)
        state['last_run'] = summary
        if not args.dry_run and not args.verify:
            persist()
            export_dashboard(state, assets)
    if not args.no_summary and not args.reseed and not args.verify:
        send_telegram(f"<b>Scanner v4 summary</b>\n{summary['evaluated']} symbol/timeframes checked\n"
                      f"{summary['sent']} alerts delivered · {summary['delivery_failed']} pending\n"
                      f"{summary['errors']} data issues · {client.calls} data requests", args.dry_run)
    print(json.dumps(summary))
    return 1 if summary['errors'] or summary['delivery_failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
