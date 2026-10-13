import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scanner
from market_calendar import closed_index, latest_week_key, stale_bar
from signals import calc_rsi, events_at, divergences_at

UTC = timezone.utc
ASSET = {'sym':'TEST','td':'TEST','name':'Test & Co','narratives':['AI & Robotics']}
NOW = datetime(2026, 9, 15, 22, tzinfo=UTC)


def bars(n=140):
    start = NOW.date() - timedelta(days=n-1)
    return [dict(date=(start+timedelta(days=i)).isoformat(),h=105,l=90,c=100) for i in range(n)]


def calculated(data, previous=55, current=49, direction=-1):
    n=len(data)
    rsi=[55.0]*n
    rsi[-2:]=[previous,current]
    return [b['c'] for b in data],rsi,{'dirs':[direction]*n,'trend':[95.0]*n}


class CalendarTests(unittest.TestCase):
    def test_equity_week_excludes_midweek(self):
        self.assertEqual(closed_index(['2026-09-07','2026-09-14'],'TEST',now=NOW,timeframe='1week'),0)

    def test_friday_settlement_and_dst(self):
        dates=['2026-09-07','2026-09-14']
        self.assertEqual(closed_index(dates,'TEST',now=datetime(2026,9,18,20,44,tzinfo=UTC),timeframe='1week'),0)
        self.assertEqual(closed_index(dates,'TEST',now=datetime(2026,9,18,20,45,tzinfo=UTC),timeframe='1week'),1)
        self.assertIsNone(closed_index(['2026-12-14'],'TEST',now=datetime(2026,12,18,20,50,tzinfo=UTC),timeframe='1week'))
        self.assertEqual(closed_index(['2026-12-14'],'TEST',now=datetime(2026,12,18,21,45,tzinfo=UTC),timeframe='1week'),0)

    def test_crypto_week_waits_for_monday(self):
        dates=['2026-09-07','2026-09-14']
        for instant in [datetime(2026,9,18,23,tzinfo=UTC),datetime(2026,9,21,0,44,tzinfo=UTC)]:
            self.assertEqual(closed_index(dates,'BTC/USD',now=instant,timeframe='1week'),0)
        self.assertEqual(closed_index(dates,'BTC/USD',now=datetime(2026,9,21,0,45,tzinfo=UTC),timeframe='1week'),1)

    def test_daily_forming_candle_and_timezone(self):
        self.assertEqual(closed_index(['2026-09-14','2026-09-15'],'TEST',now=datetime(2026,9,15,18,tzinfo=UTC)),0)
        self.assertEqual(closed_index(['2026-09-14','2026-09-15'],'TEST',now=NOW),1)

    def test_week_key_for_asset_class(self):
        friday=datetime(2026,9,18,22,tzinfo=UTC)
        self.assertEqual(latest_week_key('TEST',now=friday),'2026-09-14')
        self.assertEqual(latest_week_key('BTC/USD',now=friday),'2026-09-07')

    def test_staleness_uses_candle_not_run_time(self):
        self.assertTrue(stale_bar('2025-12-18','TEST',now=NOW))
        self.assertFalse(stale_bar('2026-09-14','TEST',now=NOW))


def chart_dates(n=140, extra=()):
    start = NOW.date() - timedelta(days=n - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)] + list(extra)


def yahoo_chart(dates):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo('America/New_York')
    ts = [int(datetime.fromisoformat(day).replace(tzinfo=tz).timestamp()) for day in dates]
    n = len(dates)
    return json.dumps({'chart': {'result': [{
        'meta': {'exchangeTimezoneName': 'America/New_York', 'instrumentType': 'EQUITY'},
        'timestamp': ts,
        'indicators': {'quote': [{'high': [105] * n, 'low': [90] * n, 'close': [100] * n}]},
    }]}}).encode()


def stooq_csv(dates):
    lines = ['Date,Open,High,Low,Close,Volume']
    lines += [f'{day},100,105,90,100,1' for day in dates]
    return ('\n'.join(lines) + '\n').encode()


class DataClientTests(unittest.TestCase):
    def test_fetch_validates_sorts_and_deduplicates(self):
        rows=[{'datetime':d,'high':'105','low':'90','close':'100'} for d in ['2026-09-15','2026-09-14','2026-09-15']]
        with patch('scanner.http_json_ex',return_value=(200,{'values':rows,'meta':{'type':'Common Stock'}},{})) as http:
            b,meta=scanner.DataClient('secret',delay=0).fetch('TEST','1week')
        self.assertEqual([x['date'] for x in b],['2026-09-14','2026-09-15'])
        self.assertEqual(meta['source'],'twelvedata')
        self.assertEqual(http.call_args.kwargs['params']['interval'],'1week')

    def test_retries_api_rate_limit(self):
        row={'datetime':'2026-09-15','high':'105','low':'90','close':'100'}
        with patch('scanner.http_json_ex',side_effect=[(200,{'status':'error','code':429}),(200,{'values':[row]},{})]),patch('scanner.time.sleep'),patch('scanner.http_bytes') as fallback:
            client=scanner.DataClient('secret',delay=0)
            bars,meta=client.fetch('TEST','1day')
        self.assertEqual(len(bars),1)
        self.assertEqual(meta['source'],'twelvedata')
        self.assertEqual(client.calls,2)
        fallback.assert_not_called()

    def test_retries_use_longer_exponential_backoff(self):
        row={'datetime':'2026-09-15','high':'105','low':'90','close':'100'}
        limited=(429,{'code':429,'message':'You have run out of API credits for the current minute.','status':'error'},{})
        with patch('scanner.http_json_ex',side_effect=[limited,limited,(200,{'values':[row]},{})]),patch('scanner.time.sleep') as sleep,patch('scanner.time.time',return_value=1000.0):
            scanner.DataClient('secret',delay=0).fetch('TEST','1day')
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[30.0,60.0])

    def test_rate_limit_falls_back_to_yahoo(self):
        dates=chart_dates(extra=('2026-09-16',))
        payload=yahoo_chart(dates)

        def fake_bytes(url, **kwargs):
            self.assertIn('query1.finance.yahoo.com/v8/finance/chart/TEST', url)
            self.assertIn('interval=1d', url)
            return payload

        limited=(429,{'code':429,'message':'You have run out of API credits for the current minute.','status':'error'},{})
        with patch('scanner.http_json_ex',return_value=limited),patch('scanner.http_bytes',side_effect=fake_bytes) as http,patch('scanner.time.sleep'),patch('scanner.time.time',return_value=1000.0),patch('scanner.datetime',wraps=datetime) as dt:
            dt.now.return_value=NOW
            client=scanner.DataClient('secret',delay=0)
            bars,meta=client.fetch('TEST','1day')
        self.assertEqual(meta['source'],'yahoo')
        self.assertEqual(client.calls,scanner.TD_ATTEMPTS)
        self.assertEqual(bars[-1]['date'],'2026-09-15')
        self.assertNotIn('2026-09-16', [b['date'] for b in bars])
        self.assertGreaterEqual(len(bars),scanner.MIN_BARS)
        self.assertEqual(http.call_count,1)

    def test_yahoo_error_falls_back_to_stooq(self):
        dates=chart_dates()

        def fake_bytes(url, **kwargs):
            if 'yahoo' in url:
                raise RuntimeError('yahoo down')
            self.assertIn('stooq.com/q/d/l/?s=test.us', url)
            self.assertIn('i=d', url)
            return stooq_csv(dates)

        with patch('scanner.http_json_ex',return_value=(400,{'message':'Market data error'},{})),patch('scanner.http_bytes',side_effect=fake_bytes),patch('scanner.datetime',wraps=datetime) as dt:
            dt.now.return_value=NOW
            bars,meta=scanner.DataClient('secret',delay=0).fetch('TEST','1day')
        self.assertEqual(meta['source'],'stooq')
        self.assertEqual(bars[-1]['date'],'2026-09-15')

    def test_daily_credit_limit_stops_twelve_data(self):
        dates=chart_dates()
        limited=(429,{'code':429,'message':'You have run out of API credits for the current day.','status':'error'},{})
        with patch('scanner.http_json_ex',return_value=limited) as td,patch('scanner.http_bytes',return_value=yahoo_chart(dates)),patch('scanner.time.sleep'),patch('scanner.datetime',wraps=datetime) as dt:
            dt.now.return_value=NOW
            client=scanner.DataClient('secret',delay=0)
            first=client.fetch('TEST','1day')
            calls=client.calls
            second=client.fetch('AAPL','1day')
        self.assertTrue(client.daily_exhausted)
        self.assertEqual(calls,1)
        self.assertEqual(client.calls,1)
        self.assertEqual(td.call_count,1)
        self.assertEqual(first[1]['source'],'yahoo')
        self.assertEqual(second[1]['source'],'yahoo')

    def test_minute_limit_that_survives_a_reset_stops_twelve_data(self):
        dates=chart_dates()
        clock={'t':1000.0}

        def fake_time():
            return clock['t']

        def fake_sleep(seconds):
            clock['t']+=seconds

        limited=(429,{'code':429,'message':'You have run out of API credits for the current minute.','status':'error'},{})
        with patch('scanner.http_json_ex',return_value=limited) as td,patch('scanner.http_bytes',return_value=yahoo_chart(dates)),patch('scanner.time.time',side_effect=fake_time),patch('scanner.time.sleep',side_effect=fake_sleep),patch('scanner.datetime',wraps=datetime) as dt:
            dt.now.return_value=NOW
            client=scanner.DataClient('secret',delay=0)
            bars,meta=client.fetch('TEST','1day')
        self.assertTrue(client.daily_exhausted)
        self.assertLess(client.calls,scanner.TD_ATTEMPTS)
        self.assertEqual(meta['source'],'yahoo')
        self.assertGreaterEqual(len(bars),scanner.MIN_BARS)
        td.assert_called()

    def test_paces_to_header_credit_limit(self):
        row={'datetime':'2026-09-15','high':'105','low':'90','close':'100'}
        headers={'api-credits-used':'2','api-credits-left':'6','api-credits-request':'1'}
        with patch('scanner.http_json_ex',return_value=(200,{'values':[row],'meta':{}},headers)),patch('scanner.time.sleep') as sleep,patch('scanner.time.monotonic',return_value=0.0):
            client=scanner.DataClient('secret',delay=8)
            client.fetch('TEST','1day')
            client.fetch('TEST','1day')
        self.assertEqual(client.minute_limit,8)
        self.assertEqual(client.credits_left,6)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[7.5])

    def test_zero_credits_left_waits_for_the_next_minute(self):
        row={'datetime':'2026-09-15','high':'105','low':'90','close':'100'}
        headers={'api-credits-used':'8','api-credits-left':'0'}
        with patch('scanner.http_json_ex',return_value=(200,{'values':[row],'meta':{}},headers)),patch('scanner.time.sleep') as sleep,patch('scanner.time.time',return_value=1000.0),patch('scanner.time.monotonic',return_value=0.0):
            client=scanner.DataClient('secret',delay=8)
            client.fetch('TEST','1day')
            client.fetch('TEST','1day')
        # 1000 % 60 == 40, so the minute resets in 21s, which is wider than 60/8.
        self.assertEqual(client.minute_limit,8)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[21.0])

    def test_rejects_nan_candles(self):
        row={'datetime':'2026-09-15','high':'NaN','low':'90','close':'100'}
        with patch('scanner.http_json_ex',return_value=(200,{'values':[row]},{})),patch.object(scanner.DataClient,'_yahoo',side_effect=RuntimeError('yahoo down')),patch.object(scanner.DataClient,'_stooq',side_effect=RuntimeError('stooq down')):
            with self.assertRaisesRegex(RuntimeError,'Invalid OHLC'):
                scanner.DataClient('secret',delay=0).fetch('TEST','1day')

    def test_provider_error_redacts_key(self):
        with patch('scanner.http_json_ex',return_value=(400,{'message':'Invalid key secret'},{})),patch.object(scanner.DataClient,'_yahoo',side_effect=RuntimeError('yahoo down')),patch.object(scanner.DataClient,'_stooq',side_effect=RuntimeError('stooq down')):
            with self.assertRaisesRegex(RuntimeError,r'Invalid key \[redacted\]'):
                try:
                    scanner.DataClient('secret',delay=0).fetch('TEST','1day')
                except RuntimeError as exc:
                    self.assertNotIn('secret',str(exc))
                    raise

    def test_crypto_symbols_map_to_yahoo_and_stooq(self):
        self.assertEqual(scanner.yahoo_symbol('BTC/USD'),'BTC-USD')
        self.assertEqual(scanner.yahoo_symbol('ONDO/USD'),'ONDO-USD')
        self.assertEqual(scanner.yahoo_symbol('NVDA'),'NVDA')
        self.assertEqual(scanner.stooq_symbol('ETH/USD'),'ethusd')
        self.assertEqual(scanner.stooq_symbol('LINK/USD'),'linkusd')
        self.assertEqual(scanner.stooq_symbol('TSLA'),'tsla.us')


class SignalTests(unittest.TestCase):
    def test_flat_rsi_is_neutral(self):
        self.assertEqual(calc_rsi([100]*40)[-1],50)

    def test_pullback_without_oversold_cross(self):
        e=events_at([100]*20,[55]*19+[49],[-1]*20,19)
        self.assertEqual([x['kind'] for x in e],['pullback'])
        self.assertEqual(e[0]['direction'],'buy')

    def test_sell_pullback(self):
        e=events_at([100]*20,[45]*19+[51],[1]*20,19)
        self.assertEqual(e[0]['direction'],'sell')

    def test_no_pullback_when_trend_just_flipped(self):
        e=events_at([100]*20,[55]*19+[49],[1]*19+[-1],19)
        self.assertEqual([x['kind'] for x in e],['flip'])

    def test_two_thresholds_same_candle(self):
        e=events_at([100]*20,[35]*19+[19],[-1]*20,19)
        self.assertEqual([x['level'] for x in e],[30,20])

    def test_bull_divergence_confirmation_without_oversold_cross(self):
        c=[100.0]*30;c[10]=90;c[20]=85
        r=[50.0]*30;r[10]=25;r[20]=32
        self.assertEqual(divergences_at(c,r,24),[])
        e=events_at(c,r,[-1]*30,25)
        self.assertEqual(len(e),1)
        self.assertEqual((e[0]['kind'],e[0]['direction']),('divergence','bullish'))
        self.assertEqual((e[0]['price1'],e[0]['price2'],e[0]['rsi1'],e[0]['rsi2']),(90,85,25,32))
        self.assertEqual(divergences_at(c,r,26),[])

    def test_bear_divergence(self):
        c=[100.0]*30;c[10]=110;c[20]=115
        r=[50.0]*30;r[10]=75;r[20]=68
        self.assertEqual(divergences_at(c,r,25)[0]['direction'],'bearish')

    def test_divergence_requires_zone_and_strength(self):
        c=[100.0]*30;c[10]=90;c[20]=85
        for first,second in [(25,27),(39,43)]:
            r=[50.0]*30;r[10]=first;r[20]=second
            self.assertEqual(divergences_at(c,r,25),[])


class IntegrationTests(unittest.TestCase):
    def evaluate(self, prior=None, **kwargs):
        b=bars()
        with patch('scanner.calculate',return_value=calculated(b)):
            return scanner.evaluate(b,ASSET,'1day',prior,now=NOW,**kwargs)

    def test_pullback_exports_and_telegram_uses_same_event(self):
        rec=self.evaluate()
        self.assertEqual(len(rec['events']),1)
        e=rec['events'][0]
        self.assertEqual(e['delivery'],'pending')
        with tempfile.TemporaryDirectory() as d:
            exported=scanner.export_dashboard({'assets':{'TEST':{'1day':rec}}},[ASSET],Path(d)/'dash.json',NOW)
        row=exported['assets'][0]
        self.assertTrue(row['pb_buy_1d'])
        self.assertEqual(row['timeframes']['1day']['events'][0]['id'],e['id'])
        message=scanner.format_alert(ASSET,e)
        self.assertIn('Test &amp; Co',message)
        self.assertIn('55.0 → 49.0',message)
        self.assertIn('2026-09-15',message)

    def test_repeat_scan_dedup_and_failed_send_retry(self):
        first=self.evaluate()
        with patch('scanner.send_telegram',return_value=False):
            self.assertEqual(scanner.deliver(ASSET,first),(0,1))
        second=self.evaluate(first)
        self.assertEqual(len(second['events']),1)
        with patch('scanner.send_telegram',return_value=True) as send:
            self.assertEqual(scanner.deliver(ASSET,second),(1,0))
            self.assertEqual(scanner.deliver(ASSET,second),(0,0))
            self.assertEqual(send.call_count,1)

    def test_two_crosses_are_both_remembered(self):
        b=bars()
        values=calculated(b,35,19);values[1][:-1]=[35.0]*139
        with patch('scanner.calculate',return_value=values):
            rec=scanner.evaluate(b,ASSET,'1day',now=NOW)
            with patch('scanner.send_telegram',return_value=True):
                self.assertEqual(scanner.deliver(ASSET,rec),(2,0))
            rec=scanner.evaluate(b,ASSET,'1day',rec,now=NOW)
        self.assertEqual(len(rec['events']),2)
        self.assertTrue(all(e['delivery']=='sent' for e in rec['events']))

    def test_divergence_flows_to_snapshot(self):
        b=bars();c=[x['c'] for x in b];c[119]=90;c[134]=85
        r=[50.0]*140;r[119]=25;r[134]=32
        for i in [119,134]:b[i].update(c=c[i],l=c[i]-1)
        with patch('scanner.calculate',return_value=(c,r,{'dirs':[-1]*140,'trend':[80]*140})):
            rec=scanner.evaluate(b,ASSET,'1day',now=NOW)
        event=rec['events'][0]
        self.assertEqual(event['kind'],'divergence')
        self.assertEqual(event['pivot2_date'],b[134]['date'])
        self.assertEqual(event['bar_date'],b[139]['date'])
        self.assertTrue(event['active'])

    def test_weekly_fields_survive_daily_export(self):
        rec=self.evaluate();weekly=copy.deepcopy(rec);weekly['signal']='bear';weekly['rsi']=65
        state={'assets':{'TEST':{'1day':rec,'1week':weekly}}}
        with tempfile.TemporaryDirectory() as d:
            out=scanner.export_dashboard(state,[ASSET],Path(d)/'dash.json',NOW)
        self.assertEqual(out['assets'][0]['signal_1w'],'bear')
        self.assertEqual(out['assets'][0]['rsi_1w'],65)

    def test_old_setup_expires_but_history_remains(self):
        prior=self.evaluate()
        b=bars()+[dict(date='2026-09-16',h=105,l=90,c=100),dict(date='2026-09-17',h=105,l=90,c=100)]
        with patch('scanner.calculate',return_value=([100]*142,[49]*142,{'dirs':[-1]*142,'trend':[95]*142})):
            rec=scanner.evaluate(b,ASSET,'1day',prior,now=NOW+timedelta(days=2))
        self.assertEqual(rec['events'][0]['age_bars'],2)
        self.assertFalse(rec['events'][0]['active'])

    def test_stale_snapshot_excludes_old_active_signals(self):
        rec=self.evaluate()
        with tempfile.TemporaryDirectory() as d:
            out=scanner.export_dashboard({'assets':{'TEST':{'1day':rec}}},[ASSET],Path(d)/'dash.json',NOW+timedelta(days=10))
        self.assertFalse(out['assets'][0]['pb_buy_1d'])
        self.assertEqual(out['assets'][0]['timeframes']['1day']['status'],'stale')

    def test_missed_signal_bar_is_replayed(self):
        b=bars();r=[55.0]*140;r[-4:]=[49,49,49,49]
        with patch('scanner.calculate',return_value=([100]*140,r,{'dirs':[-1]*140,'trend':[95]*140})):
            rec=scanner.evaluate(b,ASSET,'1day',{'processed_bar':b[-6]['date']},now=NOW)
        self.assertEqual(len(rec['events']),1)
        self.assertEqual(rec['events'][0]['age_bars'],3)
        self.assertIn('CATCH-UP',scanner.format_alert(ASSET,rec['events'][0]))

    def test_reseed_suppresses_delivery(self):
        rec=self.evaluate(reseed=True)
        self.assertEqual(rec['events'][0]['delivery'],'seeded')

    def test_duplicate_symbol_preserves_themes(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'w.json';p.write_text(json.dumps({'A':[{'sym':'X','name':'X'}],'B':[{'sym':'X','name':'X'}]}))
            result=scanner.load_watchlist(p)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['narratives'],['A','B'])

    def test_corrupt_state_fails_without_reset(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json';p.write_text('{broken')
            with self.assertRaises(RuntimeError):scanner.read_json(p)
            self.assertEqual(p.read_text(),'{broken')

    def test_telegram_requires_api_ok_flag(self):
        with patch.dict(os.environ,{'TELEGRAM_TOKEN':'fake','TELEGRAM_CHAT':'fake'}),patch('scanner.http_json') as post:
            post.return_value=(200, {'ok':False})
            self.assertFalse(scanner.send_telegram('test'))

    def test_dry_run_does_not_write_or_send(self):
        with tempfile.TemporaryDirectory() as d,contextlib.chdir(d),contextlib.redirect_stdout(io.StringIO()):
            Path('watchlist.json').write_text(json.dumps({'Test':[ASSET]}))
            with patch.dict(os.environ,{'TWELVE_DATA_KEY':'fake'}),patch('scanner.DataClient.fetch',return_value=(bars(),{})),patch('scanner.datetime',wraps=datetime) as dt,patch('scanner.calculate',return_value=calculated(bars())),patch('scanner.http_json') as net:
                dt.now.return_value=NOW
                self.assertEqual(scanner.main(['--dry-run','--timeframe','1day','--no-summary']),0)
                net.assert_not_called()
            self.assertEqual([p.name for p in Path('.').iterdir()],['watchlist.json'])

    def test_weekly_flag_never_fetches_daily(self):
        with tempfile.TemporaryDirectory() as d,contextlib.chdir(d),contextlib.redirect_stdout(io.StringIO()):
            Path('watchlist.json').write_text(json.dumps({'Test':[ASSET]}))
            with patch.dict(os.environ,{'TWELVE_DATA_KEY':'fake'}),patch('scanner.DataClient.fetch',return_value=(bars(),{})) as fetch,patch('scanner.datetime',wraps=datetime) as dt:
                dt.now.return_value=NOW
                scanner.main(['--weekly','--dry-run','--no-summary'])
                self.assertEqual([c.args[1] for c in fetch.call_args_list],['1week'])

    def test_migrates_legacy_state_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as d,contextlib.chdir(d):
            original={'TEST':{'signal':'bull','last_closed_bar':'2026-09-14','alerted_flip_date':'2026-09-01'}}
            Path('state.json').write_text(json.dumps(original))
            Path('rsi_state.json').write_text(json.dumps({'TEST:1day':{'alerted':{'30:2026-09-14':True}}}))
            state=scanner.load_state()
            self.assertEqual(state['assets']['TEST']['1day']['legacy_alerted_flip'],'2026-09-01')
            self.assertTrue(state['assets']['TEST']['1day']['legacy_rsi_alerted']['30:2026-09-14'])
            self.assertEqual(json.loads(Path('state.json').read_text()),original)

    def test_weekly_delivery_retry_does_not_require_new_week(self):
        daily=self.evaluate()
        weekly=copy.deepcopy(daily)
        weekly['last_bar']='2026-09-07'
        weekly['events'][0]['timeframe']='1week'
        weekly['events'][0]['id']='weekly-pending'
        state={'schema':4,'assets':{'TEST':{'1day':daily,'1week':weekly}}}
        with tempfile.TemporaryDirectory() as d,contextlib.chdir(d),contextlib.redirect_stdout(io.StringIO()):
            Path('watchlist.json').write_text(json.dumps({'Test':[ASSET]}))
            scanner.write_json(scanner.STATE_FILE,state)
            with patch.dict(os.environ,{'TWELVE_DATA_KEY':'fake','TELEGRAM_TOKEN':'fake','TELEGRAM_CHAT':'fake'}),patch('scanner.DataClient.fetch',return_value=(bars(),{})) as fetch,patch('scanner.datetime',wraps=datetime) as dt,patch('scanner.calculate',return_value=calculated(bars())),patch('scanner.send_telegram',return_value=True):
                dt.now.return_value=NOW
                self.assertEqual(scanner.main(['--no-summary']),0)
                self.assertEqual([c.args[1] for c in fetch.call_args_list],['1day'])
            saved=scanner.read_json(scanner.STATE_FILE)
            self.assertEqual(saved['assets']['TEST']['1week']['events'][0]['delivery'],'sent')

    def test_source_round_trips_to_dashboard(self):
        b=bars()
        with patch('scanner.calculate',return_value=calculated(b)):
            rec=scanner.evaluate(b,ASSET,'1day',meta={'source':'yahoo'},now=NOW)
        self.assertEqual(rec['source'],'yahoo')
        with tempfile.TemporaryDirectory() as d:
            out=scanner.export_dashboard({'assets':{'TEST':{'1day':rec}}},[ASSET],Path(d)/'dash.json',NOW)
        self.assertEqual(out['assets'][0]['source'],'yahoo')
        self.assertEqual(out['assets'][0]['timeframes']['1day']['source'],'yahoo')


class ScanOrderTests(unittest.TestCase):
    def test_held_then_watch_then_the_rest(self):
        assets=[
            {'sym':'NVDA','td':'NVDA','name':'NVIDIA','narratives':['AI']},
            {'sym':'ASTS','td':'ASTS','name':'AST SpaceMobile','narratives':['Space']},
            {'sym':'ETH','td':'ETH/USD','name':'Ethereum','narratives':['Crypto']},
        ]
        priority={'held':['ETH','HIMS'],'watch':['ASTS'],'symbols':{'HIMS':{'name':'Hims & Hers','td':'HIMS'}}}
        ordered=scanner.order_assets(assets,priority)
        self.assertEqual([a['sym'] for a in ordered],['ETH','HIMS','ASTS','NVDA'])
        self.assertEqual(ordered[1]['td'],'HIMS')
        self.assertEqual(ordered[1]['narratives'],['Held'])

    def test_repo_priority_file_orders_the_watchlist(self):
        held=['ETH','RKLB','RGTI','MRNA','RR','SOL','BTC','OUST','BEAM','TSLA','HIMS','MSFT','ONDO','LINK']
        watch=['ASTS','IONQ']
        priority=scanner.load_scan_priority()
        self.assertEqual(priority['held'],held)
        self.assertEqual(priority['watch'],watch)
        assets=scanner.load_watchlist()
        ordered=scanner.order_assets(assets,priority)
        syms=[a['sym'] for a in ordered]
        self.assertEqual(syms[:len(held)+len(watch)],held+watch)
        rest=[a['sym'] for a in assets if a['sym'] not in set(held+watch)]
        self.assertEqual([s for s in syms if s not in set(held+watch)],rest)
        by={a['sym']:a for a in ordered}
        self.assertEqual(by['ONDO']['td'],'ONDO/USD')
        self.assertEqual(by['LINK']['td'],'LINK/USD')
        self.assertEqual(by['MSFT']['name'],'Microsoft')

    def test_dry_run_fetches_held_names_before_the_rest(self):
        with tempfile.TemporaryDirectory() as d,contextlib.chdir(d),contextlib.redirect_stdout(io.StringIO()):
            Path('watchlist.json').write_text(json.dumps({'Test':[
                {'sym':'NVDA','name':'NVIDIA','td':'NVDA'},
                {'sym':'ETH','name':'Ethereum','td':'ETH/USD'},
                {'sym':'ASTS','name':'AST SpaceMobile','td':'ASTS'},
            ]}))
            Path('scan_priority.json').write_text(json.dumps({'held':['ETH'],'watch':['ASTS']}))
            with patch.dict(os.environ,{'TWELVE_DATA_KEY':'fake'}),patch('scanner.DataClient.fetch',return_value=(bars(),{'source':'twelvedata'})) as fetch,patch('scanner.datetime',wraps=datetime) as dt,patch('scanner.calculate',return_value=calculated(bars())):
                dt.now.return_value=NOW
                self.assertEqual(scanner.main(['--dry-run','--timeframe','1day','--no-summary']),0)
        self.assertEqual([c.args[0] for c in fetch.call_args_list],['ETH/USD','ASTS','NVDA'])


class SupertrendParityTests(unittest.TestCase):
    def test_rgti_daily_st_line_on_2026_10_12(self):
        from supertrend import supertrend
        series=json.loads((Path(__file__).resolve().parent/'fixtures'/'rgti_daily.json').read_text())
        st=supertrend([b['h'] for b in series],[b['l'] for b in series],[b['c'] for b in series],10,3.0)
        idx=next(i for i,b in enumerate(series) if b['date']=='2026-10-12')
        self.assertEqual(series[idx]['date'],series[-1]['date'])
        self.assertAlmostEqual(st['trend'][idx],15.957,delta=0.001)


if __name__=='__main__':unittest.main()
