"""V1.4 regression tests, run via unittest."""
import unittest
from datetime import datetime, timedelta
from bithumb_btc_mtf_v1 import KST, strict_swing_gate, swing_backtest_exit

def make_bars(start,n,delta):
    return [{"t":start+i*delta,"o":100+i,"h":101+i,"l":99+i,
             "c":100+i,"turnover":1000000} for i in range(n)]

class StrictGateTest(unittest.TestCase):
    def setUp(self):
        self.day=make_bars(datetime(2026,9,1,tzinfo=KST),40,timedelta(days=1))
        self.h4=make_bars(datetime(2026,10,1,tzinfo=KST),40,timedelta(hours=4))
        self.ctx={"long":"up","short":"side"}
        self.m={"score":10,"day_up":True,"h4_up":True,
          "close":self.h4[-1]["c"],"checks":{"h1_confirm":True},
          "rsi14":55,"macd_hist":.3,"volume_ratio":1.3,
          "data_gap_recent":False,"rebound_from_low_pct":3,
          "structure_risk_pct":4,"timing_ok":True}

    def test_all_conditions_pass(self):
        result=strict_swing_gate(self.m,self.ctx,self.day,self.h4)
        self.assertTrue(result["passed"],result["missing"])

    def test_10_point_bounce_with_weak_rsi_macd_is_not_buy(self):
        self.m["rsi14"]=47
        self.m["macd_hist"]=-.1
        result=strict_swing_gate(self.m,self.ctx,self.day,self.h4)
        self.assertFalse(result["passed"])
        self.assertIn("RSI(14) 50 이상",result["missing"])
        self.assertIn("MACD 히스토그램 양수",result["missing"])

    def test_btc_decline_and_gaps_block_buy(self):
        self.ctx["long"]="down"
        self.m["data_gap_recent"]=True
        self.assertFalse(strict_swing_gate(self.m,self.ctx,self.day,self.h4)["passed"])

    def test_eight_points_not_enough(self):
        self.m["score"]=8
        self.assertFalse(strict_swing_gate(self.m,self.ctx,self.day,self.h4)["passed"])

class GapTest(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,31,15,tzinfo=KST)

    def candles(self,gap,collision=False):
        data=[]
        for i in range(120):
            if i==gap:continue
            data.append({"t":self.at+timedelta(hours=i),
                         "o":100.,"h":120. if (i==2 and collision) else 101.,
                         "l":94. if i==2 else 99.,"c":100.})
        return lambda market,tf,at,count:data

    def test_gap_after_stop_keeps_resolved(self):
        result=swing_backtest_exit("KRW-TEST",self.at,.25,getter=self.candles(60))
        self.assertEqual(result["status"],"RESOLVED")
        self.assertTrue(result["stop"])

    def test_gap_before_stop_is_unknown(self):
        result=swing_backtest_exit("KRW-TEST",self.at,.25,getter=self.candles(1))
        self.assertEqual(result["status"],"UNKNOWN")
        self.assertIsNone(result["net_pct"])

    def test_stop_first_when_tp_and_sl_same_hour(self):
        result=swing_backtest_exit("KRW-TEST",self.at,.25,getter=self.candles(60,True))
        self.assertEqual(result["status"],"RESOLVED")
        self.assertTrue(result["ambiguous"])
        self.assertTrue(result["stop"])

if __name__=="__main__":
    unittest.main()
