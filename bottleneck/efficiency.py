"""Local retrieval and durable token budget. None of these operations use an LLM."""
import datetime as dt
import json
import re
import sqlite3
from core import digest, norm


def terms(text):
    return set(re.findall(r'[a-zA-Z가-힣0-9]{2,}',text.lower()))


def excerpt(text,query,limit=3000):
    if len(text)<=limit:return text
    blocks=[text[i:i+450] for i in range(0,len(text),450)]
    tokens=terms(query)
    def score(i):
        b=blocks[i].lower()
        return sum(min(3,b.count(t)) for t in tokens)+bool(re.search(r'\d',b))
    indices=[0,len(blocks)-1]+sorted(range(1,len(blocks)-1),key=score,reverse=True)
    chosen=[];used=0
    for i in indices:
        if i in chosen:continue
        if used+len(blocks[i])+7>limit:continue
        chosen.append(i);used+=len(blocks[i])+7
    return '\n[...]\n'.join(blocks[i] for i in sorted(chosen))[:limit]


def compact_docs(docs,theme,limit=3000):
    query=theme.get('hypothesis','')+' '+theme['chain_key']+' '+' '.join(theme.get('queries',[]))
    return [dict(d,text=excerpt(d['text'],query,limit)) for d in docs]


def audit_docs(docs,evidence,limit=1600):
    out=[]
    for d in docs:
        quotes=[e['quote'] for e in evidence if e.get('doc_id')==d['id']]
        if not quotes:continue
        # Exact source context around evidence; no generated summaries passed as original text.
        spans=[]
        for q in quotes:
            pos=d['text'].find(q)
            if pos>=0:spans.append(d['text'][max(0,pos-180):pos+len(q)+180])
        text='\n[...]\n'.join(dict.fromkeys(spans))
        # Never cut an evidence quote out just to meet a character target.
        if len(text)>limit:
            text='\n[...]\n'.join(dict.fromkeys(quotes))
        out.append(dict(d,text=text))
    return out


def relevant_catalog(themes,articles,limit=40):
    query=terms(' '.join(a.get('title','')+' '+a.get('sector','') for a in articles))
    def score(t):return len(query & terms(t['title']+' '+t['chain_key']+' '+t.get('sector','')))
    selected=sorted(themes,key=score,reverse=True)[:limit]
    return [dict(id=t['id'],chain_key=t['chain_key'],title=t['title']) for t in selected]


def compact_evidence(evidence,limit=32):
    # Prioritize contradictions and spread source/axis coverage. Full memory remains on disk.
    rows=sorted(evidence,key=lambda e:(e['stance']=='contradicts',e['event_date']),reverse=True)
    selected=[];covered=set()
    for e in rows:
        key=(e['origin_key'],e['axis'],e['stance'])
        if key not in covered:
            selected.append(e);covered.add(key)
        if len(selected)>=limit:return selected
    seen={e['id'] for e in selected}
    return selected+[e for e in rows if e['id'] not in seen][:limit-len(selected)]


class BudgetExceeded(RuntimeError):pass

class TokenBudget:
    """Reservations survive crashes and failed requests, and include preview runs."""
    def __init__(self,path,run_limit=160000,day_limit=500000):
        self.db=sqlite3.connect(str(path));self.run_limit=run_limit;self.day_limit=day_limit;self.used=0
        self.db.execute('CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,day TEXT,tokens INTEGER,status TEXT)')
        self.db.commit()
    def day(self):return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    def total(self):return self.db.execute('SELECT COALESCE(SUM(tokens),0) FROM usage WHERE day=?',(self.day(),)).fetchone()[0]
    def reserve(self,amount):
        self.db.execute('BEGIN IMMEDIATE')
        if self.used+amount>self.run_limit or self.total()+amount>self.day_limit:
            self.db.rollback();raise BudgetExceeded('Token budget reached; unprocessed work retained')
        row=self.db.execute('INSERT INTO usage(day,tokens,status) VALUES(?,?,?)',(self.day(),amount,'reserved'))
        self.db.commit();self.used+=amount
        return row.lastrowid
    def settle(self,receipt,actual):
        if not isinstance(actual,int) or actual<0:return
        row=self.db.execute('SELECT tokens FROM usage WHERE id=?',(receipt,)).fetchone()
        if row:
            self.used+=actual-row[0]
            self.db.execute("UPDATE usage SET tokens=?,status='measured' WHERE id=?",(actual,receipt));self.db.commit()
