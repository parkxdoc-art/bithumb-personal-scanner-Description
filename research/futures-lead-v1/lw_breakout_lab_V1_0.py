#!/usr/bin/env python3
"""LW-inspired Bithumb KRW research, V1.1. Standard library; no orders."""
import csv, io, json, math, os, re, time, threading, argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
VERSION='1.1'; STEP=300000; DAY=86400000; KST=timezone(timedelta(hours=9))
FEE=.0025; TARGET=.005; STOP=.01; MIN_LIQ=500_000_000
DEFAULT='BTC,ETH,SOL,XRP,DOGE,ADA,LINK,AVAX,SUI,PEPE'
KS=(.3,.5,.7); ENTRIES=('A','B','C'); EXITS=('DAY','SCALP')
POLICIES=[(k,a,e) for k in KS for a in ENTRIES for e in EXITS]
STABLES=set('USDT USDC DAI TUSD FDUSD USD1 PYUSD USDE USDD RLUSD EURC EURI'.split())
RATE=threading.Lock(); LOCK=threading.Lock(); NEXT=0.; BLOCK=0.; JOB={'state':'idle'}
class DataError(Exception): pass

def stamp(t):return datetime.fromtimestamp(t/1000,KST).strftime('%Y-%m-%d %H:%M')
def session(t):return t//DAY*DAY

def get(path,params):
    global NEXT,BLOCK
    with RATE:
        if time.monotonic()<BLOCK:raise DataError('API 요청 제한: 중단')
        time.sleep(max(0,NEXT-time.monotonic()));NEXT=time.monotonic()+.55
        try:
            req=Request('https://api.bithumb.com/v1'+path+'?'+urlencode(params),headers={'Accept':'application/json','User-Agent':'LW-KRW-Research/1.1'})
            with urlopen(req,timeout=15) as res: data=json.loads(res.read(4_000_000))
        except HTTPError as e:
            if e.code in (418,429):BLOCK=time.monotonic()+600;raise DataError('API 요청 제한 HTTP '+str(e.code))
            raise DataError('HTTP '+str(e.code))
        except Exception as e:raise DataError(str(e)[:160])
    if not isinstance(data,list):raise DataError('응답 형식 오류')
    return data

def parse(r,step):
    d=datetime.fromisoformat(r['candle_date_time_utc']);d=d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    t=int(d.timestamp()*1000);o,h,l,c,q=[float(r[x]) for x in ('opening_price','high_price','low_price','trade_price','candle_acc_trade_price')]
    if t%step or not all(map(math.isfinite,(o,h,l,c,q))) or not (0<l<=min(o,c)<=max(o,c)<=h and q>=0):raise ValueError('invalid candle')
    return t,o,h,l,c,q

def aggregate_day(bars,d):
    rows=[bars.get(t) for t in range(d,d+DAY,STEP)]
    if any(r is None for r in rows):return None
    return (d,rows[0][1],max(r[2] for r in rows),min(r[3] for r in rows),rows[-1][4],math.fsum(r[5] for r in rows))

def collect(coin,start,end,fetch=get,progress=lambda x:None):
    # Reconstruct daily OHLCV exclusively from complete UTC sessions (09:00 KST).
    # Fetch one extra previous session; never fill missing candles.
    daily={};bars={};invalid=0;cursor=end+STEP;warm=start-DAY
    for page in range(100):
        if cursor<=warm:break
        progress(f'{coin} 5분봉 {page+1}페이지')
        response=fetch('/candles/minutes/5',{'market':'KRW-'+coin,'count':200,'to':stamp(cursor).replace(' ','T')+':00'})
        if not response:break
        ts=[]
        for r in response:
            try:
                x=parse(r,STEP);ts.append(x[0])
                if warm<=x[0]<end+STEP:bars[x[0]]=x
            except (KeyError,ValueError,TypeError,OverflowError):invalid+=1
        if not ts or min(ts)>=cursor:raise DataError('캔들 페이지 진행 실패')
        cursor=min(ts)
    else:raise DataError('페이지 제한 초과')
    for d in range(warm,end,DAY):
        x=aggregate_day(bars,d)
        if x:daily[d]=x
    return daily,bars,invalid

def entries(rows,line):
    """A: stop-entry fill, B: first close above, C: first valid retest after first touch."""
    out={}; first=None
    for i,r in enumerate(rows):
        if r[2]>=line:
            first=i;out['A']=(i,max(r[1],line));break
    if first is None:return out
    for i in range(first,len(rows)-1):
        if rows[i][4]>=line:
            out['B']=(i+1,rows[i+1][1]);break
    for i in range(first+1,min(first+7,len(rows)-1)):
        _,o,h,l,c,q=rows[i]
        if c<line*.998:break
        if l<=line*1.003 and c>line and c>o:
            out['C']=(i+1,rows[i+1][1]);break
    return out

def net(entry,exit):return exit/entry*(1-FEE)/(1+FEE)-1

def exit_trade(rows,nextbar,i,entry,kind,method):
    if kind=='DAY':return nextbar[0],nextbar[1],'다음 09시 시가'
    stop=entry*(1-STOP); target=entry*(1+TARGET)*(1+FEE)/(1-FEE)
    end=min(i+11,len(rows)-1)
    for j in range(i,end+1):
        t,o,h,l,c,q=rows[j]
        # A's first bar may touch the low before entry; pessimistically stop first.
        if l<=stop:return t+STEP,stop if j==i else min(stop,o),'손절'
        if h>=target:return t+STEP,target,'익절'
    return rows[end][0]+STEP,rows[end][4],'60분 만료' if end==i+11 else '09시 전 종료'

def evaluate(coin,daily,bars,start,end):
    trades=[];audit=[]
    for d in range(start,end,DAY):
        prev=daily.get(d-DAY);cur=daily.get(d);nxt=bars.get(d+DAY)
        rows=[bars[t] for t in range(d,d+DAY,STEP) if t in bars]
        reason='OK'
        if not prev:reason='전일 5분봉 누락: 일봉 재구성 불가'
        elif not cur:reason='당일 5분봉 누락: 일봉 재구성 불가'
        elif prev[5]<MIN_LIQ:reason='전일 거래대금 5억 미만'
        elif prev[2]<=prev[3]:reason='전일 변동폭 0'
        elif len(rows)!=288 or not nxt:reason='5분봉/익일 시가 누락'
        elif not math.isclose(cur[1],rows[0][1],rel_tol=1e-8):reason='일봉/분봉 시가 불일치'
        diag={'coin':coin,'day':stamp(d)[:10],'bars':len(rows),'next_open':bool(nxt),'prev_krw':prev[5] if prev else None,'prev_bars':sum(t in bars for t in range(d-DAY,d,STEP)),'daily_source':'5분봉 재구성','status':reason}
        audit.append(diag)
        if reason!='OK':continue
        for k in KS:
            line=cur[1]+k*(prev[2]-prev[3]);en=entries(rows,line)
            for a,(i,price) in en.items():
                for ex in EXITS:
                    t,px,why=exit_trade(rows,nxt,i,price,ex,a)
                    trades.append({'coin':coin,'day':diag['day'],'k':k,'entry_type':a,'exit_type':ex,'line':line,'entry':stamp(rows[i][0]),'entry_ms':rows[i][0],'entry_price':price,'exit':stamp(t),'exit_price':px,'net_pct':round(net(price,px)*100,6),'reason':why})
    return trades,audit

def summarize(rows,audit):
    out=[];valid=sum(x['status']=='OK' for x in audit)
    for k,a,e in POLICIES:
        subset=[x for x in rows if (x['k'],x['entry_type'],x['exit_type'])==(k,a,e)]
        v=[x['net_pct'] for x in subset]; n=len(v)
        out.append({'k':k,'entry_type':a,'exit_type':e,'trades':n,'wins':sum(x>0 for x in v),'losses':sum(x<0 for x in v),'flat':sum(x==0 for x in v),'win_pct':round(100*sum(x>0 for x in v)/n,2) if n else None,'avg_net_pct':round(sum(v)/n,5) if n else None,'sum_net_pct':round(sum(v),5),'valid_coin_days':valid,'per_coin_day_pct':round(sum(v)/valid,5) if valid else None})
    return out

def run(coins=DEFAULT,days=30,progress=lambda x:None,now=None,acquire=collect):
    cs=list(dict.fromkeys(x.strip().upper().removeprefix('KRW-') for x in coins.split(',') if x.strip()))
    if days not in (30,60) or not 1<=len(cs)<=10 or any(not re.fullmatch('[A-Z0-9]{2,15}',x) or x in STABLES for x in cs):raise ValueError('30/60일, KRW 코인 1~10개 입력')
    now=int(time.time()*1000) if now is None else now
    # At 09:00-09:05 the next open is not yet observable as a completed 5m candle.
    end=session(now) if now-session(now)>=STEP else session(now)-DAY
    start=end-days*DAY;rows=[];audit=[];errors=[]
    for idx,coin in enumerate(cs):
        try:
            daily,bars,invalid=acquire(coin,start,end,progress=lambda msg:progress(f'[{idx+1}/{len(cs)}] {msg}'))
            r,d=evaluate(coin,daily,bars,start,end);rows+=r;audit+=d
            if invalid:errors.append(f'{coin}: 잘못된 캔들 {invalid}개')
        except (DataError,ValueError,KeyError) as e:
            errors.append(coin+': '+str(e));audit += [{'coin':coin,'day':stamp(d)[:10],'status':'수집 실패','bars':0} for d in range(start,end,DAY)]
            if '요청 제한' in str(e):
                for rest in cs[idx+1:]:audit += [{'coin':rest,'day':stamp(d)[:10],'status':'API 제한 미수집','bars':0} for d in range(start,end,DAY)]
                break
    rows.sort(key=lambda x:(x['entry_ms'],x['k'],x['entry_type'],x['exit_type'],x['coin']))
    # Independent policies; retain earliest four signals per session, deterministic ticker tie break.
    # Intrabar A timing is not identifiable at 5m resolution; tie break is a model assumption.
    counts=Counter();kept=[]
    for r in rows:
        key=(r['day'],r['k'],r['entry_type'],r['exit_type']);counts[key]+=1
        if counts[key]<=4:kept.append(r)
    return {'version':VERSION,'from':stamp(start),'until':stamp(end),'coins':cs,'days':days,'rows':kept,'audit':audit,'errors':errors,'raw_trades':len(rows),'summary':summarize(kept,audit),'uncapped_summary':summarize(rows,audit),'coverage':dict(Counter(x['status'] for x in audit))}

FIELDS=['coin','day','k','entry_type','exit_type','line','entry','entry_ms','entry_price','exit','exit_price','net_pct','reason']
AUDIT=['coin','day','bars','next_open','prev_krw','prev_bars','daily_source','status']

def csv_text(rows,fields):
    f=io.StringIO();w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows);return '\ufeff'+f.getvalue()

def start(coins,days):
    if not LOCK.acquire(False):return False
    JOB.clear();JOB.update(state='running',progress='수집 준비')
    def work():
        try:
            r=run(coins,days,lambda msg:JOB.update(progress=msg));JOB.update(state='done',result=r,progress='완료')
        except Exception as e:JOB.update(state='error',error=str(e))
        finally:LOCK.release()
    threading.Thread(target=work,daemon=True).start();return True

HTML='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LW 변동성 돌파 연구 V1.1</title><style>body{font:15px system-ui;background:#101a2b;color:#eaf1fc;max-width:1000px;margin:auto;padding:16px}section{background:#192a40;border:1px solid #354b65;border-radius:12px;padding:16px;margin:12px 0}input,select,button{font:inherit;background:#0e1c30;color:white;border:1px solid #56708f;padding:10px;border-radius:8px;box-sizing:border-box}input{width:100%}button{background:#286cb1}button:disabled{opacity:.5}a{color:#a0d6ff}small{color:#b5c6dc}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid #354b65;padding:8px;white-space:nowrap;text-align:right}.scroll{overflow:auto}h1{font-size:24px}h2{font-size:18px}</style><h1>LW 변동성 돌파 V1.1</h1><small>빗썸 KRW · 독립 연구 · 자동매매 없음</small><section><h2>1. 비교 연구</h2><label>고정 종목 (최대 10개)</label><input id="coins" value="BTC,ETH,SOL,XRP,DOGE,ADA,LINK,AVAX,SUI,PEPE"><p><label>기간 <select id="days"><option>30</option><option>60</option></select>일</label> <button id="run">연구 실행</button></p><p id="state">대기</p><small>전일 거래대금 5억 이상 · 09시 기준 · 전일·당일 288봉 완전 수집일만 · 편도 0.25% · 비교군별 하루 최대 4건. 조회량이 많아 최초 수집이 길어질 수 있습니다.</small></section><section><h2>2. 비교 결과 (18개)</h2><p id="coverage"></p><div class="scroll" id="summary"></div><p id="downloads" hidden><a href="/api/lw/csv">거래 CSV</a> · <a href="/api/lw/audit">진단 CSV</a> · <a href="/api/lw/summary">18개 요약 CSV</a></p><details><summary>하루 4건 제한 없는 비교</summary><div class="scroll" id="uncapped"></div></details></section><section><h2>3. 비교 기준</h2><p>A: 돌파가 접촉 시 진입(갭 상승 시 시가). B: 돌파 후 첫 종가 확인, 다음 시가. C: 돌파 뒤 최대 6봉 내 지지 반등 확인, 다음 시가.</p><p>DAY: 다음 09시 시가 청산. SCALP: 순익 +0.5%, 가격 -1%, 최대 60분, 장 종료 전 청산. 동일 봉 손절 우선. A는 봉 안 순서 불명으로 보수 처리. 수수료 외 슬리피지 0 가정. 고정 종목의 과거 표본으로 전체 시장 대표성 및 실수익 보장 없음.</p></section><script>const $=x=>document.getElementById(x);let timer;function table(el,rows){el.replaceChildren();if(!rows.length){el.textContent='결과 없음';return}const t=document.createElement('table'),keys=Object.keys(rows[0]);let h=t.insertRow();for(const k of keys){let c=document.createElement('th');c.textContent=k;h.append(c)}for(const r of rows){let tr=t.insertRow();for(const k of keys){let c=tr.insertCell();c.textContent=r[k]??'—'}}el.append(t)}async function poll(){try{const x=await(await fetch('/api/lw/status',{cache:'no-store'})).json();$('state').textContent=x.progress||x.error||x.state;$('run').disabled=x.state==='running';$('downloads').hidden=!x.result;if(x.error)$('state').textContent=x.error;if(x.result){let r=x.result;$('coverage').textContent=(r.summary[0].valid_coin_days===0?'정상 분석일 0: 수익성 판단 불가 / ':'')+r.from+' ~ '+r.until+' / '+JSON.stringify(r.coverage)+' / 오류: '+r.errors.join(' / ');table($('summary'),r.summary);table($('uncapped'),r.uncapped_summary)}if(x.state==='running')timer=setTimeout(poll,1800)}catch(e){$('state').textContent='연결 오류: 새로고침으로 상태 재확인';$('run').disabled=false}}$('run').onclick=async()=>{clearTimeout(timer);$('run').disabled=true;$('downloads').hidden=true;try{let r=await fetch('/api/lw/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coins:$('coins').value,days:Number($('days').value)})});if(!r.ok)throw Error(await r.text());poll()}catch(e){$('state').textContent=e.message;$('run').disabled=false}};poll();</script></html>'''

def handle_get(h,p):
    if p=='/lw':return h.respond(HTML,ctype='text/html; charset=utf-8')
    if p=='/api/lw/status':return h.respond(json.dumps(JOB,ensure_ascii=False,allow_nan=False))
    r=JOB.get('result')
    if not r:return h.respond(json.dumps({'error':'완료 결과 없음'},ensure_ascii=False),404)
    choices={'/api/lw/csv':(r['rows'],FIELDS,'TRADES'),'/api/lw/audit':(r['audit'],AUDIT,'DIAGNOSTICS'),'/api/lw/summary':(r['summary'],list(r['summary'][0]),'SUMMARY')}
    if p not in choices:return h.respond('{}',404)
    data,fields,suffix=choices[p]
    return h.respond(csv_text(data,fields),ctype='text/csv; charset=utf-8',extra={'Content-Disposition':f'attachment; filename="BITHUMB_LW_V1_1_{suffix}.csv"'})

def handle_post(h,p):
    try:
        n=int(h.headers.get('Content-Length','0'))
        if not 0<n<=1024:raise ValueError('요청 크기 오류')
        x=json.loads(h.rfile.read(n));coins=x.get('coins',DEFAULT);days=int(x.get('days',30))
        if not isinstance(coins,str) or days not in (30,60):raise ValueError('설정 오류')
        ok=start(coins,days);return h.respond(json.dumps({'ok':ok}),200 if ok else 429)
    except (ValueError,TypeError,AttributeError) as e:return h.respond(json.dumps({'error':str(e)},ensure_ascii=False),400)

class Handler(BaseHTTPRequestHandler):
    def respond(self,body,status=200,ctype='application/json; charset=utf-8',extra=None):
        b=body.encode('utf-8') if isinstance(body,str) else body;self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(b)
    def do_GET(self):
        p=urlsplit(self.path).path;handle_get(self,'/lw' if p=='/' else p)
    def do_POST(self):
        if urlsplit(self.path).path=='/api/lw/run':handle_post(self,'/api/lw/run')
        else:self.respond('{}',404)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--host',default='127.0.0.1');a.add_argument('--port',type=int,default=int(os.environ.get('PORT',8080)));x=a.parse_args();print('LW V1.1 web',x.host,x.port,flush=True);ThreadingHTTPServer((x.host,x.port),Handler).serve_forever()
