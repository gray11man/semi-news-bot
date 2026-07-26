# -*- coding: utf-8 -*-
"""
하이퍼스케일러 / 사모크레딧 / AI 캐팩스 금융 감시 모듈
- 팟캐스트 RSS (Credit Edge, Odd Lots 등) 신규 에피소드 감지
- 유튜브 주제 검색 (인물 기반이 아닌 키워드 기반)
- 구글 뉴스 RSS

celeb_watcher.py 에서 아래처럼 호출:
    from credit_watch import run_credit_watch
    ...
    run_credit_watch()          # main() 마지막 줄에 추가

필요 시크릿: 기존과 동일 (YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
새로 생기는 상태파일: seen_credit.json  ← 워크플로 git add 에 반드시 추가할 것
"""
import os, json, re, time, html
from datetime import datetime, timedelta, timezone
import requests
import feedparser

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
GEMINI_API_KEY = os.environ["GEMINI_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

GEMINI_MODEL = "gemini-3.5-flash"
STATE_FILE = "seen_credit.json"

# ── 모듈별 on/off ─────────────────────────────────────────
ENABLE_PODCAST = True
ENABLE_YOUTUBE = True
ENABLE_NEWS = True

# 팟캐스트: 최근 N일 이내 에피소드만 (첫 실행 폭탄 방지용 2차 안전장치)
PODCAST_MAX_AGE_DAYS = 5
YT_LOOKBACK_HOURS = 36
YT_MIN_DURATION_SEC = 600      # 10분 미만 제외
NEWS_LOOKBACK_HOURS = 24

SCORE_THRESHOLD = 7            # Gemini 관련성 컷
MAX_SEND_PER_RUN = 12          # 한 사이클 최대 전송 (폭주 방지)

# ═══════════════════════════════════════════════════════════
# 감시 대상 팟캐스트
#   apple_id 가 정확함. 모르면 search 만 넣어도 iTunes API가 찾아줌.
#   (애플 팟캐스트 URL 뒤 id숫자 = apple_id)
# ═══════════════════════════════════════════════════════════
PODCASTS = [
    {"name": "The Credit Edge",      "apple_id": "1674628050"},   # 확인됨
    {"name": "Odd Lots",             "search": "Odd Lots Bloomberg"},
    {"name": "Money Stuff",          "search": "Money Stuff Matt Levine"},
    {"name": "Goldman Sachs Exchanges", "search": "Goldman Sachs Exchanges"},
    {"name": "Behind the Money",     "search": "Behind the Money Financial Times"},
    {"name": "Unhedged",             "search": "Unhedged Financial Times"},
    {"name": "BG2Pod",               "search": "BG2Pod Brad Gerstner Bill Gurley"},
]

# ── 1차 키워드 필터 (Gemini 호출 전 저비용 게이트) ─────────
CREDIT_KEYWORDS = [
    # 금융/조달
    "private credit", "private debt", "direct lending", "securitization",
    "securitisation", "abs", "asset-backed", "bond issuance", "debt financing",
    "credit spread", "spreads", "investment grade", "high yield", "leverage",
    "loan", "lender", "underwriting", "refinanc", "duration", "issuance",
    "vendor financing", "sale-leaseback", "covenant", "downgrade", "rating",
    # AI 인프라
    "data center", "datacenter", "data centre", "hyperscaler", "capex",
    "capital expenditure", "neocloud", "gpu", "compute", "ai infrastructure",
    "power", "grid",
    # 메모리
    "memory", "hbm", "dram", "nand", "semiconductor", "chip",
    # 기업명
    "nvidia", "microsoft", "amazon", "alphabet", "google", "meta", "oracle",
    "coreweave", "broadcom", "micron", "sk hynix", "samsung", "tsmc",
    "blackstone", "apollo", "ares", "kkr", "wellington", "pimco",
]

# ── 유튜브 주제 검색 쿼리 ─────────────────────────────────
YT_QUERIES = [
    '"private credit" ("data center"|"data centre"|AI)',
    'hyperscaler debt "bond issuance" OR "credit spread"',
    '"data center" financing securitization ABS interview',
    '"AI capex" credit market bonds interview',
    '"data center" debt "private credit" panel',
]

# ── 구글 뉴스 RSS 쿼리 ────────────────────────────────────
NEWS_QUERIES = [
    '"private credit" "data center"',
    'hyperscaler bond issuance debt',
    '"data center" debt financing spreads',
    '"AI capex" credit market',
    'HBM pricing contract negotiation',
]


# ═══════════════════════════════════════════════════════════
# 상태 저장
# ═══════════════════════════════════════════════════════════
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("podcast", {})   # {podcast_name: [episode_id, ...]}
    s.setdefault("feeds", {})     # {podcast_name: resolved_feed_url}
    s.setdefault("youtube", [])   # [video_id, ...]
    s.setdefault("news", [])      # [link, ...]
    return s


def save_state(s):
    s["youtube"] = s["youtube"][-1500:]
    s["news"] = s["news"][-1500:]
    for k in s["podcast"]:
        s["podcast"][k] = s["podcast"][k][:60]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# 공용 유틸
# ═══════════════════════════════════════════════════════════
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def keyword_hit(text):
    t = (text or "").lower()
    return [k for k in CREDIT_KEYWORDS if k in t]


def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=30)
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


def gemini_judge(kind, title, source, body, url=""):
    """kind: 'podcast' | 'youtube' | 'news'"""
    prompt = f"""너는 반도체/AI 인프라 투자자를 위한 콘텐츠 선별 에이전트다.
아래 콘텐츠가 다음 관심사에 실질적으로 부합하는지 엄격히 판정하라.

관심사:
1. 하이퍼스케일러(MS/구글/아마존/메타/오라클)의 자금조달 — 회사채 발행, 스프레드, 신용등급, 듀레이션
2. 데이터센터 관련 사모대출/사모크레딧 — 대출 조건(LTV, advance rate, 스프레드), 터미널 밸류
3. AI 캐팩스의 재무적 지속 가능성, 벤더 파이낸싱, 자산유동화(ABS)
4. 메모리(HBM/DRAM/NAND) 가격·계약 협상·선급금
5. 위 주제로 업계 실무자(운용사, 대출기관, CFO, 애널리스트)가 직접 발언하는 인터뷰/대담

탈락 기준:
- 일반 AI 기술/제품 소개, 모델 성능 얘기뿐인 것
- 위 주제를 스치듯 언급만 하는 것
- 개인 투자 유튜브의 종목 추천/시황 요약
- 광고, 홍보, 클립 짜깁기

콘텐츠 종류: {kind}
제목: {title}
출처: {source}
본문/설명: {strip_html(body)[:1500]}

JSON만 출력 (마크다운 코드블록 금지):
{{"relevance_score": 0~10,
  "reason": "한 문장 판정 이유(한국어)",
  "summary_kr": "핵심 3줄 이내 한국어 요약. 숫자·고유명사는 반드시 살릴 것",
  "key_points": ["핵심 포인트 최대 3개(한국어)"]}}"""
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.1}},
            timeout=60)
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        txt = re.sub(r"```json|```", "", txt).strip()
        j = json.loads(txt)
        return j.get("relevance_score", 0) >= SCORE_THRESHOLD, j
    except Exception as e:
        print(f"[Gemini 오류] {e}")
        return False, {}


def fmt_msg(icon, kind_label, source, title, judge, url):
    pts = judge.get("key_points") or []
    pts_txt = "".join(f"\n • {html.escape(str(p))}" for p in pts[:3])
    return (f"{icon} <b>{html.escape(kind_label)}</b> · {html.escape(source)}\n"
            f"<b>{html.escape(title)}</b>\n\n"
            f"💡 {html.escape(judge.get('summary_kr', ''))}"
            f"{pts_txt}\n\n"
            f"점수: {judge.get('relevance_score', '?')}/10\n{url}")


# ═══════════════════════════════════════════════════════════
# 1) 팟캐스트
# ═══════════════════════════════════════════════════════════
def resolve_feed(pod, state):
    """iTunes API로 RSS 주소 해석. 결과는 상태파일에 캐시."""
    name = pod["name"]
    if pod.get("feed"):
        return pod["feed"]
    cached = state["feeds"].get(name)
    if cached:
        return cached
    try:
        if pod.get("apple_id"):
            r = requests.get("https://itunes.apple.com/lookup",
                             params={"id": pod["apple_id"], "entity": "podcast"},
                             timeout=20)
        else:
            r = requests.get("https://itunes.apple.com/search",
                             params={"term": pod["search"], "entity": "podcast",
                                     "limit": 5},
                             timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
        results = [x for x in results if x.get("feedUrl")]
        if not results:
            print(f"[팟캐스트] {name}: 피드 해석 실패")
            return None
        # search 모드면 이름 유사도 우선
        best = results[0]
        if not pod.get("apple_id"):
            for x in results:
                if name.lower() in (x.get("collectionName", "").lower()):
                    best = x
                    break
        feed = best["feedUrl"]
        print(f"[팟캐스트] {name} → {best.get('collectionName')} | {feed}")
        state["feeds"][name] = feed
        return feed
    except Exception as e:
        print(f"[팟캐스트] {name} 해석 오류: {e}")
        return None


def fetch_episodes(feed_url):
    try:
        r = requests.get(feed_url, headers={"User-Agent": UA}, timeout=25)
        if r.status_code != 200:
            print(f"[팟캐스트] HTTP {r.status_code}: {feed_url}")
            return []
        d = feedparser.parse(r.content)
    except Exception as e:
        print(f"[팟캐스트] 피드 오류 {feed_url}: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=PODCAST_MAX_AGE_DAYS)
    out = []
    for e in d.entries[:15]:
        eid = e.get("id") or e.get("guid") or e.get("link", "")
        if not eid:
            continue
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if pub:
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
        desc = e.get("summary", "") or ""
        if e.get("content"):
            desc = e["content"][0].get("value", desc)
        out.append({
            "id": eid,
            "title": e.get("title", "(제목 없음)"),
            "url": e.get("link", ""),
            "desc": desc,
        })
    return out


def run_podcast(state, budget):
    sent = 0
    for pod in PODCASTS:
        if sent >= budget:
            break
        name = pod["name"]
        feed = resolve_feed(pod, state)
        if not feed:
            continue
        eps = fetch_episodes(feed)
        if not eps:
            continue

        first_time = name not in state["podcast"]
        seen = set(state["podcast"].get(name, []))
        fresh = [e for e in eps if e["id"] not in seen]

        # 첫 등록 시엔 baseline만 저장하고 알림 생략
        if first_time:
            print(f"[팟캐스트] {name}: baseline {len(eps)}건 저장 (알림 생략)")
        else:
            for ep in reversed(fresh):
                if sent >= budget:
                    break
                blob = f"{ep['title']} {strip_html(ep['desc'])}"
                hits = keyword_hit(blob)
                if not hits:
                    print(f"  ⏭ [키워드 없음] {ep['title'][:60]}")
                    continue
                ok, j = gemini_judge("팟캐스트", ep["title"], name, ep["desc"])
                if ok:
                    send_tg(fmt_msg("🎧", "팟캐스트", name, ep["title"], j, ep["url"]))
                    sent += 1
                    print(f"  ✅ {name} - {ep['title'][:60]}")
                else:
                    print(f"  ❌ [{j.get('relevance_score')}점] {ep['title'][:60]}")
                time.sleep(1.2)

        state["podcast"][name] = list(dict.fromkeys(
            [e["id"] for e in eps] + list(seen)))
    return sent


# ═══════════════════════════════════════════════════════════
# 2) 유튜브 주제 검색
# ═══════════════════════════════════════════════════════════
def yt_search_raw(q, published_after):
    r = requests.get("https://www.googleapis.com/youtube/v3/search",
                     params={"key": YOUTUBE_API_KEY, "part": "snippet", "q": q,
                             "type": "video", "order": "date", "maxResults": 20,
                             "publishedAfter": published_after},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def yt_details(ids):
    if not ids:
        return {}
    r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                     params={"key": YOUTUBE_API_KEY,
                             "part": "contentDetails,snippet",
                             "id": ",".join(ids[:50])}, timeout=30)
    r.raise_for_status()
    return {it["id"]: it for it in r.json().get("items", [])}


def dur_sec(iso):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


YT_CHANNEL_BLACK = [r"주식", r"투자", r"코인", r"경제tv", r"클립", r"쇼츠", r"shorts"]


def run_youtube(state, budget):
    if not YOUTUBE_API_KEY:
        print("[유튜브] API 키 없음 - 건너뜀")
        return 0
    after = (datetime.now(timezone.utc) - timedelta(hours=YT_LOOKBACK_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = set(state["youtube"])
    cand = {}
    for q in YT_QUERIES:
        try:
            for it in yt_search_raw(q, after):
                vid = it["id"]["videoId"]
                if vid not in seen:
                    cand[vid] = it
        except Exception as e:
            print(f"[유튜브] 검색 실패 ({q}): {e}")
        time.sleep(1)

    print(f"[유튜브] 신규 후보 {len(cand)}건")
    if not cand:
        return 0

    details = yt_details(list(cand.keys()))
    sent = 0
    for vid, it in cand.items():
        if sent >= budget:
            break
        title = it["snippet"]["title"]
        channel = it["snippet"]["channelTitle"]
        det = details.get(vid, {})
        desc = det.get("snippet", {}).get("description", "")

        if any(re.search(p, channel.lower()) for p in YT_CHANNEL_BLACK):
            state["youtube"].append(vid)
            continue
        if dur_sec(det.get("contentDetails", {}).get("duration")) < YT_MIN_DURATION_SEC:
            state["youtube"].append(vid)
            continue
        if not keyword_hit(f"{title} {desc[:600]}"):
            state["youtube"].append(vid)
            continue

        ok, j = gemini_judge("유튜브 영상", title, channel, desc)
        # Gemini 실패 시엔 seen 처리하지 않고 다음 사이클에 재시도
        if not j:
            continue
        state["youtube"].append(vid)
        if ok:
            send_tg(fmt_msg("📺", "유튜브", channel, title, j,
                            f"https://youtu.be/{vid}"))
            sent += 1
            print(f"  ✅ {channel} - {title[:60]}")
        else:
            print(f"  ❌ [{j.get('relevance_score')}점] {title[:60]}")
        time.sleep(1.2)
    return sent


# ═══════════════════════════════════════════════════════════
# 3) 구글 뉴스 RSS
# ═══════════════════════════════════════════════════════════
def run_news(state, budget):
    seen = set(state["news"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    sent = 0
    for q in NEWS_QUERIES:
        if sent >= budget:
            break
        try:
            r = requests.get("https://news.google.com/rss/search",
                             params={"q": f"{q} when:2d", "hl": "en-US",
                                     "gl": "US", "ceid": "US:en"},
                             headers={"User-Agent": UA}, timeout=25)
            d = feedparser.parse(r.content)
        except Exception as e:
            print(f"[뉴스] 실패 ({q}): {e}")
            continue

        for e in d.entries[:8]:
            if sent >= budget:
                break
            link = e.get("link", "")
            if not link or link in seen:
                continue
            pub = e.get("published_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) < cutoff:
                continue
            seen.add(link)
            state["news"].append(link)

            title = e.get("title", "")
            source = (e.get("source", {}) or {}).get("title", "Google News")
            ok, j = gemini_judge("뉴스 기사", title, source, e.get("summary", ""))
            if ok:
                send_tg(fmt_msg("📰", "뉴스", source, title, j, link))
                sent += 1
                print(f"  ✅ {source} - {title[:60]}")
            time.sleep(1.2)
        time.sleep(0.5)
    return sent


# ═══════════════════════════════════════════════════════════
# 엔트리포인트
# ═══════════════════════════════════════════════════════════
def run_credit_watch():
    try:
        state = load_state()
        total = 0
        if ENABLE_PODCAST:
            print("── 팟캐스트 감시 ──")
            total += run_podcast(state, MAX_SEND_PER_RUN - total)
        if ENABLE_YOUTUBE:
            print("── 유튜브 주제 감시 ──")
            total += run_youtube(state, MAX_SEND_PER_RUN - total)
        if ENABLE_NEWS:
            print("── 뉴스 감시 ──")
            total += run_news(state, MAX_SEND_PER_RUN - total)
        save_state(state)
        print(f"[크레딧 감시] 총 {total}건 전송")
    except Exception as e:
        print(f"[크레딧 감시 전체 실패] {e}")


if __name__ == "__main__":
    run_credit_watch()
