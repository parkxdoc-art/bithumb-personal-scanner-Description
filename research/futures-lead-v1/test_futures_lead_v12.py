"""V1.2 offline-only regression cases: E/G/P/N groups, timing and CSV."""
import unittest
import futures_lead_research as r

class TestV12(unittest.TestCase):
    def setUp(self):
        self.t=30000*r.PERIOD_MS
        self.oi={}
        self.lsr={}
        self.taker={}
        self.prices={}
        self.btc={}
        for i in range(-20,140):
            ts=self.t+i*r.PERIOD_MS
            self.oi[ts]={"sumOpenInterest":str(1000*1.005**i),"timestamp":ts}
            self.lsr[ts]={"longShortRatio":"1.2","timestamp":ts}
            self.taker[ts]={"buyVol":"120","sellVol":"100","timestamp":ts-r.PERIOD_MS}
        for i in range(-100,600):
            ts=self.t+i*r.CANDLE_MS
            self.prices[ts]=100.0
            self.btc[ts]=300.0+i*.05

    def run_rules(self,btc=True):
        return r.produce_trades("ETH",self.oi,self.lsr,self.taker,self.prices,
                                self.t,self.t+130*r.PERIOD_MS,
                                btc_prices=self.btc if btc else None)

    def test_early_and_btc_gate_without_prior_price_spike(self):
        trades=self.run_rules()
        first=self.t
        flags={row['rule'] for row in trades if row['signal_ms']==first}
        self.assertIn('E',flags)
        self.assertIn('G',flags)
        self.assertNotIn('M',flags)
        self.assertNotIn('N',flags)
        self.assertNotIn('P',flags)

    def test_btc_filter_only_removes_g_when_btc_down(self):
        self.btc={t:500.0-(t-self.t)/r.CANDLE_MS*.05 for t in self.prices}
        trades=self.run_rules()
        self.assertGreater(sum(x['rule']=='E' for x in trades),0)
        self.assertEqual(sum(x['rule']=='G' for x in trades),0)
        trades=self.run_rules(btc=False)
        self.assertGreater(sum(x['rule']=='E' for x in trades),0)
        self.assertEqual(sum(x['rule']=='G' for x in trades),0)

    def test_flat_price_baseline_is_disjoint_from_early(self):
        for k in self.oi:
            self.oi[k]['sumOpenInterest']='1000'
        trades=self.run_rules()
        self.assertGreater(sum(x['rule']=='N' for x in trades),0)
        self.assertEqual(sum(x['rule']=='E' for x in trades),0)

    def test_pullback_entry_is_confirmed_then_one_more_closed_candle(self):
        first=self.t
        self.prices[first]=99.6
        self.prices[first+r.CANDLE_MS]=99.0
        self.prices[first+2*r.CANDLE_MS]=99.5
        self.prices[first+3*r.CANDLE_MS]=99.8
        trades=self.run_rules()
        p=[x for x in trades if x['rule']=='P' and x['signal_ms']==first]
        self.assertEqual(len(p),1)
        self.assertEqual(p[0]['entry_ms'],first+4*r.CANDLE_MS)
        self.assertAlmostEqual(p[0]['pullback'],-1.0)
        self.assertEqual(p[0]['entry_style'],'눌림 확인 뒤 다음 5분 종가')
        self.assertAlmostEqual(p[0]['net30'],(self.prices[first+3*r.CANDLE_MS+30*60_000]/99.8-1)*100-.5)
        self.assertEqual(p[0]['hit60'],False)

    def test_pullback_requires_next_completed_entry_candle(self):
        first=self.t
        self.prices[first]=99.6
        self.prices[first+r.CANDLE_MS]=99.0
        self.prices[first+2*r.CANDLE_MS]=99.5
        self.prices.pop(first+3*r.CANDLE_MS)
        self.assertIsNone(r.pullback_entry(first,self.prices,100.0))
        trades=self.run_rules()
        self.assertEqual(len([x for x in trades if x['rule']=='P' and x['signal_ms']==first]),0)

    def test_v12_csv_and_touch_stat(self):
        rows=self.run_rules()
        out=r.csv_text(rows)
        self.assertIn('BTC 1시간 상승률(%)',out)
        self.assertIn('60분내 종가 +1% 달성',out)
        self.assertIn('신호 뒤 첫 5분 종가',out)
        self.assertTrue(all('touch60' in x for x in r.describe(rows)))

if __name__=='__main__':
    unittest.main(verbosity=2)
