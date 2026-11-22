"""BTC MTF V1.2 | early swing rebound indicators using completed OHLC only."""
from __future__ import annotations
from math import sqrt
from datetime import timedelta

RULE_VERSION = "REBOUND_V1.2"

def average(xs): return sum(xs)/len(xs)

def ema_series(xs,period):
    a=2/(period+1);v=None;out=[]
    for p in xs:
        v=p if v is None else v+a*(p-v)
        out.append(v)
    return out

def rsi_series(closes,length=14):
    if len(closes)<=length:return []
    gains=[max(y-x,0) for x,y in zip(closes,closes[1:])]
    losses=[max(x-y,0) for x,y in zip(closes,closes[1:])]
    up=average(gains[:length]);dn=average(losses[:length])
    def rsi(u,d):
        return 50.0 if u==d==0 else (100.0 if d==0 else 100-100/(1+u/d))
    out=[rsi(up,dn)]
    for u,d in zip(gains[length:],losses[length:]):
        up=(up*(length-1)+u)/length;dn=(dn*(length-1)+d)/length
        out.append(rsi(up,dn))
    return out

def macd_hist(closes):
    e12,e26=ema_series(closes,12),ema_series(closes,26)
    macd=[a-b for a,b in zip(e12,e26)]
    sig=ema_series(macd,9)
    return [m-s for m,s in zip(macd,sig)],macd[-1],sig[-1]

def bollinger(closes,index=None):
    i=len(closes)-1 if index is None else index
    xs=closes[i-19:i+1]
    mid=average(xs);sd=sqrt(average([(v-mid)**2 for v in xs]))
    width=max(4*sd,mid*1e-8)
    return mid,mid-2*sd,mid+2*sd,(closes[i]-(mid-2*sd))/width

def midpoint(tail):
    return (max(x['h'] for x in tail)+min(x['l'] for x in tail))/2

def ichimoku(rows):
    # Span at current candle = computed 26 completed bars earlier, no future data.
    tenkan=midpoint(rows[-9:]);kijun=midpoint(rows[-26:])
    prev_tenkan=midpoint(rows[-10:-1])
    old=rows[:-26]
    span_a=(midpoint(old[-9:])+midpoint(old[-26:]))/2
    span_b=midpoint(old[-52:])
    return tenkan,kijun,prev_tenkan,max(span_a,span_b)

def bottom_rebound_metrics(day,h4,h1,btc4,at,baseline,contiguous,bar_end):
    if not (contiguous(day,'day',24) and contiguous(h4,'4h',80) and contiguous(h1,'1h',24)):
        return None,'반등 지표 연속 마감봉 부족: 일24 / 4시간80 / 1시간24 필요'
    if at-bar_end(h4[-1]['t'],'4h')>timedelta(hours=8):
        return None,'4시간 최신 마감봉이 오래됨'
    if at-bar_end(h1[-1]['t'],'1h')>timedelta(hours=3):
        return None,'1시간 최신 마감봉이 오래됨'
    btc={x['t']:x['c'] for x in btc4}
    if h4[-1]['t'] not in btc or h4[-5]['t'] not in btc:
        return None,'동일 시각 BTC 4시간봉 부족'
    m=dict(baseline)
    f=[x['c'] for x in h4]; hour=[x['c'] for x in h1];d=[x['c'] for x in day]
    cur=f[-1];rsi=rsi_series(f);h1rsi=rsi_series(hour)
    hist,macd,sig=macd_hist(f);tenkan,kijun,prev_tenkan,cloud=ichimoku(h4)
    ma5=average(f[-5:]);prevma5=average(f[-6:-1]);ma20=average(f[-20:])
    bbmid,bblow,bbup,bbpos=bollinger(f)
    prevpos=bollinger(f,len(f)-2)[-1]
    lo=min(x['l'] for x in h4[-25:-1])
    recent_lo=min(x['l'] for x in h4[-6:]);prior_lo=min(x['l'] for x in h4[-12:-6])
    from_low=(cur/lo-1)*100
    h1up=hour[-1]>average(hour[-7:]) and hour[-1]>hour[-2] and h1rsi[-1]>=45
    volbase=average([x['turnover'] for x in h4[-21:-1]])
    volratio=h4[-1]['turnover']/volbase if volbase>0 else 0
    btc16=(btc[h4[-1]['t']]/btc[h4[-5]['t']]-1)*100
    coin16=(f[-1]/f[-5]-1)*100
    support=lo*.998
    checks={
        'bb_recover':.20<=bbpos<=.95 and bbpos>prevpos+.035,
        'ma_recover':cur>ma5 and ma5>prevma5,
        'ichimoku_recover':cur>tenkan and (tenkan>=prev_tenkan or tenkan>=kijun*.995),
        'rsi_recover':40<=rsi[-1]<=72 and rsi[-1]>rsi[-3]+.4,
        'macd_recover':hist[-1]>hist[-3] and (hist[-1]>hist[-2] or hist[-1]>0),
        'volume_recover':volratio>=1.10,
        'h1_confirm':h1up,
    }
    m.update({
        'setup_name':'바닥 반등 초입','close':cur,'bottom_ref':round(lo,8),
        'rebound_from_low_pct':round(from_low,3),
        'recent_low_higher':recent_lo>prior_lo,
        'day_ma20_distance_pct':round((d[-1]/average(d[-20:])-1)*100,3),
        'bb_mid':round(bbmid,8),'bb_lower':round(bblow,8),'bb_upper':round(bbup,8),
        'bb_position':round(bbpos,3),'bb_prev_position':round(prevpos,3),
        'ma5':round(ma5,8),'ma20':round(ma20,8),
        'ichimoku_tenkan':round(tenkan,8),'ichimoku_kijun':round(kijun,8),
        'ichimoku_cloud_top':round(cloud,8),
        'rsi14':round(rsi[-1],2),'rsi14_prev2':round(rsi[-3],2),
        'macd_hist':round(hist[-1],8),'macd_hist_prev2':round(hist[-3],8),
        'macd_value':round(macd,8),'macd_signal':round(sig,8),
        'volume_ratio':round(volratio,3),
        'relative_pct':round(coin16-btc16,3),
        'coin_return_pct':round(coin16,3),'btc_return_pct':round(btc16,3),
        'rise24_pct':round((f[-1]/f[-7]-1)*100,3),
        'structure_risk_pct':round((cur/support-1)*100,3),'support':round(support,8),
        'checks':checks,'score':sum(checks.values()),
        'closed_kst':bar_end(h4[-1]['t'],'4h').strftime('%Y-%m-%d %H:%M'),
        'day_rise_pct':round((d[-1]/d[-2]-1)*100,3),
    })
    return m,None

def rebound_signal(m,ctx,fee):
    if not m:return None
    c=m['checks'];relative=m['relative_pct'];from_low=m['rebound_from_low_pct']
    bearish=ctx['long']=='down' or ctx['short']=='down'
    strength=relative>=(1.1 if bearish else .5)
    too_late=from_low>13 or m['rise24_pct']>16 or m['structure_risk_pct']>11 or m['rsi14']>73
    early=.5<=from_low<=19 and m['score']>=3 and relative>=-.7
    trigger=c['h1_confirm'] and c['ma_recover'] and (c['bb_recover'] or c['ichimoku_recover'])
    buy=(early and not too_late and 1.2<=from_low<=11 and m['score']>=5 and trigger and
         c['rsi_recover'] and c['macd_recover'] and m['volume_ratio']>=.9 and
         strength and m['structure_risk_pct']<=8 and m.get('timing_ok',True))
    if not (buy or early):return None
    kind='BUY' if buy else 'WATCH'
    stage='반등 시작 확인' if buy else ('급등 추격 대기' if too_late else '바닥 회복 관찰')
    names={'bb_recover':'볼밴 하단→중심 회복','ma_recover':'4시간 MA5 회복',
           'ichimoku_recover':'일목 전환선 회복','rsi_recover':'RSI 상승 전환',
           'macd_recover':'MACD 모멘텀 개선','volume_recover':'거래대금 증가',
           'h1_confirm':'1시간봉 재상승'}
    why=[
        f"단계: {stage} · 최근 저점 {m['bottom_ref']:.6g}원에서 {from_low:+.2f}% (완전 바닥 예측 아님)",
        f"볼린저밴드 위치 {m['bb_position']:.2f} (0 하단/1 상단) · 중심선 {m['bb_mid']:.6g} · {'회복' if c['bb_recover'] else '대기'}",
        f"MA5 {m['ma5']:.6g} / MA20 {m['ma20']:.6g} · {'회복' if c['ma_recover'] else '미확인'}",
        f"일목 전환선 {m['ichimoku_tenkan']:.6g}, 기준선 {m['ichimoku_kijun']:.6g}, 선행 구름 상단 {m['ichimoku_cloud_top']:.6g}",
        f"RSI(14) {m['rsi14']:.1f} (2봉 전 {m['rsi14_prev2']:.1f}) · MACD 히스토그램 {m['macd_hist']:.6g} (2봉 전 {m['macd_hist_prev2']:.6g})",
        f"4시간 거래대금 {m['volume_ratio']:.2f}배 · 1시간 반등 {'확인' if c['h1_confirm'] else '대기'}",
        f"16시간 BTC {m['btc_return_pct']:+.2f}%, 코인 {m['coin_return_pct']:+.2f}% → 상대강도 {relative:+.2f}%p",
        f"반등 징후 {m['score']}/7 · 저점 지지까지 {m['structure_risk_pct']:.2f}% · 24시간 상승 {m['rise24_pct']:+.2f}%",
    ]
    if kind=='WATCH':
        missing=[names[k] for k,v in c.items() if not v]
        if not strength:missing.append('BTC 상대강도')
        if not m.get('timing_ok',True):missing.append('선택 검색봉')
        if too_late:missing.append('급등 추격 위험')
        why.append('BUY 보류: '+(', '.join(missing) if missing else '반등 확인 조건·이격 거리'))
    else:why.append('완료봉으로 반등 시작 확인 · 실제 주문은 하지 않음')
    cost=fee/100
    plan={'tp1_net':5.0,'tp1_fraction':.5,'tp2_net':12.0,'tp2_fraction':.5,
          'stop_gross':-5.0,'max_minutes':7200,
          'tp1_gross_pct':round((1.05*(1+cost)/(1-cost)-1)*100,3),
          'tp2_gross_pct':round((1.12*(1+cost)/(1-cost)-1)*100,3),
          'support':m['support'],'support_risk_pct':m['structure_risk_pct']}
    return {'signal':kind,'stage':stage,'metrics':m,'explanation':why,'plan':plan}
