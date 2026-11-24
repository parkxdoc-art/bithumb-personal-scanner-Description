"""ORDERFLOW MINI V0.2 — read-only KRW market research, no orders."""
import csv, io, json, math, os, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from flask import Flask, Response, jsonify

VERSION = 'ORDERFLOW MINI V0.2'
API = 'https://api.bithumb.com'
FEE = .0004  # each side, 0.08% nominal round trip
HORIZONS = (60, 180, 300, 600, 1800)
INTERVAL = 15
MAX_LAG = 45
COOLDOWN = 2700
BUDGET = 100000  # illustrative fixed research principal, editable display only
KST = ZoneInfo('Asia/Seoul')

def number(x):
    try:
        n = float(x)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None

def net_pct(entry, bid):
    return 100 * (bid * (1-FEE) / (entry * (1+FEE)) - 1)

def book_metrics(book, now):
    if not isinstance(book, dict): return None
    stamp = number(book.get('timestamp'))
    if stamp is None or not -5 <= now-stamp/1000 <= 30: return None
    sides = []
    for side in ('bid','ask'):
        levels = []
        for u in book.get('orderbook_units') or []:
            if not isinstance(u, dict): continue
            p, q = number(u.get(side+'_price')), number(u.get(side+'_size'))
            if p and q: levels.append((p,q))
        sides.append(sorted(levels, reverse=side=='bid')[:5])
    b,a = sides
    if not b or not a or b[0][0]>=a[0][0]: return None
    bid,ask=b[0][0],a[0][0]
    br,ar=sum(p*q for p,q in b),sum(p*q for p,q in a)
    return dict(bid=bid,ask=ask,ratio=br/ar,spread=100*(ask-bid)/((ask+bid)/2),bid_depth=br,ask_depth=ar,ask1_krw=a[0][0]*a[0][1],bid1_krw=b[0][0]*b[0][1],book_time=stamp/1000)

def trade_metrics(trades, now):
    if not isinstance(trades,list): return None
    valid=[]; seen=set()
    for t in trades:
        if not isinstance(t,dict): continue
        stamp,p,q=number(t.get('timestamp')),number(t.get('trade_price')),number(t.get('trade_volume'))
        side=t.get('ask_bid'); sid=t.get('sequential_id')
        if not stamp or not p or not q or side not in ('BID','ASK') or stamp/1000>now+1: continue
        if sid is not None:
            if str(sid) in seen: continue
            seen.add(str(sid))
        valid.append((stamp/1000,p*q,side))
    if len(trades)>=200 and (not valid or min(t[0] for t in valid)>=now-30): return None
    window=[t for t in valid if now-30<=t[0]<=now+1]
    if len(window)<3: return None
    buy=sum(v for _,v,s in window if s=='BID'); sell=sum(v for _,v,s in window if s=='ASK')
    return dict(buy_share=100*buy/(buy+sell),trade_total=buy+sell,buy_krw=buy,sell_krw=sell,trade_count=len(window))

def qualifies(b,t):
    return bool(b and t and b['ratio']>=1.25 and b['spread']<=.20 and t['buy_share']>=58 and t['trade_total']>=10000000)

def request_public(path, params):
    r=requests.get(API+path,params=params,timeout=(3,6),headers={'Accept':'application/json','User-Agent':'orderflow-mini-v02'})
    r.raise_for_status(); data=r.json()
    if not isinstance(data,list): raise ValueError('API array expected')
    return data

def kst(t): return datetime.fromtimestamp(t,KST).strftime('%Y-%m-%d %H:%M:%S')

class Engine:
    def __init__(self,path=None,fetch=None,clock=time.time):
        self.path=path or os.getenv('ORDERFLOW_DB_PATH','orderflow_v02.sqlite3'); self.fetch=fetch or request_public; self.clock=clock
        self.lock=threading.RLock(); self.stop_event=threading.Event(); self.thread=None
        self.latest={'total':0,'books_ok':0,'eligible':0,'trades_ok':0,'errors':0,'candidates':[],'markets':[],'at':None,'at_epoch':None,'message':'검증 시작 버튼을 눌러 주세요.'}
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row
        c.execute('PRAGMA busy_timeout=20000')
        c.executescript('''CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, market TEXT NOT NULL, entered REAL NOT NULL, entry REAL NOT NULL, features TEXT NOT NULL, max_net REAL, min_net REAL, last_seen REAL, closed INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS market_entry ON signals(market,entered);
        CREATE TABLE IF NOT EXISTS marks(signal_id INTEGER NOT NULL, horizon INTEGER NOT NULL, observed REAL, bid REAL, net REAL, PRIMARY KEY(signal_id,horizon));
        CREATE TABLE IF NOT EXISTS observations(ts REAL,market TEXT,kind TEXT,metrics TEXT);
        CREATE TABLE IF NOT EXISTS cycles(ts REAL,total INTEGER,books_ok INTEGER,eligible INTEGER,trades_ok INTEGER,errors INTEGER);
        CREATE INDEX IF NOT EXISTS obs_time ON observations(ts);''')
        try:
            with c: yield c
        finally: c.close()
    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive(): return False
            self.stop_event.clear(); self.thread=threading.Thread(target=self.loop,daemon=True,name='orderflow-v02'); self.thread.start(); return True
    def stop(self): self.stop_event.set()
    def running(self): return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())
    def loop(self):
        while not self.stop_event.is_set():
            started=time.monotonic()
            try: self.cycle()
            except Exception as e:
                with self.lock: self.latest={**self.latest,'message':'검색 오류: '+str(e)[:180]}
            self.stop_event.wait(max(1,INTERVAL-(time.monotonic()-started)))
    def cycle(self):
        markets=sorted({x['market'] for x in self.fetch('/v1/market/all',{'isDetails':'false'}) if isinstance(x,dict) and str(x.get('market','')).startswith('KRW-')})
        if not markets: raise RuntimeError('KRW 마켓 없음')
        books={}; errors=0
        for i in range(0,len(markets),50):
            batch=markets[i:i+50]
            try:
                data=self.fetch('/v1/orderbook',{'markets':','.join(batch)}); now=self.clock()
                for raw in data:
                    if isinstance(raw,dict) and raw.get('market') in batch:
                        b=book_metrics(raw,now)
                        if b: books[raw['market']]=b
            except Exception: errors+=len(batch)
        eligible=[m for m,b in books.items() if b['ratio']>=1.25 and b['spread']<=.20]
        results={}; checked={}; trades_ok=0
        def verify(m):
            ticks=self.fetch('/v1/trades/ticks',{'market':m,'count':200})
            # Re-read the book AFTER trade request to avoid a stale entry reference.
            raw=self.fetch('/v1/orderbook',{'markets':m}); now=self.clock()
            b=book_metrics(next((x for x in raw if isinstance(x,dict) and x.get('market')==m),None),now)
            t=trade_metrics(ticks,now)
            return b,t,now
        with ThreadPoolExecutor(max_workers=6) as pool:
            pending={pool.submit(verify,m):m for m in eligible}
            for f in as_completed(pending):
                m=pending[f]
                try:
                    b,t,now=f.result()
                    if b: books[m]=b
                    checked[m]=dict(book=b,trade=t,checked_at=now)
                    if t: trades_ok+=1
                    if qualifies(b,t) and b['ask1_krw']>=BUDGET/(1+FEE) and b['bid1_krw']>=BUDGET*b['bid']/b['ask']/(1+FEE): results[m]=(b,t,now)
                except Exception: errors+=1
        now=self.clock()
        with self.lock:
            with self.db() as c:
                # Historical horizon marks use first fresh observed bid, never a future backfill.
                for r in c.execute('SELECT * FROM signals WHERE closed=0').fetchall():
                    b=books.get(r['market']); age=now-r['entered']
                    # The cycle may run long. The quote timestamp must be after entry and near observation.
                    usable=b and b['book_time']>=r['entered'] and 0<=now-b['book_time']<=30 and b['bid1_krw']>=BUDGET*b['bid']/r['entry']/(1+FEE)
                    if usable and b['book_time']<=r['entered']+1800+MAX_LAG:
                        obs=b['book_time']; n=net_pct(r['entry'],b['bid'])
                        c.execute('UPDATE signals SET max_net=CASE WHEN max_net IS NULL OR max_net<? THEN ? ELSE max_net END,min_net=CASE WHEN min_net IS NULL OR min_net>? THEN ? ELSE min_net END,last_seen=? WHERE id=?',(n,n,n,n,obs,r['id']))
                        c.execute('INSERT INTO observations VALUES(?,?,?,?)',(obs,r['market'],'tracking',json.dumps(b)))
                    for h in HORIZONS:
                        due=r['entered']+h
                        if usable and due<=b['book_time']<=due+MAX_LAG:
                            c.execute('INSERT OR IGNORE INTO marks VALUES(?,?,?,?,?)',(r['id'],h,b['book_time'],b['bid'],net_pct(r['entry'],b['bid'])))
                        elif now>due+MAX_LAG:
                            c.execute('INSERT OR IGNORE INTO marks VALUES(?,?,NULL,NULL,NULL)',(r['id'],h))
                    if age>1800+MAX_LAG: c.execute('UPDATE signals SET closed=1 WHERE id=?',(r['id'],))
                for m,item in checked.items():
                    c.execute('INSERT INTO observations VALUES(?,?,?,?)',(item['checked_at'],m,'screen',json.dumps(item)))
                rows=[]
                for m,(b,t,observed) in results.items():
                    # No retrospective signals: only a still-fresh quote at commit time.
                    if now-observed>15 or now-b['book_time']>15: continue
                    last=c.execute('SELECT entered FROM signals WHERE market=? ORDER BY entered DESC LIMIT 1',(m,)).fetchone()
                    new=not last or now-last['entered']>=COOLDOWN
                    if new:
                        initial=net_pct(b['ask'],b['bid'])
                        c.execute('INSERT INTO signals(market,entered,entry,features,max_net,min_net,last_seen) VALUES(?,?,?,?,?,?,?)',(m,now,b['ask'],json.dumps({**b,**t}),initial,initial,now))
                    rows.append(dict(market=m,entry=b['ask'],bid=b['bid'],**{k:v for k,v in b.items() if k!='bid'},**t,new=new,at=kst(observed)))
                c.execute('INSERT INTO cycles VALUES(?,?,?,?,?,?)',(now,len(markets),len(books),len(eligible),trades_ok,errors))
                # Keep raw research snapshots for 72h; trade outcomes remain until DB removal.
                c.execute('DELETE FROM observations WHERE ts<?',(now-3*86400,))
                c.execute('DELETE FROM cycles WHERE ts<?',(now-30*86400,))
            self.latest=dict(total=len(markets),books_ok=len(books),eligible=len(eligible),trades_ok=trades_ok,errors=errors,at=kst(now),at_epoch=now,candidates=sorted(rows,key=lambda r:(-r['buy_share'],-r['trade_total'])),markets=[dict(market=m,**books[m]) for m in markets if m in books],message='완료 · 전체 KRW 호가 1차 / 통과 종목 체결 2차')
        return self.latest
    def state(self):
        with self.lock,self.db() as c:
            recent=[]
            for r in c.execute('SELECT * FROM signals ORDER BY id DESC LIMIT 100'):
                d=dict(r); d['entered_kst']=kst(d['entered']); d['marks']={str(x['horizon']):x['net'] for x in c.execute('SELECT * FROM marks WHERE signal_id=?',(r['id'],))}; d.pop('features'); recent.append(d)
            stats=[]
            for h in HORIZONS:
                r=c.execute('SELECT count(net) n, sum(CASE WHEN net>0 THEN 1 ELSE 0 END) wins, avg(net) mean, sum(CASE WHEN net IS NULL THEN 1 ELSE 0 END) missing FROM marks WHERE horizon=?',(h,)).fetchone(); stats.append(dict(horizon=h,**dict(r)))
            return dict(**self.latest,running=self.running(),history=recent,stats=stats,total_signals=c.execute('SELECT count(*) FROM signals').fetchone()[0],fee_roundtrip_pct=.08)
    def export(self,kind):
        with self.lock,self.db() as c:
            out=io.StringIO(); w=csv.writer(out)
            if kind=='observations':
                w.writerow(['version','kst','market','kind','metrics_json']); w.writerows((VERSION,kst(r['ts']),r['market'],r['kind'],r['metrics']) for r in c.execute('SELECT * FROM observations ORDER BY ts'))
            elif kind=='coverage':
                w.writerow(['version','kst','total','books_ok','eligible','trades_ok','errors']); w.writerows((VERSION,kst(r['ts']),r['total'],r['books_ok'],r['eligible'],r['trades_ok'],r['errors']) for r in c.execute('SELECT * FROM cycles ORDER BY ts'))
            else:
                w.writerow(['version','market','entry_kst','entry_krw','fee_roundtrip_pct','max_net_pct','min_net_pct','closed','features_json']+[f'net_{h}s' for h in HORIZONS]+[f'observed_{h}s_kst' for h in HORIZONS])
                for r in c.execute('SELECT * FROM signals ORDER BY id'):
                    marks={x['horizon']:x for x in c.execute('SELECT * FROM marks WHERE signal_id=?',(r['id'],))}; w.writerow([VERSION,r['market'],kst(r['entered']),r['entry'],.08,r['max_net'],r['min_net'],r['closed'],r['features']]+[marks[h]['net'] if h in marks else '' for h in HORIZONS]+[kst(marks[h]['observed']) if h in marks and marks[h]['observed'] else '' for h in HORIZONS])
            return '\ufeff'+out.getvalue()

HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ORDERFLOW MINI V0.2</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{background:#0e1724;color:#e8eef7;font:14px/1.6 system-ui,sans-serif;margin:0}main{max-width:1200px;margin:auto;padding:16px}h1{font-size:23px;margin:0}h2{font-size:17px}p{color:#a9bbce}section{background:#172638;border:1px solid #33495f;border-radius:12px;margin:12px 0;padding:14px}button,a.btn,input{font:inherit;border:1px solid #4c6b80;border-radius:8px;padding:9px 12px;color:white;background:#253c50;text-decoration:none}button,a.btn{cursor:pointer}button.primary,a.primary{background:#32c6a7;color:#062b22;font-weight:800}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:9px}.coin{background:#101d2c;padding:12px;border:1px solid #365468;border-radius:10px}.green{color:#51e0b2}.red{color:#ff9c9c}.muted{color:#a9bbce}.big{font-size:22px;font-weight:bold}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #31465c;text-align:right}td:first-child,th:first-child{text-align:left}input{width:130px}a{color:#6de0c8}small{color:#afc0d0}</style></head><body><main>
<h1>빗썸 실시간 수급 탐색기 V0.2</h1><p id="subtitle"></p><div class="actions"><button class="primary" id="start">실시간 누적 검증 시작</button><button id="stop">수집 중지</button><a class="btn primary" id="live" href="/orderflow-v02/live">매수용 검색 →</a><a class="btn" href="/orderflow-v02">누적 검증실</a><a href="/orderflow">기존 V0.1</a></div><section><div id="status">조회 중…</div><p id="coverage"></p><small>전체 KRW 호가 검사 · 호가 조건 통과 종목만 30초 체결 조회 · 왕복 수수료 0.08% · 가상 검증용</small></section>
<section><h2>실시간 진입 후보 (4/4)</h2><label>예상 투자금 <input id="budget" type="number" min="1" step="10000" value="100000"> 원</label><p>진입 참고가 = 최우선 매도호가. 연구용 가상 원금 10만원(매수·매도 1호가 잔량 충족 필요), 화면 투자금은 수량 계산용. 실제 주문 없음.</p><div id="cards" class="grid"></div></section>
<section id="research"><h2>누적 검증 결과</h2><div id="stats" class="scroll"></div><h2>최근 가상 진입 기록</h2><div id="history" class="scroll"></div><div class="actions"><a class="btn" href="/api/orderflow-v02/csv/trades">검증 CSV</a><a class="btn" href="/api/orderflow-v02/csv/observations">수급 관찰 CSV</a><a class="btn" href="/api/orderflow-v02/csv/coverage">검색 상태 CSV</a></div></section>
<section><h2>전체 KRW 호가 결과</h2><input id="filter" placeholder="종목 검색"><div id="markets" class="scroll"></div></section><p>4/4는 실험 조건이며 수익 보장 아님. 스프레드는 실제 매도/매수 호가 차이로 반영, 추가 슬리피지와 시장 충격은 미반영. 15초 목표 간격(실제 소요 시간에 따라 지연), 누락 시점은 미측정. 무료 서버 휴면·재배포 시 중단되며 영구 디스크가 없으면 DB가 사라질 수 있어 CSV를 보관해 주세요.</p></main><script>
const $=id=>document.getElementById(id),live=location.pathname.endsWith('/live');let data=null,busy=false;const n=(x,d=4)=>x==null?'—':Number(x).toLocaleString('ko-KR',{maximumFractionDigits:d}),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));$('subtitle').textContent=live?'매수용 실시간 검색 · 진입 참고가':'실시간 누적 검증판 · 1/3/5/10/30분 추적';if(live){$('research').style.display='none';$('live').style.display='none';}
const table=(heads,rows)=>'<table><thead><tr>'+heads.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+x+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
function render(){if(!data)return;let d=data,amount=Math.max(0,Number($('budget').value)||0);$('status').textContent=(d.running?'수집 중':'수집 정지')+' · '+(d.at||'첫 검색 전')+' · '+d.message;$('coverage').textContent=`전체 ${d.total}개 / 호가 확인 ${d.books_ok}개 / 2차 대상 ${d.eligible}개 / 체결 유효 ${d.trades_ok}개 / 요청 오류 ${d.errors} · 누적 신호 ${d.total_signals}건`;
$('cards').innerHTML=(!d.running||!d.at_epoch||Date.now()/1000-d.at_epoch>45?[]:d.candidates).map(x=>`<div class="coin"><b>${esc(x.market)} · 4/4</b><div class="green big">${n(x.entry,8)}원</div><small>진입 참고가 · ${esc(x.at)}</small><div>현재 매도 가능 참고가 ${n(x.bid,8)}원</div><div>매수 체결 ${n(x.buy_share,1)}% · 호가비 ${n(x.ratio,2)}배</div><div>스프레드 ${n(x.spread,3)}% · 30초 체결 ${n(x.trade_total,0)}원</div><div>예상 수량 ${n(amount/(x.entry*1.0004),8)}개</div><div>${amount/(1.0004)>x.ask1_krw?'입력 금액이 1호가 잔량 초과 · 체결가 변동 가능':'1호가 잔량 범위 내'}</div></div>`).join('')||'<p>현재 조건 충족 후보 없음 또는 데이터 갱신 대기</p>';
$('stats').innerHTML=table(['기간','측정','수익률 > 0','승률','평균 순수익','누락'],d.stats.map(x=>[x.horizon/60+'분',x.n,x.wins??0,x.n?n(100*x.wins/x.n,1)+'%':'—',x.mean==null?'—':n(x.mean,3)+'%',x.missing??0]));$('history').innerHTML=table(['종목','진입 KST','진입가','1분','3분','5분','10분','30분','최고','최저','상태'],d.history.map(x=>[esc(x.market),esc(x.entered_kst),n(x.entry,8),...[60,180,300,600,1800].map(h=>x.marks[h]==null?'—':n(x.marks[h],3)+'%'),n(x.max_net,3)+'%',n(x.min_net,3)+'%',x.closed?'완료':'추적중']));let f=$('filter').value.toUpperCase();$('markets').innerHTML=table(['종목','매도호가','매수호가','호가비','스프레드'],d.markets.filter(x=>x.market.includes(f)).map(x=>[esc(x.market),n(x.ask,8),n(x.bid,8),n(x.ratio,2),n(x.spread,3)+'%']));}
async function refresh(){if(busy)return;busy=true;try{let r=await fetch('/api/orderflow-v02/state',{cache:'no-store'});if(!r.ok)throw Error('서버 조회 오류');data=await r.json();render()}catch(e){$('status').textContent=e.message}finally{busy=false}}
async function action(x){try{let r=await fetch('/api/orderflow-v02/'+x,{method:'POST'});if(!r.ok)throw Error('수집 제어 실패');await refresh()}catch(e){$('status').textContent=e.message}}
$('start').onclick=()=>action('start');$('stop').onclick=()=>action('stop');$('budget').oninput=render;$('filter').oninput=render;if(live)action('start');else refresh();setInterval(refresh,3000);
</script></body></html>'''

def register_orderflow_v02(app, engine=None):
    e=engine or Engine()
    @app.get('/orderflow-v02')
    @app.get('/orderflow-v02/live')
    def of02_page(): return HTML
    @app.get('/api/orderflow-v02/state')
    def of02_state(): return jsonify(e.state())
    @app.post('/api/orderflow-v02/start')
    def of02_start(): e.start(); return jsonify(ok=True)
    @app.post('/api/orderflow-v02/stop')
    def of02_stop(): e.stop(); return jsonify(ok=True)
    @app.get('/api/orderflow-v02/csv/<kind>')
    def of02_csv(kind):
        if kind not in ('trades','observations','coverage'): return 'Not found',404
        return Response(e.export(kind),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename=ORDERFLOW_MINI_V0_2_{kind.upper()}_{datetime.now(KST):%Y%m%d%H%M}.csv'})
    return e

if __name__=='__main__':
    app=Flask(__name__); register_orderflow_v02(app); app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')),threaded=True)
