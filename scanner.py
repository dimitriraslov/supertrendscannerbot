#!/usr/bin/env python3
"""Supertrend scanner v4: one candle stream, one event ledger, two outputs."""
import argparse
import copy
import html
import json
import math
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone, timedelta

import urllib.request
import urllib.parse
import urllib.error
from market_calendar import closed_index, close_at, is_crypto, latest_week_key, stale_bar
from signals import calculate, events_at
from supertrend import last_flip, label

VERSION = '4.0.0'
STATE_FILE = 'scanner_state.json'
MIN_BARS = 120
HISTORY = 400
FRESH_BARS = 1


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

    def fetch(self, symbol, timeframe):
        error = 'Request failed'
        for attempt in range(3):
            if self.last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.calls += 1
            try:
                status, data = http_json('https://api.twelvedata.com/time_series', params={
                    'symbol': symbol, 'interval': timeframe, 'outputsize': HISTORY,
                    'order': 'ASC', 'apikey': self.key}, timeout=30)
                if not isinstance(data, dict):
                    raise ValueError('Unexpected market data response')
                code = data.get('code', status)
                if status == 429 or code == 429:
                    error = 'Market data rate limit'
                    time.sleep(20 * (attempt + 1))
                    continue
                if status != 200 or data.get('status') == 'error':
                    raise ValueError(str(data.get('message', 'Market data error')).replace(self.key, '[redacted]'))
                dedup = {}
                for v in data.get('values', []):
                    bar = dict(date=v['datetime'][:10], h=float(v['high']), l=float(v['low']), c=float(v['close']))
                    if not all(math.isfinite(bar[k]) for k in ('h', 'l', 'c')) or not 0 < bar['l'] <= bar['c'] <= bar['h']:
                        raise ValueError('Invalid OHLC values returned')
                    datetime.fromisoformat(bar['date'])
                    dedup[bar['date']] = bar
                if not dedup:
                    raise ValueError('No price data returned')
                return [dedup[d] for d in sorted(dedup)], data.get('meta', {})
            except OSError:
                error = 'Market data network request failed'
            except (ValueError, KeyError, TypeError) as exc:
                error = str(exc)
                break
        raise RuntimeError(error)


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
              'status': 'stale' if stale else 'ok', 'error': None, 'meta': meta,
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
            for key in ('signal', 'flip_date', 'bars_since_flip', 'last_bar', 'price', 'supertrend', 'rsi', 'updated'):
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
    assets, state = load_watchlist(), load_state()
    if args.verify:
        assets = [a for a in assets if a['sym'].upper() == args.verify.upper()]
        if not assets:
            print('Symbol is not in watchlist.')
            return 1
    client = DataClient(key)
    summary = dict(started_at=stamp(), evaluated=0, errors=0, sent=0, delivery_failed=0)
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
                    if rec['status'] == 'stale':
                        summary['errors'] += 1
                    print(f"{sym} {tf}: {rec['signal']} · RSI {rec['rsi']:.1f} · candle {rec['last_bar']} · {rec['status']}")
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
        summary.update(finished_at=stamp(), api_requests=client.calls)
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
