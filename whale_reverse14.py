"""WHALE DETECTOR V1.4: reverse research of large price increases."""
import csv,io,math,statistics,threading,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.parse import parse_qs
import whale_detector as live
STEP=14400
DAY=86400
TARGETS=(30,50,100)
DISCOVERY=set("KRW-0G,KRW-AAVE,KRW-ADP,KRW-AI,KRW-ALICE,KRW-ANKR,KRW-ARDR,KRW-ASTR,KRW-AVA,KRW-AXIS,KRW-BARD,KRW-BEL,KRW-BIO,KRW-BMT,KRW-BORA,KRW-BSV,KRW-C98,KRW-CBK,KRW-CHECK,KRW-COOKIE,KRW-CPOOL,KRW-CT,KRW-CYBER,KRW-DEEP,KRW-DOS,KRW-EDGEX,KRW-ELSA,KRW-ERA,KRW-F,KRW-FLOKI,KRW-G,KRW-GMT,KRW-GRND,KRW-HBAR,KRW-HOME,KRW-HYPER,KRW-IN,KRW-IOTX,KRW-JUP,KRW-KGEN,KRW-LA,KRW-LISTA,KRW-MAGIC,KRW-MASK,KRW-MED,KRW-METIS,KRW-MMT,KRW-MORPHO,KRW-NCT,KRW-NFT,KRW-OBT,KRW-OP,KRW-OSMO,KRW-PENGU,KRW-POKT,KRW-POWR,KRW-PUFFER,KRW-PYTH,KRW-RAY,KRW-REQ,KRW-SANC,KRW-RPL,KRW-SEI,KRW-SIX,KRW-SOL,KRW-SPK,KRW-STRK,KRW-SWAP,KRW-TAO,KRW-THQ,KRW-TRB,KRW-UB,KRW-USELESS,KRW-VVV,KRW-WCT,KRW-WOO,KRW-XEC,KRW-XTER,KRW-YGG,KRW-ZKC".split(","))
PATTERNS={"HL":"3일 저점 +0.5%","A":"A 조용한 저점 상승 (변동폭 12% 미만)","B":"B 변동 속 저점 상승 (12% 이상)","R12":"3일 변동폭 12% 이상","R15":"3일 변동폭 15% 이상","R7":"7일 변동폭 25% 이상","FLOW":"3일 저점 상승 + 1일 거래대금 1배 이상"}
PATTERNS.update({"R37":"3일 12% + 7일 25% 변동폭","DOWNR":"3일 변동폭 12% 이상 + 하락","DOWN1":"직전 1일 가격 하락","QUIET1":"1일 변동폭 12% 이상 + 거래량 1배 미만","C":"하락 중 하락 강도 및 거래대금 감소 (3일)","D":"3일 아랫꼬리 3개 이상 + 저점 방어","RS":"BTC 3일 하락 중 상대강도 양수","CONC":"3일 거래대금 상위 3봉 집중도 40% 이상","EFF":"3일 하락봉 가격 변화/거래대금 감소","LONG":"20일 하락 후 3일 저점 상승"})
PERIODS=(1,3,7,14,20)
ADV_KEYS={"change":"가격 변화 %","span":"변동폭 %","volume":"거래량 배수 (직전 동기간)","turnover":"거래대금 배수 (직전 동기간)","down_strength":"하락 강도 후기/전기","down_turnover":"하락봉 거래대금 후기/전기","wick_count":"아랫꼬리 방어 봉 수","wick_share":"아랫꼬리 방어 봉 비율 %","close_loc":"종가 범위 위치 %","low_change":"직전 동기간 대비 저점 %","concentration":"상위 3봉 거래대금 비중 %","down_impact":"하락폭/거래대금 후기/전기","btc_change":"BTC 변화 %","rs":"BTC 상대강도 %"}
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,summary=[],details=[],coverage=[],errors=[])
def patch(**kw):
    with LOCK:STATE.update(kw)
def advanced(by,good,btc,t,d):
    n=d*6;s="_"+str(d)+"d";out={"adv_"+k+s:None for k in ADV_KEYS}
    times=range(t-(2*n+1)*STEP,t,STEP)
    # Recent and previous equal-length windows, plus the predecessor close.
    if any(k not in good for k in times):return out
    seq=[by[k] for k in times];prev=seq[1:n+1];recent=seq[n+1:];p=seq[n][4]
    def ratio(a,b):return a/b if b>0 else None
    def pct(a,b):return 100*(a/b-1)
    lo=min(b[3] for b in recent);hi=max(b[2] for b in recent)
    wick=[b for b in recent if b[2]>b[3] and (min(b[1],b[4])-b[3])/(b[2]-b[3])>=.35 and (b[4]-b[3])/(b[2]-b[3])>=.5]
    def down(segment,base):
        moves=[max(0.,-pct(b[4],a)) for a,b in zip([base]+[x[4] for x in segment[:-1]],segment)]
        return sum(moves),sum(b[6] for b,v in zip(segment,moves) if v>0)
    half=n//2
    da,ta=down(recent[:half],p);db,tb=down(recent[half:],recent[half-1][4])
    va=sum(b[5] for b in prev);vb=sum(b[5] for b in recent)
    qa=sum(b[6] for b in prev);qb=sum(b[6] for b in recent)
    impact_a=ratio(da,ta);impact_b=ratio(db,tb)
    metrics=dict(change=pct(recent[-1][4],p),span=pct(hi,lo),volume=ratio(vb,va),turnover=ratio(qb,qa),
        down_strength=ratio(db,da),down_turnover=ratio(tb,ta),wick_count=len(wick),wick_share=100*len(wick)/n,
        close_loc=100*(recent[-1][4]-lo)/(hi-lo) if hi>lo else None,low_change=pct(lo,min(b[3] for b in prev)),
        concentration=100*sum(sorted((b[6] for b in recent),reverse=True)[:3])/qb if qb>0 else None,
        down_impact=ratio(impact_b,impact_a) if impact_a is not None and impact_b is not None else None)
    old=btc.get(t-(n+1)*STEP);last=btc.get(t-STEP)
    if old and last:
        metrics["btc_change"]=pct(last[4],old[4]);metrics["rs"]=pct(recent[-1][4]/p,last[4]/old[4])
    out.update({"adv_"+k+s:v for k,v in metrics.items()})
    return out
def evaluate(m,rows,start,stop,ctx,btc):
    by={r[0]:r for r in rows};valid=ctx["r131valid"];iso=ctx["iso"]
    good={t for t,r in by.items() if valid(r)}
    windows=[];cov=dict(market=m,received_bars=len(by),expected_windows=(stop-start)//STEP,valid_windows=0,missing_history=0,missing_future=0)
    for t in range(start,stop,STEP):
        if any(k not in good for k in range(t-169*STEP,t,STEP)):
            cov["missing_history"]+=1;continue
        if any(k not in good for k in range(t,t+42*STEP,STEP)):
            cov["missing_future"]+=1;continue
        p=by[t][1];future=[by[k] for k in range(t,t+42*STEP,STEP)]
        peak=100*(max(r[2] for r in future)/p-1)
        windows.append((t,peak))
    cov["valid_windows"]=len(windows)
    events=[];until=0
    for t,peak in windows:
        if peak>=30-1e-9 and t>=until:
            events.append((t,peak));until=t+7*DAY
    # Controls have a full non-rising target window and do not overlap selected event outcome windows.
    controls=[];until=0
    for t,peak in windows:
        if peak>=30-1e-9 or t<until or any(abs(t-e)<7*DAY for e,_ in events):continue
        controls.append((t,peak));until=t+7*DAY
    out=[]
    for kind,items in (("PUMP",events),("CONTROL",controls)):
        for t,peak in items:
            hist=[by[k] for k in range(t-169*STEP,t,STEP)];future=[by[k] for k in range(t,t+42*STEP,STEP)]
            p=future[0][1];r=dict(record_type="detail",market=m,kind=kind,reference_time=iso(t),reference_ts=t,reference_price=p,peak_7d_pct=peak,
            low_7d_pct=100*(min(b[3] for b in future)/p-1),extreme_peak_flag=peak>300)
            for target in TARGETS:
                hit=next((i for i,b in enumerate(future,1) if 100*(b[2]/p-1)>=target-1e-9),None)
                r["hit"+str(target)+"_hours_upper"]=hit*4 if hit else None
                r["hit"+str(target)+"_low_pct"]=100*(min(b[3] for b in future[:hit])/p-1) if hit else None
            for d in (1,3,7):
                z,_=ctx["whale32candles"](hist,t,d);n=d*6;recent=hist[-n:];base=hist[-4*n:-n]
                suffix="_"+str(d)+"d";v=statistics.fmean(b[6] for b in base)*6
                daily=[sum(b[6] for b in recent[i:i+6]) for i in range(0,n,6)]
                r["turnover_ratio"+suffix]=statistics.fmean(daily)/v if v>0 else None
                r["elevated_day_share"+suffix]=100*sum(q>=1.2*v for q in daily)/d if v>0 else None
                for k in ("period_change_pct","period_range_pct","volume_ratio","low_change_pct","obv_ratio_pct","volume_flat","higher_low","obv_up","candle_score"):
                    r[k+suffix]=z[k] if z else None
                r["old_prefilter"+suffix]=bool(z and z["volume_flat"] and z["candle_score"]>=(2 if d==7 else 3)) if d in (3,7) else None
            z=live.candidate(hist,t,ctx)
            r["old_pre"]=False;r["old_c3"]=False
            if z:
                move=100*(p/z["reference_price"]-1);window=100*(p/z["window_start_price"]-1)
                r["old_pre"]=abs(move)<=3 and abs(window)<=z["change_limit_pct"]
                r["old_c3"]=r["old_pre"] and all(z.get(k) is True for k in live.KEYS[:3])
                r["old_reason"]="포착" if r["old_pre"] else "가격 이탈 또는 추격"
            elif not r["volume_flat_3d"] and not r["volume_flat_7d"]:r["old_reason"]="3/7일 횡보·거래량 필수조건 미충족"
            else:r["old_reason"]="3/7일 매집 점수 부족 또는 자료 부족"
            r["boundary_first"]=t==windows[0][0]
            for d in PERIODS:r.update(advanced(by,good,btc,t,d))
            r["discovery_market"]=m in DISCOVERY
            lo=r.get("low_change_pct_3d");span=r.get("period_range_pct_3d");span7=r.get("period_range_pct_7d");flow=r.get("turnover_ratio_1d")
            hl=lo>=.5 if lo is not None else None
            r.update(pattern_HL=hl,pattern_A=(hl and span<12) if hl is not None and span is not None else None,
                     pattern_B=(hl and span>=12) if hl is not None and span is not None else None,
                     pattern_R12=span>=12 if span is not None else None,pattern_R15=span>=15 if span is not None else None,
                     pattern_R7=span7>=25 if span7 is not None else None,pattern_FLOW=(hl and flow>=1) if hl is not None and flow is not None else None)
            def flag(keys,pred):
                vals=[r.get(k) for k in keys]
                return pred(*vals) if all(x is not None for x in vals) else None
            r.update(
                pattern_R37=flag(["period_range_pct_3d","period_range_pct_7d"],lambda a,b:a>=12 and b>=25),
                pattern_DOWNR=flag(["period_range_pct_3d","period_change_pct_3d"],lambda a,b:a>=12 and b<0),
                pattern_DOWN1=flag(["period_change_pct_1d"],lambda a:a<0),
                pattern_QUIET1=flag(["period_range_pct_1d","volume_ratio_1d"],lambda a,b:a>=12 and b<1),
                pattern_C=flag(["adv_change_3d","adv_down_strength_3d","adv_down_turnover_3d"],lambda a,b,c:a<0 and b<1 and c<1),
                pattern_D=flag(["adv_wick_count_3d","adv_low_change_3d"],lambda a,b:a>=3 and b>=-.5),
                pattern_RS=flag(["adv_btc_change_3d","adv_rs_3d"],lambda a,b:a<0 and b>0),
                pattern_CONC=flag(["adv_concentration_3d"],lambda a:a>=40),
                pattern_EFF=flag(["adv_down_impact_3d"],lambda a:a<1),
                pattern_LONG=flag(["adv_change_20d","adv_low_change_3d"],lambda a,b:a<0 and b>=.5))
            out.append(r)
    cov.update(events=len(events),controls=len(controls),status="ok" if windows else "missing")
    return out,cov
def summarize(rows):
    result=[]
    cohorts={"CONTROL":[r for r in rows if r["kind"]=="CONTROL"]}
    for k in TARGETS:cohorts["PUMP"+str(k)]=[r for r in rows if r["kind"]=="PUMP" and r["peak_7d_pct"]>=k-1e-9]
    for name,rs in cohorts.items():
        n=len(rs)
        out=dict(record_type="summary",cohort=name,count=n,markets=len({r["market"] for r in rs}),old_pre_count=sum(r["old_pre"] for r in rs),old_c3_count=sum(r["old_c3"] for r in rs),old_pre_pct=100*sum(r["old_pre"] for r in rs)/n if n else None,extreme_peak_count=sum(r["extreme_peak_flag"] for r in rs))
        for d in (1,3,7):
            s="_"+str(d)+"d"
            for k in ("period_change_pct","period_range_pct","volume_ratio","turnover_ratio","elevated_day_share","low_change_pct","obv_ratio_pct"):
                vals=[r[k+s] for r in rs if r.get(k+s) is not None]
                out["median_"+k+s]=statistics.median(vals) if vals else None
            for k in ("volume_flat","higher_low","obv_up"):
                vals=[r[k+s] for r in rs if r.get(k+s) is not None]
                out[k+s+"_pct"]=100*sum(vals)/len(vals) if vals else None
        result.append(out)
    return result
def advanced_summary(rows):
    out=[]
    for name,group in (("CONTROL",[r for r in rows if r["kind"]=="CONTROL"]),("PUMP30",[r for r in rows if r["kind"]=="PUMP"]),("PUMP50",[r for r in rows if r["kind"]=="PUMP" and r["peak_7d_pct"]>=50-1e-9]),("PUMP100",[r for r in rows if r["kind"]=="PUMP" and r["peak_7d_pct"]>=100-1e-9])):
        for d in PERIODS:
            row=dict(record_type="advanced",cohort=name,period_days=d,count=len(group))
            for k in ADV_KEYS:
                vals=[r.get("adv_"+k+"_"+str(d)+"d") for r in group]
                vals=[v for v in vals if v is not None]
                row["median_"+k]=statistics.median(vals) if vals else None;row["n_"+k]=len(vals)
            out.append(row)
    return out
def patterns(rows):
    out=[]
    for scope,rs in (("all",rows),("exclude_first_boundary",[r for r in rows if not r["boundary_first"]]),("no_chase",[r for r in rows if not r["boundary_first"] and r["period_change_pct_3d"]<=15 and not r["extreme_peak_flag"]])):
        for key,label in PATTERNS.items():
            row=dict(record_type="pattern",pattern=key,pattern_label=label,scope=scope)
            for name,group in (("control",[r for r in rs if r["kind"]=="CONTROL"]),("pump30",[r for r in rs if r["kind"]=="PUMP"]),("pump50",[r for r in rs if r["kind"]=="PUMP" and r["peak_7d_pct"]>=50-1e-9]),("pump100",[r for r in rs if r["kind"]=="PUMP" and r["peak_7d_pct"]>=100-1e-9])):
                valid=[r for r in group if r["pattern_"+key] is not None];n=len(valid);hits=sum(r["pattern_"+key] is True for r in valid)
                row.update({name+"_n":n,name+"_hits":hits,name+"_pct":100*hits/n if n else None})
            out.append(row)
    return out
def run(ctx,limit):
    begin=time.monotonic()
    try:
        candles=ctx["candles"];iso=ctx["iso"];end=int(ctx["now"]().timestamp())
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(end-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 확인 실패")
        stop=cut-7*DAY+STEP;start=stop-30*DAY
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not universe:raise RuntimeError("시장 목록 없음")
        total=len(universe)
        if limit==80:
            universe=[m for m in universe if m not in DISCOVERY]
            if len(universe)>80:universe=[universe[i*len(universe)//80] for i in range(80)]
        elif limit==81:
            universe=[m for m in universe if m in DISCOVERY]
        elif limit==160:
            universe=[m for m in universe if m not in DISCOVERY]
            if len(universe)>160:universe=[universe[i*len(universe)//160] for i in range(160)]
        pages=math.ceil((180+241+41)/200)
        btc={}
        try:btc={r[0]:r for r in candles("KRW-BTC",240,cut,200,pages=pages,latest=False) if ctx["r131valid"](r)}
        except Exception:pass
        patch(phase="급등 및 대조 구간 추출",total=len(universe),universe=len(universe),total_universe=total,limit=limit,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut))
        rows=[];cov=[];errors=[]
        def fetch(m):return evaluate(m,candles(m,240,cut,200,pages=pages,latest=False),start,stop,ctx,btc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    a,c=f.result();rows.extend(a);cov.append(c)
                except Exception as e:errors.append(dict(market=m,error=str(e)));cov.append(dict(market=m,status="error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        rows.sort(key=lambda r:(r["kind"]!="PUMP",-r["peak_7d_pct"],r["market"],r["reference_ts"]))
        summary=summarize(rows)
        for r in summary:r.update(signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut),universe=len(universe),total_universe=total,valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),sample_limit=limit,sample_method={80:"new_80_excludes_discovery",81:"original_80",160:"new_160_excludes_discovery",0:"all"}[limit])
        pattern_rows=patterns(rows);advanced_rows=advanced_summary(rows)
        for p in pattern_rows:p.update(sample_limit=limit,signal_start=iso(start),signal_end_exclusive=iso(stop))
        allrows=summary+pattern_rows+advanced_rows+rows+[dict(r,record_type="coverage") for r in cov]+[dict(r,record_type="error") for r in errors]
        fields=list(dict.fromkeys(k for r in allrows for k in r))
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(allrows)
        name="WHALE_DETECTOR_V1_4_REVERSE_30D_"+({80:"NEW80",81:"ORIGINAL80",160:"NEW160",0:"ALL"}[limit])+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv"
        patch(status="done",phase="완료",summary=summary,patterns=pattern_rows,advanced=advanced_rows,details=rows,coverage=cov,errors=errors,csv_bytes=("\ufeff"+b.getvalue()).encode("utf-8"),csv_name=name,valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx,limit):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,limit),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.4</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{font:14px/1.55 system-ui;max-width:820px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23}section{background:white;border:1px solid #d7e3dd;border-radius:12px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{display:inline-block;padding:10px;margin:3px;border-radius:8px;border:1px solid #a5bbb0;background:white;color:#176345;text-decoration:none;font:inherit}button{background:#176345;color:white;width:97%}button:disabled{opacity:.5}progress{width:100%}small{display:block;color:#53645d}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.4</h1><small>실제 급등 종목 역추적 · 매집 공통점 확인</small><a href="/whaledetector">기존 실시간</a><a href="/whalebt11">기존 백테스트</a><section><label>30일 검증 <select id="limit"><option value="80">새로운 80종목 (기본)</option><option value="160">새로운 160종목</option><option value="81">기존 80종목</option><option value="0">전체 KRW</option></select></label><small>7일 결과가 성숙한 최근 30일의 4시간 시점. 다음 봉 시가 대비 7일 내 +30% 이상을 기록한 최초 기준 시점부터 같은 종목 7일 중복 제거. +50/+100%는 그 급등 사례의 부분집합. 실제 상승 개시 시점으로 단정하지 않음. 비급등 대조군은 +30% 미만이며 선정 급등 구간과 후속 7일이 겹치지 않도록 선택. 1/3/7/14/20일 완료 봉 특징 비교. 14/20일 동기간 비교에 필요한 봉이 부족하면 해당 지표만 미확인. 아랫꼬리·하락봉 거래대금은 매도 압력의 캔들 대용 지표이며 실제 체결 방향이나 흡수 증거가 아님. 과거 체결·호가 미확인. 현재 상장 종목 기준, 새 표본은 V1.2 기존 80개 종목을 제외한 균등 표본. 동일 시장 기간이므로 시간 외부검증은 아님.</small><button id="go">급등 역추적 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>급등군 vs 비급등군</h2><label>선행 기간 <select id="days"><option>1</option><option selected>3</option><option>7</option></select>일</label><div class="scroll"><table id="tbl"></table></div><p><small>급등군과 대조군의 중앙값 비교. 사후 선택된 표본의 특징이며 예측 성능이나 고래 매집 입증이 아님. +50/+100%군은 중복 포함. 기존 포착률은 선정 기준 시점에서의 캔들 예비 검색식 통과 비율.</small></p><h2>추가 공통점 재확인</h2><label>시작 경계 <select id="scope"><option value="all">전체</option><option value="exclude_first_boundary">종목별 첫 유효 시점 제외</option><option value="no_chase">첫 시점·3일 +15% 초과·+300% 초과 제외</option></select></label><div class="scroll"><table id="patterns"></table></div><small>V1.2에서 정한 조건을 그대로 확인. 각 비율은 해당 사례군의 충족 건수/유효 건수. 매집 증거나 실전 승률이 아님.</small><h2>추가 지표 중앙값 비교</h2><label>기간 <select id="advdays"><option>1</option><option selected>3</option><option>7</option><option>14</option><option>20</option></select>일</label><div class="scroll"><table id="adv"></table></div><small>각 값 뒤 괄호는 유효 사례 수. 하락 강도·하락봉 거래대금·가격 영향은 기간 후반/전반 비율 (1 미만 감소). 거래량·거래대금 배수는 직전 동일 기간 대비. BTC 상대강도는 종가 수익률의 상대 비율. 과거 체결·호가 미검증. 탐색 가설이며 별도 기간 검증 필요.</small><h2>급등 사례 상위 20개</h2><div class="scroll"><table id="events"></table></div><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],events=[],ps=[],adv=[],loaded=null,busy=false,starting=false;const cacheKey='whale_reverse14_summary';
const f=x=>typeof x==='number'?x.toFixed(2):'—';
function table(t,heads,rows){t.replaceChildren();let h=t.createTHead().insertRow();for(let x of heads){let th=document.createElement('th');th.textContent=x;h.append(th)}let b=t.createTBody();for(let row of rows){let tr=b.insertRow();for(let v of row)tr.insertCell().textContent=v}}
const advLabels={"change":"가격 변화 %","span":"변동폭 %","volume":"거래량 배수","turnover":"거래대금 배수","down_strength":"하락 강도 후기/전기","down_turnover":"하락봉 거래대금 후기/전기","wick_count":"아랫꼬리 방어 봉 수","wick_share":"아랫꼬리 방어 봉 비율 %","close_loc":"종가 범위 위치 %","low_change":"저점 변화 %","concentration":"상위 3봉 거래대금 %","down_impact":"하락 가격영향 후기/전기","btc_change":"BTC 변화 %","rs":"BTC 상대강도 %"};
function render(){let a=adv.filter(x=>x.period_days===Number($('advdays').value));table($('adv'),['추가 지표','비급등','+30%','+50%','+100%'],Object.keys(advLabels).map(k=>[advLabels[k],...['CONTROL','PUMP30','PUMP50','PUMP100'].map(g=>{let x=a.find(r=>r.cohort===g);return x?f(x['median_'+k])+' ('+x['n_'+k]+')':'—'})]));table($('patterns'),['조건','비급등','+30%','+50%','+100%'],ps.filter(r=>r.scope===$('scope').value).map(r=>[r.pattern_label,...['control','pump30','pump50','pump100'].map(k=>r[k+'_hits']+'/'+r[k+'_n']+' ('+f(r[k+'_pct'])+'%)')]));let s='_'+$('days').value+'d';table($('tbl'),['구분','사례','기존 포착','가격변화%','범위%','거래량배','거래대금배','유입일%','저점변화%','OBV%'],data.map(r=>[r.cohort==='CONTROL'?'비급등':r.cohort.replace('PUMP','+'),r.count,f(r.old_pre_pct)+'%',...['period_change_pct','period_range_pct','volume_ratio','turnover_ratio','elevated_day_share','low_change_pct','obv_ratio_pct'].map(k=>f(r['median_'+k+s]))]));table($('events'),['종목','기준 시각','7일 최고','기존 포착','미포착 이유'],events.map(r=>[r.market,r.reference_time,f(r.peak_7d_pct)+'%',r.old_pre?'포착':'미포착',r.old_reason]))}
function download(blob,name){let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
function links(){let a=document.createElement('a');a.href='#';a.textContent='전체 결과 CSV';a.onclick=async e=>{e.preventDefault();try{let r=await fetch('/whalereverse14/csv?scan_id='+encodeURIComponent(loaded));if(!r.ok)throw Error('HTTP '+r.status);let n=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);download(await r.blob(),n?n[1]:'WHALE_DETECTOR_V1_4_FULL.csv')}catch(e){$('err').textContent='전체 CSV 실패: '+e.message+' · 요약 CSV 저장 가능'}};let b=document.createElement('a');b.href='#';b.textContent='요약 CSV (비상 저장)';b.onclick=e=>{e.preventDefault();let saved=[...data,...ps,...adv],k=[...new Set(saved.flatMap(Object.keys))],esc=x=>'"'+String(x??'').replaceAll('"','""')+'"';download(new Blob(['\ufeff'+[k.map(esc).join(','),...saved.map(r=>k.map(x=>esc(r[x])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}),'WHALE_DETECTOR_V1_4_SUMMARY.csv')};$('links').replaceChildren(a,b)}
$('days').onchange=render;$('scope').onchange=render;$('advdays').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let r=await fetch('/whalereverse14/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();$('go').disabled=$('limit').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 '+s.valid_markets+'종목 · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalereverse14/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;events=x.events;ps=x.patterns||[];adv=x.advanced||[];loaded=s.scan_id;try{localStorage.setItem(cacheKey,JSON.stringify({data,events,ps,adv,loaded}))}catch(e){}render();links();$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalereverse14/start?limit='+$('limit').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};try{let x=JSON.parse(localStorage.getItem(cacheKey)||'null');if(x&&Array.isArray(x.data)&&x.data.length){data=x.data;events=x.events||[];ps=x.ps||[];adv=x.adv||[];loaded=x.loaded;render();links();$('out').hidden=false}}catch(e){}poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not(p=="/whalereverse14" or p.startswith("/whalereverse14/")):return False
    q=parse_qs(u.query)
    if post and p=="/whalereverse14/start":
        try:
            limit=int(q.get("limit",["80"])[0])
            if limit not in (0,80,81,160):raise ValueError()
        except ValueError:h.js({"error":"invalid limit"},400);return True
        h.js({"started":start(ctx,limit)})
    elif not post and p=="/whalereverse14":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalereverse14/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","patterns","advanced","details","coverage","errors","csv_bytes")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalereverse14/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"],patterns=STATE.get("patterns",[]),advanced=STATE.get("advanced",[]),events=[r for r in STATE["details"] if r["kind"]=="PUMP"][:20])
        h.js(d)
    elif not post and p=="/whalereverse14/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id");payload=STATE.get("csv_bytes") if ok else None;name=STATE.get("csv_name")
        if payload is None:h.js({"error":"scan expired; use summary CSV"},409);return True
        h.send(payload,"text/csv; charset=utf-8",filename=name)
    else:h.js({"error":"not found"},404)
    return True
