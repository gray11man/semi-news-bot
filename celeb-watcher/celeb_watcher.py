# -*- coding: utf-8 -*-
"""
통합 감시 봇 (단일 파일 — 별도 모듈 import 없음)

PART 1  공통 유틸 / Gemini 호출기 (배치 + 쿼터 예산 + 백오프)
PART 2  AI 유명인사 유튜브 출연 감시
PART 3  네이버 블로그 감시
PART 4  하이퍼스케일러 / 사모크레딧 / AI 캐팩스 금융 감시 (팟캐스트·뉴스·유튜브)

핵심 변경 (429 대응)
- Gemini 호출을 항목당 1회 → 8건 배치 1회로 축소 (하루 ~100회 → ~12회)
- 전역 호출 예산(MAX_GEMINI_CALLS)으로 상한 고정
- 429 응답 본문을 그대로 출력해 어떤 쿼터인지 확인 가능
- 쿼터 소진 시 즉시 중단 (로그 도배 방지)

필요 시크릿: YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
상태 파일: seen_celeb_ids.json, seen_twitter_blog.json, seen_credit.json
"""
import os, json, re, time, html
from datetime import datetime, timedelta, timezone
import requests
import feedparser

# ═══════════════════════════════════════════════════════════
# PART 1 — 공통
# ═══════════════════════════════════════════════════════════
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

# 429 나면 순서대로 폴백
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

MAX_GEMINI_CALLS = 12      # 한 사이클 전체 Gemini 호출 상한 (셀럽 + 크레딧 합산)
BATCH_SIZE = 8             # 한 번에 판정할 항목 수
NOTIFY_WHEN_EMPTY = False  # True면 결과 없을 때도 "없음" 알림

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_gm = {"n": 0, "dead": False}


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": msg[:4000],
                            "parse_mode": "HTML",
                            "disable_web_page_preview": False}, timeout=30)
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


def _retry_delay(body):
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)', body or "")
    return int(m.group(1)) if m else None


def gemini_call(prompt, max_retry=2):
    """모델 폴백 + 백오프 + 전역 예산. 실패 시 None."""
    if _gm["dead"]:
        return None
    if _gm["n"] >= MAX_GEMINI_CALLS:
        print(f"[Gemini] 예산 {MAX_GEMINI_CALLS}회 소진 - 이후 판정 중단")
        _gm["dead"] = True
        return None

    for model in GEMINI_MODELS:
        for attempt in range(max_retry):
            _gm["n"] += 1
            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    params={"key": GEMINI_API_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.1}},
                    timeout=90)
                if r.status_code == 429:
                    body = r.text[:500].replace("\n", " ")
                    print(f"[429] {model} attempt{attempt+1} | {body}")
                    if "PerDay" in body:
                        print("  → 일일 쿼터 소진. 재시도 무의미, 폴백")
                        break
                    wait = _retry_delay(body) or (5 * (2 ** attempt))
                    time.sleep(min(wait, 40))
                    continue
                if r.status_code == 404:
                    print(f"[404] 모델 없음: {model} → 폴백")
                    break
                r.raise_for_status()
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return re.sub(r"```json|```", "", txt).strip()
            except Exception as e:
                print(f"[Gemini 오류] {model}: {str(e)[:180]}")
                time.sleep(3)
        print(f"[Gemini] {model} 실패 → 다음 모델")

    print("[Gemini] 전 모델 실패 - 이번 사이클 판정 전면 중단")
    _gm["dead"] = True
    return None


def parse_json_array(out, n):
    """idx 필드 기준으로 길이 n 배열에 정렬. 실패 시 [None]*n"""
    if out is None:
        return [None] * n
    try:
        arr = json.loads(out)
        res = [None] * n
        for j in arr:
            i = j.get("idx")
            if isinstance(i, int) and 0 <= i < n:
                res[i] = j
        return res
    except Exception as e:
        print(f"[배치 파싱 실패] {str(e)[:150]} | {str(out)[:200]}")
        return [None] * n


# ═══════════════════════════════════════════════════════════
# PART 2 — AI 유명인사 유튜브 감시
# ═══════════════════════════════════════════════════════════
SEEN_FILE = "seen_celeb_ids.json"
LOOKBACK_HOURS = 36
MIN_DURATION_SEC = 1800      # 일반 인물: 30분 미만 제외
CORE_MIN_DURATION_SEC = 240  # 핵심 인물: 4분 미만만 제외
SCORE_THRESHOLD = 7
STRICT_SCORE = 9
MAX_CELEB_CANDIDATES = 24    # 배치 판정 대상 상한

CORE_PERSONS = {
    "Jensen Huang", "Sam Altman", "Sarah Friar", "Mira Murati",
    "Ilya Sutskever", "Dario Amodei", "Daniela Amodei",
    "Sundar Pichai", "Satya Nadella", "Lisa Su", "Mark Zuckerberg",
    "Sanjay Mehrotra", "Aravind Srinivas",
    "Jakub Pachocki", "Kevin Weil", "Rahul Patil", "Krishna Rao",
    "Greg Brockman", "Eric Lefkofsky",
}

PERSONS = {
    "Jensen Huang":    ["jensen huang", "젠슨 황", "젠슨황"],
    "Sam Altman":      ["sam altman", "샘 알트만", "샘 올트먼"],
    "Sarah Friar":     ["sarah friar"],
    "Mira Murati":     ["mira murati", "미라 무라티"],
    "Ilya Sutskever":  ["ilya sutskever", "일리야 수츠케버"],
    "Dario Amodei":    ["dario amodei", "다리오 아모데이"],
    "Daniela Amodei":  ["daniela amodei"],
    "Sundar Pichai":   ["sundar pichai", "순다르 피차이"],
    "Satya Nadella":   ["satya nadella", "사티아 나델라"],
    "Aravind Srinivas": ["aravind srinivas"],
    "Lisa Su":         ["lisa su", "리사 수"],
    "Mark Zuckerberg": ["mark zuckerberg", "저커버그"],
    "Sanjay Mehrotra": ["sanjay mehrotra", "micron ceo"],
    "Dylan Patel":     ["dylan patel", "semianalysis"],
    "Hock Tan":        ["hock tan"],
    "C.C. Wei":        ["c.c. wei", "cc wei", "wei che-chia"],
    "Gavin Baker":     ["gavin baker"],
    "Greg Brockman":   ["greg brockman"],
    "Jonathan Ross":   ["jonathan ross groq"],
    "Andrew Feldman":  ["andrew feldman"],
    "Elon Musk":       ["elon musk", "일론 머스크"],
    "Jakub Pachocki":  ["jakub pachocki"],
    "Kevin Weil":      ["kevin weil"],
    "Rahul Patil":     ["rahul patil anthropic"],
    "Krishna Rao":     ["krishna rao anthropic"],
    "Aaron Levie":     ["aaron levie"],
    "Marc Andreessen": ["marc andreessen"],
    "Chamath Palihapitiya": ["chamath"],
    "Kevin Scott":     ["kevin scott microsoft"],
    "Amin Vahdat":     ["amin vahdat"],
    "Michael Dell":    ["michael dell"],
    "Arvind Krishna":  ["arvind krishna"],
    "Cristiano Amon":  ["cristiano amon"],
    "Eric Lefkofsky":  ["eric lefkofsky", "레프코프스키"],
}

STRICT_PERSONS = {"Elon Musk"}
TOPIC_FREE_PERSONS = {"Eric Lefkofsky"}

SEARCH_BATCHES = [
    ('"Jensen Huang"|"Lisa Su"|"Satya Nadella"|"Sundar Pichai"', True),
    ('"Sam Altman"|"Sarah Friar"|"Mira Murati"|"Ilya Sutskever"', True),
    ('"Dario Amodei"|"Daniela Amodei"|"Aravind Srinivas"|"Mark Zuckerberg"', True),
    ('"Sanjay Mehrotra"|"Dylan Patel"|"Hock Tan"|"C.C. Wei"', True),
    ('"Gavin Baker"|"Greg Brockman"|"Jonathan Ross" Groq|"Andrew Feldman"', False),
    ('"Elon Musk" (memory|HBM|compute|datacenter|chip|GPU|Dojo)', False),
    ('"Jakub Pachocki"|"Kevin Weil"|"Rahul Patil"|"Krishna Rao"', True),
    ('"Aaron Levie"|"Marc Andreessen"|"Chamath"|"Kevin Scott"', False),
    ('"Amin Vahdat"|"Michael Dell"|"Arvind Krishna"|"Cristiano Amon"', False),
    ('"Eric Lefkofsky" Tempus', True),
]

TITLE_BLACKLIST = [
    "shorts", "#shorts", "reaction", "리액션", "요약정리", "총정리",
    "주식", "종목", "매수", "매도", "급등", "코인", "숏폼", "클립모음",
    "ai voice", "ai 목소리", "성대모사", "밈", "meme", "compilation",
    "fan made", "tribute", "motivational", "동기부여",
]
CHANNEL_BLACKLIST_PATTERNS = [
    r"주식", r"투자", r"경제tv", r"코인", r"단테", r"클립", r"쇼츠",
    r"motivation", r"quotes", r"success",
]
TRUSTED_CHANNELS = [
    "bloomberg", "cnbc", "bg2 pod", "all-in", "lex fridman", "dwarkesh",
    "nvidia", "openai", "anthropic", "microsoft", "google", "20vc",
    "no priors", "a16z", "wsj", "financial times", "the information",
    "stanford", "acquired", "bipartisan", "cheeky pint", "training data",
]


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-3000:], f)


def yt_search(query, published_after, include_medium=False):
    items = []
    for d in ["long"] + (["medium"] if include_medium else []):
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"key": YOUTUBE_API_KEY, "part": "snippet", "q": query,
                    "type": "video", "order": "date", "maxResults": 25,
                    "publishedAfter": published_after, "videoDuration": d},
            timeout=30)
        r.raise_for_status()
        items += r.json().get("items", [])
    return items


def get_video_details(video_ids):
    if not video_ids:
        return {}
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"key": YOUTUBE_API_KEY,
                "part": "contentDetails,statistics,snippet",
                "id": ",".join(video_ids[:50])}, timeout=30)
    r.raise_for_status()
    return {it["id"]: it for it in r.json().get("items", [])}


def parse_duration(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def match_person(text):
    t = text.lower()
    for name, aliases in PERSONS.items():
        for a in [name.lower()] + aliases:
            if a and a in t:
                return name
    return None


def hard_filter(item, detail):
    title = item["snippet"]["title"].lower()
    channel = item["snippet"]["channelTitle"].lower()
    desc = (detail.get("snippet", {}).get("description") or "").lower()

    person = match_person(title) or match_person(desc[:500])
    if not person:
        return None, "인물명 없음"
    for b in TITLE_BLACKLIST:
        if b in title:
            return None, f"제목 블랙리스트: {b}"
    for p in CHANNEL_BLACKLIST_PATTERNS:
        if re.search(p, channel):
            return None, f"채널 블랙리스트: {p}"
    dur = parse_duration(detail.get("contentDetails", {}).get("duration"))
    min_dur = CORE_MIN_DURATION_SEC if person in CORE_PERSONS else MIN_DURATION_SEC
    if dur < min_dur:
        return None, f"길이 미달 ({dur//60}분 < {min_dur//60}분)"
    return person, None


CELEB_PROMPT = """다음 유튜브 영상들 각각을 조건에 따라 엄격히 판정하라.

공통 조건 — 해당 인물 '본인'이 직접 출연(인터뷰/대담/키노트/팟캐스트)해야 함.
  · 제3자가 그 인물을 논평/분석/요약하는 영상 → direct_appearance=false
  · AI 음성, 클립 짜깁기, 자막 번역 재업로드 → direct_appearance=false

모드별 주제 조건:
  [일반] AI 수요/토큰 소비, 메모리(HBM/DRAM/NAND), 컴퓨팅 인프라/GPU/데이터센터/capex
         중 하나를 실질적으로 다뤄야 함. 일반 AI 잡담·제품 홍보·커리어 얘기뿐이면 5 이하.
  [주제무관] 본인 직접 출연만 확인되면 relevance_score 8 이상 부여.
  [엄격] 위 주제가 영상의 핵심이어야 함. 스치듯 언급이면 탈락.
         정치/우주/자동차/소셜미디어 주제는 무조건 탈락(3 이하).

영상 목록:
{items}

출력: JSON 배열만. 마크다운 금지. 입력과 같은 개수, 같은 순서.
[{{"idx": 0, "direct_appearance": true, "relevance_score": 0, "reason": "한 문장", "summary_kr": "한 줄 요약(한국어)"}}]"""


def judge_celeb_batch(chunk):
    """chunk: [(person, item, detail, vid)] → [judgment|None]"""
    lines = []
    for i, (person, item, detail, _vid) in enumerate(chunk):
        mode = ("주제무관" if person in TOPIC_FREE_PERSONS
                else "엄격" if person in STRICT_PERSONS else "일반")
        desc = (detail.get("snippet", {}).get("description") or "")[:600]
        lines.append(
            f"[{i}] 인물: {person} | 모드: {mode}\n"
            f"제목: {item['snippet']['title']}\n"
            f"채널: {item['snippet']['channelTitle']}\n"
            f"설명: {strip_html(desc)}")
    out = gemini_call(CELEB_PROMPT.format(items="\n\n".join(lines)))
    return parse_json_array(out, len(chunk))


def send_telegram_celeb(person, item, judge, video_id):
    msg = (f"🎙 <b>{html.escape(person)}</b> 출연 감지\n"
           f"📺 {html.escape(item['snippet']['channelTitle'])}\n"
           f"<b>{html.escape(item['snippet']['title'])}</b>\n"
           f"💡 {html.escape(judge.get('summary_kr',''))}\n"
           f"점수: {judge.get('relevance_score','?')}/10\n"
           f"https://youtu.be/{video_id}")
    send_tg(msg)


def run_celeb_watch():
    seen = load_seen()
    published_after = (datetime.now(timezone.utc)
                       - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = {}
    for q, inc_med in SEARCH_BATCHES:
        try:
            for it in yt_search(q, published_after, inc_med):
                vid = it["id"]["videoId"]
                if vid not in seen:
                    candidates[vid] = it
        except Exception as e:
            print(f"검색 실패 ({q}): {str(e)[:150]}")
        time.sleep(1)

    print(f"신규 후보: {len(candidates)}건")
    if not candidates:
        save_seen(seen)
        if NOTIFY_WHEN_EMPTY:
            send_tg("🔍 새로운 인터뷰 없음 (이번 주기)")
        return

    details = get_video_details(list(candidates.keys()))

    # 1단계: 하드필터 (Gemini 호출 없음)
    passed = []
    for vid, item in candidates.items():
        detail = details.get(vid, {})
        person, reject = hard_filter(item, detail)
        if not person:
            seen.add(vid)   # 하드필터 탈락은 재검토 가치 없음
            print(f"❌ [{reject}] {item['snippet']['title'][:60]}")
            continue
        passed.append((person, item, detail, vid))

    passed = passed[:MAX_CELEB_CANDIDATES]
    nb = (len(passed) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"[셀럽] Gemini 판정 대상 {len(passed)}건 → 배치 {nb}회")

    sent = 0
    for i in range(0, len(passed), BATCH_SIZE):
        chunk = passed[i:i + BATCH_SIZE]
        results = judge_celeb_batch(chunk)
        for (person, item, detail, vid), j in zip(chunk, results):
            if j is None:
                print(f"  ⚠ 판정실패(다음사이클 재시도): {item['snippet']['title'][:50]}")
                continue
            seen.add(vid)
            channel = item["snippet"]["channelTitle"].lower()
            trusted = any(t in channel for t in TRUSTED_CHANNELS)
            if person in STRICT_PERSONS:
                th = STRICT_SCORE
            else:
                th = SCORE_THRESHOLD - 1 if trusted else SCORE_THRESHOLD
            score = j.get("relevance_score", 0)
            if j.get("direct_appearance") and score >= th:
                send_telegram_celeb(person, item, j, vid)
                sent += 1
                print(f"  ✅ {person} - {item['snippet']['title'][:55]}")
            else:
                print(f"  ❌ [{score}점/{th} {j.get('reason','')[:40]}] "
                      f"{item['snippet']['title'][:45]}")
        time.sleep(2)

    save_seen(seen)
    if sent == 0 and NOTIFY_WHEN_EMPTY:
        send_tg(f"🔍 후보 {len(candidates)}건 검토했으나 조건 충족 영상 없음")
    print(f"[셀럽] 완료: {sent}건 전송")


# ═══════════════════════════════════════════════════════════
# PART 3 — 네이버 블로그 감시
# ═══════════════════════════════════════════════════════════
NAVER_BLOG_IDS = [
    "richyun0108", "cybermw", "hardark",
    "kk_kontemp", "tmdejr1267", "engineerinvestor",
]
SEEN_BLOG_FILE = "seen_twitter_blog.json"


def load_blog_state():
    try:
        with open(SEEN_BLOG_FILE, encoding="utf-8") as f:
            d = json.load(f)
            d.setdefault("blog", {})
            return d
    except Exception:
        return {"blog": {}}


def save_blog_state(state):
    with open(SEEN_BLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_blog_posts(blog_id):
    try:
        resp = requests.get(f"https://rss.blog.naver.com/{blog_id}.xml",
                            headers={"User-Agent": UA}, timeout=20)
        if resp.status_code != 200:
            print(f"[블로그 오류] {blog_id}: HTTP {resp.status_code}")
            return []
        feed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"[블로그 오류] {blog_id}: {e}")
        return []

    posts = []
    for entry in feed.entries[:10]:
        raw = entry.get("link", "")
        clean = raw.split("?")[0].rstrip("/")
        pub = entry.get("published", "") or entry.get("updated", "")
        title = entry.get("title", "(제목 없음)")
        posts.append({"id": clean or f"{blog_id}:{title}:{pub}",
                      "title": title, "url": clean or raw})
    print(f"[블로그] {blog_id}: {len(posts)}건")
    return posts


def run_blog_watch():
    try:
        state = load_blog_state()
        seen = state.setdefault("blog", {})
        total = 0
        for blog_id in NAVER_BLOG_IDS:
            posts = fetch_blog_posts(blog_id)
            if not posts:
                continue
            first = blog_id not in seen
            already = set(seen.get(blog_id, []))
            fresh = [p for p in posts if p["id"] and p["id"] not in already]
            if first:
                print(f"[블로그] {blog_id}: baseline 저장 (알림 생략)")
            else:
                for p in reversed(fresh):
                    send_tg(f"📝 <b>{html.escape(blog_id)}</b> 새 글\n\n"
                            f"{html.escape(p['title'])}\n\n{p['url']}")
                    total += 1
                    time.sleep(0.5)
            seen[blog_id] = list(dict.fromkeys(
                [p["id"] for p in posts if p["id"]] + list(already)))[:50]
        print(f"[블로그] {total}건 전송")
        save_blog_state(state)
    except Exception as e:
        print(f"[블로그 감시 실패] {str(e)[:200]}")


# ═══════════════════════════════════════════════════════════
# PART 4 — 크레딧 / 사모대출 / AI 캐팩스 금융 감시
# ═══════════════════════════════════════════════════════════
CREDIT_STATE_FILE = "seen_credit.json"
FEED_CACHE_VERSION = 2

ENABLE_PODCAST = True
ENABLE_CREDIT_YT = False    # 유튜브 API 쿼터 절약 위해 기본 OFF
ENABLE_NEWS = True

PODCAST_MAX_AGE_DAYS = 5
NEWS_LOOKBACK_HOURS = 24
CREDIT_SCORE_THRESHOLD = 7
CREDIT_MAX_SEND = 8
CREDIT_MAX_CANDIDATES = 24

PODCASTS = [
    {"name": "The Credit Edge", "apple_id": "1674628050"},
    {"name": "Odd Lots",         "search": "Odd Lots Bloomberg",       "verify": "odd lots"},
    {"name": "Money Stuff",      "search": "Money Stuff Matt Levine",  "verify": "money stuff"},
    {"name": "GS Exchanges",     "search": "Goldman Sachs Exchanges",  "verify": "exchanges"},
    {"name": "Behind the Money", "search": "Behind the Money FT",      "verify": "behind the money"},
    {"name": "Unhedged",         "search": "Unhedged Financial Times", "verify": "unhedged"},
    {"name": "BG2Pod",           "search": "BG2Pod Gerstner Gurley",   "verify": "bg2"},
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

CREDIT_YT_QUERIES = [
    '"private credit" ("data center"|"data centre"|AI)',
    'hyperscaler debt "bond issuance" OR "credit spread"',
]

NEWS_QUERIES = [
    '"private credit" "data center"',
    'hyperscaler bond issuance debt',
    '"data center" debt financing spreads',
    '"AI capex" credit market',
    'HBM pricing contract negotiation',
]

CREDIT_YT_CHANNEL_BLACK = [r"주식", r"투자", r"코인", r"경제tv", r"클립", r"쇼츠", r"shorts"]


def load_credit_state():
    try:
        with open(CREDIT_STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("podcast", {})
    s.setdefault("feeds", {})
    s.setdefault("youtube", [])
    s.setdefault("news", [])
    if s.get("feed_ver") != FEED_CACHE_VERSION:
        print("[캐시] 피드 캐시 재해석")
        s["feeds"] = {}
        s["feed_ver"] = FEED_CACHE_VERSION
    return s


def save_credit_state(s):
    s["youtube"] = s["youtube"][-1500:]
    s["news"] = s["news"][-1500:]
    for k in s["podcast"]:
        s["podcast"][k] = s["podcast"][k][:60]
    with open(CREDIT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def keyword_hit(text):
    t = (text or "").lower()
    return any(k in t for k in CREDIT_KEYWORDS)


def resolve_feed(pod, state):
    name = pod["name"]
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
                       if verify in (x.get("collectionName") or "").lower()]
        if not results:
            print(f"[팟캐스트] {name}: 해석 실패 → apple_id 직접 지정 필요")
            return None
        best = results[0]
        print(f"[팟캐스트] {name} → {best.get('collectionName')}")
        state["feeds"][name] = best["feedUrl"]
        return best["feedUrl"]
    except Exception as e:
        print(f"[팟캐스트] {name} 해석 오류: {str(e)[:150]}")
        return None


def fetch_episodes(feed_url, name):
    try:
        r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=25)
        if r.status_code != 200:
            print(f"[팟캐스트] {name}: HTTP {r.status_code}")
            return []
        d = feedparser.parse(r.content)
    except Exception as e:
        print(f"[팟캐스트] {name}: 피드 오류 {str(e)[:120]}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=PODCAST_MAX_AGE_DAYS)
    out, old = [], 0
    for e in d.entries[:15]:
        eid = e.get("id") or e.get("guid") or e.get("link", "")
        if not eid:
            continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if pub and datetime(*pub[:6], tzinfo=timezone.utc) < cutoff:
            old += 1
            continue
        desc = e.get("summary", "") or ""
        if e.get("content"):
            desc = e["content"][0].get("value", desc)
        out.append({"id": eid, "title": e.get("title", "(제목없음)"),
                    "url": e.get("link", ""), "desc": desc})
    print(f"[팟캐스트] {name}: 최근 {PODCAST_MAX_AGE_DAYS}일 {len(out)}건 (기간초과 {old})")
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
                print(f"  ⏭ [키워드 없음] {ep['title'][:55]}")
                continue
            cands.append({"kind": "팟캐스트", "icon": "🎧", "source": name,
                          "title": ep["title"], "body": ep["desc"],
                          "url": ep["url"], "seen_key": None})
    return cands


def collect_credit_youtube(state):
    after = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = set(state["youtube"])
    cand = {}
    for q in CREDIT_YT_QUERIES:
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
            print(f"[크레딧YT] 검색 실패: {str(e)[:120]}")
        time.sleep(1)
    if not cand:
        return []
    try:
        det = get_video_details(list(cand.keys()))
    except Exception as e:
        print(f"[크레딧YT] 상세조회 실패: {str(e)[:120]}")
        return []

    out = []
    for vid, it in cand.items():
        title = it["snippet"]["title"]
        ch = it["snippet"]["channelTitle"]
        d = det.get(vid, {})
        desc = d.get("snippet", {}).get("description", "")
        if any(re.search(p, ch.lower()) for p in CREDIT_YT_CHANNEL_BLACK) \
           or parse_duration(d.get("contentDetails", {}).get("duration")) < 600 \
           or not keyword_hit(f"{title} {desc[:600]}"):
            state["youtube"].append(vid)
            continue
        out.append({"kind": "유튜브", "icon": "📺", "source": ch, "title": title,
                    "body": desc, "url": f"https://youtu.be/{vid}",
                    "seen_key": ("youtube", vid)})
    print(f"[크레딧YT] 판정 대상 {len(out)}건")
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
            print(f"[뉴스] 실패 ({q}): {str(e)[:120]}")
            continue
        for e in d.entries[:6]:
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


CREDIT_PROMPT = """너는 반도체/AI 인프라 투자자를 위한 콘텐츠 선별 에이전트다.
아래 콘텐츠 각각이 다음 관심사에 실질적으로 부합하는지 엄격히 판정하라.

관심사:
1. 하이퍼스케일러(MS/구글/아마존/메타/오라클) 자금조달 — 회사채 발행, 스프레드, 신용등급, 듀레이션
2. 데이터센터 사모대출/사모크레딧 — 대출 조건(LTV, advance rate, 스프레드), 터미널 밸류
3. AI 캐팩스의 재무적 지속가능성, 벤더 파이낸싱, 자산유동화(ABS)
4. 메모리(HBM/DRAM/NAND) 가격·계약 협상·선급금
5. 위 주제로 업계 실무자(운용사, 대출기관, CFO, 애널리스트)가 직접 발언하는 인터뷰/대담

탈락: 일반 AI 기술·제품 소개, 스치듯 언급, 개인투자 채널의 종목추천/시황요약, 광고

콘텐츠 목록:
{items}

출력: JSON 배열만. 마크다운 금지. 입력과 같은 개수, 같은 순서.
[{{"idx": 0, "relevance_score": 0, "summary_kr": "핵심 3줄 이내 한국어 요약, 숫자·고유명사 유지", "key_points": ["최대 3개"]}}]"""


def judge_credit_batch(chunk):
    blob = "\n\n".join(
        f"[{i}] 종류: {c['kind']}\n제목: {c['title']}\n출처: {c['source']}\n"
        f"설명: {strip_html(c['body'])[:700]}"
        for i, c in enumerate(chunk))
    out = gemini_call(CREDIT_PROMPT.format(items=blob))
    return parse_json_array(out, len(chunk))


def run_credit_watch():
    try:
        state = load_credit_state()
        cands = []
        if ENABLE_PODCAST:
            print("── 팟캐스트 ──")
            cands += collect_podcast(state)
        if ENABLE_CREDIT_YT:
            print("── 크레딧 유튜브 ──")
            cands += collect_credit_youtube(state)
        if ENABLE_NEWS:
            print("── 뉴스 ──")
            cands += collect_news(state)

        cands = cands[:CREDIT_MAX_CANDIDATES]
        nb = (len(cands) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"[크레딧] 판정 대상 {len(cands)}건 → 배치 {nb}회")

        sent = 0
        for i in range(0, len(cands), BATCH_SIZE):
            chunk = cands[i:i + BATCH_SIZE]
            for c, j in zip(chunk, judge_credit_batch(chunk)):
                if j is None:
                    print(f"  ⚠ 판정실패(재시도 대상): {c['title'][:50]}")
                    continue
                if c["seen_key"]:
                    state[c["seen_key"][0]].append(c["seen_key"][1])
                score = j.get("relevance_score", 0)
                if score >= CREDIT_SCORE_THRESHOLD and sent < CREDIT_MAX_SEND:
                    pts = "".join(f"\n • {html.escape(str(p))}"
                                  for p in (j.get("key_points") or [])[:3])
                    send_tg(f"{c['icon']} <b>{html.escape(c['kind'])}</b> · "
                            f"{html.escape(c['source'])}\n"
                            f"<b>{html.escape(c['title'])}</b>\n\n"
                            f"💡 {html.escape(j.get('summary_kr',''))}{pts}\n\n"
                            f"점수: {score}/10\n{c['url']}")
                    sent += 1
                    print(f"  ✅ [{score}] {c['source']} - {c['title'][:50]}")
                else:
                    print(f"  ❌ [{score}] {c['title'][:50]}")
            time.sleep(2)

        save_credit_state(state)
        print(f"[크레딧] 전송 {sent}건")
    except Exception as e:
        print(f"[크레딧 감시 실패] {str(e)[:250]}")


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════
def main():
    try:
        run_celeb_watch()
    except Exception as e:
        print(f"[셀럽 감시 실패] {str(e)[:250]}")

    run_blog_watch()      # Gemini 미사용 — 항상 실행
    run_credit_watch()    # 남은 Gemini 예산으로 실행

    print(f"=== Gemini 총 호출 {_gm['n']}회 / 상한 {MAX_GEMINI_CALLS} ===")


if __name__ == "__main__":
    main()
