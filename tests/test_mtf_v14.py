"""V1.4 regression tests, run via unittest."""
import unittest
from datetime import datetime, timedelta
from bithumb_btc_mtf_v1 import KST, strict_swing_gate, swing_backtest_exit, tf_end, is_contiguous
from btc_mtf_macd_v141 import analyze_macd, all_macd_views
from btc_mtf_explain_v142 import build_explanation
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


class ExplainableSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.day=make_bars(datetime(2026,8,1,tzinfo=KST),40,timedelta(days=1))
        self.h4=make_bars(datetime(2026,10,1,tzinfo=KST),40,timedelta(hours=4))
        self.h1=make_bars(datetime(2026,10,1,tzinfo=KST),30,timedelta(hours=1))
        self.m={
            "score":9,"close":self.h4[-1]["c"],"day_up":True,"h4_up":True,
            "h4_higher_low":True,"h4_higher_high":True,
            "checks":{"h1_confirm":True,"ma_recover":True,
                      "rsi_recover":True,"macd_recover":True},
            "rsi14":56,"macd_h4":{"state":"GOLDEN"},
            "volume_ratio":1.4,"rebound_from_low_pct":4.0,
            "structure_risk_pct":3.6,"data_gap_recent":False,
            "timing_ok":True,"bb_position":0.7,"body_ratio":0.7,
            "atr_pct":1.2,"h4_distance_ma7_pct":1.2,
            "relative_pct":0.9,"closed_kst":"2026-10-10 00:00",
        }
        self.ctx={"long":"up","short":"side"}

    def explain(self):
        return build_explanation(self.m,self.ctx,self.day,self.h4,self.h1,.25)

    def test_scorecards_are_four_checklists_not_profit_probabilities(self):
        d=self.explain()
        self.assertEqual(d["status"],"RESEARCH_ONLY")
        self.assertEqual(len(d["scores"]),6)
        self.assertTrue(all(0<=v<=100 for v in d["scores"].values()))
        self.assertEqual(len(d["factors"]["structure"]),4)
        self.assertEqual(len(d["factors"]["entry"]),4)
        self.assertEqual(d["sell_pressure_proxy"]["orderbook_sell_wall"],None)
        self.assertEqual(d["sell_pressure_proxy"]["taker_sell_strength"],None)
        self.assertIsNotNone(d["context"]["last_24h_closed_turnover_krw"])

    def test_future_breakout_is_only_a_conditional_reference(self):
        d=self.explain()
        p=d["plan"]
        self.assertEqual(d["status"],"RESEARCH_ONLY")
        self.assertTrue(p["requires_next_completed_4h_body_close_above_trigger"])
        self.assertEqual(p["requires_future_4h_turnover_ratio"],2.0)
        self.assertGreater(p["trigger_ref"],d["context"]["close_ref"])
        self.assertGreater(p["trigger_ref"],p["stop_ref"])
        self.assertGreater(p["breakeven_ref"],p["trigger_ref"])

    def test_unsafe_stop_distance_hides_targets(self):
        for x in self.h4[-6:]:
            x["l"]=50.
        d=self.explain()
        self.assertFalse(d["plan"]["plan_valid"])
        self.assertIsNone(d["plan"]["target1_ref"])
        self.assertIsNone(d["plan"]["target2_ref"])
        self.assertTrue(any("구조적 손절 거리" in w for w in d["warnings"]))

    def test_dead_macd_not_equivalent_to_trend_confirmation(self):
        self.m["macd_h4"]={"state":"DEAD"}
        self.m["rsi14"]=43
        d=self.explain()
        self.assertFalse(d["factors"]["trend"][-1]["passed"])
        self.assertTrue(any("MACD" in w for w in d["warnings"]))

if __name__=="__main__":
    unittest.main()
