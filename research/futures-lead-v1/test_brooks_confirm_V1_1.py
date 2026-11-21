#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reproducible offline tests for Brooks paired entry V1.1. Never calls exchange."""
import csv
import io
import unittest
from unittest.mock import patch
import brooks_confirm_lab_V1_1 as m
import classic_priceaction_lab as core


def bars_fixture(n=1160):
    t=(1_783_000_000_000//core.HOUR)*core.HOUR
    return [{'t':t+i*core.FIVE,'o':100.0,'h':105.0,'l':95.0,'c':100.0,'v':10_000_000.0} for i in range(n)]


class TestBrooksConfirm(unittest.TestCase):
    def test_version_and_filename(self):
        self.assertEqual(m.VERSION,'1.1')
        self.assertIn('V1_1',m.CSV_NAME)
        self.assertIn('V1_1',m.DIAG_NAME)
        self.assertIn('V1.1',m.HTML)
    def test_same_baseline_close_model(self):
        bars=bars_fixture(80);bars[11].update(o=100,c=101,h=102)
        bars[12].update(c=102.2,h=104)
        a=m.create_pair('BTC',bars,10,1_000_000_000)
        base=core.exit_trade(bars,10)
        self.assertEqual(a['base_net_pct'],base['net_pct'])
        self.assertEqual(a['base_entry_kst'],core.kst(base['entry_time_ms']))
        self.assertTrue(a['confirmed'])
    def test_confirmation_uses_completed_candle(self):
        b=bars_fixture(50)
        b[10]['c']=100;b[11].update(o=100,c=100.2)
        self.assertTrue(m.bullish_confirmation(b,10))
        b[11]['c']=99.9
        self.assertFalse(m.bullish_confirmation(b,10))
        b[11].update(o=101,c=100.2)
        self.assertFalse(m.bullish_confirmation(b,10))
    def test_confirmed_enters_one_candle_later(self):
        b=bars_fixture(50)
        b[11].update(o=100,c=101);b[12]['c']=101.5
        p=m.create_pair('ETH',b,10,1e9)
        self.assertEqual(p['base_entry_price'],101)
        self.assertEqual(p['confirmed_entry_price'],101.5)
        self.assertEqual(p['base_entry_kst'],core.kst(b[11]['t']+core.FIVE))
        self.assertEqual(p['confirmed_entry_kst'],core.kst(b[12]['t']+core.FIVE))
        self.assertEqual(p['confirm_bar_kst'],p['base_entry_kst'])
    def test_skipped_has_no_fabricated_entry(self):
        b=bars_fixture(50);b[11]['c']=99
        p=m.create_pair('BTC',b,10,1e9)
        self.assertFalse(p['confirmed'])
        self.assertEqual(p['confirmed_entry_kst'],'')
        self.assertEqual(p['confirmed_net_pct'],'')
        self.assertEqual(p['policy_net_pct'],0.0)
    def test_stop_uses_confirmed_entry_and_fee(self):
        b=bars_fixture(50);b[11].update(o=100,c=101);b[12]['c']=101;b[13]['c']=99
        p=m.create_pair('BTC',b,10,1e9)
        self.assertEqual(p['confirmed_reason'],'손절')
        self.assertLess(p['confirmed_net_pct'],-1.5)
    def test_take_after_entry(self):
        b=bars_fixture(50);b[11].update(o=100,c=100.5);b[12]['c']=100.0;b[13]['c']=101.5
        p=m.create_pair('BTC',b,10,1e9)
        self.assertEqual(p['confirmed_reason'],'익절')
        self.assertGreater(p['confirmed_net_pct'],0.5)
    def test_time_expiry_60min(self):
        b=bars_fixture(50);b[11].update(o=100,c=100.5);b[12]['c']=100.5
        p=m.create_pair('BTC',b,10,1e9)
        self.assertEqual(p['confirmed_reason'],'시간만료')
        self.assertEqual(p['confirmed_hold_min'],60)
    def test_not_using_high_as_tp(self):
        b=bars_fixture(50);b[11].update(o=100,c=100.5)
        for bar in b[13:25]:bar['h']=150;bar['l']=1
        p=m.create_pair('BTC',b,10,1e9)
        self.assertEqual(p['confirmed_reason'],'시간만료')
    def test_last_completed_candle_needed(self):
        b=bars_fixture(15)
        with self.assertRaises(ValueError):m.exit_after_entry(b,3)
    def test_paired_delta(self):
        b=bars_fixture(50);b[11].update(o=100,c=101);b[12]['c']=100.8;b[13]['c']=102
        p=m.create_pair('BTC',b,10,1e9)
        self.assertAlmostEqual(p['paired_delta_pct'],p['confirmed_net_pct']-p['base_net_pct'],places=4)
    def test_coverage_gap(self):
        b=bars_fixture(10);b.pop(3)
        result=m.coverage_report('BTC',b,b[0]['t'],b[0]['t']+10*core.FIVE)
        self.assertEqual(result['expected_bars'],10)
        self.assertEqual(result['observed_bars'],9)
        self.assertEqual(result['missing_bars'],1)
        self.assertEqual(result['gap_count'],1)
    def test_coincident_strategy_cap_from_source(self):
        b=bars_fixture(80);b[11].update(o=100,c=101)
        x=m.create_pair('BTC',b,10,1e9)
        y=dict(x,signal_ms=x['signal_ms']+5*core.FIVE)
        z=dict(x,signal_ms=x['signal_ms']+core.COOLDOWN_MS)
        selected,dropped=m.select_pairs([x,y,z])
        self.assertEqual(len(selected),2)
        self.assertEqual(dropped['cooldown'],1)
    def test_missed_profitable_trades_reported(self):
        b=bars_fixture(80);b[11]['c']=99.8
        b[12]['c']=101.0
        skip=m.create_pair('BTC',b,10,1e9)
        b2=bars_fixture(80);b2[11].update(o=100,c=101);b2[12]['c']=101.0
        good=m.create_pair('ETH',b2,10,1e9)
        summary,_=m.summarise([skip,good],b[10]['t'],b[10]['t']+2*core.DAY)
        self.assertEqual(summary['base_all']['n'],2)
        self.assertEqual(summary['skipped_base']['n'],1)
        self.assertEqual(summary['confirmed_only']['n'],1)
        self.assertEqual(summary['confirmed_policy']['n'],2)
    def test_daily_zero_days(self):
        since=1_783_000_000_000//core.DAY*core.DAY
        _,days=m.summarise([],since,since+2*core.DAY)
        self.assertEqual(len(days),3)
        self.assertTrue(all(x['signals']==0 and x['base_avg'] is None for x in days))
    def test_csv_utf8_and_no_nested_dict(self):
        b=bars_fixture(40);b[11].update(o=100,c=101)
        p=m.create_pair('BTC',b,10,1e9)
        out=m.csv_text([p],m.PAIR_FIELDS)
        self.assertTrue(out.startswith('\ufeffcoin,'))
        rows=list(csv.DictReader(io.StringIO(out.lstrip('\ufeff'))))
        self.assertEqual(rows[0]['coin'],'BTC')
        self.assertEqual(rows[0]['confirmed'],'True')
    def test_insufficient_candles_status(self):
        b=bars_fixture(25)
        signals,diag=m.scan_brooks('BTC',b,b[0]['t'],b[-1]['t']+core.FIVE)
        self.assertEqual(signals,[])
        self.assertEqual(diag['status'],'INSUFFICIENT')
    def test_full_24h_bars_needed(self):
        b=bars_fixture(1160)
        # Mock 1h gate to isolate 24h completeness. Never sees live HTTP.
        ends=[b[0]['t']+i*core.HOUR for i in range(200)]
        hours=[{'up':True} for _ in ends]
        with patch.object(core,'hourly_trend',return_value=(ends,hours)),patch.object(core,'is_brooks',return_value=True):
            a,da=m.scan_brooks('BTC',b,b[320]['t'],b[1130]['t'])
            self.assertGreater(len(a),0)
            # Remove one candle so that a genuine 24h gap around i fails.
            b2=b[:500]+b[501:]
            bb,db=m.scan_brooks('BTC',b2,b[320]['t'],b[1130]['t'])
            self.assertTrue(any(x['signal_ms']==b[500]['t']+core.FIVE for x in a))
            self.assertFalse(any(x['signal_ms']==b[500]['t']+core.FIVE for x in bb))
    def test_no_http_in_injected_research(self):
        b=bars_fixture(1200)
        def fake(coin,a,z):return b
        r=m.run_research('BTC,ETH',6,now_ms=b[-1]['t']+core.FIVE,acquire=fake)
        self.assertEqual(r['version'],m.VERSION)
        self.assertEqual(len(r['diagnostics']),2)
    def test_coverage_error_record(self):
        def raise_api(coin,a,z):raise core.ResearchError('가상 네트워크 실패')
        with self.assertRaises(core.ResearchError):
            m.run_research('BTC',6,acquire=raise_api,now_ms=bars_fixture(10)[-1]['t'])
    def test_api_limit_break_stops_remaining(self):
        def limited(coin,a,z):raise core.ResearchError('빗썸 API 요청 제한(HTTP 429)')
        with self.assertRaises(core.ResearchError):
            m.run_research('BTC,ETH',6,acquire=limited,now_ms=bars_fixture(10)[-1]['t'])


def run_tests():
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(TestBrooksConfirm)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():raise AssertionError('V1.1 regression tests failed')
    print('PASS:',result.testsRun,'Brooks V1.1 offline regression tests')


if __name__=='__main__':run_tests()