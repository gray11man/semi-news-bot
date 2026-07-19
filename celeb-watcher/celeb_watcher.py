# -*- coding: utf-8 -*-
"""
AI 업계 유명인사 유튜브 출연 감시 봇 + 네이버블로그 감시
- YouTube Data API로 인물별 신규 영상 검색
- 3단계 노이즈 필터: 하드필터 -> 채널필터 -> Gemini Flash 판정
- 통과한 것만 텔레그램 전송
- [NEW] 네이버 블로그 4개 6시간 주기 감시 -> 텔레그램 전송
  (트위터 감시는 비용 문제로 비활성화)

필요 시크릿:
  YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os, json, re, time, html
from datetime import datetime, timedelta, timezone
import requests
import feedparser

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen_celeb_ids.json"
LOOKBACK_HOURS = 8          # 워크플로 주기보다 여유있게
MIN_DURATION_SEC = 1800     # 일반 인물: 30분 미만 제외
CORE_MIN_DURATION_SEC = 240 # 핵심 인물: 쇼츠/클립(4분 미만)만 제외

# 핵심 인물: 길이 무관 통과 (30분 컷 미적용)
CORE_PERSONS = {
    "Jensen Huang", "Sam Altman", "Sarah Friar", "Mira Murati",
    "Ilya Sutskever", "Dario Amodei", "Daniela Amodei",
    "Sundar Pichai", "Satya Nadella", "Lisa Su", "Mark Zuckerberg",
    "Sanjay Mehrotra", "Aravind Srinivas",
    "Jakub Pachocki", "Kevin Weil", "Rahul Patil", "Krishna Rao",
    "Greg Brockman", "Eric Lefkofsky",
}
GEMINI_MODEL = "gemini-3.5-flash"
SCORE_THRESHOLD = 7         # Gemini 관련성 점수 컷

# ── 감시 대상 인물 (이름 + 별칭) ──────────────────────────
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
    "Aravind Srinivas":["aravind srinivas"],
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
    # OpenAI C-suite (CTO 직책 없음 → 수석과학자/CPO로 대체)
    "Jakub Pachocki":  ["jakub pachocki"],
    "Kevin Weil":      ["kevin weil"],
    # Anthropic C-suite
    "Rahul Patil":     ["rahul patil anthropic"],
    "Krishna Rao":     ["krishna rao anthropic"],
    # 업계 CEO/인프라 발언 많은 인물
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

# 엄격 모드: 발언량 많은 인물은 메모리/컴퓨팅 직결 주제만 통과 (점수 9 이상)
STRICT_PERSONS = {"Elon Musk"}
STRICT_SCORE = 9

# 주제 무관 통과 인물: 본인 출연만 확인되면 주제(메모리/컴퓨팅) 상관없이 통과
TOPIC_FREE_PERSONS = {"Eric Lefkofsky"}

# 배치 검색용 그룹 (query, include_medium) — 핵심 인물 포함 배치는 4~20분 영상도 검색
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

# ── 하드 블랙리스트 (제목/채널명에 있으면 즉시 제외) ──────
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

# 신뢰 채널 (있으면 Gemini 점수 -1 완화)
TRUSTED_CHANNELS = [
    "bloomberg", "cnbc", "bg2 pod", "all-in", "lex fridman", "dwarkesh",
    "nvidia", "openai", "anthropic", "microsoft", "google", "20vc",
    "no priors", "a16z", "wsj", "financial times", "the information",
    "stanford", "acquired", "bipartisan", "cheeky pint", "training data",
]

# ═══════════════════════════════════════════════════════════
# [NEW] 네이버 블로그 감시 대상
# ═══════════════════════════════════════════════════════════
NAVER_BLOG_IDS = [
    "richyun0108",
    "cybermw",
    "hardark",
    "kk_kontemp",
    "tmdejr1267",
]

SEEN_TWITTER_BLOG_FILE = "seen_twitter_blog.json"


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-3000:], f)


# ── [NEW] 블로그 상태 로드/저장 (기존 seen과 분리된 별도 파일) ──
def load_tw_blog_state():
    try:
        with open(SEEN_TWITTER_BLOG_FILE, encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("blog", {})
            return data
    except Exception:
        return {"blog": {}}


def save_tw_blog_state(state):
    with open(SEEN_TWITTER_BLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def yt_search(query, published_after, include_medium=False):
    items = []
    durations = ["long"] + (["medium"] if include_medium else [])
    for d in durations:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "key": YOUTUBE_API_KEY, "part": "snippet", "q": query,
                "type": "video", "order": "date", "maxResults": 25,
                "publishedAfter": published_after,
                "videoDuration": d,
            }, timeout=30)
        r.raise_for_status()
        items += r.json().get("items", [])
    return items


def get_video_details(video_ids):
    if not video_ids:
        return {}
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"key": YOUTUBE_API_KEY, "part": "contentDetails,statistics,snippet",
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


def gemini_judge(person, title, channel, desc):
    trusted = any(t in channel.lower() for t in TRUSTED_CHANNELS)
    prompt = f"""다음 유튜브 영상이 조건을 만족하는지 엄격하게 판정하라.

조건:
1. {person} 본인이 직접 출연(인터뷰/대담/키노트/팟캐스트)하는 영상일 것.
   - 제3자가 그 인물에 대해 논평/분석/요약하는 영상은 무조건 탈락.
   - AI 음성, 클립 짜깁기, 자막 번역 재업로드도 탈락.
{"2. 주제 제한 없음 — 본인 직접 출연만 확인되면 relevance_score 8 이상 부여." if person in TOPIC_FREE_PERSONS else
"""2. 영상에서 다음 주제 중 하나를 실질적으로 다룰 것: AI 수요/토큰 소비, 메모리(HBM/DRAM/NAND), 컴퓨팅 인프라/GPU/데이터센터/capex.
   - 제목·설명에 위 주제 관련 단서가 전혀 없고 일반 AI 잡담/제품 홍보/커리어 얘기뿐이면 relevance_score 5 이하로 줄 것."""}
{"3. [엄격] 위 주제가 영상의 핵심이어야 함. 스치듯 언급이면 탈락. 정치/우주/자동차/소셜미디어 주제는 무조건 탈락." if person in STRICT_PERSONS else ""}

영상 정보:
- 제목: {title}
- 채널: {channel}
- 설명: {desc[:800]}

JSON만 출력 (마크다운 금지):
{{"direct_appearance": true/false, "relevance_score": 0~10, "reason": "한 문장", "summary_kr": "한 줄 요약(한국어)"}}"""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.1}},
        timeout=60)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"```json|```", "", text).strip()
    j = json.loads(text)
    if person in STRICT_PERSONS:
        threshold = STRICT_SCORE  # 신뢰 채널 완화 없음
    else:
        threshold = SCORE_THRESHOLD - 1 if trusted else SCORE_THRESHOLD
    ok = j.get("direct_appearance") and j.get("relevance_score", 0) >= threshold
    return ok, j


def send_telegram(person, item, judge, video_id):
    title = html.escape(item["snippet"]["title"])
    channel = html.escape(item["snippet"]["channelTitle"])
    summary = html.escape(judge.get("summary_kr", ""))
    score = judge.get("relevance_score", "?")
    msg = (f"🎙 <b>{html.escape(person)}</b> 출연 감지\n"
           f"📺 {channel}\n"
           f"<b>{title}</b>\n"
           f"💡 {summary}\n"
           f"점수: {score}/10\n"
           f"https://youtu.be/{video_id}")
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
              "disable_web_page_preview": False}, timeout=30)


def send_telegram_text(text):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=30)


# ═══════════════════════════════════════════════════════════
# [NEW] 네이버 블로그 감시 — RSS
# ═══════════════════════════════════════════════════════════
def fetch_blog_posts(blog_id):
    rss_url = f"https://rss.blog.naver.com/{blog_id}.xml"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(rss_url, headers=headers, timeout=20)
        print(f"[블로그 디버그] {blog_id}: HTTP {resp.status_code}, 응답길이 {len(resp.content)}바이트")
        if resp.status_code != 200:
            print(f"[블로그 오류] {blog_id}: HTTP {resp.status_code}")
            return []
        feed = feedparser.parse(resp.content)
        if feed.bozo:
            print(f"[블로그 경고] {blog_id}: 파싱 경고 - {feed.bozo_exception}")
    except Exception as e:
        print(f"[블로그 오류] {blog_id}: {e}")
        return []

    print(f"[블로그 디버그] {blog_id}: entries {len(feed.entries)}건")
    posts = []
    for entry in feed.entries[:10]:
        title = entry.get("title", "(제목 없음)")
        raw_link = entry.get("link", "")
        # 네이버 블로그 링크의 트래킹 쿼리스트링(?fromRss=true&trackingCode=rss 등) 제거
        # -> 매 실행마다 동일한 글이 동일한 ID로 인식되도록 안정화
        clean_link = raw_link.split("?")[0].rstrip("/")
        published = entry.get("published", "") or entry.get("updated", "")
        stable_id = clean_link or f"{blog_id}:{title}:{published}"
        posts.append({
            "id": stable_id,
            "title": title,
            "url": clean_link or raw_link,
        })
    return posts


def check_blogs(state):
    seen = state.setdefault("blog", {})
    new_items = []
    for blog_id in NAVER_BLOG_IDS:
        posts = fetch_blog_posts(blog_id)
        if not posts:
            continue
        is_new_blog = blog_id not in seen  # 이 블로그를 처음 체크하는 경우만 baseline 처리
        already = set(seen.get(blog_id, []))
        fresh = [p for p in posts if p["id"] and p["id"] not in already]
        if not is_new_blog:
            for p in reversed(fresh):
                new_items.append((blog_id, p))
        else:
            print(f"[블로그] {blog_id}: 신규 baseline 저장 (알림 생략)")
        all_ids = [p["id"] for p in posts if p["id"]]
        seen[blog_id] = list(dict.fromkeys(all_ids + list(already)))[:50]
    return new_items


def send_telegram_blog(blog_id, post):
    msg = (f"📝 <b>{html.escape(blog_id)}</b> 새 블로그 글\n\n"
           f"{html.escape(post['title'])}\n\n"
           f"{post['url']}")
    send_telegram_text(msg)


def run_blog_watch():
    """블로그 감시 실행. 실패해도 유튜브 파트에 영향 없도록 예외 격리."""
    try:
        state = load_tw_blog_state()

        new_posts = check_blogs(state)

        for blog_id, p in new_posts:
            send_telegram_blog(blog_id, p)
            time.sleep(0.5)

        print(f"[블로그] {len(new_posts)}건 전송")
        save_tw_blog_state(state)
    except Exception as e:
        print(f"[블로그 감시 전체 실패] {e}")


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════
def main():
    seen = load_seen()
    published_after = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = {}
    for q, inc_med in SEARCH_BATCHES:
        try:
            for it in yt_search(q, published_after, inc_med):
                vid = it["id"]["videoId"]
                if vid not in seen:
                    candidates[vid] = it
        except Exception as e:
            print(f"검색 실패 ({q}): {e}")
        time.sleep(1)

    print(f"신규 후보: {len(candidates)}건")
    if not candidates:
        save_seen(seen)
        send_telegram_text("🔍 새로운 인터뷰 없음 (이번 주기)")
    else:
        details = get_video_details(list(candidates.keys()))
        sent = 0
        for vid, item in candidates.items():
            seen.add(vid)
            detail = details.get(vid, {})
            person, reject = hard_filter(item, detail)
            if not person:
                print(f"❌ [{reject}] {item['snippet']['title'][:60]}")
                continue
            try:
                ok, judge = gemini_judge(
                    person, item["snippet"]["title"],
                    item["snippet"]["channelTitle"],
                    detail.get("snippet", {}).get("description", ""))
            except Exception as e:
                print(f"Gemini 오류: {e}")
                continue
            if ok:
                send_telegram(person, item, judge, vid)
                sent += 1
                print(f"✅ 전송: {person} - {item['snippet']['title'][:60]}")
            else:
                print(f"❌ [Gemini 탈락 {judge.get('relevance_score')}점, "
                      f"{judge.get('reason','')}] {item['snippet']['title'][:60]}")
            time.sleep(1.5)

        save_seen(seen)
        if sent == 0:
            send_telegram_text(f"🔍 후보 {len(candidates)}건 검토했으나 조건 충족 영상 없음")
        print(f"완료: {sent}건 전송")

    # [NEW] 네이버 블로그 감시 (유튜브 파트와 독립적으로 실행)
    run_blog_watch()


if __name__ == "__main__":
    main()
