"""Offline regression tests for BITHUMB classic three-strategy V1.0."""
import unittest
import sys
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import math

sys.path.insert(0,__file__.rsplit('/',1)[0])
import classic_priceaction_lab as p

T=1_700_000_000_000//p.HOUR*p.HOUR


def mkbars(n=2000, oscillate=True):
    out=[]
    for i in range(n):
        val=100+0.02*i+(0.45*math.sin(i*.42)+0.2*math.sin(i*.97) if oscillate else 0)
        prior=100+0.02*(i-1)+(0.45*math.sin((i-1)*.42)+0.2*math.sin((i-1)*.97) if oscillate else 0)
        out.append(dict(t=T+i*p.FIVE,o=prior,c=val,h=max(val,prior)+0.08,l=min(val,prior)-0.08,v=5_000_000))
    return out

class ClassicResearchTests(unittest.TestCase):
    def test_coin_validation(self):
        self.assertEqual(p.coin_list('btc,ETH,BTC'),['BTC','ETH'])
        for val in ('','BTC;ETH',','.join('C%02d' % i for i in range(21))):
            with self.assertRaises(ValueError):p.coin_list(val)

    def test_no_complete_hour_from_incomplete_data(self):
        b=mkbars(1000)
        e,h=p.hourly_trend(b)
        self.assertTrue(all(t%p.HOUR==0 for t in e))
        # Delete one candle, making a gap inside a formerly complete hour.
        lost=b.pop(711)
        e2,_=p.hourly_trend(b)
        self.assertLess(len(e2),len(e))
        self.assertNotEqual(lost['t']//p.HOUR*p.HOUR+p.HOUR, next((t for t in e2 if t==lost['t']//p.HOUR*p.HOUR+p.HOUR),None))

    def test_hourly_trend_is_past_only(self):
        b=mkbars(1900)
        a=p.hourly_trend(b)
        b[-4]['c']*=100
        later=p.hourly_trend(b)
        early=min(len(a[0]),len(later[0]))-1
        self.assertEqual(a[1][early-1],later[1][early-1])

    def test_day_cap_and_cooldown(self):
        signals=[]
        for i in range(25):
            signals.append({'coin':f'C{i}', 'strategy':'BROOKS','signal_ms':T+i*p.FIVE, 'net_pct':0.4,'exit_reason':'시간만료'})
        chosen,drop=p.cap_and_cooldown(signals)
        self.assertEqual(len(chosen),4)
        self.assertEqual(drop['daily_cap'],21)

    def test_no_overlapping_research_trade_signal(self):
        b=mkbars()
        c,diag=p.candidates_for_coin('BTC',b,T+800*p.FIVE,T+2000*p.FIVE)
        self.assertTrue(c)
        self.assertGreater(diag['trend_bars'],0)
        self.assertTrue(set(x['strategy'] for x in c).issubset(set(p.STRATEGIES)))
        for r in c:
            self.assertGreater(r['entry_time_ms'],r['signal_ms'])
            self.assertGreater(r['exit_time_ms'],r['entry_time_ms'])
            self.assertLessEqual(r['hold_min'],60)
            self.assertGreater(r['liquidity_24h_krw'],500_000_000)

    def test_no_signals_with_gap_in_future(self):
        b=mkbars(1500)
        b[1100]['t']+=2*p.FIVE
        _,d=p.candidates_for_coin('BTC',b,T+800*p.FIVE,T+1500*p.FIVE)
        self.assertGreaterEqual(d['gaps'],1)

    def test_fee_and_first_exit(self):
        b=mkbars(150)
        i=50
        b[i+1]['c']=100
        b[i+2]['c']=101.12
        b[i+3]['c']=96
        got=p.exit_trade(b,i)
        self.assertEqual(got['exit_reason'],'익절')
        self.assertEqual(got['hold_min'],5)
        self.assertAlmostEqual(got['net_pct'],0.62)

    def test_stop_before_rebound(self):
        b=mkbars(150)
        i=50
        b[i+1]['c']=100
        b[i+2]['c']=98.6
        b[i+3]['c']=102
        got=p.exit_trade(b,i)
        self.assertEqual(got['exit_reason'],'손절')
        self.assertAlmostEqual(got['net_pct'],-1.9)

    def test_no_lookahead_when_candle_window_incomplete(self):
        b=mkbars(800)
        # Future data insufficient -> candidates not generated.
        c,d=p.candidates_for_coin('BTC',b,T+650*p.FIVE,T+800*p.FIVE)
        self.assertEqual(c,[])

    def test_deterministic_synthetic(self):
        b=mkbars()
        def acquire(c,s,e):return b
        a=p.run_research('BTC,ETH',6,now_ms=T+2000*p.FIVE,acquire=acquire)
        out=p.run_research('BTC,ETH',6,now_ms=T+2000*p.FIVE,acquire=acquire)
        self.assertEqual(a,out)
        self.assertEqual(a['covered'],2)
        self.assertEqual(set(a['summary']),set(p.STRATEGIES))
        self.assertEqual(sum(x['n'] for x in a['summary'].values()),len(a['signals']))

    def test_duplicate_candles_pagination_and_kst_to(self):
        start=T+50*p.FIVE
        end=T+300*p.FIVE
        called=[]
        def fake(path,params):
            self.assertEqual(path,'/v1/candles/minutes/5')
            self.assertEqual(params['count'],200)
            ms=int(datetime.fromisoformat(params['to']).replace(tzinfo=p.KST).timestamp()*1000)
            called.append(ms)
            entries=[]
            for i in range(310):
                t=T+i*p.FIVE
                if t>=ms:continue
                iso=datetime.fromtimestamp(t/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
                entries.append({'candle_date_time_utc':iso,'opening_price':100,'high_price':101,
                                'low_price':99,'trade_price':100,'candle_acc_trade_price':1_000_000})
            return list(reversed(entries))[:200]
        bars=p.candles('BTC',start,end,get_json=fake)
        self.assertEqual(len(bars),250)
        self.assertEqual(bars[0]['t'],start)
        self.assertEqual(bars[-1]['t'],end-p.FIVE)
        self.assertEqual(called[0],end)
        self.assertEqual(len(called),2)

    def test_csv_versions_and_flags(self):
        self.assertIn('V1.0',p.HTML)
        self.assertTrue(any('BITHUMB_CLASSIC_PRICEACTION_V1_0.csv' in str(x) for x in p.handle_get.__code__.co_consts))
        self.assertIn('net_pct',p.CSV_FIELDS)
        self.assertIn('슬리피지 0%',p.HTML)

    def test_raschke_strong_trend_can_trigger(self):
        # Unit-test the recognizable pattern without depending on market data.
        b=mkbars(100,False);ma=p.ema([x['c'] for x in b],20)
        adx=[45.0]*100;pdi=[38.0]*100;mdi=[15.0]*100
        i=95;j=93
        adx[j-1]=40;adx[j]=42
        b[j]['l']=ma[j]-0.08;b[j]['c']=ma[j]
        b[i-1]['h']=b[i-1]['c']+0.1
        b[i]['o']=b[i-1]['c']
        b[i]['c']=max(b[i-1]['h'],b[j]['h'])+0.25
        self.assertTrue(p.is_raschke(b,ma,adx,pdi,mdi,i))

if __name__=='__main__': unittest.main(verbosity=2)