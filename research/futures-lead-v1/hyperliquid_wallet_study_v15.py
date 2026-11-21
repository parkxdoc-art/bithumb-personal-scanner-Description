#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V1.5: read-only historical public Hyperliquid wallet execution study.
No account connection, trading, copying, auto-signals or credentials.
Only published pseudonymous wallet addresses are sampled. The sample is NOT
proof of a person's identity or profitability. This is NOT the Binance G-rule.
"""
import argparse
import csv
import io
import json
import math
import statistics
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

VERSION = '1.5.1'
API = 'https://api.hyperliquid.xyz/info'
DAY_MS = 24*60*60*1000
KST = timezone(timedelta(hours=9))
COINS = {'BTC', 'ETH', 'SOL', 'XRP', 'SUI'}
# Source: Hyperliquid public leaderboard mirrored by LabelYX, Aug 2026 snapshot.
# Aliases are community display names, NOT real-world identity verification.
WALLETS = (
    ('BobbyBigSize', '0x7fdafde5cfb5465924316eced2d3715494c517d1'),
    ('x35767', '0x880ac484a1743862989a441d6d867238c7aa311c'),
    ('0xb83d…6e36', '0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36'),
)

class ResearchError(Exception): pass


def utc_kst(ms):
    return datetime.fromtimestamp(ms/1000, tz=KST).strftime('%Y-%m-%d %H:%M')


def d(x):
    try: return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError): raise ResearchError('체결 숫자 형식 오류')


def info(payload):
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    req = Request(API, data=body, headers={
        'Content-Type':'application/json', 'Accept':'application/json',
        'User-Agent':'ReadOnlyTradePatternResearch/1.5'})
    try:
        with urlopen(req, timeout=22) as r:
            return json.loads(r.read(6_000_000).decode('utf-8'))
    except HTTPError as e:
        if e.code in (418, 429):
            raise ResearchError('Hyperliquid 요청 제한. 추가 요청 없이 중단 (HTTP %d)' % e.code)
        raise ResearchError('Hyperliquid HTTP %s' % e.code)
    except (URLError, TimeoutError, OSError, ValueError) as e:
        raise ResearchError('Hyperliquid 데이터 접근 실패: %s' % str(e)[:160])


def get_fills(wallet, start_ms, end_ms, api=info, pause=0.7):
    """Bound each request to one UTC day. Never silently accept a 2000 cap."""
    assert wallet.startswith('0x') and len(wallet) == 42
    rows, seen = [], set()
    cursor = start_ms
    request_count = 0
    while cursor < end_ms:
        nxt = min(end_ms, ((cursor//DAY_MS)+1)*DAY_MS)
        parts = [(cursor,nxt-1)]
        while parts:
            a,b = parts.pop(0)
            result = api({'type':'userFillsByTime', 'user':wallet,
                          'startTime':a, 'endTime':b, 'aggregateByTime':False})
            request_count += 1
            if not isinstance(result,list):
                raise ResearchError('체결 API 응답 형식 오류')
            if len(result) >= 2000:
                if b-a < 15*60_000:
                    raise ResearchError('체결 2,000건 한도: 15분 단위도 포화. 불완전 자료 제외')
                half = (a+b)//2
                parts[0:0] = [(a,half),(half+1,b)]
            else:
                for x in result:
                    try:
                        t=int(x.get('time',0))
                        if t<a or t>b: continue
                        k=(str(x.get('hash','')),str(x.get('tid','')), t,
                           str(x.get('coin','')),str(x.get('sz','')),str(x.get('px','')))
                        if k not in seen:
                            seen.add(k); rows.append(x)
                    except (ValueError,AttributeError): continue
            if request_count > 96:
                raise ResearchError('API 호출 96회 안전 한도 초과, 이 지갑 제외')
            if pause: time.sleep(pause)
        cursor = nxt
    return sorted(rows,key=lambda x:(int(x['time']),str(x.get('tid','')))),request_count


def roundtrips(fills):
    """Reconstruct only episodes whose FLAT opening and closing are both visible.
    Start-position signed; side=B increases position, S decreases it. Ignore
    unobserved positions, flips and malformed fills rather than guessing.
    Fees and closedPnl are distinct fields: net_proxy=closedPnl-fee is a
    diagnostic proxy, not audited whole-wallet profitability.
    """
    active = {}
    episodes = []
    skipped = {'pre_existing_or_unobserved':0,'flips':0,'malformed':0}
    for x in fills:
        coin = x.get('coin','')
        if coin not in COINS: continue
        if str(x.get('dir','')).lower().startswith('spot'): continue
        try:
            ts=int(x['time']); start=d(x['startPosition']); size=d(x['sz'])
            fee=d(x.get('fee','0')); pnl=d(x.get('closedPnl','0'))
            px=d(x['px'])
            side=x.get('side'); sign = 1 if side == 'B' else -1 if side == 'A' else None
            # Exchange fill API uses 'B'/'A' (buy/ask); never interpret others.
            if sign is None or size <= 0 or px <= 0: raise ResearchError('잘못된 체결값')
            end=start + size*sign
        except (KeyError, ResearchError, ValueError, InvalidOperation):
            skipped['malformed'] += 1; active.pop(coin,None); continue
        if start * end < 0:
            active.pop(coin,None); skipped['flips'] += 1; continue
        if start == 0 and end != 0:
            active[coin]={'coin':coin,'side':'LONG' if end>0 else 'SHORT',
                          'entry_ms':ts,'entry_px':float(px),
                          'gross':pnl,'fee':fee,'fills':1}
            continue
        event=active.get(coin)
        if not event:
            if start != 0: skipped['pre_existing_or_unobserved'] += 1
            continue
        # Position stays in the same direction; if position history is
        # discontinuous or restarted, drop it rather than stitch incorrectly.
        if (start>0) != (event['side']=='LONG') or start == 0:
            active.pop(coin,None);skipped['malformed']+=1;continue
        event['gross']+=pnl;event['fee']+=fee;event['fills']+=1
        if end==0:
            hold=(ts-event['entry_ms'])/60000
            if hold>=0:
                episodes.append({'coin':coin,'side':event['side'],
                    'entry_ms':event['entry_ms'],'exit_ms':ts,
                    'entry_kst':utc_kst(event['entry_ms']),
                    'exit_kst':utc_kst(ts),'entry_px':event['entry_px'],
                    'hold_min':round(hold,2),'gross_usdc':round(float(event['gross']),4),
                    'fees_usdc':round(float(event['fee']),4),
                    'net_proxy_usdc':round(float(event['gross']-event['fee']),4),
                    'fill_count':event['fills']})
            active.pop(coin,None)
    return episodes,skipped


def stats(rows):
    intraday=[x for x in rows if 5 <= x['hold_min'] <= 240]
    vals=[x['net_proxy_usdc'] for x in intraday]
    return {'roundtrips':len(rows),'intraday':len(intraday),
        'win_rate':round(sum(v>0 for v in vals)/len(vals)*100,1) if vals else None,
        'median_hold_min':round(statistics.median(x['hold_min'] for x in intraday),1) if intraday else None,
        'median_net_proxy_usdc':round(statistics.median(vals),2) if vals else None,
        'largest_loss_proxy_usdc':round(min(vals),2) if vals else None,
        'long_n':sum(x['side']=='LONG' for x in intraday),
        'short_n':sum(x['side']=='SHORT' for x in intraday)}


def study(days=6, api=info, pause=0.7, progress=lambda x:None, now=None):
    days=int(days)
    if not 1<=days<=13: raise ValueError('1~13일만 가능')
    now=now or int(time.time()*1000)
    end=(now//(15*60_000))*(15*60_000)
    start=end-days*DAY_MS
    results=[]; all_episodes=[]; errors=[]
    for i,(alias,wallet) in enumerate(WALLETS,1):
        progress('%s (%d/%d) 체결 조회' % (alias,i,len(WALLETS)))
        try:
            fills,nreq=get_fills(wallet,start,end,api=api,pause=pause)
            eps,skips=roundtrips(fills)
            for ep in eps:ep['wallet']=wallet;ep['alias']=alias
            all_episodes.extend(eps)
            results.append({'alias':alias,'wallet':wallet,'fills':len(fills),
                            'api_calls':nreq,'skipped':skips,**stats(eps)})
        except ResearchError as e:
            errors.append('%s: %s' % (alias,e))
            results.append({'alias':alias,'wallet':wallet,'error':str(e)})
            if '요청 제한' in str(e):
                errors.append('요청 제한으로 나머지 지갑 조회도 중단')
                break
    return {'version':VERSION,'days':days,'from':utc_kst(start),'to':utc_kst(end),
            'wallets':results,'episodes':sorted(all_episodes,key=lambda x:x['entry_ms'],reverse=True),
            'errors':errors,'limits':[
            '상위 지갑은 표본 편향이 큽니다. 닉네임은 실제 신원 인증이 아닙니다.',
            '완전한 진입·청산이 관측된 포지션만 기록합니다. 기간 전부터 열려 있던 거래는 제외합니다.',
            'closedPnl-fee는 체결 필드 기반 추정치이며 계정 전체 순수익률이 아닙니다.',
            '선물 펀딩·지갑 입출금·타거래소 헤지·슬리피지는 검증하지 않습니다.',
            'API는 최근 10,000 체결만 접근 가능해 과거 체결이 누락될 수 있습니다.',
            '포지션 수익성은 빗썸 현물 단타의 수익성을 입증하지 않습니다.']} 


def to_csv(data):
    out=io.StringIO();w=csv.writer(out)
    keys=['alias','wallet','coin','side','entry_kst','exit_kst','entry_px',
          'hold_min','gross_usdc','fees_usdc','net_proxy_usdc','fill_count']
    w.writerow(keys)
    for r in data['episodes']:
        w.writerow([r.get(k,'') for k in keys])
    return '\ufeff'+out.getvalue()



def to_diagnostics_csv(data):
    """Keep API errors and acquisition status separate from closed-trades CSV."""
    out = io.StringIO()
    w = csv.writer(out)
    fields = ['alias','wallet','collection_status','fills','roundtrips','intraday_5_240',
              'api_calls','pre_existing_or_unobserved','flips','malformed','error']
    w.writerow(fields)
    for row in data.get('wallets', []):
        skipped = row.get('skipped') or {}
        w.writerow([row.get('alias',''),row.get('wallet',''),
                    'ERROR' if row.get('error') else 'OK',
                    row.get('fills',''),row.get('roundtrips',''),
                    row.get('intraday',''),row.get('api_calls',''),
                    skipped.get('pre_existing_or_unobserved',''),
                    skipped.get('flips',''),skipped.get('malformed',''),
                    row.get('error','')])
    for err in data.get('errors', []):
        w.writerow(['RESEARCH','','ERROR','','','','','','','',err])
    return '\ufeff' + out.getvalue()

HTML = '''<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>공개 트레이더 체결 연구 V1.5.1</title><style>
*{box-sizing:border-box}body{background:#0c1526;color:#edf3fa;font:15px system-ui,sans-serif;line-height:1.65;margin:0}main{max-width:960px;margin:auto;padding:22px}article{background:#17243a;border:1px solid #334762;padding:18px;border-radius:16px;margin:20px 0}h1{font-size:25px}h2{font-size:19px}button,a.link{display:inline-block;color:white;background:#277dcb;border:0;border-radius:9px;padding:12px 17px;font-weight:700;text-decoration:none;cursor:pointer}button:disabled{opacity:.55}p,small{color:#afc0d4}.scroll{overflow:auto}table{border-collapse:collapse;min-width:700px;width:100%}td,th{padding:9px;border-bottom:1px solid #34445d;text-align:left;font-size:13px}th{color:#aac3e1}code{word-break:break-all;color:#b4e0ff}#err{white-space:pre-wrap;color:#ffb7b7}a{color:#8bceff} .kpi{font-size:18px;font-weight:bold}select{background:#091326;color:white;border:1px solid #506888;border-radius:7px;padding:12px}
</style></head><body><main>
<small>HYPERLIQUID · 공개 지갑 연구 · 투자 주문 없음</small>
<p><a href="/traders/offline" style="font-weight:700">새 V1.6: CSV/JSON 파일 기반 연구실 열기 (API 요청 없음)</a></p><h1>유명 트레이더 체결 패턴 연구 V1.5.1</h1>
<p>공개 지갑 3개를 대상으로 BTC·ETH·SOL·XRP·SUI의 실제 체결을 조회합니다. 개인 실명·전략 의도는 확인되지 않았습니다. 바이낸스 API는 사용하지 않습니다.</p>
<article><h2>연구 실행</h2><p>가장 최근 6일 기준, 지갑별 완료된 왕복 포지션과 5~240분 보유 포지션을 분리합니다. 일부 체결이 누락되면 완전한 기록으로 해석하지 않습니다.</p>
<label>연구 기간 <select id="days"><option value="6">최근 6일</option><option value="13">최근 13일</option></select></label>
<button id="start" onclick="run()">체결 기록 조사</button> <b id="state">미실행</b><p id="progress"></p><p id="err"></p></article>
<article><h2>선정된 관측 대상 (실명 확인 아님)</h2><p>BobbyBigSize · x35767 · 0xb83d…6e36</p><small>공개 순위표 기반 사전 후보이며 수익성 검증 전입니다. 같은 전략을 모방하지 않습니다.</small></article>
<article id="results" hidden><h2>지갑별 연구 결과</h2><div class="scroll"><table><thead><tr><th>지갑</th><th>체결 수</th><th>완료 포지션</th><th>5~240분</th><th>승률(추정)</th><th>중위 보유분</th><th>최대 손실(USDC 추정)</th></tr></thead><tbody id="wallets"></tbody></table></div><p><a id="csv" href="/api/wallet-study/csv">완료 포지션 CSV 받기</a> · <a href="/api/wallet-study/diagnostics.csv">수집 진단 CSV 받기 (체결 수·오류)</a></p>
<h2>최근 완료 포지션</h2><div class="scroll"><table><thead><tr><th>지갑</th><th>코인</th><th>방향</th><th>진입(KST)</th><th>청산(KST)</th><th>보유분</th><th>손익 추정(USDC)</th></tr></thead><tbody id="episodes"></tbody></table></div><p id="fetchwarn" style="font-weight:bold;color:#ffb7b7"></p><p id="note"></p></article>
<article><h2>해석 주의</h2><p>① 전체 계좌 수익률이 아닌 완료 포지션의 추정치입니다. ② 단일 체결이 여러 번 쪼개질 수 있으므로 체결 수≠거래 횟수입니다. ③ 지갑은 거래소 외부 헤지·입출금이 있을 수 있습니다. ④ 출처 API는 최근 체결 수 제한이 있습니다. ⑤ 익숙한 트레이더의 닉네임이 실제 인물 신원을 인증하지 않습니다.</p></article>
</main><script>
const $=x=>document.getElementById(x);const esc=s=>String(s??'—').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));let timer=null;
async function run(){ $('err').textContent='';$('start').disabled=true;try{let r=await fetch('/api/wallet-study/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:Number($('days').value)})});let v=await r.json();if(!r.ok){throw Error(v.error||'요청 실패')}poll();timer=setInterval(poll,1300)}catch(e){$('err').textContent=e.message;$('start').disabled=false}}
async function poll(){try{let r=await fetch('/api/wallet-study/status');let j=await r.json();$('state').textContent=j.state==='running'?'조회 중':j.state==='done'?'완료':j.state==='error'?'오류':'미실행';$('progress').textContent=j.progress||'';if(j.state!=='running'){if(timer){clearInterval(timer);timer=null}$('start').disabled=false;if(j.state==='error')$('err').textContent=j.error||'조회 실패';if(j.state==='done'&&j.result)show(j.result)}}catch(e){$('err').textContent=e.message}}
function show(j){$('results').hidden=false;$('wallets').innerHTML=(j.wallets||[]).map(x=>'<tr><td title="'+esc(x.wallet)+'">'+esc(x.alias)+'</td><td>'+esc(x.fills)+'</td><td>'+esc(x.roundtrips)+'</td><td>'+esc(x.intraday)+'</td><td>'+esc(x.win_rate===null?'—':x.win_rate+'%')+'</td><td>'+esc(x.median_hold_min)+'</td><td>'+esc(x.largest_loss_proxy_usdc)+'</td></tr>').join('');$('episodes').innerHTML=(j.episodes||[]).slice(0,60).map(x=>'<tr><td>'+esc(x.alias)+'</td><td>'+esc(x.coin)+'</td><td>'+esc(x.side)+'</td><td>'+esc(x.entry_kst)+'</td><td>'+esc(x.exit_kst)+'</td><td>'+esc(x.hold_min)+'</td><td>'+esc(x.net_proxy_usdc)+'</td></tr>').join('');$('fetchwarn').textContent=(j.errors||[]).length?('수집 오류: '+j.errors.join(' / ')):((j.episodes||[]).length===0?'완료된 포지션 0건. 체결 수와 제외 사유는 진단 CSV에서 확인해 주세요.':'');$('note').textContent=(j.limits||[]).join(' / ')}poll();
</script></body></html>'''

STATE = {'state':'idle','progress':'','result':None,'error':None}
LOCK=threading.Lock()
_NEXT_ALLOWED=0.0

def status():
    with LOCK:return dict(STATE)

def _worker(days):
    global _NEXT_ALLOWED
    def progress(s):
        with LOCK:STATE['progress']=s
    try:
        result=study(days,progress=progress)
        with LOCK:
            STATE.update(state='done',progress='완료',result=result,error=None)
            _NEXT_ALLOWED=time.time()+30*60
    except Exception as e:
        with LOCK:
            STATE.update(state='error',error=str(e),progress='실패')
            _NEXT_ALLOWED=time.time()+10*60

def start(days):
    global _NEXT_ALLOWED
    with LOCK:
        if STATE['state']=='running':return False,'이미 조사 중입니다'
        if time.time() < _NEXT_ALLOWED:return False,'안전한 API 호출을 위해 잠시 재실행할 수 없습니다. 기존 결과를 확인해 주세요.'
        if int(days) not in (6,13):return False,'기간은 6일 또는 13일만 가능합니다'
        STATE.update(state='running',progress='조회 시작',error=None,result=None)
    threading.Thread(target=_worker,args=(int(days),),daemon=True).start()
    return True,''


def selftest():
    t=1763000000000
    wallet=WALLETS[0][1]
    fills=[
        {'coin':'BTC','time':t,'side':'B','startPosition':'0','sz':'1','px':'100','fee':'0.1','closedPnl':'0','dir':'Open Long'},
        {'coin':'BTC','time':t+600000,'side':'A','startPosition':'1','sz':'0.4','px':'102','fee':'0.1','closedPnl':'0.8','dir':'Close Long'},
        {'coin':'BTC','time':t+1200000,'side':'A','startPosition':'0.6','sz':'0.6','px':'103','fee':'0.1','closedPnl':'1.8','dir':'Close Long'},
        {'coin':'ETH','time':t+100,'side':'A','startPosition':'-3','sz':'1','px':'8','fee':'0.1','closedPnl':'0.5','dir':'Close Short'},
    ]
    episodes, skips=roundtrips(fills)
    assert len(episodes)==1 and episodes[0]['hold_min']==20
    assert abs(episodes[0]['net_proxy_usdc']-2.3)<1e-8
    assert skips['pre_existing_or_unobserved']==1
    assert stats(episodes)['intraday']==1
    calls=[]
    def fake(p):
        calls.append((p['startTime'],p['endTime']))
        return [{**x,'hash':str(i),'tid':i} for i,x in enumerate(fills) if p['startTime']<=x['time']<=p['endTime']]
    got,n=get_fills(wallet,t-100,t+DAY_MS,api=fake,pause=0)
    assert len(got)==4 and n==2 and calls[0][1]+1==calls[1][0]
    assert '\ufeff' in to_csv({'episodes':episodes})
    diagnostic = to_diagnostics_csv({'wallets':[{'alias':'test','wallet':wallet,'fills':4,'roundtrips':1,'intraday':1,'skipped':{'malformed':2}}], 'errors':[]})
    assert 'test' in diagnostic and '4,1,1' in diagnostic and 'OK' in diagnostic
    fail_diagnostic = to_diagnostics_csv({'wallets':[{'alias':'test','wallet':wallet,'error':'HTTP 429'}],'errors':['요청 제한으로 중단']})
    assert 'HTTP 429' in fail_diagnostic and '요청 제한으로 중단' in fail_diagnostic
    print('V1.5 SELFTEST PASS: closed positions, partial fills, fees, unknown entry excluded, pagination, CSV')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--selftest',action='store_true')
    p.add_argument('--fetch',action='store_true')
    p.add_argument('--days',type=int,default=6)
    p.add_argument('--csv',default='HYPERLIQUID_WALLET_STUDY_V1_5_1.csv')
    args=p.parse_args()
    if args.selftest:selftest()
    elif args.fetch:
        data=study(args.days,progress=print)
        with open(args.csv,'w',encoding='utf-8-sig',newline='') as f:f.write(to_csv(data).lstrip('\ufeff'))
        print(json.dumps({'wallets':data['wallets'],'errors':data['errors']},ensure_ascii=False,indent=2))
        print('Saved',args.csv)
    else:p.print_help()