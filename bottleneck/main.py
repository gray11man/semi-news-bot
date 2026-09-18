"""Industry Shift Radar. Default = preview; --send enables configured Telegram delivery."""
import argparse
import concurrent.futures
from collections import Counter
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from core import Store, UTC, alert_key, chunks, digest, evaluate, now, render, validate_evidence
from network import ApiError, BudgetError, RequestTooLarge, Gemini, collect, fetch_body, rss_url, telegram
from prompts import ASSESS, ASSESS_SCHEMA, DISCOVERY, DISCOVERY_SCHEMA, AUDIT, AUDIT_SCHEMA
from sources import DEFAULT_PRIMARY_DOMAINS, DRIVERS, SECTORS
from efficiency import TokenBudget, BudgetExceeded, compact_docs, audit_docs, relevant_catalog, compact_evidence

ROOT=Path(__file__).resolve().parent

def config():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    c=dict(key=os.getenv('GEMINI_KEY') or os.getenv('GEMINI_API_KEY',''),
           model=os.getenv('RADAR_MODEL',os.getenv('PICK_MODEL','gemini-3.8-flash')),
           token=os.getenv('BOTTLENECK_TOKEN',os.getenv('TELEGRAM_BOT_TOKEN','')),
           chat=os.getenv('BOTTLENECK_CHAT_ID',os.getenv('TELEGRAM_CHAT_ID','')),
           data=Path(os.getenv('RADAR_DATA_DIR',str(ROOT/'data'))),
           calls=int(os.getenv('MAX_GEMINI_CALLS','10')),
           screen_batches=int(os.getenv('SCREEN_BATCHES','3')),
           batch_size=int(os.getenv('SCREEN_BATCH_SIZE','80')),
           research=int(os.getenv('RESEARCH_THEMES_PER_RUN','3')),
           docs=int(os.getenv('DOCS_PER_THEME','16')),
           doc_chars=int(os.getenv('DOC_CHARS','3000')),
           run_tokens=int(os.getenv('MAX_RUN_TOKENS','160000')),
           day_tokens=int(os.getenv('MAX_DAY_TOKENS','500000')),
           input_tokens=int(os.getenv('MAX_INPUT_TOKENS','36000')),
           history_days=int(os.getenv('HISTORY_SEARCH_DAYS','365')),
           fresh_hours=int(os.getenv('FRESH_HOURS','72')),
           empty=os.getenv('SEND_EMPTY','1')=='1',
           primary=DEFAULT_PRIMARY_DOMAINS.copy())
    for k in ('calls','screen_batches','batch_size','research','docs','history_days','fresh_hours','doc_chars','run_tokens','day_tokens','input_tokens'):
        if c[k]<1: raise RuntimeError(f'{k} must be positive')
    if c['batch_size']>150 or c['docs']>40: raise RuntimeError('Batch size <=150, docs <=40')
    path=ROOT/'sources.json'
    c['extra_feeds']=[]
    if path.exists():
        custom=json.loads(path.read_text(encoding='utf-8'))
        c['primary']+=custom.get('primary_domains',[])
        c['extra_feeds']=custom.get('rss_feeds',[])
    return c


def discover_specs(days):
    specs=[]
    for name,ko,en in SECTORS:
        # Compound phrases remain intact; every sector gets the same discovery treatment.
        for lang,query in [('ko',ko),('en',en)]:
            query=' OR '.join('"'+x.strip()+'"' for x in query.split('|'))
            specs.append(dict(sector=name,url=rss_url(query,lang,days)))
    for label,query in DRIVERS:
        specs.append(dict(sector='산업간 연결·'+label,url=rss_url(query,'en',days)))
    specs.extend([
        dict(sector='일본·산업전환',url=rss_url('設備投資 OR 供給不足 OR 新用途 OR 長期契約','ja',days)),
        dict(sector='중화권·산업전환',url=rss_url('產能 OR 供應短缺 OR 新應用 OR 長期合約','zh',days)),
    ])
    return specs


def audit_id_problem(evidence, checks):
    """Require exactly one verdict per requested evidence ID; never guess mappings."""
    expected={e['id'] for e in evidence}
    counts=Counter(x['evidence_id'] for x in checks)
    missing=expected-set(counts)
    extra=set(counts)-expected
    duplicate={key for key,n in counts.items() if n>1}
    if missing or extra or duplicate:
        return f'재검수 ID 불일치: 누락 {len(missing)} / 미요청 {len(extra)} / 중복 {len(duplicate)}; 해당 가설 알림 보류, 다음 검토에서 재시도'
    return ''


def research(theme,store,ai,c):
    issues=[]
    queries=list(dict.fromkeys(theme['queries']))[:5]
    # Queries include a mandatory explicit negative search, even if discovery omitted one.
    queries.append(theme['chain_key'].split('|')[0]+' oversupply substitution cancellation demand decline')
    specs=[dict(sector=theme['sector'],url=rss_url(q,'en' if q.isascii() else 'ko',c['history_days'])) for q in queries]
    found,health=collect(specs,days=c['history_days'])
    if health['healthy']<len(specs): issues.append('심층 검색 일부 실패')
    seeds=[]
    for sid in theme.get('seed_ids',[]):
        row=store.db.execute('SELECT data FROM articles WHERE id=?',(sid,)).fetchone()
        if row: seeds.append(json.loads(row[0]))
    # Seeds plus diversified publishers; do not spend all body requests on one syndicated story.
    by_id={a['id']:a for a in sorted(seeds+found,key=lambda a:a['published'],reverse=True)}
    groups={}
    for a in by_id.values(): groups.setdefault(a.get('publisher') or a['id'],[]).append(a)
    chosen=[]
    while groups and len(chosen)<c['docs']*2:
        for p in list(groups):
            chosen.append(groups[p].pop(0))
            if not groups[p]: del groups[p]
            if len(chosen)>=c['docs']*2: break
    docs=[]; attempts=0; failures=0
    offset=0
    while offset<len(chosen) and len(docs)<c['docs']:
        uncached=[]
        count=min(4,c['docs']-len(docs))
        group=chosen[offset:offset+count];offset+=len(group)
        for article in group:
            cached=store.doc(article['id'])
            if cached and (dt.datetime.now(UTC)-dt.datetime.fromisoformat(cached['fetched'])).days<7:
                docs.append(cached)
            else:uncached.append(article)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(fetch_body,a,c['primary']):a for a in uncached}
            for future in concurrent.futures.as_completed(futures):
                attempts+=1
                try:
                    doc=future.result();store.save_doc(doc);docs.append(doc)
                except Exception:
                    failures+=1
        # Other publishers in the reserve pool replace failed bodies.
    issues.append(f'원문 조회 {attempts}건 / 실패 {failures}건 / 확보 {len(docs)}건')
    if not docs:
        # Rotate failed themes so one paywall cannot starve the entire queue.
        store.db.execute('UPDATE themes SET reviewed=? WHERE id=?',(now(),theme['id'])); store.db.commit()
        return None,issues+['검증 가능한 원문 없음']
    fingerprint=digest(json.dumps(sorted((d['id'],digest(d['text'])) for d in docs)))
    previous=store.meta('research_input:'+theme['id'])
    last=store.meta('research_time:'+theme['id'])
    if previous==fingerprint and last and (dt.datetime.now(UTC)-dt.datetime.fromisoformat(last)).days<7:
        store.db.execute('UPDATE themes SET reviewed=? WHERE id=?',(now(),theme['id']));store.db.commit()
        return None,issues
    prior=compact_evidence(store.evidence(theme['id']))
    input_docs=compact_docs(docs,theme,c.get('doc_chars',3000))
    while True:
        try:
            response=ai.json(ASSESS,dict(today_utc=now(),theme={k:v for k,v in theme.items() if k!='seed_ids'},docs=input_docs,past_evidence=prior),ASSESS_SCHEMA)
            break
        except RequestTooLarge:
            limit=max(len(d['text']) for d in input_docs)
            if limit<=900:raise
            input_docs=compact_docs(docs,theme,max(800,limit//2))
            issues.append('입력 예산에 맞춰 원문 발췌 범위 축소')
    valid=validate_evidence(response['evidence'],docs)
    if len(valid)<len(response['evidence']): issues.append('일부 인용·날짜·출처 검증 탈락')
    if response['evidence'] and not valid:
        # Never overwrite an earlier assessment with wholly invalid model evidence.
        raise ApiError('All model evidence failed source validation')
    merged={e['id']:e for e in prior}; merged.update({e['id']:e for e in valid})
    evidence=list(merged.values())
    audit_sources={d['id']:d for d in docs}
    for e in evidence:
        if e.get('doc_id') not in audit_sources:
            cached=store.doc(e.get('doc_id',''))
            if cached:audit_sources[cached['id']]=cached
    audit=ai.json(AUDIT,dict(today_utc=now(),assessment={k:v for k,v in response.items() if k!='evidence'},evidence=evidence,docs=audit_docs(list(audit_sources.values()),evidence)),AUDIT_SCHEMA)
    problem=audit_id_problem(evidence,audit['checks'])
    if problem:
        # Preserve prior verified evidence and input fingerprint so this remains retryable.
        store.db.execute('UPDATE themes SET reviewed=? WHERE id=?',(now(),theme['id']))
        store.db.commit()
        print('[warning] '+problem,flush=True)
        return None,issues+[problem]
    checks={x['evidence_id']:x for x in audit['checks']}
    for e in evidence:
        check=checks[e['id']]
        e['origin_key']=check['origin_key'].strip().lower()
        e['primary']=e['primary'] and check['official_source']
        if not check['supported'] or e['origin_key'] in ('','unknown','unclear','미상'):
            e['stance']='context'
    gate=evaluate(response,evidence,c['fresh_hours'])
    if not audit['narrative_supported']:
        gate['state']='관찰'
        issues.append('독립 재심사에서 서술 근거 부족: 알림 보류')
    response['audit']=audit
    assessment=dict(response,gate=gate,retrieval=dict(health=health,bodies=len(docs),issues=issues))
    store.record(theme['id'],assessment,evidence)
    store.set_meta('research_input:'+theme['id'],fingerprint)
    store.set_meta('research_time:'+theme['id'],now())
    return (assessment,evidence),issues


def review_queue(themes,limit,new_first=False):
    # Alternate established and new themes; neither queue can starve the other.
    old=[t for t in themes if t['reviewed']]
    new=[t for t in themes if not t['reviewed']]
    out=[]
    while (old or new) and len(out)<limit:
        for group in ((new,old) if new_first else (old,new)):
            if group and len(out)<limit: out.append(group.pop(0))
    return out


def write_report(store,path,health,issues,ai):
    lines=['# 산업변화 레이더 실행 보고서',f'실행 UTC: {now()}',
           f'수집: {health.get("healthy",0)}/{health.get("total",0)} 피드 정상',
           f'Gemini 호출: {ai.calls}/{ai.max_calls}, 응답 토큰 통계 합계: {ai.tokens}',
           '이 보고서의 관찰 가설은 검증된 산업전환 또는 투자수익을 뜻하지 않습니다.']
    backlog=store.pending_count()
    if getattr(ai,'budget',None):
        lines.append(f'토큰 예산 사용/예약: 실행 {ai.budget.used}/{ai.budget.run_limit}, 한국 날짜 하루 {ai.budget.total()}/{ai.budget.day_limit}')
        lines.append(f'입력 사전 계수 API: {getattr(ai,"count_calls",0)}회 / 미리보기도 하루 예산에 포함')
    lines.append(f'아직 심사하지 못한 기사: {backlog}건 (삭제하지 않고 다음 실행에서 처리)')
    if issues: lines+=['','## 수집·검증 상태']+['- '+x for x in sorted(set(issues))]
    lines+=['','## 산업별 실제 검토 범위 (누적)',
            '| 산업 | 수집 기사 | 심사 완료 | 활성 대기 | 별도 보관 |', '|---|---:|---:|---:|---:|']
    coverage={r['sector']:r for r in store.db.execute('SELECT sector,COUNT(*) AS n,SUM(CASE WHEN screened=1 THEN 1 ELSE 0 END) AS done FROM articles GROUP BY sector')}
    for sector in list(dict.fromkeys([s[0] for s in SECTORS]+list(coverage))):
        r=coverage.get(sector)
        n=r['n'] if r else 0;done=r['done'] if r else 0
        archived=store.db.execute('SELECT COUNT(*) FROM articles WHERE sector=? AND screened=0 AND id IN (SELECT id FROM article_archive)',(sector,)).fetchone()[0]
        lines.append(f'| {sector} | {n} | {done} | {n-done-archived} | {archived} |')
    for theme in store.themes():
        row=store.db.execute('SELECT data FROM snapshots WHERE theme=? ORDER BY id DESC LIMIT 1',(theme['id'],)).fetchone()
        lines+=['',f'## {theme["title"]}']
        if not row:
            lines += ['상태: 검증 대기',theme['hypothesis']]; continue
        assessment=json.loads(row[0]); evidence=store.evidence(theme['id'])
        lines.append(render(theme,assessment,assessment['gate'],evidence))
        lines.append('### 원문과 검증 기록')
        for e in evidence:
            lines.append(f'- [{e["publisher"]}]({e["url"]}) | {e["stance"]} | {e["axis"]} | '
                         f'원출처: {e["origin_key"]} | 사건일: {e["event_date"]}\n'
                         f'  근거 인용: {e["quote"]}\n  요약: {e["fact"]}')
    lines+=['','## 전송 상태']
    for row in store.db.execute("SELECT status,COUNT(*) AS n FROM outbox GROUP BY status"):
        lines.append(f'- {row["status"]}: {row["n"]}')
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp'); temp.write_text('\n\n'.join(lines),encoding='utf-8'); temp.replace(path)


def deliver(store,c):
    store.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'"); store.db.commit()
    for row in store.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY created,id").fetchall():
        store.db.execute("UPDATE outbox SET status='sending' WHERE id=?",(row['id'],)); store.db.commit()
        status,mid=telegram(row['text'],c['token'],c['chat'])
        store.db.execute('UPDATE outbox SET status=?,message_id=? WHERE id=?',(status,mid,row['id'])); store.db.commit()
        if status!='sent':
            raise RuntimeError('Telegram delivery failed/uncertain; see --outbox. No automatic uncertain retry.')
        time.sleep(1.1)


def check_store_compatibility():
    import inspect
    required=('triage_articles','pending_count','pending_articles')
    missing=[name for name in required if not callable(getattr(Store,name,None))]
    if not missing and 'ranked' not in inspect.signature(Store.pending_articles).parameters:
        missing.append('pending_articles(ranked)')
    if missing:
        raise RuntimeError('파일 버전 불일치: bottleneck/core.py도 함께 교체해야 합니다. 누락: '+', '.join(missing))


def error_location(exc):
    # Frame metadata only: no source lines, local values, URLs or credentials.
    frames=[];tb=exc.__traceback__
    while tb is not None:
        frames.append(f'{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno} ({tb.tb_frame.f_code.co_name})')
        tb=tb.tb_next
    detail=''
    if isinstance(exc,AttributeError):
        name=getattr(exc,'name',None)
        if isinstance(name,str) and name.isidentifier():detail=' missing_attribute='+name
    return type(exc).__name__+detail+' | '+' -> '.join(frames[-6:])


def run(c,send=False,bootstrap=False):
    check_store_compatibility()
    if not c['key']: raise RuntimeError('GEMINI_KEY is required')
    if send and (not c['token'] or not c['chat']): raise RuntimeError('Telegram token/chat ID are required')
    c['data'].mkdir(parents=True,exist_ok=True)
    store=Store(c['data']/'radar.sqlite3')
    if not send:
        # Copy persisted memory into a disposable DB: preview cannot consume the live queue.
        preview=Store(':memory:'); store.db.backup(preview.db); store.db.close(); store=preview
    ai=Gemini(c['key'],c['model'],c['calls']); issues=[]; alerts=0
    ai.budget=TokenBudget(c['data']/'usage.sqlite3',c.get('run_tokens',160000),c.get('day_tokens',500000))
    ai.input_limit=c.get('input_tokens',36000)
    days=30 if bootstrap else max(1,(c['fresh_hours']+23)//24)
    specs=discover_specs(days)+c['extra_feeds']
    articles,health=collect(specs,days)
    store.add_articles(articles)
    store.triage_articles(days=30 if bootstrap else 7)
    reviewed_ids=set(); reviewed_sectors=set(); researched=0
    if health['healthy']==0: raise RuntimeError('All feeds failed; this is not a no-signal result')
    if health['healthy']<health['total']: issues.append(f'피드 일부 실패: {health["total"]-health["healthy"]}개')
    try:
        for _ in range(c['screen_batches']):
            batch_cursor=int(store.meta('screen_cursor','0'))
            batch=store.pending_articles(c['batch_size'],newest=batch_cursor%2==0,ranked=batch_cursor%2==1)
            if not batch: break
            if ai.calls>=ai.max_calls-2:
                issues.append('호출 예산으로 기사 심사 일부 이월'); break
            known=store.themes()
            catalog=relevant_catalog(known,batch)
            compact_batch=[{k:(v[:220] if k=='summary' else v) for k,v in a.items() if k in ('id','title','summary','sector','published')} for a in batch]
            while True:
                try:
                    response=ai.json(DISCOVERY,dict(now_utc=now(),articles=compact_batch,known_themes=catalog),DISCOVERY_SCHEMA)
                    break
                except RequestTooLarge:
                    if len(batch)<=1:raise
                    batch=batch[:max(1,len(batch)//2)];compact_batch=compact_batch[:len(batch)]
                    issues.append('기사 입력량을 줄여 분할 심사; 나머지 이월')
            input_ids={a['id'] for a in batch}
            for candidate in response['candidates']:
                if not candidate['seed_ids'] or not set(candidate['seed_ids'])<=input_ids:
                    raise ApiError('Discovery returned invalid article IDs')
                if not candidate['queries'] or not candidate['chain_key'].strip():
                    raise ApiError('Discovery returned empty search plan')
                store.upsert_theme(candidate)
            reviewed_ids.update(a['id'] for a in batch)
            reviewed_sectors.update(a['sector'] for a in batch)
            store.screened([a['id'] for a in batch])
            store.set_meta('screen_cursor',batch_cursor+1)
        # Oldest reviewed first: quiet/negative themes cannot be displaced forever by popular sectors.
        for theme in review_queue(store.themes(),c['research'],new_first=int(store.meta('review_cursor','0'))%2==0):
            if ai.calls+2>ai.max_calls:
                issues.append('호출 예산으로 심층 검증 이월'); break
            store.set_meta('review_cursor',int(store.meta('review_cursor','0'))+1)
            print(f'[research] {theme["title"]}',flush=True)
            result,problems=research(theme,store,ai,c); issues+=problems
            researched+=1
            if not result: continue
            assessment,evidence=result; gate=assessment['gate']
            if gate['state']=='관찰': continue
            key=alert_key(theme['id'],gate,evidence)
            if store.meta('last_alert:'+theme['id'])==key: continue
            # Replays of an older event set are also covered by immutable outbox IDs.
            content=render(theme,assessment,gate,evidence)
            urls=list(dict.fromkeys(e['url'] for e in evidence if e['stance']!='context'))
            content+='\n원문\n'+'\n'.join(urls)
            for i,part in enumerate(chunks(content)):
                store.enqueue(f'{key}:{i:03d}',part)
            store.set_meta('last_alert:'+theme['id'],key)
            alerts+=1
    except (BudgetError,BudgetExceeded) as exc:
        issues.append('예산 도달: '+str(exc))
    except (ApiError,ValueError) as exc:
        issues.append('분석 중단: '+str(exc))
        write_report(store,c['data']/('latest.md' if send else 'preview.md'),health,issues,ai)
        # The final exception is intentionally generic for shell callers, but
        # this short, already-sanitized issue makes the Actions log actionable
        # without requiring the user to download the report artifact first.
        print('[diagnostic] '+issues[-1],file=sys.stderr,flush=True)
        store.db.close()
        raise RuntimeError('Analysis incomplete; backlog and evidence retained. See report.') from None
    backlog=store.pending_count()
    unreviewed=sum(not t['reviewed'] for t in store.themes())
    if backlog or unreviewed: issues.append(f'처리 대기: 기사 {backlog}건 / 미검증 가설 {unreviewed}개')
    stats=f'이번 실행: 기사 {len(reviewed_ids)}건 / {len(reviewed_sectors)}개 분야 심사 · 가설 {researched}개 조사'
    archived=store.db.execute('SELECT COUNT(*) FROM article_archive').fetchone()[0]
    issues.append(stats)
    issues.append(f'미심사 별도 보관 {archived}건 (7일 경과 또는 중복; 심사 완료 아님)')
    if not alerts and c['empty']:
        status='오늘 검토한 범위에서 알림 기준을 통과한 새 신호가 없습니다. 전체 산업에 변화가 없다는 뜻은 아닙니다.'
        if issues: status+='\n검증 범위 제한: '+' / '.join(sorted(set(issues)))
        store.enqueue(digest('status:'+now()),status)
    output=c['data']/('latest.md' if send else 'preview.md')
    write_report(store,output,health,issues,ai)
    if send:
        try: deliver(store,c)
        finally: write_report(store,output,health,issues,ai)
    else:
        print('[preview] Telegram 미전송 / 실제 기억·전송이력 미변경')
    print(f'[done] 새 알림 {alerts}개, 가설 {len(store.themes())}개, Gemini {ai.calls}회; {output}')
    store.db.close()
    ai.budget.db.close()


def main():
    parser=argparse.ArgumentParser(description='전 산업 구조변화 조기 탐지')
    parser.add_argument('--send',action='store_true',help='분석 저장 및 Telegram 전송')
    parser.add_argument('--bootstrap',action='store_true',help='최초 30일 단서 수집 (신규 이벤트 알림 규칙은 유지)')
    parser.add_argument('--outbox',action='store_true',help='전송 상태 확인')
    parser.add_argument('--resolve',nargs=2,metavar=('ID','sent|retry'),help='수신 확인 불명 건을 수동 해결')
    parser.add_argument('--retry-pending',action='store_true',help='분석 없이 명확한 전송 실패 건 재시도')
    args=parser.parse_args(); c=config()
    from filelock import FileLock,Timeout
    c['data'].mkdir(parents=True,exist_ok=True)
    try:
        with FileLock(str(c['data']/'run.lock'),timeout=0):
            if args.outbox or args.resolve or args.retry_pending:
                store=Store(c['data']/'radar.sqlite3')
                if args.resolve:
                    ident,action=args.resolve
                    if action not in ('sent','retry'): raise RuntimeError('Action must be sent or retry')
                    row=store.db.execute('SELECT status FROM outbox WHERE id=?',(ident,)).fetchone()
                    if not row or row[0] not in ('uncertain','sending'): raise RuntimeError('Not an uncertain message')
                    store.db.execute('UPDATE outbox SET status=? WHERE id=?',('sent' if action=='sent' else 'pending',ident)); store.db.commit()
                if args.retry_pending:
                    if not c['token'] or not c['chat']: raise RuntimeError('Telegram configuration missing')
                    deliver(store,c)
                for row in store.db.execute('SELECT id,created,status FROM outbox ORDER BY created DESC LIMIT 100'):
                    print(dict(row))
                store.db.close(); return
            run(c,send=args.send and os.getenv('DRY_RUN')!='1',bootstrap=args.bootstrap)
    except Timeout: raise RuntimeError('Another run is active') from None

if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Never print a network exception containing tokens or key-bearing request URLs.
        print('[error] '+(str(exc) if isinstance(exc,RuntimeError) else error_location(exc)),file=sys.stderr)
        sys.exit(1)
