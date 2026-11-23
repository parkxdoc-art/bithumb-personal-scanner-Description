"""API regression: pinned swing MACD is available even when no candidate."""
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
import bithumb_btc_mtf_v1 as main

class FocusDiagnosticTest(unittest.TestCase):
    def setUp(self):
        d=datetime(2026,8,1,tzinfo=main.KST)
        h=datetime(2026,10,1,tzinfo=main.KST)
        def rows(start, step, n, slope):
            return [
                {"t":start+step*i, "c":100+i*slope,
                 "o":99.8+i*slope, "h":100.7+i*slope,
                 "l":99.3+i*slope, "turnover":10000000.0}
                for i in range(n)
            ]
        self.day=rows(d,timedelta(days=1),80,.3)
        self.h4=rows(h,timedelta(hours=4),90,.18)
        self.h1=rows(h,timedelta(hours=1),50,.06)
        self.m={
            "score":9,"day_up":True,"h4_up":True,"day_ma20_distance_pct":2,"rsi14":56,
            "macd_hist":0.13,"volume_ratio":1.3,"atr_pct":1.4,"bb_position":0.65,
            "body_ratio":0.65,"h4_distance_ma7_pct":1.4,
            "h4_higher_low":True,"h4_higher_high":True,"relative_pct":1.1,
            "close":self.h4[-1]["c"],"rebound_from_low_pct":3.4,
            "structure_risk_pct":4.2,"data_gap_recent":False,
            "checks":{"h1_confirm":True,"ma_recover":True,
                      "rsi_recover":True,"macd_recover":True},
            "closed_kst":"2026-10-10 00:00",
        }
    def fetch(self,market,tf,at=None,count=70):
        if tf=="day":return self.day
        if tf=="4h":return self.h4
        if tf=="1h":return self.h1
        raise AssertionError(tf)
    def test_pinned_symbol_returns_macd_even_without_candidate_signal(self):
        with patch.object(main,"market_list",return_value={"KRW-NEAR":"니어프로토콜"}),\
             patch.object(main,"btc_context",return_value={"long":"up","short":"side"}),\
             patch.object(main,"candles",side_effect=self.fetch),\
             patch.object(main,"ttl_get",return_value=None),\
             patch.object(main,"swing_metrics",return_value=({},None)),\
             patch.object(main.rebound,"bottom_rebound_metrics",return_value=(dict(self.m),None)),\
             patch.object(main.rebound,"rebound_signal",return_value=None):
            client=main.app.test_client()
            resp=client.post("/api/coin",json={
                "market":"KRW-NEAR","strategy":"swing","mode":"live",
                "tf":"4h","fee":0.25,"want_diagnostic":True
            })
            self.assertEqual(resp.status_code,200,resp.get_json())
            data=resp.get_json()
            self.assertTrue(data["ok"])
            self.assertIsNone(data["signal"])
            self.assertEqual(data["diagnostic"]["score"],9)
            self.assertEqual(data["diagnostic"]["macd"]["day_chart"]["params"],"3,10,16")
            self.assertEqual(data["diagnostic"]["macd"]["h4"]["params"],"12,26,9")
            self.assertIn("scores",data["diagnostic"]["explain"])
            self.assertEqual(data["diagnostic"]["explain"]["status"],"RESEARCH_ONLY")
            self.assertIsNone(data["diagnostic"]["explain"]["sell_pressure_proxy"]["orderbook_sell_wall"])

if __name__=="__main__":
    unittest.main()
