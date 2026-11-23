"""V1.4 regression tests, run via unittest."""
import unittest
from datetime import datetime, timedelta
from bithumb_btc_mtf_v1 import KST, strict_swing_gate, swing_backtest_exit, tf_end, is_contiguous
from btc_mtf_macd_v141 import analyze_macd, all_macd_views
from test_mtf_v141_api import FocusDiagnosticTest  # include focus API regression in Render's V1.4 test pattern

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
          "data_gap_recent":False,"macd_h4":{"state":"GOLDEN"},"rebound_from_low_pct":3,
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


class MacdCrossTest(unittest.TestCase):
    def make(self, prices, hours=4):
        start=datetime(2026,10,1,tzinfo=KST)
        return [{"t":start+timedelta(hours=hours*i),"c":float(p),
                 "o":float(p),"h":float(p)+1,"l":float(p)-1} for i,p in enumerate(prices)]

    def test_current_golden_state_does_not_mean_new_golden_cross(self):
        rows=self.make([100]*55+[99-j*.75 for j in range(12)]+[90+j*1.2 for j in range(20)])
        events=[]
        for i in range(45,len(rows)+1):
            view=analyze_macd(rows[:i],12,26,9,"4h",tf_end,is_contiguous)
            if view["event"]=="GOLDEN_CROSS":
                events.append(i)
        self.assertTrue(events,"synthetic scenario should contain at least one golden cross")
        idx=events[-1]
        cross=analyze_macd(rows[:idx],12,26,9,"4h",tf_end,is_contiguous)
        after=analyze_macd(rows[:idx+1],12,26,9,"4h",tf_end,is_contiguous)
        self.assertEqual(cross["state"],"GOLDEN")
        self.assertEqual(cross["event"],"GOLDEN_CROSS")
        self.assertEqual(after["state"],"GOLDEN")
        self.assertEqual(after["event"],"NONE")
        self.assertEqual(after["last_event"],"GOLDEN_CROSS")
        self.assertEqual(after["bars_since_cross"],1)

    def test_gapped_latest_data_cannot_claim_a_cross(self):
        rows=self.make([100]*65+[90+j*.9 for j in range(20)])
        rows[-2]["t"]+=timedelta(hours=1)
        view=analyze_macd(rows,12,26,9,"4h",tf_end,is_contiguous)
        self.assertEqual(view["state"],"UNKNOWN")
        self.assertEqual(view["event"],"NONE")

    def test_standard_and_daily_chart_settings_are_separate(self):
        start=datetime(2026,9,1,tzinfo=KST)
        day=[{"t":start+timedelta(days=i),"c":100+i*.2} for i in range(80)]
        h4=self.make([100+i*.1 for i in range(90)])
        views=all_macd_views(day,h4,tf_end,is_contiguous)
        self.assertEqual(views["h4"]["params"],"12,26,9")
        self.assertEqual(views["day"]["params"],"12,26,9")
        self.assertEqual(views["day_chart"]["params"],"3,10,16")

    def test_strict_gate_requires_macd_bullish_state(self):
        test=StrictGateTest()
        test.setUp()
        test.m["macd_h4"]={"state":"DEAD"}
        self.assertFalse(strict_swing_gate(test.m,test.ctx,test.day,test.h4)["passed"])

if __name__=="__main__":
    unittest.main()
