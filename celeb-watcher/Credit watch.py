# -*- coding: utf-8 -*-
"""
하이퍼스케일러 / 사모크레딧 / AI 캐팩스 금융 감시 모듈  (v2)

v1 대비 변경점
- Gemini 호출을 항목별 1회 → 배치 1회(최대 8건)로 축소 (25회 → 3회 수준)
- 시작 시 쿼터 프로브: 429면 즉시 중단하고 남은 쿼터를 셀럽봇에 양보
- 429 지수 백오프 + retryDelay 파싱 + Flash-Lite 폴백
- 판정 실패한 항목은 seen에 넣지 않음 (다음 사이클 재시도)
- 팟캐스트 피드 이름 검증 (엉뚱한 팟캐스트 매칭 방지)
- 피드 캐시 버전 관리

celeb_watcher.py 에서 호출 (반드시 main() 맨 마지막):
    from credit_watch import run_credit_watch
    ...
    run_blog_watch()
    run_credit_watch()      # ← 셀럽봇이 쿼터를 먼저 쓰도록 마지막에
"""
import os, json, re, time, html
from datetime import datetime, timedelta, timezone
import requests
import feedparser

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY = os.environ["GEMINI_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "seen_credit.json"
FEED_CACHE_VERSION = 2      # 올리면 피드 캐시 전체 재해석

# ── 모델 (429 시 순서대로 폴백) ───────────────────────────
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash-lite"]

# ── 쿼터 예산 ─────────────────────────────────────────────
MAX_GEMINI_CALLS = 6        # 이 모듈이 한 사이클에 쓸 Gemini 호출 상한
BATCH_SIZE = 8              # 한 번에 판정할 항목 수
MAX_CANDIDATES = 40         # 배치에 넣을 후보 상한
MAX_SEND_PER_RUN = 12

# ── 모듈별 on/off ─────────────────────────────────────────
ENABLE_PODCAST = True
ENABLE_YOUTUBE = False      # 유튜브 API 쿼터 아끼려면 False 권장
ENABLE_NEWS = True

PODCAST_MAX_AGE_DAYS = 5
YT_LOOKBACK_HOURS = 36
YT_MIN_DURATION_SEC = 600
NEWS_LOOKBACK_HOURS = 24
SCORE_THRESHOLD = 7

# ═══════════════════════════════════════════════════════════
# 감시 대상 팟캐스트
#   verify: 해석된 팟캐스트 제목에 이 문자열이 없으면 거부 (오매칭 방지)
#   apple_id 를 넣으면 검색 없이 정확히 해석됨
#   (애플 팟캐스트 URL 의 id 뒤 숫자)
# ═══════════════════════════════════════════════════════════
PODCASTS = [
    {"name": "The Credit Edge", "apple_id": "1674628050"},
    {"name": "Odd Lots",        "search": "Odd Lots Bloomberg",       "verify": "odd lots"},
    {"name": "Money Stuff",     "search": "Money Stuff Matt Levine",  "verify": "money stuff"},
    {"name": "GS Exchanges",    "search": "Goldman Sachs Exchanges",  "verify": "exchanges"},
    {"name": "Behind the Money","search": "Behind the Money FT",      "verify": "behind the money"},
    {"name": "Unhedged",        "search": "Unhedged Financial Times", "verify": "unhedged"},
    {"name": "BG2Pod",          "search": "BG2Pod Gerstner Gurley",   "verify": "bg2"},
]

CREDIT_KEYWORDS = [
    "private credit", "private debt", "direct lending", "securitization",
    "securitisation", "asset-backed", "bond issuance", "debt financing",
    "credit spread", "spreads", "investment grade", "high yield", "leverage",
    "lender", "underwriting", "refinanc", "duration", "issuance",
    "vendor financing", "sale-leaseback", "covenant", "downgrade", "rating",
    "data center", "datacenter", "data centre", "hyperscaler", "capex",
    "capital expenditure", "neocloud", "gpu", "ai infrastructure", "grid",
    "memory", "hbm", "dram", "nand", "semiconductor",
    "nvidia", "microsoft", "amazon", "alphabet", "google", "meta", "oracle",
    "coreweave", "broadcom", "micron", "sk hynix", "samsung", "tsmc",
    "blackstone", "apollo", "ares", "kkr", "wellington", "pimco",
]

YT_QUERIES = [
    '"private credit" ("data center"|"data centre"|AI)',
    'hyperscaler debt "bond issuance" OR "credit spread"',
    '"AI capex" credit market bonds interview',
]

NEWS_QUERIES = [
    '"private credit" "data center"',
    'hyperscaler bond issuance debt',
    '"data center" debt financing spreads',
    '"AI capex" credit market',
    'HBM pricing contract negotiation',
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_calls = {"n": 0, "dead": False}


# ═══════════════════════════════════════════════════════════
# 상태
# ═══════════════════════════════════════════════════════════
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("podcast", {})
    s.setdefault("feeds", {})
    s.setdefault("youtube", [])
    s.setdefault("news", [])
    if s.get("feed_ver") != FEED_CACHE_VERSION:
        print("[캐시] 피드 캐시 버전 변경 → 전체 재해석")
        s["feeds"] = {}
        s["feed_ver"] = FEED_CACHE_VERSION
    return s


def save_state(s):
    s["youtube"] = s["youtube"][-1500:]
    s["news"] = s["news"][-1500:]
    for k in s["podcast"]:
        s["podcast"][k] = s["podcast"][k][:60]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def keyword_hit(text):
    t = (text or "").lower()
    return any(k in t for k in CREDIT_KEYWORDS)


def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[:4000],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": False}, timeout=30)
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


# ═══════════════════════════════════════════════════════════
# Gemini — 백오프 + 모델 폴백 + 예산 관리
# ═══════════════════════════════════════════════════════════
def _retry_delay(body):
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', body or "")
    return int(m.group(1)) if m else None


def gemini_call(prompt, max_retry=3):
    if _calls["dead"]:
        return None
    if _calls["n"] >= MAX_GEMINI_CALLS:
        print(f"[Gemini] 예산 {MAX_GEMINI_CALLS}회 소진 - 중단")
        _calls["dead"] = True
        return None

    for model in GEMINI_MODELS:
        for attempt in range(max_retry):
            _calls["n"] += 1
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": GEMINI_API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.1}},
                    timeout=90)
                if r.status_code == 429:
                    body = r.text[:600]
                    qid = re.search(r'"quotaId"\s*:\s*"([^"]+)"', body)
                    qname = qid.group(1) if qid else "?"
                    print(f"[Gemini 429] model={model} quota={qname} attempt={attempt+1}")
                    if "PerDay" in qname:
                        print("  → 일일 쿼터 소진. 재시도 무의미, 폴백")
                        break
                    wait = _retry_delay(body) or (5 * (2 ** attempt))
                    print(f"  → {wait}초 대기 후 재시도")
                    time.sleep(min(wait, 60))
                    continue
                r.raise_for_status()
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return re.sub(r"```json|```", "", txt).strip()
            except Exception as e:
                print(f"[Gemini 오류] {model}: {str(e)[:200]}")
                time.sleep(3)
        print(f"[Gemini] {model} 실패 → 다음 모델 폴백")

    print("[Gemini] 모든 모델 실패 - 이번 사이클 판정 중단")
    _calls["dead"] = True
    return None


def probe_gemini():
    """쿼터가 살아있는지 1회 확인. 죽었으면 모듈 전체 스킵."""
    out = gemini_call('JSON만 출력: {"ok":true}', max_retry=1)
    if out is None:
        print("[프로브] Gemini 사용 불가 → 크레딧 감시 이번 사이클 스킵")
        return False
    return True


BATCH_PROMPT = """너는 반도체/AI 인프라 투자자를 위한 콘텐츠 선별 에이전트다.
아래 여러 콘텐츠 각각이 다음 관심사에 실질적으로 부합하는지 엄격히 판정하라.

관심사:
1. 하이퍼스케일러(MS/구글/아마존/메타/오라클) 자금조달 — 회사채 발행, 스프레드, 신용등급, 듀레이션
2. 데이터센터 사모대출/사모크레딧 — 대출 조건(LTV, advance rate, 스프레드), 터미널 밸류
3. AI 캐팩스의 재무적 지속가능성, 벤더 파이낸싱, 자산유동화(ABS)
4. 메모리(HBM/DRAM/NAND) 가격·계약 협상·선급금
5. 위 주제로 업계 실무자(운용사, 대출기관, CFO, 애널리스트)가 직접 발언하는 인터뷰/대담

탈락: 일반 AI 기술·제품 소개, 스치듯 언급, 개인투자 유튜브의 종목추천/시황요약, 광고

콘텐츠 목록:
{items}

출력: JSON 배열만. 마크다운 금지. 입력과 같은 개수, 같은 순서.
[{{"idx": 0, "relevance_score": 0, "summary_kr": "핵심 3줄 이내 한국어 요약, 숫자·고유명사 유지", "key_points": ["최대 3개"]}}]"""


def judge_batch(items):
    """items: [{'kind','title','source','body'}] → [judgment|None]"""
    if not items:
        return []
    blob = "\n\n".join(
        f"[{i}] 종류: {it['kind']}\n제목: {it['title']}\n출처: {it['source']}\n"
        f"설명: {strip_html(it['body'])[:700]}"
        for i, it in enumerate(items))
    out = gemini_call(BATCH_PROMPT.format(items=blob))
    if out is None:
        return [None] * len(items)
    try:
        arr = json.loads(out)
        res = [None] * len(items)
        for j in arr:
            i = j.get("idx")
            if isinstance(i, int) and 0 <= i < len(items):
                res[i] = j
        return res
    except Exception as e:
        print(f"[배치 파싱 실패] {str(e)[:200]} | 원문: {out[:200]}")
        return [None] * len(items)


def fmt_msg(icon, kind, source, title, j, url):
    pts = j.get("key_points") or []
    pts_txt = "".join(f"\n • {html.escape(str(p))}" for p in pts[:3])
    return (f"{icon} <b>{html.escape(kind)}</b> · {html.escape(source)}\n"
            f"<b>{html.escape(title)}</b>\n\n"
            f"💡 {html.escape(j.get('summary_kr',''))}{pts_txt}\n\n"
            f"점수: {j.get('relevance_score','?')}/10\n{url}")


# ═══════════════════════════════════════════════════════════
# 후보 수집 — 팟캐스트
# ═══════════════════════════════════════════════════════════
def resolve_feed(pod, state):
    name = pod["name"]
    if pod.get("feed"):
        return pod["feed"]
    if state["feeds"].get(name):
        return state["feeds"][name]
    try:
        if pod.get("apple_id"):
            r = requests.get("https://itunes.apple.com/lookup",
                             params={"id": pod["apple_id"], "entity": "podcast"},
                             timeout=20)
        else:
            r = requests.get("https://itunes.apple.com/search",
                             params={"term": pod["search"], "entity": "podcast",
                                     "limit": 10}, timeout=20)
        r.raise_for_status()
        results = [x for x in r.json().get("results", []) if x.get("feedUrl")]
        verify = (pod.get("verify") or "").lower()
        if verify:
            results = [x for x in results
                       if verify in (x.get("collectionName", "") or "").lower()]
        if not results:
            print(f"[팟캐스트] {name}: 해석 실패 "
                  f"(verify='{verify}' 만족 결과 없음) → apple_id 직접 지정 필요")
            return None
        best = results[0]
        print(f"[팟캐스트] {name} → {best.get('collectionName')} | {best['feedUrl']}")
        state["feeds"][name] = best["feedUrl"]
        return best["feedUrl"]
    except Exception as e:
        print(f"[팟캐스트] {name} 해석 오류: {e}")
        return None


def fetch_episodes(feed_url, name):
    try:
        r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=25)
        if r.status_code != 200:
            print(f"[팟캐스트] {name}: HTTP {r.status_code}")
            return []
        d = feedparser.parse(r.content)
    except Exception as e:
        print(f"[팟캐스트] {name}: 피드 오류 {e}")
        return []

    total = len(d.entries)
    cutoff = datetime.now(timezone.utc) - timedelta(days=PODCAST_MAX_AGE_DAYS)
    out, too_old = [], 0
    for e in d.entries[:15]:
        eid = e.get("id") or e.get("guid") or e.get("link", "")
        if not eid:
            continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if pub and datetime(*pub[:6], tzinfo=timezone.utc) < cutoff:
            too_old += 1
            continue
        desc = e.get("summary", "") or ""
        if e.get("content"):
            desc = e["content"][0].get("value", desc)
        out.append({"id": eid, "title": e.get("title", "(제목없음)"),
                    "url": e.get("link", ""), "desc": desc})
    print(f"[팟캐스트] {name}: 피드 {total}건 / 최근 {PODCAST_MAX_AGE_DAYS}일 {len(out)}건 "
          f"(기간초과 {too_old}건)")
    return out


def collect_podcast(state):
    cands = []
    for pod in PODCASTS:
        name = pod["name"]
        feed = resolve_feed(pod, state)
        if not feed:
            continue
        eps = fetch_episodes(feed, name)
        if not eps:
            continue
        first = name not in state["podcast"]
        seen = set(state["podcast"].get(name, []))
        state["podcast"][name] = list(dict.fromkeys(
            [e["id"] for e in eps] + list(seen)))
        if first:
            print("  ↳ baseline 저장, 알림 생략")
            continue
        for ep in reversed([e for e in eps if e["id"] not in seen]):
            if not keyword_hit(f"{ep['title']} {strip_html(ep['desc'])}"):
                print(f"  ⏭ [키워드 없음] {ep['title'][:60]}")
                continue
            cands.append({"kind": "팟캐스트", "icon": "🎧", "source": name,
                          "title": ep["title"], "body": ep["desc"],
                          "url": ep["url"], "seen_key": None})
    return cands


# ═══════════════════════════════════════════════════════════
# 후보 수집 — 유튜브 / 뉴스
# ═══════════════════════════════════════════════════════════
YT_CHANNEL_BLACK = [r"주식", r"투자", r"코인", r"경제tv", r"클립", r"쇼츠", r"shorts"]


def dur_sec(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def collect_youtube(state):
    if not YOUTUBE_API_KEY:
        return []
    after = (datetime.now(timezone.utc) - timedelta(hours=YT_LOOKBACK_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = set(state["youtube"])
    cand = {}
    for q in YT_QUERIES:
        try:
            r = requests.get("https://www.googleapis.com/youtube/v3/search",
                             params={"key": YOUTUBE_API_KEY, "part": "snippet",
                                     "q": q, "type": "video", "order": "date",
                                     "maxResults": 20, "publishedAfter": after},
                             timeout=30)
            r.raise_for_status()
            for it in r.json().get("items", []):
                vid = it["id"]["videoId"]
                if vid not in seen:
                    cand[vid] = it
        except Exception as e:
            print(f"[유튜브] 검색 실패: {str(e)[:150]}")
        time.sleep(1)
    if not cand:
        print("[유튜브] 신규 후보 없음")
        return []

    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"key": YOUTUBE_API_KEY,
                                 "part": "contentDetails,snippet",
                                 "id": ",".join(list(cand)[:50])}, timeout=30)
        r.raise_for_status()
        details = {it["id"]: it for it in r.json().get("items", [])}
    except Exception as e:
        print(f"[유튜브] 상세조회 실패: {e}")
        return []

    out = []
    for vid, it in cand.items():
        title = it["snippet"]["title"]
        ch = it["snippet"]["channelTitle"]
        det = details.get(vid, {})
        desc = det.get("snippet", {}).get("description", "")
        if any(re.search(p, ch.lower()) for p in YT_CHANNEL_BLACK) \
           or dur_sec(det.get("contentDetails", {}).get("duration")) < YT_MIN_DURATION_SEC \
           or not keyword_hit(f"{title} {desc[:600]}"):
            state["youtube"].append(vid)   # 판정 없이 걸러진 건 바로 seen 처리
            continue
        out.append({"kind": "유튜브", "icon": "📺", "source": ch, "title": title,
                    "body": desc, "url": f"https://youtu.be/{vid}",
                    "seen_key": ("youtube", vid)})
    print(f"[유튜브] 판정 대상 {len(out)}건")
    return out


def collect_news(state):
    seen = set(state["news"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    out, dedup = [], set()
    for q in NEWS_QUERIES:
        try:
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": f"{q} when:2d", "hl": "en-US",
                                     "gl": "US", "ceid": "US:en"},
                             headers={"User-Agent": UA}, timeout=25)
            d = feedparser.parse(r.content)
        except Exception as e:
            print(f"[뉴스] 실패 ({q}): {str(e)[:150]}")
            continue
        for e in d.entries[:8]:
            link = e.get("link", "")
            if not link or link in seen or link in dedup:
                continue
            pub = e.get("published_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) < cutoff:
                continue
            dedup.add(link)
            out.append({"kind": "뉴스", "icon": "📰",
                        "source": (e.get("source", {}) or {}).get("title", "News"),
                        "title": e.get("title", ""), "body": e.get("summary", ""),
                        "url": link, "seen_key": ("news", link)})
        time.sleep(0.5)
    print(f"[뉴스] 판정 대상 {len(out)}건")
    return out


# ═══════════════════════════════════════════════════════════
# 엔트리포인트
# ═══════════════════════════════════════════════════════════
def run_credit_watch():
    try:
        state = load_state()

        if not probe_gemini():
            save_state(state)   # 팟캐스트 baseline 등은 저장
            return

        cands = []
        if ENABLE_PODCAST:
            print("── 팟캐스트 ──")
            cands += collect_podcast(state)
        if ENABLE_YOUTUBE:
            print("── 유튜브 ──")
            cands += collect_youtube(state)
        if ENABLE_NEWS:
            print("── 뉴스 ──")
            cands += collect_news(state)

        cands = cands[:MAX_CANDIDATES]
        nbatch = (len(cands) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"[판정] 총 후보 {len(cands)}건 → 배치 {nbatch}회")

        sent = 0
        for i in range(0, len(cands), BATCH_SIZE):
            chunk = cands[i:i + BATCH_SIZE]
            results = judge_batch(chunk)
            for c, j in zip(chunk, results):
                if j is None:
                    print(f"  ⚠ 판정실패(다음사이클 재시도): {c['title'][:50]}")
                    continue
                if c["seen_key"]:
                    state[c["seen_key"][0]].append(c["seen_key"][1])
                score = j.get("relevance_score", 0)
                if score >= SCORE_THRESHOLD and sent < MAX_SEND_PER_RUN:
                    send_tg(fmt_msg(c["icon"], c["kind"], c["source"],
                                    c["title"], j, c["url"]))
                    sent += 1
                    print(f"  ✅ [{score}] {c['source']} - {c['title'][:50]}")
                else:
                    print(f"  ❌ [{score}] {c['title'][:50]}")
            time.sleep(2)

        save_state(state)
        print(f"[크레딧 감시] 전송 {sent}건 / Gemini 호출 {_calls['n']}회")
    except Exception as e:
        print(f"[크레딧 감시 전체 실패] {str(e)[:300]}")


if __name__ == "__main__":
    run_credit_watch()
