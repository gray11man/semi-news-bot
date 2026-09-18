"""Pure Python evidence gates, persistent memory, and durable delivery queue."""
import datetime as dt
import hashlib
import json
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

UTC = dt.timezone.utc
AXES = ['new_use', 'demand', 'supply', 'pricing', 'commitment', 'policy', 'substitution']

def now():
    return dt.datetime.now(UTC).isoformat()

def digest(x):
    return hashlib.sha256(x.encode()).hexdigest()[:24]

def norm(x):
    return re.sub(r'\s+', ' ', x).strip().lower()

def canonical(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip('/'), p.query, ''))

def domain(url):
    host = (urlsplit(url).hostname or '').lower()
    try:
        import tldextract
        p = tldextract.TLDExtract(suffix_list_urls=())(host)
        return p.top_domain_under_public_suffix or host
    except ImportError:
        return host.removeprefix('www.')

def primary(url, allowlist):
    host = (urlsplit(url).hostname or '').lower()
    return any(host == d or host.endswith('.' + d) for d in allowlist)

def valid_date(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS articles(id TEXT PRIMARY KEY, published TEXT, sector TEXT,
            data TEXT NOT NULL, screened INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS themes(id TEXT PRIMARY KEY, data TEXT NOT NULL,
            reviewed TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(theme TEXT, id TEXT, data TEXT NOT NULL,
            PRIMARY KEY(theme,id));
        CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, theme TEXT,
            created TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, created TEXT, text TEXT,
            status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS article_archive(id TEXT PRIMARY KEY, reason TEXT NOT NULL);
        ''')
        self.db.commit()

    def meta(self, key, default=''):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))
        self.db.commit()

    def add_articles(self, items):
        for a in items:
            self.db.execute('INSERT OR IGNORE INTO articles(id,published,sector,data) VALUES(?,?,?,?)',
                            (a['id'], a['published'], a['sector'], json.dumps(a, ensure_ascii=False)))
        self.db.commit()

    def triage_articles(self, days=7):
        """Reversible local exclusions; never label unreviewed articles as screened."""
        cutoff=(dt.datetime.now(UTC)-dt.timedelta(days=days)).isoformat()
        self.db.execute("DELETE FROM article_archive WHERE reason='expired' AND id IN (SELECT id FROM articles WHERE published>=?)",(cutoff,))
        self.db.execute("INSERT OR IGNORE INTO article_archive SELECT id,'expired' FROM articles WHERE screened=0 AND published<?",(cutoff,))
        seen=set()
        for row in self.db.execute('SELECT id,data,screened FROM articles ORDER BY screened DESC,published DESC,id').fetchall():
            a=json.loads(row['data'])
            # Conservative exact title+publisher dedupe. Different numbers remain distinct.
            key=(norm(a.get('title','')),norm(a.get('publisher','')))
            if key[0] and key in seen and not row['screened']:
                self.db.execute("INSERT OR IGNORE INTO article_archive VALUES(?,'duplicate')",(row['id'],))
            seen.add(key)
        self.db.commit()

    def pending_count(self):
        return self.db.execute('SELECT COUNT(*) FROM articles WHERE screened=0 AND id NOT IN (SELECT id FROM article_archive)').fetchone()[0]

    def pending_articles(self, limit, newest=False, ranked=False):
        rows=self.db.execute('SELECT data FROM articles WHERE screened=0 AND id NOT IN (SELECT id FROM article_archive) ORDER BY published DESC,id').fetchall()
        groups={}
        signals=('contract','capacity','shortage','investment','regulation','closure','demand','계약','증설','부족','투자','규제','철수','수요','납기')
        for row in rows:
            a=json.loads(row[0]);groups.setdefault(a['sector'],[]).append(a)
        # Rotate sectors across runs, including when batch size is smaller than sector count.
        sectors=sorted(groups)
        last=self.meta('last_screen_sector')
        sectors=[x for x in sectors if x>last]+[x for x in sectors if x<=last]
        for sector,items in groups.items():
            if newest:
                items.sort(key=lambda a:a['published'],reverse=True)
            elif ranked:
                # Rank only within sector; unmatched titles remain eligible on newest rounds.
                items.sort(key=lambda a:(sum(t in (a.get('title','')+' '+a.get('summary','')).lower() for t in signals),a['published']),reverse=True)
            else:items.sort(key=lambda a:a['published'])
        out=[]
        while sectors and len(out)<limit:
            for sector in sectors[:]:
                out.append(groups[sector].pop(0))
                if not groups[sector]:sectors.remove(sector)
                if len(out)>=limit:break
        return out

    def screened(self, ids):
        self.db.executemany('UPDATE articles SET screened=1 WHERE id=?', [(i,) for i in ids])
        if ids:
            row=self.db.execute('SELECT sector FROM articles WHERE id=?',(ids[-1],)).fetchone()
            if row:self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('last_screen_sector',row[0]))
        self.db.commit()

    def themes(self):
        return [dict(json.loads(r['data']), id=r['id'], reviewed=r['reviewed'])
                for r in self.db.execute('SELECT * FROM themes ORDER BY reviewed,created')]

    def upsert_theme(self, candidate):
        existing = candidate.get('existing_id', '')
        if existing and not self.db.execute('SELECT 1 FROM themes WHERE id=?', (existing,)).fetchone():
            raise ValueError('Unknown existing theme ID')
        tid = existing or digest(norm(candidate['chain_key']))
        row = self.db.execute('SELECT data FROM themes WHERE id=?', (tid,)).fetchone()
        c = dict(candidate)
        if row:
            old = json.loads(row[0])
            c['seed_ids'] = list(dict.fromkeys(old.get('seed_ids', []) + c.get('seed_ids', [])))
            self.db.execute('UPDATE themes SET data=? WHERE id=?', (json.dumps(c, ensure_ascii=False), tid))
        else:
            self.db.execute('INSERT INTO themes(id,data,created) VALUES(?,?,?)',
                            (tid, json.dumps(c, ensure_ascii=False), now()))
        self.db.commit()
        return tid

    def evidence(self, tid):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM evidence WHERE theme=?', (tid,))]

    def record(self, tid, assessment, evidence):
        # Current stance replaces earlier stance for the same fact/source. History stays in snapshots.
        for e in evidence:
            self.db.execute('INSERT OR REPLACE INTO evidence VALUES(?,?,?)',
                            (tid, e['id'], json.dumps(e, ensure_ascii=False)))
        self.db.execute('INSERT INTO snapshots(theme,created,data) VALUES(?,?,?)',
                        (tid, now(), json.dumps(assessment, ensure_ascii=False)))
        self.db.execute('UPDATE themes SET reviewed=? WHERE id=?', (now(), tid))
        self.db.commit()

    def enqueue(self, key, text):
        self.db.execute('INSERT OR IGNORE INTO outbox(id,created,text) VALUES(?,?,?)', (key, now(), text))
        self.db.commit()

    def doc(self, doc_id):
        row = self.db.execute('SELECT data FROM docs WHERE id=?', (doc_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_doc(self, doc):
        self.db.execute('INSERT OR REPLACE INTO docs VALUES(?,?)', (doc['id'], json.dumps(doc, ensure_ascii=False)))
        self.db.commit()


def validate_evidence(rows, docs, today=None):
    """Accept only quotes present in fetched article text, never RSS snippets or invented URLs."""
    today = today or dt.datetime.now(UTC).date()
    lookup = {d['id']: d for d in docs}
    accepted = []
    for e in rows:
        d = lookup.get(e.get('doc_id'))
        if not d or d.get('status') != 'body': continue
        quote = e.get('quote', '')
        date = valid_date(e.get('event_date'))
        # Reject undated/old events for verification gates. They can remain background in the report.
        if not date or not 0 <= (today - date).days <= 365: continue
        if len(quote) < 24 or norm(quote) not in norm(d['text']): continue
        if e.get('axis') not in AXES or e.get('stance') not in ('supports','contradicts','context'): continue
        origin = norm(e.get('origin_key', ''))
        fact = norm(e.get('fact_key', ''))
        if not origin or origin in ('unknown', 'unclear', '미상') or not fact: continue
        # Exact repeat/reclassification shares an ID; timestamps cannot manufacture new evidence.
        ev = dict(e, id=digest(d['id'] + fact), url=d['url'], publisher=domain(d['url']),
                  primary=e.get('official_source', False), fetched=d['fetched'],
                  text_hash=digest(norm(d['text'])))
        accepted.append(ev)
    return accepted


def source_counts(evidence):
    # Collapse same publisher OR syndicated origin OR exact copied body via connected components.
    groups = []
    for e in evidence:
        keys = {('publisher', e['publisher']), ('origin', norm(e['origin_key'])), ('text', e['text_hash'])}
        overlap = [g for g in groups if g & keys]
        for g in overlap:
            keys |= g; groups.remove(g)
        groups.append(keys)
    return len(groups)


def evaluate(assessment, evidence, fresh_hours=72, today=None):
    current = today or dt.datetime.now(UTC)
    evidence = [e for e in evidence if valid_date(e['event_date']) and
                0 <= (current.date()-valid_date(e['event_date'])).days <= 365]
    support = [e for e in evidence if e['stance']=='supports']
    negative = [e for e in evidence if e['stance']=='contradicts']
    axes = set(e['axis'] for e in support)
    # Date-level source dates cannot support an hours-level freshness claim: also use fetched/RSS dates upstream.
    fresh = [e for e in support if (current.date()-valid_date(e['event_date'])).days <= max(1, fresh_hours//24)]
    counts = source_counts(support)
    points = {'new_use':20,'demand':15,'supply':15,'pricing':10,'commitment':20,'policy':10,'substitution':10}
    score = min(100, sum(points[a] for a in axes))
    state = '관찰'
    # No claim of "certain megatrend". Fact verification does not prove an investment thesis.
    if (counts >= 5 and len(axes)>=2 and any(e['primary'] for e in support) and fresh
        and assessment['structural'] and assessment['economic_path']):
        state = '초기 신호'
        if len(axes)>=3 and 'commitment' in axes and len({e['fact_key'] for e in support})>=3:
            state = '변화 강화'
    if negative and assessment['thesis_broken'] and source_counts(negative)>=5:
        state = '반증 경고'
    return {'state':state, 'score':score, 'independent_sources':counts,
            'axes':sorted(axes), 'counter_sources':source_counts(negative),
            'evidence_ids':sorted(e['id'] for e in evidence)}


def alert_key(tid, gate, evidence):
    # Reprints do not trigger new alerts: semantic original-fact keys, not article IDs.
    facts = sorted(set(norm(e['fact_key']) for e in evidence if e['stance']!='context'))
    return digest(tid + gate['state'] + json.dumps(facts, ensure_ascii=False))


def render(theme, assessment, gate, evidence):
    facts = []
    for e in evidence:
        if e['stance']=='context': continue
        item = f"• {e['fact']} ({e['event_date']})"
        if item not in facts: facts.append(item)
    text = (f"산업변화 레이더 | {gate['state']}\n{theme['title']}\n\n"
            f"왜 지금: {assessment['change']}\n\n"
            f"산업 연결: {assessment['chain']}\n\n"
            f"확인된 근거\n" + '\n'.join(facts[:5]) + '\n\n'
            f"이익이 옮겨갈 곳(추론): {assessment['beneficiaries']}\n"
            f"공급자가 돈을 벌 조건: {assessment['profit_capture']}\n"
            f"반대 근거·깨질 조건: {assessment['countercase']}\n"
            f"다음 확인: {assessment['next_check']}\n\n"
            f"연결고리 검증 출처 {gate['independent_sources']}곳 · 점수 {gate['score']}/100"
            " (증거 축 충족도이며 성공확률이 아닙니다)\n"
            "5개 출처는 산업 연결고리 전체 기준이며, 모든 개별 주장이 5중 확인됐다는 뜻은 아닙니다.\n")
    return text


def chunks(text, limit=3500):
    out=[]; current=''; units=0
    for char in text:
        size=len(char.encode('utf-16-le'))//2
        if units+size>limit:
            out.append(current); current=''; units=0
        current+=char; units+=size
    if current: out.append(current)
    return out
