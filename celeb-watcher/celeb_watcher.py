# -*- coding: utf-8 -*-
"""
AI 업계 유명인사 유튜브 출연 감시 봇
- YouTube Data API로 인물별 신규 영상 검색
- 3단계 노이즈 필터: 하드필터 -> 채널필터 -> Gemini Flash 판정
- 통과한 것만 텔레그램 전송
필요 시크릿: YOUTUBE_API_KEY, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os, json, re, time, html
from datetime import datetime, timedelta, timezone
import requests

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
    "Greg Brockman",
}
GEMINI_MODEL = "gemini-2.0-flash"
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
}

# 엄격 모드: 발언량 많은 인물은 메모리/컴퓨팅 직결 주제만 통과 (점수 9 이상)
STRICT_PERSONS = {"Elon Musk"}
STRICT_SCORE = 9

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
2. 영상에서 다음 주제 중 하나를 실질적으로 다룰 것: AI 수요/토큰 소비, 메모리(HBM/DRAM/NAND), 컴퓨팅 인프라/GPU/데이터센터/capex.
   - 제목·설명에 위 주제 관련 단서가 전혀 없고 일반 AI 잡담/제품 홍보/커리어 얘기뿐이면 relevance_score 5 이하로 줄 것.
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
        return

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
    print(f"완료: {sent}건 전송")


if __name__ == "__main__":
    main()
