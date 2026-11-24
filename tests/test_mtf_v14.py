"""V1.4 regression tests, run via unittest."""
import unittest
from datetime import datetime, timedelta
from bithumb_btc_mtf_v1 import KST, strict_swing_gate, swing_backtest_exit, tf_end, is_contiguous
from btc_mtf_macd_v141 import analyze_macd, all_macd_views
from btc_mtf_explain_v142 import build_explanation
from btc_mtf_combo_v143 import evaluate_combined, REQUIRED_TREND_CHECKS
from btc_mtf_watch_v144 import classify_watch_tracks
from btc_mtf_replay_v145 import HourlyReplayPath
from btc_mtf_quality_v146 import snapshot_liquidity, investigate_missing_hour
from btc_mtf_study_v147 import compare_watch_cohorts
from unittest.mock import patch
import bithumb_btc_mtf_v1 as main
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


class CombinedSignalTest(unittest.TestCase):
    """The original signal alone can never bypass structure/entry/risk checks."""
    def setUp(self):
        start=datetime(2026,10,1,tzinfo=KST)
        self.h4=[
            {"t":start+timedelta(hours=4*i), "o":95+i*.5,
             "h":99+i*.5, "l":94+i*.5, "c":96+i*.5, "turnover":100}
            for i in range(6)
        ]
        self.h4[-1].update({"o":102.0,"c":106.0,"h":107.0,"l":101.0,"turnover":250})
        self.m={"score":10,"technical_score":7,"quality_score":3,"body_ratio":.72,"volume_ratio":2.5}
        self.strict={"passed":True, "checks":{key:True for key in REQUIRED_TREND_CHECKS}}
        self.evidence={
            "scores":{"structure":100,"timing":100,"trend":100,
                      "setup":100,"entry":100,"safety":100},
            "sell_pressure_proxy":{"red_candle_turnover_pct":20.0},
            "plan":{"plan_valid":True},
        }

    def classify(self,original="BUY"):
        return evaluate_combined(original,self.strict,self.evidence,
                                 self.m,self.h4,is_contiguous)

    def test_only_old_buy_and_completed_breakout_can_be_combined_buy(self):
        r=self.classify()
        self.assertEqual(r["signal"],"BUY")
        self.assertTrue(r["latest_completed_4h_breakout"])
        self.assertLess(r["prior_3bar_breakout_level"],self.h4[-1]["c"])
        self.assertTrue(r["buy_ready"])

    def test_old_watch_remains_watch_even_if_other_gates_pass(self):
        r=self.classify("WATCH")
        self.assertEqual(r["signal"],"WATCH")
        self.assertFalse(r["buy_ready"])

    def test_no_current_bar_breakout_means_watch_not_buy(self):
        self.h4[-1].update({"o":98,"c":100,"h":103,"l":97})
        r=self.classify()
        self.assertFalse(r["latest_completed_4h_breakout"])
        self.assertEqual(r["signal"],"WATCH")

    def test_sell_turnover_proxy_and_structure_score_are_real_watch_filters(self):
        self.evidence["sell_pressure_proxy"]["red_candle_turnover_pct"]=75
        self.evidence["scores"]["structure"]=50
        r=self.classify()
        self.assertEqual(r["signal"],"RESEARCH")
        self.assertFalse(r["watch_ready"])
        self.assertTrue(any("음봉 거래대금" in t for t in r["missing_watch"]))
        self.assertTrue(any("구조 점수" in t for t in r["missing_watch"]))

    def test_unavailable_red_candle_ratio_does_not_become_zero(self):
        self.evidence["sell_pressure_proxy"]["red_candle_turnover_pct"]=None
        self.assertEqual(self.classify()["signal"],"RESEARCH")

    def test_noncontiguous_four_hour_bars_block_combined_watch(self):
        self.h4[-2]["t"]+=timedelta(hours=1)
        r=self.classify()
        self.assertFalse(r["watch_ready"])
        self.assertEqual(r["signal"],"RESEARCH")

    def test_existing_strict_gate_is_not_bypassed(self):
        self.strict["checks"]["일봉 MA20 상향 + 22봉 연속"]=False
        r=self.classify()
        self.assertEqual(r["signal"],"RESEARCH")


class SeparateWatchTracksTest(unittest.TestCase):
    def setUp(self):
        self.m={
            "score":8,"technical_score":6,"quality_score":2,
            "rsi14":48,"timing_ok":True,"volume_ratio":.9,
            "structure_risk_pct":5,"data_gap_recent":False,
            "macd_h4":{"state":"DEAD"},
        }
        self.explain={"scores":{"timing":75,"safety":75,"trend":50}}
        self.ctx={"long":"side","short":"up"}
        self.strict={"passed":False,"checks":{
            "일봉 MA20 상향 + 22봉 연속":False,
            "4시간봉 MA20 상회·상향 + 21봉 연속":False}}
        self.combined={"signal":"RESEARCH","watch_ready":False}

    def classify(self,original="WATCH"):
        return classify_watch_tracks(original,self.strict,self.combined,
                                     self.explain,self.m,self.ctx)

    def test_early_watch_is_observation_not_buy(self):
        result=self.classify()
        self.assertEqual(result["kind"],"EARLY_WATCH")
        self.assertTrue(result["early_ready"])
        self.assertFalse(result["trend_ready"])
        self.assertNotEqual(result["kind"],"BUY")

    def test_old_buy_does_not_promote_early_watch_to_buy(self):
        result=self.classify("BUY")
        self.assertEqual(result["kind"],"EARLY_WATCH")
        self.assertTrue(result["buy_unchanged"])

    def test_weak_macd_and_rsi_and_trend_are_research_not_watch(self):
        self.explain["scores"]["trend"]=25
        result=self.classify("BUY")
        self.assertEqual(result["kind"],"RESEARCH")
        self.assertIn("MACD 데드·RSI 50미만·추세약세 동시 발생 제외",
                      result["early_missing"])

    def test_missing_recent_bars_blocks_early_watch(self):
        self.m["data_gap_recent"]=True
        self.assertEqual(self.classify()["kind"],"RESEARCH")

    def test_trend_watch_requires_previous_strict_watch_gate(self):
        self.m["macd_h4"]["state"]="GOLDEN"
        self.strict["checks"].update({
            "일봉 MA20 상향 + 22봉 연속":True,
            "4시간봉 MA20 상회·상향 + 21봉 연속":True})
        self.combined["watch_ready"]=True
        self.combined["signal"]="WATCH"
        result=self.classify()
        self.assertEqual(result["kind"],"TREND_WATCH")
        self.assertTrue(result["trend_ready"])

    def test_previous_v143_buy_retains_buy_when_early_conditions_fail(self):
        self.combined["signal"]="BUY"
        self.m["data_gap_recent"]=True
        self.m["score"]=7
        self.assertEqual(self.classify("BUY")["kind"],"BUY")

    def test_no_original_candidate_cannot_become_early_watch(self):
        self.assertEqual(self.classify(None)["kind"],"RESEARCH")


class TargetedReplayRecoveryTest(unittest.TestCase):
    """Use only real returned 1h source bars; no 30m price substitution."""

    def setUp(self):
        self.at=datetime(2026,10,31,15,tzinfo=KST)
        self.base=[{"t":self.at+timedelta(hours=i),"o":100.0,"h":100.1,
                    "l":99.9,"c":100.0,"turnover":1000} for i in range(120)]
        self.gap=self.at+timedelta(hours=1)
        self.calls=[]

    def get(self,market,tf,to,count=100):
        self.calls.append((market,tf,to,count))
        if tf=="1h" and count==160:
            return [r for r in self.base if r["t"]!=self.gap]
        if tf=="1h":
            return [r for r in self.base if r["t"]==self.gap]
        raise AssertionError("Unexpected timeframe")

    def test_exact_hour_recovered_and_fee_unchanged(self):
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=self.get)
        self.assertEqual(got["status"],"RESOLVED")
        self.assertEqual(got["minutes"],7200)
        self.assertEqual(got["audit"]["recovered_one_hour_candles"],1)
        self.assertEqual(got["audit"]["status_code"],"RECOVERED_1H")
        self.assertEqual(got["audit"]["hourly_recheck_calls"],1)
        self.assertEqual(self.calls[1][2],self.gap+timedelta(hours=1,minutes=1))
        self.assertAlmostEqual(got["net_pct"],-0.079968,places=4)

    def test_persistent_hourly_gap_keeps_unknown(self):
        def no_gap_return(market,tf,to,count=100):
            return [r for r in self.base if r["t"]!=self.gap]
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=no_gap_return)
        self.assertEqual(got["status"],"UNKNOWN")
        self.assertIsNone(got["net_pct"])
        self.assertEqual(got["audit"]["status_code"],"SOURCE_1H_NOT_RETURNED")
        self.assertEqual(got["audit"]["first_unresolved_or_recovered_hour_kst"],"2026-10-31 16:00")

    def test_30m_bars_diagnose_but_never_create_1h_execution_price(self):
        def half(market,tf,to,count=100):
            if tf=="30m":return [{"t":self.gap+timedelta(minutes=30),"o":99,"h":100,"l":99,"c":99}]
            return [r for r in self.base if r["t"]!=self.gap]
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=half,detailed=True)
        self.assertEqual(got["status"],"UNKNOWN")
        self.assertIsNone(got["net_pct"])
        self.assertEqual(got["audit"]["status_code"],"SOURCE_1H_MISSING_BUT_30M_EXISTS")
        self.assertEqual(got["audit"]["lower_timeframe_bars_in_missing_hour"],1)

    def test_no_1h_or_30m_cannot_claim_zero_trades(self):
        def no_gap(market,tf,to,count=100):
            if tf=="30m":return []
            return [r for r in self.base if r["t"]!=self.gap]
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=no_gap,detailed=True)
        self.assertEqual(got["status"],"UNKNOWN")
        self.assertEqual(got["audit"]["status_code"],"SOURCE_1H_30M_NOT_RETURNED")

    def test_recheck_api_error_is_not_converted_to_profit(self):
        def broken(market,tf,to,count=100):
            if tf=="1h" and count!=160:raise RuntimeError("temporary HTTP 503")
            return [r for r in self.base if r["t"]!=self.gap]
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=broken)
        self.assertEqual(got["status"],"UNKNOWN")
        self.assertEqual(got["audit"]["status_code"],"HOURLY_RECHECK_API_ERROR")
        self.assertIn("503",got["audit"]["detail_error"])

    def test_gap_after_stop_skips_unnecessary_recheck(self):
        late=self.at+timedelta(hours=65)
        def stops(market,tf,to,count=100):
            self.calls.append((tf,count))
            rows=[dict(r) for r in self.base if r["t"]!=late]
            for r in rows:
                if r["t"]==self.at+timedelta(hours=2):r["l"]=90
            return rows
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=stops,detailed=True)
        self.assertEqual(got["status"],"RESOLVED")
        self.assertTrue(got["stop"])
        self.assertEqual(got["audit"]["hourly_recheck_calls"],0)
        self.assertEqual(got["audit"]["total_candle_api_calls"],1)

    def test_missing_entry_may_be_recovered_only_by_true_hourly_bar(self):
        def missing_first(market,tf,to,count=100):
            if tf=="1h" and count==160:return [r for r in self.base if r["t"]!=self.at]
            if tf=="1h":return [r for r in self.base if r["t"]==self.at]
            return []
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=missing_first)
        self.assertEqual(got["status"],"RESOLVED")
        self.assertEqual(got["audit"]["recovered_one_hour_candles"],1)

    def test_missing_entry_unreturned_remains_unknown(self):
        def no_entry(market,tf,to,count=100):
            return [r for r in self.base if r["t"]!=self.at]
        got=main.swing_backtest_exit("KRW-TEST",self.at,.04,getter=no_entry)
        self.assertEqual(got["status"],"UNKNOWN")
        self.assertEqual(got["reason_code"],"ENTRY_HOUR_MISSING")

    def test_spotcheck_route_returns_five_without_account_keys(self):
        fake={"status":"UNKNOWN","net_pct":None,"audit":{"status_code":"SOURCE_1H_NOT_RETURNED"}}
        with patch.object(main,"swing_backtest_exit",return_value=fake) as b:
            r=main.app.test_client().post("/api/replay-spotcheck",json={"fee":0.04})
            self.assertEqual(r.status_code,200,r.get_json())
            got=r.get_json()
            self.assertEqual(len(got["items"]),5)
            self.assertEqual(got["fee_each_pct"],.04)
            self.assertEqual(b.call_count,5)
            self.assertTrue(all(i["backtest"]["status"]=="UNKNOWN" for i in got["items"]))
            get_result=main.app.test_client().get("/api/replay-spotcheck?fee=0.04")
            self.assertEqual(get_result.status_code,200,get_result.get_json())
            self.assertEqual(len(get_result.get_json()["items"]),5)
            self.assertEqual(b.call_count,5)  # GET uses 2-minute cache


    def test_spotcheck_rejects_unsupported_fee(self):
        response=main.app.test_client().get("/api/replay-spotcheck?fee=1.0")
        self.assertEqual(response.status_code,400)
        self.assertFalse(response.get_json()["ok"])


class HistoricalLiquidityV146Test(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,31,15,tzinfo=KST)
        self.start=self.at-timedelta(hours=24)
        self.h1=[{"t":self.start+timedelta(hours=i),"o":100,"c":100,
                  "turnover":6_000_000} for i in range(24)]

    def test_full_precisely_prior_day_is_four_of_four(self):
        result=snapshot_liquidity(self.h1,self.at)
        self.assertEqual(result["hourly_bars_observed"],24)
        self.assertEqual(result["checks_passed"],4)
        self.assertEqual(result["kind"],"OBSERVED_OK")
        self.assertEqual(result["observed_turnover_24h_krw"],144_000_000)
        self.assertFalse(result["changes_buy_watch_gate"])

    def test_future_bar_excluded_even_if_present_in_argument(self):
        h=self.h1+[{"t":self.at,"turnover":900_000_000},
                    {"t":self.at+timedelta(hours=1),"turnover":800_000_000}]
        original=list(h)
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["hourly_bars_observed"],24)
        self.assertEqual(result["observed_turnover_24h_krw"],144_000_000)
        self.assertEqual(original,h)

    def test_missing_hour_reports_incomplete_observation_not_false_zero_volume(self):
        h=[x for x in self.h1 if x["t"]!=self.start+timedelta(hours=8)]
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["hourly_bars_observed"],23)
        self.assertFalse(result["observed_turnover_is_complete"])
        self.assertEqual(result["longest_missing_hours"],1)
        self.assertIn("2026-10-30 23:00",result["missing_hour_samples_kst"])
        self.assertTrue(any("불완전" in text for text in result["warnings"]))

    def test_recent_hourly_outage_raises_data_gap_risk(self):
        h=self.h1[:-5]
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["recent_6h_bars_observed"],1)
        self.assertEqual(result["kind"],"DATA_GAP")

    def test_low_turnover_with_complete_history_is_only_warning(self):
        h=[dict(x,turnover=1000) for x in self.h1]
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["kind"],"THIN_OBSERVED")
        self.assertTrue(result["observed_turnover_is_complete"])
        self.assertFalse(result["changes_buy_watch_gate"])

    def test_invalid_turnover_rejected_without_synthetic_zero(self):
        h=[dict(x) for x in self.h1]
        h[-1]["turnover"]=float("nan")
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["hourly_bars_observed"],23)
        self.assertFalse(result["checks"]["직전 완료 1시간봉 확인"])

    def test_sparse_snapshot_with_nonzero_minute_not_used_for_buy(self):
        h=[dict(x) for x in self.h1[:12]]
        result=snapshot_liquidity(h,self.at)
        self.assertEqual(result["kind"],"DATA_GAP")
        self.assertFalse(result["changes_buy_watch_gate"])


class MicroGapV146Test(unittest.TestCase):
    def setUp(self):
        self.at=datetime(2026,10,31,16,tzinfo=KST)
        self.calls=[]

    def test_true_1m_and_5m_with_exact_hour_boundary(self):
        def getter(market,tf,to,count):
            self.calls.append((tf,to,count))
            points = [self.at-timedelta(minutes=1),
                      self.at+timedelta(minutes=15),
                      self.at+timedelta(hours=1)]
            return [{"t":t} for t in points]
        r=investigate_missing_hour("KRW-TEST",self.at,getter)
        self.assertEqual(r["status_code"],"LOWER_TIMEFRAME_BARS_EXIST")
        self.assertEqual(r["five_min_bars_observed"],1)
        self.assertEqual(r["one_min_bars_observed"],1)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[0][0],"5m")
        self.assertEqual(self.calls[1][0],"1m")
        self.assertFalse(r["changes_exit_result"])
        self.assertEqual(self.calls[0][1],self.at+timedelta(hours=1,minutes=1))

    def test_no_lower_intervals_does_not_prove_no_trades(self):
        r=investigate_missing_hour("KRW-TEST",self.at,
                                   lambda market,tf,to,count: [])
        self.assertEqual(r["status_code"],"NO_LOWER_TF_BARS_RETURNED")
        self.assertIn("미확정",r["status_text"])

    def test_api_error_remains_explicit(self):
        def one_error(market,tf,to,count):
            if tf=="1m":raise RuntimeError("API timeout")
            return []
        r=investigate_missing_hour("KRW-TEST",self.at,one_error)
        self.assertEqual(r["status_code"],"LOWER_TIMEFRAME_API_ERROR")
        self.assertIn("timeout",r["errors"]["1m"])

    def test_spotcheck_api_adds_micro_evidence_but_preserves_unknown(self):
        fake_bt={"status":"UNKNOWN","net_pct":None,"reason_code":"MISSING_HOURLY_BAR",
                 "audit":{"status_code":"SOURCE_1H_30M_NOT_RETURNED",
                          "first_unresolved_or_recovered_hour_kst":"2026-10-31 16:00"}}
        micro={"status_code":"LOWER_TIMEFRAME_BARS_EXIST",
               "five_min_bars_observed":1,"one_min_bars_observed":1,
               "changes_exit_result":False}
        with patch.object(main,"swing_backtest_exit",return_value=fake_bt),\
             patch.object(main,"investigate_missing_hour",return_value=micro) as diag,\
             patch.object(main,"ttl_get",return_value=None),\
             patch.object(main,"ttl_put"):
            resp=main.app.test_client().get("/api/replay-spotcheck?fee=0.04")
            self.assertEqual(resp.status_code,200,resp.get_json())
            data=resp.get_json()
            self.assertEqual(len(data["items"]),5)
            self.assertEqual(diag.call_count,5)
            self.assertTrue(all(x["gap_micro"]["five_min_bars_observed"]==1
                                for x in data["items"]))
            self.assertTrue(all(x["backtest"]["status"]=="UNKNOWN"
                                and x["backtest"]["net_pct"] is None
                                for x in data["items"]))


class CohortStudyV147Test(unittest.TestCase):
    def row(self, market, at="2026-11-13 15:00:00", kind="EARLY_WATCH",
            score=4, bars=24, net=None, status="UNKNOWN", legacy="WATCH"):
        return {
            "market":market,"at":at,"kind":kind,"legacy_signal":legacy,
            "liquidity_audit":{
                "checks_passed":score,
                "hourly_bars_observed":bars,
                "observed_turnover_is_complete":bars==24,
                "observed_turnover_24h_krw":150_000_000,
            },
            "backtest":{"status":status,"net_pct":net,
                        "reason":"missing 1h" if status=="UNKNOWN" else ""},
        }

    def test_all_unknown_100_percent_missing_not_zero_percent_profit(self):
        rows=[self.row("KRW-BLUE"),self.row("KRW-MBL")]
        report=compare_watch_cohorts(rows,scanned=15,issues=3)
        self.assertEqual(report["summary"]["candidates"],2)
        self.assertEqual(report["summary"]["unknown"],2)
        self.assertEqual(report["summary"]["resolved"],0)
        self.assertIsNone(report["summary"]["positive_fraction_resolved_pct"])
        self.assertIsNone(report["summary"]["mean_hypothetical_net_pct"])
        self.assertEqual(report["scanned"],15)
        self.assertEqual(report["issues"],3)

    def test_resolved_only_average_and_fee_is_already_in_simulated_net(self):
        rows=[
            self.row("KRW-BLUE",net=3.0,status="RESOLVED"),
            self.row("KRW-2Z",net=-1.0,status="RESOLVED"),
            self.row("KRW-MBL",net=None,status="UNKNOWN")
        ]
        report=compare_watch_cohorts(rows,fee_each=.04)
        self.assertEqual(report["summary"]["positive"],1)
        self.assertEqual(report["summary"]["nonpositive"],1)
        self.assertEqual(report["summary"]["unknown"],1)
        self.assertEqual(report["summary"]["mean_hypothetical_net_pct"],1.0)
        self.assertEqual(report["summary"]["positive_fraction_resolved_pct"],50.0)
        self.assertEqual(report["fee_each_pct"],.04)

    def test_watch_buy_and_research_categories_do_not_mix(self):
        rows=[
            self.row("KRW-BLUE",kind="EARLY_WATCH",net=1,status="RESOLVED"),
            self.row("KRW-MBL",kind="TREND_WATCH",net=-2,status="RESOLVED"),
            self.row("KRW-2Z",kind="BUY",net=5,status="RESOLVED",legacy="BUY"),
            self.row("KRW-NXPC",kind="RESEARCH",net=None,legacy="BUY")
        ]
        r=compare_watch_cohorts(rows)
        count={x["label"]:x["candidates"] for x in r["by_kind"]}
        self.assertEqual(count,{"BUY":1,"EARLY_WATCH":1,"TREND_WATCH":1,"RESEARCH":1})
        self.assertEqual(r["original_buy"]["candidates"],2)
        self.assertEqual(r["original_buy"]["resolved"],1)
        self.assertTrue(all(x["watch_is_not_trade"] for x in r["by_kind"]))

    def test_quality_four_not_automatically_complete_24h(self):
        one=self.row("KRW-MBL",score=4,bars=23)
        r=compare_watch_cohorts([one])
        self.assertEqual(r["rows"][0]["quality"],"Q4")
        self.assertEqual(r["rows"][0]["coverage"],"INCOMPLETE_24H")

    def test_quality_coverage_missing_is_unverified(self):
        one=self.row("KRW-MBL")
        one["liquidity_audit"]={}
        r=compare_watch_cohorts([one])
        self.assertEqual(r["rows"][0]["quality"],"Q_UNKNOWN")
        self.assertEqual(r["rows"][0]["coverage"],"UNKNOWN_24H")

    def test_duplicate_coin_time_removed_but_other_dates_kept(self):
        a=self.row("KRW-BLUE",at="2026-11-12 15:00:00")
        b=self.row("KRW-BLUE",at="2026-11-13 15:00:00")
        r=compare_watch_cohorts([a,a,b])
        self.assertEqual(r["summary"]["candidates"],2)
        self.assertEqual(r["summary"]["unique_markets"],1)
        self.assertEqual(r["summary"]["unique_snapshots"],2)
        self.assertEqual(r["duplicates_skipped"],1)
        self.assertEqual(len(r["by_day"]),2)

    def test_invalid_resolved_nan_stays_unknown(self):
        x=self.row("KRW-MBL",net=float("nan"),status="RESOLVED")
        y=self.row("KRW-2Z",net=None,status="RESOLVED")
        r=compare_watch_cohorts([x,y])
        self.assertEqual(r["summary"]["resolved"],0)
        self.assertEqual(r["summary"]["unknown"],2)
        self.assertIsNone(r["rows"][0]["net_pct"])

    def test_bad_fields_and_oversized_input_rejected(self):
        self.assertRaises(ValueError,compare_watch_cohorts,[{}]*1501)
        self.assertRaises(ValueError,compare_watch_cohorts,[],fee_each=.5)
        r=compare_watch_cohorts([{"market":"BAD","at":"2026-11-13","kind":"BUY"}])
        self.assertEqual(r["summary"]["candidates"],0)
        self.assertEqual(r["invalid_skipped"],1)

    def test_api_is_read_only_and_rejects_live(self):
        client=main.app.test_client()
        rows=[self.row("KRW-BLUE",net=-0.2,status="RESOLVED")]
        resp=client.post("/api/study-v147",json={
            "mode":"backtest","strategy":"swing","fee":.04,
            "items":rows,"scanned":10,"issues":2})
        self.assertEqual(resp.status_code,200,resp.get_json())
        data=resp.get_json()
        self.assertEqual(data["study"]["summary"]["candidates"],1)
        self.assertEqual(data["study"]["by_kind"][1]["label"],"EARLY_WATCH")
        self.assertEqual(data["study"]["by_kind"][1]["resolved"],1)
        invalid=client.post("/api/study-v147",json={
            "mode":"live","strategy":"swing","fee":.04,"items":rows})
        self.assertEqual(invalid.status_code,400)
        invalid2=client.post("/api/study-v147",json={
            "mode":"backtest","strategy":"scalp","fee":.04,"items":rows})
        self.assertEqual(invalid2.status_code,400)


class ExactHistoricalCandleBoundaryV148Test(unittest.TestCase):
    """Historical exact closes are included; live near-close safety stays."""

    def setUp(self):
        self.snapshot=datetime(2026,11,18,15,0,0,tzinfo=KST)
        self.server_time=datetime(2026,11,23,21,54,0,tzinfo=KST)

    def raw(self,stamp,turnover=6_000_000):
        return {
            "candle_date_time_kst":stamp.strftime("%Y-%m-%dT%H:%M:%S"),
            "opening_price":100,"high_price":101,"low_price":99,
            "trade_price":100,"candle_acc_trade_price":turnover,
        }

    def test_historical_hour_ending_exactly_at_1500_is_included(self):
        raw=[self.raw(self.snapshot-timedelta(hours=2)),
             self.raw(self.snapshot-timedelta(hours=1)),
             self.raw(self.snapshot)]
        with patch.object(main,"now_kst",return_value=self.server_time),\
             patch.object(main,"api_get",return_value=raw) as api:
            candles=main.candles("KRW-XRP","1h",self.snapshot,count=50)
        self.assertEqual([x["t"] for x in candles],
                         [self.snapshot-timedelta(hours=2),
                          self.snapshot-timedelta(hours=1)])
        self.assertEqual(api.call_args.args[1]["to"],"2026-11-18 15:00:00")

    def test_historical_hourly_liquidity_has_full_24_verified_hours(self):
        raw=[self.raw(self.snapshot-timedelta(hours=i)) for i in range(1,25)]
        with patch.object(main,"now_kst",return_value=self.server_time),\
             patch.object(main,"api_get",return_value=raw):
            rows=main.candles("KRW-XRP","1h",self.snapshot,count=50)
        quality=snapshot_liquidity(rows,self.snapshot)
        self.assertEqual(quality["hourly_bars_observed"],24)
        self.assertEqual(quality["recent_6h_bars_observed"],6)
        self.assertTrue(quality["observed_turnover_is_complete"])
        self.assertEqual(quality["checks_passed"],4)
        self.assertNotIn("2026-11-18 14:00",quality["missing_hour_samples_kst"])

    def test_live_at_exact_close_maintains_two_second_guard(self):
        raw=[self.raw(self.snapshot-timedelta(hours=1))]
        now=self.snapshot+timedelta(seconds=1)
        with patch.object(main,"now_kst",return_value=now),\
             patch.object(main,"api_get",return_value=raw):
            rows=main.candles("KRW-XRP","1h",self.snapshot,count=50)
        self.assertEqual(rows,[])

    def test_live_after_two_seconds_can_include_closed_hour(self):
        raw=[self.raw(self.snapshot-timedelta(hours=1))]
        now=self.snapshot+timedelta(seconds=3)
        with patch.object(main,"now_kst",return_value=now),\
             patch.object(main,"api_get",return_value=raw):
            # The LIVE snapshot itself must be +3 seconds. An older pinned
            # clock exactly at 15:00 must NOT bypass its 2-second guard.
            rows=main.candles("KRW-XRP","1h",now,count=50)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["t"],self.snapshot-timedelta(hours=1))

    def test_historical_four_hour_and_daily_exact_closes(self):
        at4=datetime(2026,11,18,16,0,tzinfo=KST)
        atday=datetime(2026,11,18,0,0,tzinfo=KST)
        with patch.object(main,"now_kst",return_value=self.server_time):
            with patch.object(main,"api_get",
                              return_value=[self.raw(at4-timedelta(hours=4)),
                                            self.raw(at4)]):
                h4=main.candles("KRW-XRP","4h",at4,count=40)
            with patch.object(main,"api_get",
                              return_value=[self.raw(atday-timedelta(days=1)),
                                            self.raw(atday)]):
                day=main.candles("KRW-XRP","day",atday,count=40)
        self.assertEqual([x["t"] for x in h4],[at4-timedelta(hours=4)])
        self.assertEqual([x["t"] for x in day],[atday-timedelta(days=1)])

    def test_historical_just_before_exact_close_cannot_look_ahead(self):
        at=self.snapshot-timedelta(seconds=1)
        with patch.object(main,"now_kst",return_value=self.server_time),\
             patch.object(main,"api_get",
                          return_value=[self.raw(self.snapshot-timedelta(hours=1))]):
            rows=main.candles("KRW-XRP","1h",at,count=50)
        self.assertEqual(rows,[])

    def test_candle_logic_does_not_change_buy_watch_rules(self):
        with patch.object(main,"now_kst",return_value=self.server_time):
            cutoff=main.candle_close_cutoff(self.snapshot)
        self.assertEqual(cutoff,self.snapshot)
        self.assertFalse(cutoff<self.snapshot)


class ExpandedHistoricalUniverseV149Test(unittest.TestCase):
    """Historical market-coverage switch only; no BUY/WATCH gate changes."""

    def test_historical_swing_10_25_50_dropdown_default_25(self):
        html=main.HTML
        self.assertIn('id="history-breadth"',html)
        self.assertIn('<option value="10">상위 10종목',html)
        self.assertIn('<option value="25" selected>상위 25종목',html)
        self.assertIn('<option value="50">상위 50종목',html)

    def test_multiday_swing_uses_coverage_override_not_generic_40(self):
        html=main.HTML
        self.assertIn("const expandedSwing=mode==='backtest'&&strategy==='swing'&&daysCount>1;",html)
        self.assertIn("if(expandedSwing){",html)
        self.assertIn("items=items.slice(0,historicalLimit);",html)
        self.assertIn("if(![10,25,50].includes(historicalLimit))",html)

    def test_one_day_live_and_scalp_bounds_stay_intact(self):
        html=main.HTML
        self.assertIn("items=scope==='all'?items:items.slice(0,Number(scope));",html)
        self.assertIn("(daysCount===7?10:20)",html)
        self.assertIn("if(daysCount>1)items=items.slice(0,historicalLimit);",html)
        self.assertIn("if(mode==='backtest')cfg.at=",html)

    def test_existing_four_worker_parallelism_is_unchanged(self):
        html=main.HTML
        self.assertIn("Array.from({length:Math.min(4,items.length)},work)",html)
        self.assertIn("for(const c of dates){",html)

    def test_only_historical_multiday_swing_shows_breadth(self):
        html=main.HTML
        self.assertIn("mode==='backtest'&&strategy==='swing'&&Number($('days').value)>1",html)
        self.assertIn("syncHistoricalBreadth();",html)
        self.assertIn("id=\"backtest-days-field\"",html)
        self.assertIn("id=\"history-breadth-field\"",html)

    def test_cohort_and_candidates_export_requested_and_actual_scope(self):
        html=main.HTML
        self.assertIn("lastCfg.historical_coin_limit=expandedSwing?historicalLimit:null;",html)
        self.assertIn("lastCfg.actual_selected_markets=items.length;",html)
        self.assertIn("requested_market_limit",html)
        self.assertIn("actual_selected_markets",html)
        self.assertIn("COHORT_SUMMARY",html)
        self.assertIn("COHORT_DETAILS",html)
        self.assertIn("BTC-MTF-V1.4.9",html)

    def test_old_signal_gates_still_invoked_without_coverage_argument(self):
        import inspect
        source=inspect.getsource(main.coin_api)
        self.assertIn("strict_swing_gate(m,ctx,day,h4)",source)
        self.assertIn("evaluate_combined(previous,gate,explain,m,h4,is_contiguous)",source)
        self.assertIn("classify_watch_tracks(previous,gate,combined,explain,m,ctx)",source)
        self.assertNotIn("historicalLimit",source)
        self.assertNotIn("history-breadth",source)


class Chart360MiniIsolatedResearchTest(unittest.TestCase):
    def setUp(self):
        from chart360_mini_lab import KST as C_KST
        self.C_KST=C_KST
        self.at=datetime(2026,11,23,20,0,tzinfo=C_KST)

    def minute(self,t,o=100,h=101,l=99,c=100,v=2_000_000):
        return {"t":t,"o":o,"h":h,"l":l,"c":c,"v":v}

    def test_hourly_structure_requires_lower_high_and_lower_low(self):
        from chart360_mini_lab import _h4_bearish
        h4=[]
        for i in range(12):
            t=self.at-timedelta(hours=4*(12-i))
            high=115 if i<6 else 105
            low=95 if i<6 else 90
            h4.append(self.minute(t,h=high,l=low))
        self.assertEqual(_h4_bearish(h4,self.at),(True,True))
        self.assertEqual(_h4_bearish(h4[:4],self.at),None)

    def test_m15_choch_requires_closed_bullish_break_and_prior_lower_low(self):
        from chart360_mini_lab import _m15_chochs
        start=self.at-timedelta(minutes=17*15)
        rows=[]
        for i in range(17):
            t=start+timedelta(minutes=15*i)
            if i<8:r=self.minute(t,h=120,l=95,c=100)
            elif i<16:r=self.minute(t,h=102,l=85,c=98)
            else:r=self.minute(t,o=100,h=105,l=99,c=104)
            rows.append(r)
        event=_m15_chochs(rows)
        self.assertEqual(len(event),1)
        self.assertEqual(event[0]["level"],102)
        self.assertEqual(event[0]["end"],self.at)

    def test_stop_first_conservative_replay(self):
        from chart360_mini_lab import _simulate
        rows=[self.minute(self.at,h=104,l=96,c=103)]
        out=_simulate(rows,0,100,98,.04)
        self.assertEqual(out["status"],"RESOLVED")
        self.assertEqual(out["exit_reason"],"STOP")
        self.assertLess(out["net_pct"],0)

    def test_next_minute_gap_must_stay_unknown(self):
        from chart360_mini_lab import _simulate
        rows=[self.minute(self.at,h=100,l=99,c=100),
              self.minute(self.at+timedelta(minutes=2),h=105,l=99,c=104)]
        out=_simulate(rows,0,100,98,.04,max_hold=2)
        self.assertEqual(out["status"],"UNKNOWN")
        self.assertEqual(out["exit_reason"],"MISSING_1M_BAR")
        self.assertIsNone(out["net_pct"])

    def test_open_replay_without_m15_structure_must_not_invent_trades(self):
        from chart360_mini_lab import replay
        bars=[self.minute(self.at+timedelta(minutes=i)) for i in range(80)]
        out=replay("KRW-XRP",bars,[],[],fee_each=.04)
        self.assertEqual(out["summary"]["signals"],0)
        self.assertIsNone(out["summary"]["mean_net_pct"])

    def test_lab_route_mounted_and_original_v149_health_unchanged(self):
        client=main.app.test_client()
        page=client.get("/chart360")
        self.assertEqual(page.status_code,200)
        self.assertIn("CHART360 MINI V0.5",page.get_data(as_text=True))
        self.assertEqual(client.get("/health").get_json()["version"],"BTC MTF V1.4.9")
        invalid=client.post("/api/chart360",json={"markets":["KRW-XRP"],"hours":100})
        self.assertEqual(invalid.status_code,400)
        self.assertFalse(invalid.get_json()["ok"])

    def test_unexpected_api_payload_rejected_and_not_fabricated(self):
        from chart360_mini_lab import _minute_data
        response=type("FakeResponse",(),{
          "raise_for_status":lambda self:None,
          "json":lambda self:{"bad":1},
        })
        with patch("chart360_mini_lab.requests.get",return_value=response()):
            self.assertRaises(RuntimeError,_minute_data,"KRW-XRP",1,self.at,10)

    def test_offline_real_replay_payload_has_no_automatic_orders(self):
        """Keep CI deterministic; live public-candle probe was run separately."""
        from chart360_mini_lab import replay
        self.assertTrue(callable(replay))
        for rule in main.app.url_map.iter_rules():
            if "order" in rule.rule.lower():
                self.assertFalse(set(rule.methods or ()) & {"POST","PUT","PATCH","DELETE"},
                                 "no order-capable HTTP route allowed: "+rule.rule)



class Chart360MiniV02FunnelTest(unittest.TestCase):
    def setUp(self):
        from chart360_mini_lab import KST
        self.at=datetime(2026,11,24,12,0,tzinfo=KST)

    def bar(self,t,lo=99,hi=101,close=100,open_=100,v=2000000):
        return {"t":t,"o":open_,"h":hi,"l":lo,"c":close,"v":v}

    def choch(self):
        # Event closes at self.at. Completed, consecutive 15m rows.
        start=self.at-timedelta(minutes=17*15)
        rows=[]
        for i in range(17):
            t=start+timedelta(minutes=15*i)
            if i<8: r=self.bar(t,lo=95,hi=120)
            elif i<16:r=self.bar(t,lo=85,hi=102,close=98)
            else:r=self.bar(t,lo=99,hi=105,close=104,open_=100)
            rows.append(r)
        return rows

    def h4(self,bearish=True):
        rows=[]
        start=self.at-timedelta(hours=48)
        for i in range(12):
            t=start+timedelta(hours=4*i)
            hi=115 if i<6 else 105
            lo=95 if i<6 else 90
            if not bearish:hi=111;lo=95
            rows.append(self.bar(t,hi=hi,lo=lo))
        return rows

    def one_minutes(self,lo=101):
        return [self.bar(self.at+timedelta(minutes=i),lo=lo,hi=102,
                         close=101,open_=101) for i in range(90)]

    def test_zero_trade_no_choch_has_market_diagnostic_not_empty(self):
        from chart360_mini_lab import replay
        bars=self.one_minutes()
        out=replay("KRW-XRP",bars,[],self.h4(),scan_begin=self.at)
        d=out["diagnostic"]
        self.assertEqual(out["summary"]["signals"],0)
        self.assertEqual(d["primary_blocker_code"],"NO_CHoCH")
        self.assertEqual(d["m15_events_in_test"],0)
        self.assertEqual(d["event_details"],[])
        self.assertIsNone(out["summary"]["mean_net_pct"])

    def test_no_one_minute_data_is_not_misreported_as_no_choch(self):
        from chart360_mini_lab import replay
        out=replay("KRW-XRP",[],self.choch(),self.h4(),scan_begin=self.at)
        self.assertEqual(out["summary"]["signals"],0)
        self.assertEqual(out["diagnostic"]["primary_blocker_code"],"M1_NO_CANDLES")
        self.assertEqual(out["diagnostic"]["last_actual_1m_kst"],None)

    def test_event_h4_missing_has_exact_reason_and_no_trade(self):
        from chart360_mini_lab import replay
        out=replay("KRW-XRP",self.one_minutes(),self.choch(),[],
                   scan_begin=self.at)
        d=out["diagnostic"]
        self.assertEqual(d["m15_events_in_test"],1)
        self.assertEqual(d["h4_missing_event"],1)
        self.assertEqual(d["h4_event_pass"],0)
        self.assertEqual(d["primary_blocker_code"],"H4_DATA_MISSING")
        self.assertEqual(d["event_details"][0]["reason_code"],"H4_DATA_MISSING")
        self.assertEqual(out["summary"]["signals"],0)

    def test_h4_lh_ll_mismatch_identified_separately(self):
        from chart360_mini_lab import replay
        out=replay("KRW-XRP",self.one_minutes(),self.choch(),
                   self.h4(False),scan_begin=self.at)
        d=out["diagnostic"]
        self.assertEqual(d["h4_failed_event"],1)
        self.assertEqual(d["h4_missing_event"],0)
        self.assertEqual(d["primary_blocker_code"],"H4_LH_LL_FAILED")
        self.assertEqual(d["h4_snapshot_code"],"H4_NO_LH_NO_LL")
        self.assertEqual(d["event_details"][0]["entries"],0)

    def test_h4_pass_and_pullback_without_m1_breakout(self):
        from chart360_mini_lab import replay
        out=replay("KRW-XRP",self.one_minutes(),self.choch(),
                   self.h4(True),scan_begin=self.at)
        d=out["diagnostic"]
        self.assertEqual(d["h4_event_pass"],1)
        self.assertEqual(d["pullback_event"],1)
        self.assertEqual(d["m1_breakout_event"],0)
        self.assertEqual(d["primary_blocker_code"],"M1_NO_BREAKOUT")
        self.assertEqual(out["summary"]["signals"],0)

    def test_no_data_api_error_still_has_diagnostic_row(self):
        from chart360_mini_lab import run_backtest
        def fail(market,tf,to,count):
            raise RuntimeError("HTTP 503")
        out=run_backtest(markets=["KRW-XRP","KRW-ETH"],hours=6,
                         now=self.at,getter=fail)
        self.assertEqual(len(out["markets"]),2)
        self.assertEqual(out["total"]["signals"],0)
        for item in out["markets"]:
            self.assertEqual(item["diagnostic"]["primary_blocker_code"],"DATA_API_ERROR")
            self.assertEqual(item["diagnostic"]["reason_counts"],{"DATA_API_ERROR":1})
            self.assertIn("503",item["error"])

    def test_stage_diagnostics_do_not_change_original_trade_signals(self):
        from chart360_mini_lab import replay
        one=self.one_minutes()
        a=replay("KRW-XRP",one,self.choch(),self.h4(),scan_begin=self.at)
        b=replay("KRW-XRP",one,self.choch(),self.h4(),scan_begin=self.at)
        self.assertEqual(a["trades"],b["trades"])
        self.assertEqual(a["summary"],b["summary"])

    def test_lab_has_independent_diagnostic_csv_and_nonempty_entry_gate(self):
        from chart360_mini_lab import HTML, VERSION
        self.assertEqual(VERSION,"CHART360 MINI V0.5")
        self.assertIn('id="diagnostic-csv"',HTML)
        self.assertIn("primary_blocker_code",HTML)
        self.assertIn("event_details",HTML)
        self.assertIn("$('diagnostic-csv').disabled=false",HTML)
        self.assertIn("$('csv').disabled=trades.length===0",HTML)
        self.assertIn("String.fromCharCode(0xFEFF)",HTML)
        self.assertIn("String.fromCharCode(10)",HTML)
        self.assertIn("CHART360_MINI_V0_5_",HTML)
        self.assertNotIn("CHART360_MINI_V0_1_",HTML)

    def test_readonly_route_v02_root_scanner_still_v149(self):
        client=main.app.test_client()
        p=client.get("/chart360")
        self.assertEqual(p.status_code,200)
        self.assertIn("CHART360 MINI V0.5",p.get_data(as_text=True))
        root=client.get("/health")
        self.assertEqual(root.status_code,200)
        self.assertEqual(root.get_json()["version"],"BTC MTF V1.4.9")



class Chart360MiniV03ABRegressionTest(unittest.TestCase):
    def setUp(self):
        from chart360_mini_lab import KST
        self.at=datetime(2026,11,24,12,0,tzinfo=KST)

    def bar(self,t,lo=103.2,hi=103.9,close=103.5,open_=103.5,v=2000000):
        return {"t":t,"o":open_,"h":hi,"l":lo,"c":close,"v":v}

    def m15_choch(self):
        start=self.at-timedelta(minutes=17*15)
        out=[]
        for i in range(17):
            t=start+timedelta(minutes=15*i)
            if i<8:
                out.append(self.bar(t,lo=95,hi=120,close=100,open_=100))
            elif i<16:
                out.append(self.bar(t,lo=85,hi=102,close=98,open_=98))
            else:
                out.append(self.bar(t,lo=99,hi=105,close=104,open_=100))
        return out

    def h4(self,bearish=False):
        start=self.at-timedelta(hours=48)
        out=[]
        for i in range(12):
            t=start+timedelta(hours=i*4)
            hi=(115 if i<6 else 105) if bearish else 111
            lo=(95 if i<6 else 90) if bearish else 95
            out.append(self.bar(t,lo=lo,hi=hi,close=100,open_=100))
        return out

    def m1(self):
        start=self.at-timedelta(minutes=30)
        out=[self.bar(start+timedelta(minutes=i)) for i in range(140)]
        out[30]=self.bar(self.at,lo=102,hi=104,close=103,open_=103.5)
        out[31]=self.bar(self.at+timedelta(minutes=1),lo=102.7,hi=103.9,close=103.2,open_=103.3)
        out[32]=self.bar(self.at+timedelta(minutes=2),lo=103.4,hi=105,close=104.8,open_=103.5)
        for i in range(33,140):
            t=start+timedelta(minutes=i)
            out[i]=self.bar(t,lo=104.0,hi=105.0,close=104.8,open_=104.9)
        return out

    def test_ab_only_h4_gate_differs_in_same_synthetic_candles(self):
        from chart360_mini_lab import replay
        minute=self.m1()
        m15=self.m15_choch()
        h4=self.h4(False)
        a=replay("KRW-XRP",minute,m15,h4,scan_begin=self.at,require_h4=True)
        b=replay("KRW-XRP",minute,m15,h4,scan_begin=self.at,require_h4=False)
        self.assertEqual(a["summary"]["signals"],0)
        self.assertEqual(b["summary"]["signals"],1)
        self.assertEqual(a["diagnostic"]["primary_blocker_code"],"H4_LH_LL_FAILED")
        self.assertEqual(b["diagnostic"]["entry_event"],1)
        self.assertEqual(b["trades"][0]["signal"],"B_ENTRY_TEST")
        self.assertFalse(b["h4_gate_required"])
        self.assertEqual(b["trades"][0]["m15_choch_kst"],self.at.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(b["trades"][0]["entry_kst"],(self.at+timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M"))

    def test_ab_not_extrapolated_from_future_candles(self):
        from chart360_mini_lab import replay
        one=self.m1()[:33]
        b=replay("KRW-XRP",one,self.m15_choch(),self.h4(False),
                 scan_begin=self.at,require_h4=False)
        self.assertEqual(b["summary"]["signals"],0)

    def test_4h_missing_is_a_blocker_only_in_A(self):
        from chart360_mini_lab import replay
        one=self.m1()
        a=replay("KRW-XRP",one,self.m15_choch(),[],
                 scan_begin=self.at,require_h4=True)
        b=replay("KRW-XRP",one,self.m15_choch(),[],
                 scan_begin=self.at,require_h4=False)
        self.assertEqual(a["summary"]["signals"],0)
        self.assertEqual(a["diagnostic"]["primary_blocker_code"],"H4_DATA_MISSING")
        self.assertEqual(b["summary"]["signals"],1)
        self.assertEqual(b["diagnostic"]["primary_blocker_code"],"ENTRY_TEST")
        self.assertIsNone(b["diagnostic"]["h4_snapshot_lower_high"])

    def test_replay_with_realistic_api_returns_both_variants_from_same_calls(self):
        from chart360_mini_lab import run_backtest
        minute=self.m1()
        m15=self.m15_choch()
        h4=self.h4(False)
        called=[]
        def getter(market,tf,to,count):
            called.append((market,tf))
            data={1:minute,15:m15,240:h4}[tf]
            return [x for x in data if x["t"]<to][-count:]
        now=self.at+timedelta(hours=2)
        result=run_backtest(markets=["KRW-XRP"],hours=6,now=now,getter=getter)
        item=result["markets"][0]
        self.assertEqual(item["summary"],item["variants"]["A"]["summary"])
        self.assertEqual(result["comparison"]["A"]["signals"],0)
        self.assertEqual(result["comparison"]["B"]["signals"],1)
        self.assertEqual(result["comparison"]["B"]["resolved"],1)
        self.assertEqual(result["total"]["signals"],0)
        self.assertLessEqual(len(called),10)  # one dataset fetched, NOT once per variant

    def test_api_error_returns_both_zero_rows_for_diagnostic_export(self):
        from chart360_mini_lab import run_backtest
        def bad(*_):raise RuntimeError("API down")
        out=run_backtest(markets=["KRW-XRP","KRW-NEAR"],hours=12,
                         now=self.at,getter=bad)
        self.assertEqual(len(out["markets"]),2)
        for m in out["markets"]:
            self.assertEqual(set(m["variants"]),{"A","B","C"})
            self.assertEqual(m["variants"]["A"]["diagnostic"]["primary_blocker_code"],"DATA_API_ERROR")
            self.assertEqual(m["variants"]["B"]["diagnostic"]["primary_blocker_code"],"DATA_API_ERROR")
        self.assertEqual(out["comparison"]["A"]["signals"],0)
        self.assertEqual(out["comparison"]["B"]["signals"],0)

    def test_input_accepts_five_krw_coins_rejects_six(self):
        from chart360_mini_lab import run_backtest, MARKETS
        def bad(*_):raise RuntimeError("API down")
        out=run_backtest(markets=list(MARKETS),hours=6,now=self.at,getter=bad)
        self.assertEqual(len(out["markets"]),5)
        self.assertEqual(len(out["markets"][0]["variants"]),3)
        self.assertRaises(ValueError,run_backtest,markets=list(MARKETS)+["KRW-XRP"],hours=6,now=self.at,getter=bad)

    def test_html_ab_market_and_csv_are_labeled(self):
        from chart360_mini_lab import HTML, VERSION
        self.assertEqual(VERSION,"CHART360 MINI V0.5")
        self.assertIn('<option value="ALL">5종목 한 번에 비교</option>',HTML)
        self.assertIn("ABC_DIAGNOSTIC",HTML)
        self.assertIn("ABC_TRADES",HTML)
        self.assertIn("h4_gate_required",HTML)
        self.assertIn("last.markets.flatMap",HTML)
        self.assertIn("CHART360_MINI_V0_5_",HTML)
        self.assertNotIn("CHART360_MINI_V0_2_",HTML)




class Chart360MiniV04ClosedConfirmationTest(unittest.TestCase):
    """Only one new choice: confirm 1m close after B's completed breakout."""
    def setUp(self):
        self.parent=Chart360MiniV03ABRegressionTest()
        self.parent.setUp()

    def scenario(self,confirmation=True):
        m1=self.parent.m1()
        t=self.parent.at
        if confirmation:
            m1[33]=self.parent.bar(t+timedelta(minutes=3),lo=104.7,
                hi=105.1,close=104.9,open_=104.8)
            m1[34]=self.parent.bar(t+timedelta(minutes=4),lo=104.3,
                hi=105.3,close=104.8,open_=104.95)
        return m1,self.parent.m15_choch(),self.parent.h4(False)

    def test_C_entry_one_closed_minute_later_without_changing_B(self):
        from chart360_mini_lab import replay
        one,m15,h4=self.scenario(True)
        b=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,
                 require_h4=False)
        c=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,
                 require_h4=False,confirmation_bars=1)
        self.assertEqual(b["summary"]["signals"],1)
        self.assertEqual(c["summary"]["signals"],1)
        self.assertEqual(b["trades"][0]["entry_kst"],
                         (self.parent.at+timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(c["trades"][0]["entry_kst"],
                         (self.parent.at+timedelta(minutes=4)).strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(c["trades"][0]["entry_delay_minutes"],1)
        self.assertEqual(c["trades"][0]["signal"],"C_CONFIRM_TEST")
        self.assertEqual(c["trades"][0]["confirm_bullish_continuation"],True)
        self.assertEqual(b["trades"][0]["confirm_bullish_continuation"],None)
        self.assertIn("breakout_body_pct",b["trades"][0])
        self.assertIn("prior20_1m_contiguous",c["trades"][0])

    def test_C_rejects_bearish_confirmation_while_B_still_enters(self):
        from chart360_mini_lab import replay
        one,m15,h4=self.scenario(False)
        b=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,
                 require_h4=False)
        c=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,
                 require_h4=False,confirmation_bars=1)
        self.assertEqual(b["summary"]["signals"],1)
        self.assertEqual(c["summary"]["signals"],0)
        self.assertGreaterEqual(c["stats"]["confirm_rejected"],1)
        self.assertEqual(c["diagnostic"]["primary_blocker_code"],"M1_CONFIRM_REJECTED")

    def test_C_missing_next_complete_minute_does_not_invent_entry(self):
        from chart360_mini_lab import replay
        one,m15,h4=self.scenario(True)
        one=one[:34] # C's confirmation candle closed; no following open
        c=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,
                 require_h4=False,confirmation_bars=1)
        self.assertEqual(c["summary"]["signals"],0)
        self.assertGreaterEqual(c["stats"]["confirm_gap"],1)
        self.assertEqual(c["diagnostic"]["primary_blocker_code"],"M1_CONFIRM_DATA_GAP")

    def test_C_cannot_see_future_confirmation_at_breakout_time(self):
        from chart360_mini_lab import replay
        one,m15,h4=self.scenario(True)
        # Remove confirmation candle. There is no permitted completed candle
        # after breakout, so C cannot use its future close.
        c=replay("KRW-XRP",one[:33],m15,h4,scan_begin=self.parent.at,
                 require_h4=False,confirmation_bars=1)
        self.assertEqual(c["summary"]["signals"],0)

    def test_A_and_B_output_unchanged_when_C_is_evaluated(self):
        from chart360_mini_lab import replay
        one,m15,h4=self.scenario(True)
        a1=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,require_h4=True)
        b1=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,require_h4=False)
        c=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,require_h4=False,confirmation_bars=1)
        a2=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,require_h4=True)
        b2=replay("KRW-XRP",one,m15,h4,scan_begin=self.parent.at,require_h4=False)
        self.assertEqual(a1["trades"],a2["trades"])
        self.assertEqual(b1["trades"],b2["trades"])
        self.assertEqual(a1["summary"],a2["summary"])
        self.assertEqual(b1["summary"],b2["summary"])
        self.assertEqual(c["variant"],"C")

    def test_error_three_variant_rows_and_comparison_still_zero(self):
        from chart360_mini_lab import run_backtest
        def fail(*args): raise RuntimeError("503")
        r=run_backtest(markets=["KRW-XRP"],hours=6,
                       now=self.parent.at,getter=fail)
        self.assertEqual(set(r["markets"][0]["variants"]),{"A","B","C"})
        self.assertEqual(r["comparison"]["C"]["signals"],0)
        self.assertEqual(r["comparison"]["C"]["unknown"],0)

    def test_v04_html_csv_includes_C_confirmation_and_both_features(self):
        from chart360_mini_lab import HTML,VERSION
        self.assertEqual(VERSION,"CHART360 MINI V0.5")
        self.assertIn("['A','B','C']",HTML)
        self.assertIn("confirm_reject_event",HTML)
        self.assertIn("confirm_gap_event",HTML)
        self.assertIn("breakout_upper_wick_pct",HTML)
        self.assertIn("prior20_1m_contiguous",HTML)
        self.assertIn("ABC_DIAGNOSTIC",HTML)
        self.assertIn("ABC_TRADES",HTML)
        self.assertIn("CHART360_MINI_V0_5_",HTML)




class Chart360MiniV04DataCoverageTest(unittest.TestCase):
    def test_full_30m_warmup_and_6h_candles_means_complete_only_if_all_observed(self):
        from chart360_mini_lab import historical_1m, KST
        end=datetime(2026,11,24,12,0,tzinfo=KST)
        start=end-timedelta(minutes=390)
        rows=[{"t":start+timedelta(minutes=i),
               "o":100,"h":101,"l":99,"c":100,"v":1.0}
              for i in range(390)]
        def complete(m,tf,to,count):
            return [r for r in rows if r["t"]<to][-count:]
        bars,audit=historical_1m("KRW-XRP",end,6,getter=complete)
        self.assertEqual(len(bars),390)
        self.assertEqual(audit["minutes_missing"],0)
        self.assertEqual(audit["coverage_pct"],100.0)
        self.assertFalse(audit["incomplete"])
        rows.pop(100)
        _,audit=historical_1m("KRW-XRP",end,6,getter=complete)
        self.assertEqual(audit["minutes_missing"],1)
        self.assertTrue(audit["incomplete"])
        self.assertLess(audit["coverage_pct"],100.0)



class Chart360MiniV05MultiDayRegressionTest(unittest.TestCase):
    def setUp(self):
        from chart360_mini_lab import KST
        self.end=datetime(2026,11,24,19,0,tzinfo=KST)

    def candle(self,t):
        return {"t":t,"o":100.0,"h":101.0,"l":99.0,"c":100.0,"v":1_000_000.0}

    def fixture(self,hours=168):
        begin=self.end-timedelta(hours=hours,minutes=30)
        return [self.candle(begin+timedelta(minutes=i))
                for i in range(hours*60+30)]

    def test_paged_seven_days_1m_over_fifty_200_bar_calls(self):
        from chart360_mini_lab import historical_1m
        data=self.fixture(168)
        calls=[]
        def getter(market,tf,to,count):
            calls.append((to,count))
            return [r for r in data if r["t"]<to][-count:]
        bars,audit=historical_1m("KRW-XRP",self.end,168,getter=getter)
        self.assertEqual(len(bars),10110)
        self.assertEqual(audit["minutes_requested"],10110)
        self.assertEqual(audit["minutes_observed"],10110)
        self.assertFalse(audit["incomplete"])
        self.assertEqual(audit["coverage_pct"],100.0)
        self.assertGreater(len(calls),50)
        self.assertLess(len(calls),65)
        self.assertTrue(all(n<=200 for _,n in calls))
        self.assertTrue(all(calls[i+1][0]<calls[i][0]
                            for i in range(len(calls)-1)))

    def test_bars_after_signal_end_are_exit_only(self):
        from chart360_mini_lab import historical_1m
        end=self.end
        scan_end=end-timedelta(minutes=60)
        begin=scan_end-timedelta(hours=72,minutes=30)
        data=[self.candle(begin+timedelta(minutes=i))
              for i in range(int((end-begin).total_seconds()/60))]
        def getter(m,tf,to,count):
            return [r for r in data if r["t"]<to][-count:]
        bars,audit=historical_1m("KRW-XRP",end,72,getter=getter,scan_end=scan_end)
        self.assertEqual(len(bars),72*60+90)
        self.assertEqual(audit["minutes_requested"],72*60+90)
        self.assertEqual(sum(b["t"]>=scan_end for b in bars),60)

    def test_15minute_backfill_over_200_rows(self):
        from chart360_mini_lab import historical_15m
        signal_end=self.end
        signal_begin=signal_end-timedelta(hours=168)
        start=signal_begin-timedelta(hours=7)
        bars=[self.candle(start+timedelta(minutes=15*i)) for i in range(710)]
        calls=[]
        def getter(m,tf,to,count):
            calls.append((tf,to,count))
            return [r for r in bars if r["t"]<to][-count:]
        out,audit=historical_15m("KRW-XRP",signal_begin,signal_end,getter)
        self.assertGreater(len(out),650)
        self.assertGreater(len(calls),3)
        self.assertTrue(all(c[0]==15 and c[2]==200 for c in calls))
        self.assertLessEqual(out[-1]["t"]+timedelta(minutes=15),signal_end)

    def test_calendar_split_boundary_partial_days_and_zero_signals(self):
        from chart360_mini_lab import _daily_rows
        start=datetime(2026,11,17,18,0,tzinfo=self.end.tzinfo)
        finish=start+timedelta(hours=168)
        data=[]
        variants={k:{"trades":[]} for k in ("A","B","C")}
        daily=_daily_rows("KRW-XRP",variants,data,start,finish)
        self.assertEqual(len(daily),8*3)
        self.assertTrue(daily[0]["partial_day"])
        self.assertFalse(next(x for x in daily if x["date_kst"]=="2026-11-18")["partial_day"])
        self.assertEqual(daily[0]["signals"],0)
        self.assertIsNone(daily[0]["mean_net_pct"])
        self.assertEqual(daily[0]["data_status"],"INCOMPLETE")
        self.assertEqual(daily[-1]["date_kst"],"2026-11-24")

    def test_paired_comparison_ignores_unknown_and_matches_breakout(self):
        from chart360_mini_lab import _paired_trades,_pair_summary
        def t(breakout,entry,status,net,reason):
            return {"breakout_kst":breakout,"entry_kst":entry,
                    "status":status,"net_pct":net,"exit_reason":reason}
        b={"trades":[t("2026-11-20 10:00","2026-11-20 10:00","RESOLVED",1.2,"TP_1_5R"),
                     t("2026-11-20 11:00","2026-11-20 11:00","UNKNOWN",None,"MISSING_1M_BAR"),
                     t("2026-11-20 12:00","2026-11-20 12:00","RESOLVED",-1,"STOP")]}
        c={"trades":[t("2026-11-20 10:00","2026-11-20 10:01","RESOLVED",1.5,"TP_1_5R"),
                     t("2026-11-20 11:00","2026-11-20 11:01","RESOLVED",0.8,"TP_1_5R"),
                     t("2026-11-20 13:00","2026-11-20 13:01","RESOLVED",0.1,"TIME_EXIT")]}
        pairs=_paired_trades("KRW-XRP",{"A":{"trades":[]},"B":b,"C":c})
        m=_pair_summary(pairs)
        self.assertEqual(m["paired"],2)
        self.assertEqual(m["resolved_pairs"],1)
        self.assertEqual(m["unresolved_pairs"],1)
        self.assertEqual(m["B_only"],1)
        self.assertEqual(m["C_only"],1)
        self.assertEqual(m["mean_C_minus_B_pctpoints"],0.3)

    def test_three_day_no_data_error_has_dates_not_fake_losses(self):
        from chart360_mini_lab import run_backtest
        def fail(*args):raise RuntimeError("503 no data")
        out=run_backtest(markets=["KRW-XRP"],hours=72,now=self.end,getter=fail)
        self.assertEqual(len(out["daily_by_market"]),12)
        self.assertEqual(out["comparison"]["B"]["signals"],0)
        self.assertEqual(out["failed_markets"],["KRW-XRP"])
        self.assertTrue(all(row["data_status"]=="API_ERROR" for row in out["daily_by_market"]))
        self.assertTrue(all(row["unknown"]==0 for row in out["daily_by_market"]))
        self.assertEqual(out["pair_summary"]["paired"],0)

    def test_seven_day_time_window_is_168_hours_and_evaluation_end_hour_earlier(self):
        from chart360_mini_lab import run_backtest
        def fail(*args):raise RuntimeError("temporarily unavailable")
        out=run_backtest(markets=["KRW-XRP"],hours=168,now=self.end,getter=fail)
        from datetime import datetime as DT
        start=DT.fromisoformat(out["scan_begin_kst"])
        stop=DT.fromisoformat(out["scan_end_kst"])
        self.assertEqual(stop-start,timedelta(hours=168))
        self.assertEqual(DT.fromisoformat(out["asof_kst"])-stop,timedelta(minutes=60))
        self.assertEqual(out["exit_horizon_minutes"],60)
        self.assertEqual(out["comparison"]["A"]["resolved"],0)

    def test_html_has_7_day_daywise_and_paired_CSV_export(self):
        from chart360_mini_lab import HTML,VERSION
        self.assertEqual(VERSION,"CHART360 MINI V0.5")
        self.assertIn('<option value="72" selected>최근 3일</option>',HTML)
        self.assertIn('<option value="168">최근 7일</option>',HTML)
        self.assertIn('id="daily-csv"',HTML)
        self.assertIn('id="pairs-csv"',HTML)
        self.assertIn('id="daily-view"',HTML)
        self.assertIn('id="pair-view"',HTML)
        self.assertIn("BC_PAIRED_BREAKOUTS",HTML)
        self.assertIn("DAILY_MARKET",HTML)
        self.assertIn("asof_kst:asof||null",HTML)
        self.assertNotIn("CHART360_MINI_V0_4_",HTML)



class Chart360MiniV05SignalWindowSafetyTest(unittest.TestCase):
    def test_C_requires_confirmation_close_inside_requested_signal_window(self):
        from chart360_mini_lab import replay
        example=Chart360MiniV04ClosedConfirmationTest()
        example.setUp()
        minute,m15,h4=example.scenario(True)
        t=example.parent.at
        a=replay("KRW-XRP",minute,m15,h4,scan_begin=t,
                 scan_end=t+timedelta(minutes=4),require_h4=False)
        c=replay("KRW-XRP",minute,m15,h4,scan_begin=t,
                 scan_end=t+timedelta(minutes=4),
                 require_h4=False,confirmation_bars=1)
        self.assertEqual(a["summary"]["signals"],1)
        self.assertEqual(c["summary"]["signals"],0)

    def test_two_different_cutoffs_not_accepted_in_one_replay(self):
        from chart360_mini_lab import run_backtest
        from datetime import datetime
        from chart360_mini_lab import KST
        now=datetime(2026,11,24,20,0,tzinfo=KST)
        with self.assertRaises(ValueError):
            run_backtest(markets=["KRW-XRP"],hours=168,now=now,
                         asof_kst="2026-11-24 19:58")



class OrderflowMiniV01ResearchTest(unittest.TestCase):
    def setUp(self):
        import orderflow_mini_lab as flow
        self.flow=flow
        self.now=1800000000000

    def book(self,market="KRW-XRP",age_ms=1000):
        return {"market":market,"timestamp":self.now-age_ms,
            "orderbook_units":[{"bid_price":100,"bid_size":1000000,
                                "ask_price":100.1,"ask_size":100000}]}

    def ticks(self,market="KRW-XRP"):
        return ([{"market":market,"timestamp":self.now-2000,
                  "trade_price":100,"trade_volume":50000,"ask_bid":"BID"}]*4+
                [{"market":market,"timestamp":self.now-3000,
                  "trade_price":100,"trade_volume":25000,"ask_bid":"ASK"}]*2)

    def test_book_notional_and_actual_trade_are_separate(self):
        r=self.flow.analyze("KRW-XRP",self.book(),self.ticks(),self.now)
        self.assertTrue(r["book_ok"])
        self.assertEqual(r["bid5_krw"],100000000)
        self.assertEqual(r["ask5_krw"],10010000)
        self.assertEqual(r["trade_buy_krw_30s"],20000000)
        self.assertEqual(r["trade_sell_krw_30s"],5000000)
        self.assertEqual(r["trade_net_krw_30s"],15000000)
        self.assertEqual(r["trade_buy_share_pct_30s"],80)
        self.assertEqual(r["score"],4)
        self.assertEqual(r["status"],"관찰 우세")
        self.assertEqual(r["trade_count_30s"],6)

    def test_stale_orderbook_is_not_a_signal(self):
        r=self.flow.analyze("KRW-XRP",self.book(age_ms=90000),
                            self.ticks(),self.now)
        self.assertIsNone(r["score"])
        self.assertFalse(r["book_fresh"])
        self.assertEqual(r["status"],"자료 주의")

    def test_rest_trade_sample_capped_blocks_scoring(self):
        many=self.ticks()[:1]*self.flow.TRADE_COUNT
        r=self.flow.analyze("KRW-XRP",self.book(),many,self.now)
        self.assertTrue(r["trade_sample_capped"])
        self.assertIsNone(r["score"])

    def test_no_recent_trades_is_not_fabricated_buy_or_zero_score(self):
        old=[{"market":"KRW-XRP","timestamp":self.now-60000,
              "trade_price":100,"trade_volume":200,
              "ask_bid":"BID"}]
        r=self.flow.analyze("KRW-XRP",self.book(),old,self.now)
        self.assertEqual(r["trade_count_30s"],0)
        self.assertIsNone(r["trade_buy_share_pct_30s"])
        self.assertIsNone(r["score"])
        self.assertEqual(r["status"],"자료 주의")

    def test_trade_error_keeps_book_notional_but_no_score(self):
        r=self.flow.analyze("KRW-XRP",self.book(),None,self.now,
                            trade_error="429 Too Many Requests")
        self.assertTrue(r["book_ok"])
        self.assertEqual(r["bid5_krw"],100000000)
        self.assertIsNone(r["score"])
        self.assertIn("429",r["trade_error"])

    def test_all_five_fixed_markets_scan_one_book_and_trade_each(self):
        calls=[]
        def fetch(path,params):
            calls.append((path,params))
            if path=="/v1/orderbook":
                return [self.book(m) for m in self.flow.MARKETS]
            return self.ticks(params["market"])
        out=self.flow.scan(fetcher=fetch,now_ms=self.now)
        self.assertEqual(out["version"],"ORDERFLOW MINI V0.1")
        self.assertEqual(len(out["markets"]),5)
        self.assertEqual(len(calls),6)
        self.assertEqual(sum(path=="/v1/orderbook" for path,_ in calls),1)
        self.assertEqual(len(set(x["market"] for x in out["markets"])),5)
        self.assertTrue(all(x["score"]==4 for x in out["markets"]))
        self.assertTrue(all(x["trade_count_30s"]==6 for x in out["markets"]))

    def test_bad_orderbook_cannot_create_false_candidate(self):
        r=self.flow.analyze("KRW-XRP",
                            {"market":"KRW-XRP","orderbook_units":[]},
                            self.ticks(),self.now)
        self.assertFalse(r["book_ok"])
        self.assertIsNone(r["score"])
        self.assertEqual(r["status"],"데이터 확인")

    def test_html_and_isolated_routes_leave_existing_health_unchanged(self):
        from unittest.mock import patch
        self.assertIn("ORDERFLOW MINI V0.1",self.flow.HTML)
        self.assertIn("data-market",self.flow.HTML)
        self.assertIn("setInterval(update,15000)",self.flow.HTML)
        self.assertIn("ORDERFLOW_MINI_V0_1_",self.flow.HTML)
        self.assertIn("최근 30초",self.flow.HTML)
        c=main.app.test_client()
        home=c.get("/health")
        self.assertEqual(home.status_code,200)
        self.assertEqual(home.get_json()["version"],"BTC MTF V1.4.9")
        page=c.get("/orderflow")
        self.assertEqual(page.status_code,200)
        self.assertIn("ORDERFLOW MINI V0.1",page.get_data(as_text=True))
        with patch.object(main,"scan_orderflow_snapshot",
                          return_value={"markets":[],"version":"ORDERFLOW MINI V0.1"}):
            response=c.get("/api/orderflow")
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.get_json()["ok"])

    def test_api_failure_reports_error_not_fake_success(self):
        from unittest.mock import patch
        with patch.object(main,"scan_orderflow_snapshot",side_effect=RuntimeError("429")):
            response=main.app.test_client().get("/api/orderflow")
            self.assertEqual(response.status_code,502)
            self.assertFalse(response.get_json()["ok"])
            self.assertIn("429",response.get_json()["error"])




if __name__=="__main__":
    unittest.main()
