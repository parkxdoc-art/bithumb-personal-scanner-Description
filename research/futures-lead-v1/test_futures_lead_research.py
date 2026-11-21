"""Offline functional tests. Uses deterministic fake data, NEVER reports trading performance."""
import unittest
from unittest.mock import patch
import futures_lead_research as r


class TestResearch(unittest.TestCase):
    def setUp(self):
        self.t = 25000*r.PERIOD_MS
        self.oi={}
        self.lsr={}
        self.taker={}
        self.prices={}
        # 6-day sample so comparison rule can be evaluated.
        for i in range(-100,650):
            end=self.t+i*r.PERIOD_MS
            self.oi[end]={"sumOpenInterest":str(1000*1.005**i),"timestamp":end}
            self.lsr[end]={"longShortRatio":str(1.005**i),"timestamp":end}
            self.taker[end]={"buyVol":"120","sellVol":"100","timestamp":end-r.PERIOD_MS}
        for j in range(-400,2000):
            self.prices[self.t+j*r.CANDLE_MS]=100*1.001**j

    def test_timestamp_start_end_distinction(self):
        self.assertEqual(r.rounded_end(self.t+2),self.t)
        self.assertEqual(r.taker_end(self.t-r.PERIOD_MS),self.t)

    def test_no_missing_candles_filled_forward(self):
        ts=self.t+4*r.PERIOD_MS
        feat=r.row_features(ts,4,[self.t+i*r.PERIOD_MS for i in range(5)],
                             self.oi,self.lsr,self.taker,self.prices)
        self.assertIsNotNone(feat)
        del self.prices[ts-r.CANDLE_MS]
        self.assertIsNone(r.row_features(ts,4,[self.t+i*r.PERIOD_MS for i in range(5)],
                                          self.oi,self.lsr,self.taker,self.prices))

    def test_fee_and_delay(self):
        trades=r.produce_trades('ETH',self.oi,self.lsr,self.taker,self.prices,
                                self.t,self.t+500*r.PERIOD_MS)
        self.assertTrue(trades)
        for x in trades:
            self.assertEqual(x['entry_ms'],x['signal_ms']+r.CANDLE_MS)
            for m in (30,60,180):
                expectation=(self.prices[x['signal_ms']+m*60000]/x['entry_price']-1)*100-0.5
                self.assertAlmostEqual(x[f'net{m}'],expectation)

    def test_research_without_position_ratio(self):
        now=self.t+600*r.PERIOD_MS
        def fake_history(path,sym,since,end,headers=None):
            if path.endswith('topLongShortPositionRatio'):
                raise r.APIError('requires key',401)
            return self.oi if path.endswith('openInterestHist') else self.taker
        with patch.object(r,'bih_markets',return_value={'ETH'}),\
             patch.object(r,'bin_symbols',return_value={'ETH':'ETHUSDT'}),\
             patch.object(r,'history',side_effect=fake_history),\
             patch.object(r,'bih_candles',return_value=self.prices):
            report=r.research(['ETH'],1,now_ms=now)
        self.assertFalse(report['ratio_available'])
        self.assertTrue(report['warnings'])
        self.assertEqual(report['mapped'],1)
        self.assertEqual(report['summary'][0]['n'],0)
        self.assertTrue(report['summary'][2]['n']>0)

    def test_api_pagination_over_500_rows(self):
        seq=[{"timestamp":self.t+i*r.PERIOD_MS,"sumOpenInterest":"1000"} for i in range(600)]
        calls=[]
        def fake_fetch(base,path,params,headers=None):
            calls.append(params['startTime'])
            data=[x for x in seq if params['startTime']<=x['timestamp']<=params['endTime']]
            return data[:params['limit']]
        with patch.object(r,'fetch_json',side_effect=fake_fetch):
            got=r.history('/futures/data/openInterestHist','ETHUSDT',self.t,self.t+600*r.PERIOD_MS)
        self.assertEqual(len(got),600)
        self.assertEqual(len(calls),2)

    def test_input_validation(self):
        with self.assertRaises(ValueError): r.coins_parse('BTC, ETH,../SOL')
        self.assertEqual(r.coins_parse('BTC,KRW-ETH,BTC'),['BTC','ETH'])

if __name__=='__main__':
    unittest.main(verbosity=2)
