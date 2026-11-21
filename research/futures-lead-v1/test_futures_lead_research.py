"""V1.3 offline regression tests. Deterministic fixtures; NOT market performance."""
import importlib.util
import io
import csv
import contextlib
import os
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('futures_lead_research',os.path.join(os.path.dirname(__file__),'futures_lead_research.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)

class TestV13(unittest.TestCase):
    def setUp(self):
        self.t=25000*r.PERIOD_MS
        self.prices={self.t+i*r.CANDLE_MS:100 for i in range(-1000,2500)}

    def oi_taker(self, n=620):
        oi={};taker={}
        for i in range(-12,n):
            t=self.t+i*r.PERIOD_MS
            oi[t]={"sumOpenInterest":str(1000*(1.005**i)),"timestamp":t}
            taker[t]={"buyVol":"120","sellVol":"100","timestamp":t-r.PERIOD_MS}
        return oi,taker

    def test_version_integrity(self):
        self.assertEqual(r.VERSION,'1.3')
        self.assertIn('V1.3',r.HTML)
        self.assertIn('BITHUMB_FUTURES_LEAD_V1_3.csv',open(r.__file__,encoding='utf8').read())
        self.assertEqual(list(r.RULES),['G','N','X'])
        self.assertNotIn('BINANCE_API_KEY',r.research.__code__.co_names)

    def test_early_gate_keeps_fixed_thresholds(self):
        f={"oi":0.9,"taker":1.1,"momentum":0.1}
        self.assertTrue(r.early_candidate(f))
        for variant in [{'momentum':0.5},{'oi':0.79},{'taker':1.079},{'momentum':-0.6}]:
            self.assertFalse(r.early_candidate(dict(f,**variant)))

    def test_signal_uses_closed_prices_before_signal_only(self):
        oi,taker=self.oi_taker(20)
        times=sorted(oi)
        at=self.t+4*r.PERIOD_MS
        f=r.row_features(at,times.index(at),times,oi,taker,self.prices)
        self.assertIsNotNone(f)
        self.assertTrue(r.early_candidate(f))
        broken=self.prices.copy()
        del broken[at-r.CANDLE_MS]
        self.assertIsNone(r.row_features(at,times.index(at),times,oi,taker,broken))

    def test_btc_requires_closed_price(self):
        self.assertEqual(r.btc_momentum(self.t,self.prices),0)
        p=self.prices.copy();del p[self.t-r.CANDLE_MS]
        self.assertIsNone(r.btc_momentum(self.t,p))

    def test_only_g_with_btc_positive(self):
        oi,taker=self.oi_taker(80)
        rows=r.produce_trades('ETH',oi,taker,self.prices,self.t,self.t+75*r.PERIOD_MS,btc_prices=self.prices)
        self.assertTrue(rows)
        self.assertEqual({x['rule'] for x in rows},{'G'})
        self.assertTrue(all(x['entry_ms']==x['signal_ms']+r.CANDLE_MS for x in rows))
        signals=[x['signal_ms'] for x in rows]
        self.assertTrue(all(b-a>=3*r.HOURS_MS for a,b in zip(signals,signals[1:])))

    def test_btc_negative_sets_only_x_control(self):
        oi,taker=self.oi_taker(80)
        btc=self.prices.copy()
        for i in range(-1000,2500):
            btc[self.t+i*r.CANDLE_MS]=100-(i+1000)*0.0001
        rows=r.produce_trades('ETH',oi,taker,self.prices,self.t,self.t+75*r.PERIOD_MS,btc_prices=btc)
        self.assertEqual({x['rule'] for x in rows},{'X'})

    def test_close_first_target_before_stop(self):
        p=self.prices.copy();p[self.t+r.CANDLE_MS]=101.1;p[self.t+2*r.CANDLE_MS]=97.0
        a=r.outcomes_for_entry(self.t,100,p)
        self.assertEqual(a['first_event'],'익절')
        self.assertAlmostEqual(a['strategy_net60'],0.6)
        self.assertTrue(a['hit60'])

    def test_close_first_stop_before_target(self):
        p=self.prices.copy();p[self.t+r.CANDLE_MS]=98.9;p[self.t+2*r.CANDLE_MS]=105.0
        a=r.outcomes_for_entry(self.t,100,p)
        self.assertEqual(a['first_event'],'손절')
        self.assertAlmostEqual(a['strategy_net60'],-1.6)
        self.assertTrue(a['hit60'])  # peak eventual +5%, but loss first!

    def test_timeout_and_fee(self):
        p=self.prices.copy()
        a=r.outcomes_for_entry(self.t,100,p)
        self.assertEqual(a['first_event'],'시간만료')
        self.assertAlmostEqual(a['strategy_net60'],-0.5)
        for m in (15,30,60,180):self.assertAlmostEqual(a[f'net{m}'],-0.5)

    def test_missing_future_bar_excludes_event(self):
        p=self.prices.copy();del p[self.t+7*r.CANDLE_MS]
        self.assertIsNone(r.outcomes_for_entry(self.t,100,p))

    def test_nearest_matched_control_same_coin_and_day(self):
        base={'coin':'ETH','signal_ms':self.t}
        g=[dict(base,rule='G')]
        pool=[{'coin':'XRP','signal_ms':self.t,'rule':'N'},
              {'coin':'ETH','signal_ms':self.t-4*r.HOURS_MS,'rule':'N'},
              {'coin':'ETH','signal_ms':self.t+2*r.PERIOD_MS,'rule':'N'},
              {'coin':'ETH','signal_ms':self.t+r.PERIOD_MS,'rule':'N'}]
        found=r.select_controls(g,pool)
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['signal_ms'],self.t+r.PERIOD_MS)

    def test_nearest_control_uniqueness(self):
        g=[{'coin':'ETH','signal_ms':self.t},{'coin':'ETH','signal_ms':self.t+3*r.HOURS_MS}]
        pool=[{'coin':'ETH','signal_ms':self.t+r.PERIOD_MS}]
        out=r.select_controls(g,pool)
        self.assertEqual(len(out),1)

    def test_outcome_csv_export_and_group_stats(self):
        f={'oi':0.9,'momentum':0.1,'taker':1.2}
        a=r.make_row('ETH','G',self.t,f,0.4,self.prices)
        b=r.make_row('ETH','N',self.t+2*r.PERIOD_MS,f,0.5,self.prices)
        summary=r.describe([a,b]);self.assertEqual([x['rule'] for x in summary],['G','N','X'])
        self.assertEqual(summary[0]['n'],1)
        self.assertIsNone(summary[2]['avg60'])
        data=list(csv.reader(io.StringIO(r.csv_text([a,b]))))
        self.assertEqual(len(data),3)
        self.assertEqual(len(data[0]),len(data[1]))
        self.assertEqual(len(data[0]),21)

    def test_no_api_key_top_trader_calls(self):
        now=self.t+75*r.PERIOD_MS
        oi,taker=self.oi_taker(80)
        def fake_hist(path, symbol, start,end,headers=None):
            if path.endswith('openInterestHist'):return oi
            if path.endswith('takerlongshortRatio'):return taker
            raise AssertionError('unexpected top longshort API call')
        with patch.object(r,'bih_markets',return_value={'ETH'}),\
             patch.object(r,'bin_symbols',return_value={'ETH':'ETHUSDT'}),\
             patch.object(r,'history',side_effect=fake_hist),\
             patch.object(r,'bih_candles',return_value=self.prices):
            results=r.research('ETH',1,now_ms=now)
        self.assertEqual(results['mapped'],1)
        self.assertGreater(results['g_count'],0)
        self.assertTrue(all(x['rule'] in ('G','N','X') for x in results['trades']))

    def test_endtime_400_fallback(self):
        rows=[{'timestamp':self.t+i*r.PERIOD_MS,'sumOpenInterest':'1000'} for i in range(480)]
        calls=[]
        def fake_get(base,path,params,headers=None):
            calls.append((params['startTime'],params['endTime']))
            if params['endTime']-params['startTime']>=96*r.PERIOD_MS:
                raise r.APIError("parameter 'endTime' is invalid",400)
            return [x for x in rows if params['startTime']<=x['timestamp']<=params['endTime']]
        with patch.object(r,'fetch_json',side_effect=fake_get):
            got=r.history('/futures/data/openInterestHist','ETHUSDT',self.t,self.t+480*r.PERIOD_MS)
        self.assertEqual(len(got),480)
        self.assertTrue(len(calls)>5)

if __name__=='__main__': unittest.main(verbosity=2)
