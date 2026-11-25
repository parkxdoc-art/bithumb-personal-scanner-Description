"""ORDERFLOW MINI V0.3: public market data, paper research, device archive."""
import csv, io, json, os, sqlite3, time, uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import jsonify, request
from orderflow_mini_v02 import Engine as BaseEngine, book_metrics, number, net_pct, kst, FEE, BUDGET, HORIZONS, MAX_LAG, COOLDOWN, HTML as OLD_HTML

VERSION="ORDERFLOW MINI V0.3"
WINDOWS=(30,60,180)
VALUES=(1000000,3000000,5000000,10000000)
STABLES={"USDT","USDC","DAI","TUSD","FDUSD","USD1","PYUSD","USDE","USDD","RLUSD","EURC","EURI","USDP","GUSD","FRAX","USDS","SUSD","USDG","USDK","USDQ","XAUT","PAXG"}
COHORTS=tuple(f"{w}s_{v//1000000}M_{p}" for w in WINDOWS for v in VALUES for p in ("plain","up"))
BOOT=uuid.uuid4().hex

def trade_windows(ticks,now,market):
    valid=[]; seen=set(); windows={}; reasons={}
    for t in ticks if isinstance(ticks,list) else []:
        if not isinstance(t,dict) or t.get("market") not in (None,market): continue
        stamp,p,q=number(t.get("timestamp")),number(t.get("trade_price")),number(t.get("trade_volume"))
        side=t.get("ask_bid"); sid=t.get("sequential_id")
        if not stamp or not p or not q or side not in ("BID","ASK") or stamp/1000>now: continue
        if sid is not None:
            if str(sid) in seen: continue
            seen.add(str(sid))
        valid.append((stamp/1000,p,q,side))
    valid.sort(key=lambda t:t[0])
    for w in WINDOWS:
        key=str(w)
        if len(ticks)>=200 and (not valid or valid[0][0]>=now-w):
            reasons[key]="sample_cap"; continue
        a=[t for t in valid if now-w<=t[0]<=now]
        if len(a)<3: reasons[key]="few_trades"; continue
        buy=sum(p*q for _,p,q,s in a if s=="BID")
        sell=sum(p*q for _,p,q,s in a if s=="ASK")
        if not buy+sell: reasons[key]="zero_value"; continue
        windows[key]=dict(buy_share=100*buy/(buy+sell),trade_total=buy+sell,buy_krw=buy,sell_krw=sell,trade_count=len(a),price_change_pct=100*(a[-1][1]/a[0][1]-1),last_age=now-a[-1][0])
        reasons[key]="ok"
    return windows,reasons

def matches(m,b,windows):
    if m[4:] in STABLES or not b or b["ratio"]<1.25 or b["spread"]>.20: return []
    out=[]
    for w in WINDOWS:
        t=windows.get(str(w))
        if not t or t["buy_share"]<58 or t["last_age"]>15: continue
        for v in VALUES:
            if t["trade_total"]>=v:
                out.append(f"{w}s_{v//1000000}M_plain")
                if t["price_change_pct"]>0: out.append(f"{w}s_{v//1000000}M_up")
    return out

class Engine(BaseEngine):
    def __init__(self,path=None,fetch=None,clock=time.time):
        super().__init__(path or os.getenv("ORDERFLOW_V03_DB_PATH","orderflow_v03.sqlite3"),fetch,clock)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row
        c.execute("PRAGMA busy_timeout=20000")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY,market TEXT,cohort TEXT,entered REAL,entry REAL,features TEXT,max_net REAL,min_net REAL,closed INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS signal_lookup ON signals(market,cohort,entered);
        CREATE TABLE IF NOT EXISTS marks(signal_id INTEGER,horizon INTEGER,observed REAL,bid REAL,net REAL,PRIMARY KEY(signal_id,horizon));
        CREATE TABLE IF NOT EXISTS archive(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,ts REAL);
        """)
        c.execute("INSERT OR IGNORE INTO meta VALUES('session',?)",(uuid.uuid4().hex,)); c.commit()
        try:
            with c: yield c
        finally: c.close()
    def event(self,c,kind,row,now):
        c.execute("INSERT INTO archive(kind,payload,ts) VALUES(?,?,?)",(kind,json.dumps(row,ensure_ascii=False),now))
    def trade_row(self,c,r):
        marks={x["horizon"]:x for x in c.execute("SELECT * FROM marks WHERE signal_id=?",(r["id"],))}
        d=dict(version=VERSION,market=r["market"],cohort=r["cohort"],entry_epoch=r["entered"],entry_kst=kst(r["entered"]),entry_krw=r["entry"],fee_roundtrip_pct=.08,max_net_pct=r["max_net"],min_net_pct=r["min_net"],closed=r["closed"],features_json=r["features"])
        for h in HORIZONS:
            a=marks.get(h)
            d["net_%ss"%h]=a["net"] if a else None
            d["observed_%ss_kst"%h]=kst(a["observed"]) if a and a["observed"] is not None else None
            d["mark_%ss"%h]="measured" if a and a["net"] is not None else "missing" if a else "pending"
        return d
    def cycle(self):
        markets=sorted({x["market"] for x in self.fetch("/v1/market/all",{"isDetails":"false"}) if isinstance(x,dict) and str(x.get("market","")).startswith("KRW-")})
        if not markets: raise RuntimeError("KRW 마켓 없음")
        books={}; errors=0
        for i in range(0,len(markets),50):
            batch=markets[i:i+50]
            try:
                raw=self.fetch("/v1/orderbook",{"markets":",".join(batch)})
                now=self.clock()
                for x in raw:
                    if isinstance(x,dict) and x.get("market") in batch:
                        b=book_metrics(x,now)
                        if b: books[x["market"]]=b
            except Exception: errors+=len(batch)
        eligible=[m for m,b in books.items() if m[4:] not in STABLES and b["ratio"]>=1.25 and b["spread"]<=.20]
        checked={}; candidates=[]; trade_ok=0
        def verify(m):
            ticks=self.fetch("/v1/trades/ticks",{"market":m,"count":200})
            raw=self.fetch("/v1/orderbook",{"markets":m}); now=self.clock()
            b=book_metrics(next((x for x in raw if isinstance(x,dict) and x.get("market")==m),None),now)
            windows,reasons=trade_windows(ticks,now,m)
            return b,windows,reasons,now
        with ThreadPoolExecutor(max_workers=6) as pool:
            fs={pool.submit(verify,m):m for m in eligible}
            for f in as_completed(fs):
                m=fs[f]
                try:
                    b,w,reasons,obs=f.result()
                    if b: books[m]=b
                    checked[m]=dict(book=b,windows=w,reasons=reasons,observed=obs)
                    if w: trade_ok+=1
                except Exception as exc:
                    errors+=1; checked[m]=dict(book=books.get(m),windows={},reasons={str(w):"api_error" for w in WINDOWS},observed=self.clock(),error=str(exc)[:120])
        now=self.clock()
        with self.lock,self.db() as c:
            changed=set(); tracked=set()
            for r in c.execute("SELECT * FROM signals WHERE closed=0").fetchall():
                b=books.get(r["market"]); age=now-r["entered"]
                usable=b and b["book_time"]>=r["entered"] and 0<=now-b["book_time"]<=30 and b["bid1_krw"]>=BUDGET*b["bid"]/r["entry"]/(1+FEE)
                if usable and b["book_time"]<=r["entered"]+1800+MAX_LAG:
                    obs=b["book_time"]; n=net_pct(r["entry"],b["bid"])
                    c.execute("UPDATE signals SET max_net=max(max_net,?),min_net=min(min_net,?) WHERE id=?",(n,n,r["id"]))
                    changed.add(r["id"])
                    if r["market"] not in tracked:
                        self.event(c,"observations",dict(version=VERSION,kst=kst(obs),market=r["market"],kind="tracking",metrics_json=json.dumps(b)),now); tracked.add(r["market"])
                for h in HORIZONS:
                    due=r["entered"]+h
                    if usable and due<=b["book_time"]<=due+MAX_LAG:
                        cur=c.execute("INSERT OR IGNORE INTO marks VALUES(?,?,?,?,?)",(r["id"],h,b["book_time"],b["bid"],net_pct(r["entry"],b["bid"])))
                        if cur.rowcount: changed.add(r["id"])
                    elif now>due+MAX_LAG:
                        cur=c.execute("INSERT OR IGNORE INTO marks VALUES(?,?,NULL,NULL,NULL)",(r["id"],h))
                        if cur.rowcount: changed.add(r["id"])
                if age>1800+MAX_LAG:
                    c.execute("UPDATE signals SET closed=1 WHERE id=?",(r["id"],)); changed.add(r["id"])
            for m,x in checked.items():
                b,w,obs=x["book"],x["windows"],x["observed"]
                self.event(c,"observations",dict(version=VERSION,kst=kst(obs),market=m,kind="screen",metrics_json=json.dumps(x)),now)
                if not b or now-obs>15 or now-b["book_time"]>15 or b["ask1_krw"]<BUDGET/(1+FEE) or b["bid1_krw"]<BUDGET*b["bid"]/b["ask"]/(1+FEE): continue
                cohorts=matches(m,b,w)
                if not cohorts: continue
                for cohort in cohorts:
                    last=c.execute("SELECT entered FROM signals WHERE market=? AND cohort=? ORDER BY entered DESC LIMIT 1",(m,cohort)).fetchone()
                    if last and now-last["entered"]<COOLDOWN: continue
                    initial=net_pct(b["ask"],b["bid"])
                    cur=c.execute("INSERT INTO signals(market,cohort,entered,entry,features,max_net,min_net) VALUES(?,?,?,?,?,?,?)",(m,cohort,now,b["ask"],json.dumps(dict(book=b,windows=w)),initial,initial))
                    changed.add(cur.lastrowid)
                candidates.append(dict(market=m,entry=b["ask"],**b,windows=w,cohorts=cohorts,observed=obs,at=kst(obs)))
            for sid in changed:
                r=c.execute("SELECT * FROM signals WHERE id=?",(sid,)).fetchone()
                self.event(c,"trades",self.trade_row(c,r),now)
            self.event(c,"coverage",dict(version=VERSION,kst=kst(now),total=len(markets),books_ok=len(books),eligible=len(eligible),trades_ok=trade_ok,errors=errors),now)
            c.execute("DELETE FROM archive WHERE ts<?",(now-3*86400,))
            self.latest=dict(total=len(markets),books_ok=len(books),eligible=len(eligible),trades_ok=trade_ok,errors=errors,at=kst(now),at_epoch=now,candidates=candidates,markets=[dict(market=m,**b) for m,b in books.items()],message="완료 · 전체 KRW / 24개 비교 조건")
        return self.latest
    def state(self):
        with self.lock,self.db() as c:
            return dict(**self.latest,running=self.running(),boot=BOOT,session=c.execute("SELECT value FROM meta WHERE key='session'").fetchone()[0],fee_roundtrip_pct=.08,cohorts=COHORTS,total_signals=c.execute("SELECT count(*) FROM signals").fetchone()[0])
    def archive(self,after=0):
        with self.lock,self.db() as c:
            session=c.execute("SELECT value FROM meta WHERE key='session'").fetchone()[0]
            first=c.execute("SELECT min(seq) FROM archive").fetchone()[0]
            rows=c.execute("SELECT * FROM archive WHERE seq>? ORDER BY seq LIMIT 500",(after,)).fetchall()
            return dict(session=session,first=first,events=[dict(seq=r["seq"],kind=r["kind"],payload=json.loads(r["payload"])) for r in rows])

CSS=OLD_HTML.split("<style>",1)[1].split("</style>",1)[0]
HTML=r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ORDERFLOW MINI V0.3</title><style>'''+CSS+r'''select{font:inherit;background:#253c50;color:white;padding:8px;border:1px solid #4c6b80;border-radius:8px} .warn{color:#ffcd80}input[type=file]{width:auto;max-width:100%}</style></head><body><main>
<h1>빗썸 실시간 수급 탐색기 V0.3</h1><p id="subtitle"></p>
<div class="actions"><button class="primary" id="start">실시간 누적 검증 시작</button><button id="stop">수집 중지</button><a class="btn primary" href="/flow-v03/live">매수용 검색 →</a><a class="btn" href="/flow-v03">누적 검증실</a><a href="/orderflow-v02">기존 V0.2</a></div>
<section><div id="status">서버 확인 중</div><p id="coverage"></p><div id="backup" class="warn">기기 보관함 연결 중</div><div id="gap" class="warn"></div><p>전체 KRW 호가 검사 · 스테이블코인 진입 제외 · 왕복 수수료 0.08% · 실제 주문 없음. 15초 목표 간격.</p></section>
<section><h2>실시간 진입 후보</h2><label>비교 조건 <select id="cohort"></select></label> <label>투자금 <input id="budget" type="number" min="1" value="100000"> 원</label><p>가상 원금 10만원, 최우선 매도호가 진입·매수호가 평가. 실제 수익성을 보장하지 않아.</p><div id="cards" class="grid"></div></section>
<section id="research"><h2>내 기기 누적 검증</h2><p>동일 브라우저에 자동 저장된 기록. V0.2 CSV는 별도 버전으로 보존. 24개 조건은 중복 가상 신호이며 실제 거래 횟수가 아님.</p><div id="stats" class="scroll"></div><h2>조건별 5분 / 30분 비교</h2><div id="compare" class="scroll"></div><h2>최근 가상 진입 기록 (선택 조건)</h2><div id="history" class="scroll"></div><h2>자동 보관 / CSV 복원</h2><div class="actions"><button data-export="trades">검증 CSV</button><button data-export="observations">수급 관찰 CSV</button><button data-export="coverage">검색 상태 CSV</button></div><p><input type="file" id="restore" accept=".csv,text/csv" multiple> V0.2/V0.3 CSV 복원 (기기 보관함에 병합)</p><div id="restoreStatus"></div><p>무료 서버 휴면 시 수집 중단. 이미 기기에 동기화한 기록은 유지되지만 브라우저 데이터 삭제·시크릿 모드·저장공간 부족 시 사라질 수 있어. CSV도 따로 보관해 줘. 기기 간 자동 동기화 없음.</p></section>
<section><h2>전체 KRW 호가</h2><input id="filter" placeholder="종목 검색"><div id="markets" class="scroll"></div></section>
</main><script>
const $=id=>document.getElementById(id),live=location.pathname.endsWith('/live'),H=[60,180,300,600,1800],BASE='30s_10M_plain',V='ORDERFLOW MINI V0.3';
const n=(x,d=3)=>x==null||x===''?'—':Number(x).toLocaleString('ko-KR',{maximumFractionDigits:d});
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const table=(heads,rows)=>'<table><thead><tr>'+heads.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
let data=null,busy=false,db=null,localTrades=[],lastCycle=null;
$('subtitle').textContent=live?'매수용 실시간 검색':'30/60/180초 × 100/300/500/1000만원 × 가격 상승 확인 유/무';
if(live)$('research').style.display='none';
const cohorts=[];
for(const w of [30,60,180])for(const v of [1,3,5,10])for(const p of ['plain','up']){let o=document.createElement('option');o.value=w+'s_'+v+'M_'+p;cohorts.push(o.value);o.textContent=w+'초 / '+v*100+'만원 / '+(p==='up'?'가격 상승 확인':'가격 조건 없음');$('cohort').append(o)}$('cohort').value=BASE;
function openDB(){return new Promise((resolve,reject)=>{let r=indexedDB.open('ORDERFLOW_ARCHIVE_V03',1);r.onupgradeneeded=()=>{let d=r.result;let s=d.createObjectStore('rows',{keyPath:'key'});s.createIndex('kind','kind');d.createObjectStore('meta')};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error)})}
const req=r=>new Promise((resolve,reject)=>{r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error)});
function txDone(t){return new Promise((resolve,reject)=>{t.oncomplete=resolve;t.onabort=t.onerror=()=>reject(t.error||Error('저장 실패'))})}
function kst(x){return new Date(Number(x)*1000).toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}).replace('T',' ')}
function normalize(kind,p){let r={...p};r.version=r.version||V;if(kind==='trades')r.cohort=r.cohort||BASE;return r}
function recordKey(kind,r){return JSON.stringify([kind,r.version,kind==='trades'?r.entry_kst:r.kst,kind==='coverage'?'':r.market,kind==='trades'?r.cohort:kind==='observations'?r.kind:''])}
function mergeTrade(old,r){if(!old)return r;let q={...old,...r};for(const h of H){const k='net_'+h+'s',o='observed_'+h+'s_kst',m='mark_'+h+'s';if(old[k]!=null&&old[k]!==''&&(r[k]==null||r[k]==='')){q[k]=old[k];q[o]=old[o];q[m]=old[m]}else if(old[m]==='missing'&&r[m]==='pending')q[m]='missing'}for(const [k,f] of [['max_net_pct',Math.max],['min_net_pct',Math.min]]){let a=[old[k],r[k]].filter(x=>x!=null&&x!=='').map(Number);q[k]=a.length?f(...a):null}q.closed=Math.max(Number(old.closed)||0,Number(r.closed)||0);return q}
async function storeBatch(items,session,cursor){let t=db.transaction(['rows','meta'],'readwrite'),done=txDone(t),s=t.objectStore('rows');for(const i of items){let r=normalize(i.kind,i.payload),key=recordKey(i.kind,r);if(i.kind==='trades'){let old=await req(s.get(key));r=mergeTrade(old?.row,r)}s.put({key,kind:i.kind,row:r})}if(session)t.objectStore('meta').put(cursor,'cursor:'+session);await done}
async function getRows(kind){return (await req(db.transaction('rows','readonly').objectStore('rows').index('kind').getAll(kind))).map(x=>x.row)}
async function meta(key){return req(db.transaction('meta','readonly').objectStore('meta').get(key))}
async function backup(){if(!db||!data)return;try{let session=data.session,after=Number(await meta('cursor:'+session)||0);let count=0;for(let i=0;i<20;i++){let res=await fetch('/api/flow-v03/archive?after='+after,{cache:'no-store'});if(!res.ok)throw Error('백업 조회 오류');let j=await res.json();if(j.session!==session)throw Error('서버 재시작 감지 · 다음 조회에서 재개');if(j.first!=null&&j.first>after+1)$('gap').textContent='서버 보관 기한 경과 또는 미동기화 구간 있음';if(!j.events.length)break;after=j.events.at(-1).seq;await storeBatch(j.events,session,after);count+=j.events.length;if(j.events.length<500)break}
localTrades=await getRows('trades');let cov=await getRows('coverage');let all=cov.filter(x=>x.version===V).map(x=>x.kst).sort();lastCycle=all.at(-1)||null;$('backup').textContent='기기 보관 성공 · 가상 진입 '+localTrades.length+'건 / 검색 '+cov.length+'회 · 새 동기화 '+count+'행';renderResearch()}catch(e){$('backup').textContent='자동 보관 실패: '+e.message+' · CSV 별도 보관 권장'}}
function stats(rows,h){let a=rows.map(x=>x['net_'+h+'s']).filter(v=>v!=null&&v!==''&&Number.isFinite(Number(v))).map(Number),win=a.filter(v=>v>0).length;return {n:a.length,win,mean:a.length?a.reduce((s,v)=>s+v,0)/a.length:null}}
function renderResearch(){let selected=$('cohort').value,all=localTrades.filter(x=>x.version===V),rows=all.filter(x=>x.cohort===selected);$('stats').innerHTML=table(['기간','측정','수익','승률','평균 순수익','누락'],H.map(h=>{let a=stats(rows,h);return [h/60+'분',a.n,a.win,a.n?n(100*a.win/a.n,1)+'%':'—',a.mean==null?'—':n(a.mean,3)+'%',rows.filter(x=>x['mark_'+h+'s']==='missing').length]}));
$('compare').innerHTML=table(['조건','가상 진입','5분 측정','5분 평균 %','30분 측정','30분 평균 %'],cohorts.map(c=>{let r=all.filter(x=>x.cohort===c),a=stats(r,300),b=stats(r,1800);return [c,r.length,a.n,n(a.mean),b.n,n(b.mean)]}));
$('history').innerHTML=table(['종목','진입 KST','진입가','1분','3분','5분','10분','30분','최고','최저'],rows.sort((a,b)=>String(b.entry_kst).localeCompare(String(a.entry_kst))).slice(0,100).map(x=>[x.market,x.entry_kst,n(x.entry_krw,8),...H.map(h=>n(x['net_'+h+'s'])),n(x.max_net_pct),n(x.min_net_pct)]))}
function render(){if(!data)return;let d=data,selected=$('cohort').value,w=selected.split('s_')[0],amount=Math.max(0,Number($('budget').value)||0);$('status').textContent=(d.running?'수집 중':'수집 정지')+' · '+(d.at||'첫 검색 전')+' · '+d.message;$('coverage').textContent='전체 '+d.total+'개 / 호가 '+d.books_ok+'개 / 체결 2차 '+d.eligible+'개 / 유효 '+d.trades_ok+'개 / 오류 '+d.errors;
$('cards').innerHTML=(!d.running?[]:d.candidates.filter(x=>Date.now()/1000-x.observed<=30&&x.cohorts.includes(selected))).map(x=>{let t=x.windows[w];return '<div class="coin"><b>'+esc(x.market)+'</b><div class="green big">'+n(x.entry,8)+'원</div><small>진입 참고가 · '+esc(x.at)+'</small><div>매도 가능 참고가 '+n(x.bid,8)+'원</div><div>매수 체결 '+n(t.buy_share,1)+'% · 호가비 '+n(x.ratio,2)+'배</div><div>체결대금 '+n(t.trade_total,0)+'원 · 가격 변화 '+n(t.price_change_pct,3)+'%</div><div>예상 수량 '+n(amount/(x.entry*1.0004),8)+'개</div><small>'+(amount/1.0004>x.ask1_krw?'입력금액 1호가 잔량 초과':'1호가 잔량 범위 내')+'</small></div>'}).join('')||'<p>현재 선택 조건의 신선한 후보 없음</p>';
let f=$('filter').value.toUpperCase();$('markets').innerHTML=table(['종목','매도호가','매수호가','호가비','스프레드'],d.markets.filter(x=>x.market.includes(f)).map(x=>[x.market,n(x.ask,8),n(x.bid,8),n(x.ratio,2),n(x.spread,3)+'%']));renderResearch()}
async function refresh(){if(busy)return;busy=true;try{let r=await fetch('/api/flow-v03/state',{cache:'no-store'});if(!r.ok)throw Error('서버 조회 오류');let next=await r.json();if(data&&data.boot!==next.boot)$('gap').textContent='서버 재시작 감지 · 이전 기기 기록 유지 · 수집 재시작 필요';if(lastCycle&&next.at&&Date.parse(next.at.replace(' ','T')+'+09:00')-Date.parse(lastCycle.replace(' ','T')+'+09:00')>120000)$('gap').textContent='수집 공백 2분 이상 확인 · 공백 기간 수익률은 미측정';data=next;render();await backup()}catch(e){$('status').textContent=e.message}finally{busy=false}}
async function action(x){try{let r=await fetch('/api/flow-v03/'+x,{method:'POST'});if(!r.ok)throw Error('수집 제어 실패');await refresh()}catch(e){$('status').textContent=e.message}}
function parseCSV(s){s=s.replace(/^\uFEFF/,'');let out=[],row=[],v='',q=false;for(let i=0;i<s.length;i++){let c=s[i];if(q){if(c==='"'&&s[i+1]==='"'){v+='"';i++}else if(c==='"')q=false;else v+=c}else if(c==='"'&&v==='')q=true;else if(c===','){row.push(v);v=''}else if(c==='\n'){row.push(v.replace(/\r$/,''));out.push(row);row=[];v=''}else v+=c}if(q)throw Error('CSV 따옴표 오류');if(v||row.length){row.push(v.replace(/\r$/,''));out.push(row)}return out}
$('restore').onchange=async()=>{if(!db)return;try{let count=0;for(let file of $('restore').files){let lines=parseCSV(await file.text()),heads=lines.shift()||[];let kind=heads.includes('entry_krw')?'trades':heads.includes('metrics_json')?'observations':heads.includes('books_ok')?'coverage':null;if(!kind)throw Error('지원하지 않는 CSV: '+file.name);let items=[];for(let row of lines){if(row.length!==heads.length)continue;let p=Object.fromEntries(heads.map((h,i)=>[h,row[i]]));if(!['ORDERFLOW MINI V0.2',V].includes(p.version))continue;if(kind==='trades'&&(!p.market||!p.entry_kst||!Number(p.entry_krw)))continue;if(kind!=='trades'&&!p.kst)continue;items.push({kind,payload:p})}for(let i=0;i<items.length;i+=200)await storeBatch(items.slice(i,i+200));count+=items.length}localTrades=await getRows('trades');renderResearch();$('restoreStatus').textContent='복원 '+count+'행 처리 (중복 병합) · V0.2는 별도 버전으로 CSV에 보존'}catch(e){$('restoreStatus').textContent='복원 오류: '+e.message}};
document.querySelectorAll('[data-export]').forEach(b=>b.onclick=async()=>{try{if(!db)throw Error('보관함 미연결');let kind=b.dataset.export,rows=await getRows(kind),heads=kind==='trades'?['version','market','cohort','entry_epoch','entry_kst','entry_krw','fee_roundtrip_pct','max_net_pct','min_net_pct','closed','features_json',...H.map(h=>'net_'+h+'s'),...H.map(h=>'observed_'+h+'s_kst'),...H.map(h=>'mark_'+h+'s')]:kind==='coverage'?['version','kst','total','books_ok','eligible','trades_ok','errors']:['version','kst','market','kind','metrics_json'];let q=v=>'"'+String(v??'').replace(/"/g,'""')+'"';let csv='\uFEFF'+[heads,...rows.map(r=>heads.map(h=>r[h]))].map(r=>r.map(q).join(',')).join('\n');let url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='ORDERFLOW_MINI_V0_3_'+kind.toUpperCase()+'_'+kst(Date.now()/1000).replace(/[^0-9]/g,'').slice(0,12)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000)}catch(e){$('backup').textContent=e.message}});
$('start').onclick=()=>action('start');$('stop').onclick=()=>action('stop');$('budget').oninput=render;$('cohort').onchange=render;$('filter').oninput=render;
(async()=>{try{db=await openDB();localTrades=await getRows('trades');let a=(await getRows('coverage')).filter(x=>x.version===V).map(x=>x.kst).sort();lastCycle=a.at(-1)||null;renderResearch();$('backup').textContent='기기 보관함 연결 완료'}catch(e){$('backup').textContent='브라우저 저장 불가: '+e.message}if(live)await action('start');else await refresh();setInterval(refresh,5000)})();
</script></body></html>'''

def register_flow_v03(app,engine=None):
    e=engine or Engine()
    @app.get("/flow-v03")
    @app.get("/flow-v03/live")
    def flow03_page(): return HTML
    @app.get("/api/flow-v03/state")
    def flow03_state(): return jsonify(e.state())
    @app.post("/api/flow-v03/start")
    def flow03_start(): e.start(); return jsonify(ok=True)
    @app.post("/api/flow-v03/stop")
    def flow03_stop(): e.stop(); return jsonify(ok=True)
    @app.get("/api/flow-v03/archive")
    def flow03_archive():
        try: after=max(0,int(request.args.get("after","0")))
        except ValueError: return jsonify(error="invalid cursor"),400
        return jsonify(e.archive(after))
    return e
