# -*- coding: utf-8 -*-
"""
통합 감시 봇 (단일 파일)

PART 1  공통 유틸 / Gemini 호출기
PART 2  AI·반도체·데이터센터 업계 핵심인물의 "직접 출연" 유튜브 감시
PART 3  네이버 블로그 감시
PART 4  하이퍼스케일러 / 사모크레딧 / AI CAPEX 금융 감시

핵심 설계:
- 사람 목록은 넓게 잡는다.
- 채널 화이트리스트를 사용하지 않는다. -> 새 채널/새 팟캐스트를 놓치지 않기 위해서.
- 명백한 쓰레기만 코드로 제거한다.
- 후보를 메타데이터 점수로 우선 정렬한다.
- Gemini가 "본인이 실제로 출연했는가"를 별도로 판정한다.
- direct_appearance + source_originality + evidence가 모두 있어야 알림한다.
- 기존 네이버/크레딧 감시는 유지한다.

필요 시크릿:
YOUTUBE_API_KEY
GEMINI_KEY
TELEGRAM_TOKEN
TELEGRAM_CHAT_ID
"""

import os
import json
import re
import time
import html
import difflib
from datetime import datetime, timedelta, timezone

import requests
import feedparser


# ============================================================
# PART 1 — 공통
# ============================================================

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_KEY"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

GEMINI_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

MAX_GEMINI_CALLS = 8
BATCH_SIZE = 8
NOTIFY_WHEN_EMPTY = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_gm = {"n": 0, "dead": False, "notified": False}


def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": msg[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
    except Exception as e:
        print(f"[텔레그램 실패] {e}")


def _notify_gemini_dead(reason):
    if _gm["notified"]:
        return

    _gm["notified"] = True

    send_tg(
        "⚠️ <b>Gemini 판정 중단</b>\n\n"
        f"사유: {html.escape(str(reason))}\n"
        f"이번 사이클 호출 {_gm['n']}회 / 상한 {MAX_GEMINI_CALLS}\n\n"
        "※ 미판정 항목은 다음 사이클에 자동 재시도됩니다."
    )


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _retry_delay(body):
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)', body or "")
    return int(m.group(1)) if m else None


def gemini_call(prompt, max_retry=2):
    if _gm["dead"]:
        return None

    if _gm["n"] >= MAX_GEMINI_CALLS:
        print(f"[Gemini] 예산 {MAX_GEMINI_CALLS}회 소진")
        _gm["dead"] = True
        _notify_gemini_dead(f"호출 예산 {MAX_GEMINI_CALLS}회 소진")
        return None

    for model in GEMINI_MODELS:
        for attempt in range(max_retry):
            _gm["n"] += 1

            try:
                r = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    params={"key": GEMINI_API_KEY},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.1,
                            "thinkingConfig": {"thinkingBudget": 512},
                            "maxOutputTokens": 4096,
                        },
                    },
                    timeout=90,
                )

                if r.status_code == 429:
                    body = r.text[:500].replace("\n", " ")
                    print(f"[429] {model} attempt{attempt + 1} | {body}")

                    if "PerDay" in body:
                        print("[Gemini] 일일 쿼터 소진")
                        break

                    wait = _retry_delay(body) or (5 * (2 ** attempt))
                    time.sleep(min(wait, 40))
                    continue

                if r.status_code == 404:
                    print(f"[404] 모델 없음: {model}")
                    break

                r.raise_for_status()

                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                txt = re.sub(r"```json|```", "", txt).strip()
                return txt

            except Exception as e:
                print(f"[Gemini 오류] {model}: {str(e)[:180]}")
                time.sleep(3)

        print(f"[Gemini] {model} 실패 → 다음 모델")

    print("[Gemini] 전 모델 실패")
    _gm["dead"] = True
    _notify_gemini_dead("전 모델 응답 실패")
    return None


def parse_json_array(out, n):
    if out is None:
        return [None] * n

    try:
        arr = json.loads(out)

        if not isinstance(arr, list):
            return [None] * n

        res = [None] * n

        for j in arr:
            if not isinstance(j, dict):
                continue

            i = j.get("idx")

            if isinstance(i, int) and 0 <= i < n:
                res[i] = j

        return res

    except Exception as e:
        print(f"[배치 파싱 실패] {str(e)[:150]} | {str(out)[:250]}")
        return [None] * n


# ============================================================
# PART 2 — AI / 반도체 / 데이터센터 핵심인물 유튜브 감시
# ============================================================

SEEN_FILE = "seen_celeb_ids.json"

LOOKBACK_HOURS = 36

# 직접 출연 감시이므로 너무 짧은 영상은 기본적으로 배제.
# 다만 핵심 인물의 공식 키노트/인터뷰는 더 짧아도 후보로 유지.
MIN_DURATION_SEC = 480

# 후보 우선순위용.
PREFERRED_DURATION_SEC = 1200

# 최종 알림은 매우 엄격하게.
SCORE_THRESHOLD = 9
MAX_CELEB_CANDIDATES = 24

# Gemini가 읽는 설명 길이.
DESC_CHARS_FOR_GEMINI = 1500


# ------------------------------------------------------------
# 핵심 인물 목록
# 사람 목록은 "알림 목록"이 아니라 "검색 목록"이다.
# 따라서 넓게 잡는다.
# ------------------------------------------------------------

PERSONS = {
    # NVIDIA
    "Jensen Huang": ["jensen huang", "젠슨 황", "젠슨황"],
    "Colette Kress": ["colette kress"],
    "Jay Puri": ["jay puri nvidia"],

    # OpenAI
    "Sam Altman": ["sam altman", "샘 알트만", "샘 올트먼"],
    "Greg Brockman": ["greg brockman"],
    "Kevin Weil": ["kevin weil"],
    "Jakub Pachocki": ["jakub pachocki"],
    "Sarah Friar": ["sarah friar"],
    "Mark Chen": ["mark chen openai"],

    # Anthropic
    "Dario Amodei": ["dario amodei", "다리오 아모데이"],
    "Daniela Amodei": ["daniela amodei"],
    "Rahul Patil": ["rahul patil anthropic"],
    "Krishna Rao": ["krishna rao anthropic"],

    # Google / DeepMind
    "Sundar Pichai": ["sundar pichai", "순다르 피차이"],
    "Demis Hassabis": ["demis hassabis"],
    "Jeff Dean": ["jeff dean"],
    "Amin Vahdat": ["amin vahdat"],
    "Yann LeCun": ["yann lecun", "yann le cun"],
    "Noam Shazeer": ["noam shazeer"],

    # Microsoft
    "Satya Nadella": ["satya nadella", "사티아 나델라"],
    "Kevin Scott": ["kevin scott microsoft"],
    "Mustafa Suleyman": ["mustafa suleyman"],

    # Meta
    "Mark Zuckerberg": ["mark zuckerberg", "저커버그", "zuckerberg"],
    "Andrew Bosworth": ["andrew bosworth", "boz"],

    # xAI / Tesla
    "Elon Musk": ["elon musk", "일론 머스크"],
    "Ashok Elluswamy": ["ashok elluswamy"],

    # AMD
    "Lisa Su": ["lisa su", "리사 수"],
    "Mark Papermaster": ["mark papermaster"],
    "Forrest Norrod": ["forrest norrod"],

    # Intel
    "Lip-Bu Tan": ["lip-bu tan", "lip bu tan"],
    "Sandra Rivera": ["sandra rivera"],

    # Broadcom
    "Hock Tan": ["hock tan"],
    "Charlie Kawwas": ["charlie kawwas"],

    # Marvell
    "Matt Murphy": ["matt murphy marvell"],
    "Chris Koopmans": ["chris koopmans"],

    # Micron
    "Sanjay Mehrotra": ["sanjay mehrotra", "micron ceo"],
    "Sumit Sadana": ["sumit sadana"],

    # TSMC
    "C.C. Wei": ["c.c. wei", "cc wei", "wei che-chia"],
    "Mark Liu": ["mark liu tsmc"],
    "Morris Chang": ["morris chang"],

    # Samsung / SK hynix
    "Young-Hyun Jun": ["young-hyun jun", "young hyun jun"],
    "Jong-Hee Han": ["jong-hee han", "jong hee han"],
    "Chey Tae-won": ["chey tae-won", "choi tae-won", "최태원"],

    # Arista Networks
    "Jayshree Ullal": ["jayshree ullal"],
    "Andy Bechtolsheim": ["andy bechtolsheim"],
    "Ken Duda": ["ken duda"],

    # Cisco
    "Chuck Robbins": ["chuck robbins"],
    "Jonathan Rosenberg": ["jonathan rosenberg cisco"],

    # Dell
    "Michael Dell": ["michael dell", "michael s dell"],
    "Jeff Clarke": ["jeff clarke dell"],

    # HPE
    "Antonio Neri": ["antonio neri"],
    "Fidelma Russo": ["fidelma russo"],

    # Supermicro
    "Charles Liang": ["charles liang", "liang charles", "supermicro ceo"],

    # Cerebras
    "Andrew Feldman": ["andrew feldman cerebras"],

    # Groq
    "Jonathan Ross": ["jonathan ross groq"],

    # Tenstorrent
    "Jim Keller": ["jim keller", "jim keller tenstorrent"],

    # SambaNova
    "Rodrigo Liang": ["rodrigo liang"],

    # Graphcore
    "Nigel Toon": ["nigel toon"],

    # CoreWeave
    "Michael Intrator": ["michael intrator", "coreweave ceo"],

    # Crusoe
    "Chase Lochmiller": ["chase lochmiller", "crusoe"],

    # Lambda
    "Stephen Balaban": ["stephen balaban", "lambda labs"],

    # Applied Digital
    "Wes Cummins": ["wes cummins"],

    # AI infrastructure / data center / power
    "Giordano Albertazzi": ["giordano albertazzi"],
    "Peter Herweck": ["peter herweck"],
    "Craig Arnold": ["craig arnold eaton"],
    "Roland Busch": ["roland busch"],
    "Scott Strazik": ["scott strazik"],

    # Qualcomm / Mobileye
    "Cristiano Amon": ["cristiano amon"],
    "Amnon Shashua": ["amnon shashua"],

    # IBM / Palantir / enterprise AI
    "Arvind Krishna": ["arvind krishna"],
    "Alex Karp": ["alex karp"],
    "Thomas Kurian": ["thomas kurian"],
    "Chet Kapoor": ["chet kapoor"],

    # Cloud
    "Andy Jassy": ["andy jassy"],
    "Matt Garman": ["matt garman"],
    "Swami Sivasubramanian": ["swami sivasubramanian"],

    # AI search / model startups
    "Aravind Srinivas": ["aravind srinivas"],
    "Mira Murati": ["mira murati", "미라 무라티"],
    "Ilya Sutskever": ["ilya sutskever", "일리야 수츠케버"],
    "Arthur Mensch": ["arthur mensch"],
    "Aidan Gomez": ["aidan gomez"],
    "Eric Lefkofsky": ["eric lefkofsky", "레프코프스키"],

    # Robotics / physical AI
    "Marc Raibert": ["marc raibert"],
    "Robert Playter": ["robert playter"],
    "Brett Adcock": ["brett adcock"],
    "Jonathan Hurst": ["jonathan hurst"],

    # Investors / AI ecosystem
    "Marc Andreessen": ["marc andreessen"],
    "Ben Horowitz": ["ben horowitz"],
    "Chamath Palihapitiya": ["chamath", "chamath palihapitiya"],
    "Gavin Baker": ["gavin baker"],
    "Dylan Patel": ["dylan patel", "semianalysis"],

    # Asia AI / tech
    "Masayoshi Son": ["masayoshi son", "son masayoshi"],
    "Pony Ma": ["pony ma"],
    "Robin Li": ["robin li baidu"],
    "Liang Wenfeng": ["liang wenfeng", "liang wen-feng"],
    "Ren Zhengfei": ["ren zhengfei"],
}


CORE_PERSONS = {
    "Jensen Huang",
    "Sam Altman",
    "Dario Amodei",
    "Daniela Amodei",
    "Demis Hassabis",
    "Sundar Pichai",
    "Satya Nadella",
    "Lisa Su",
    "Mark Zuckerberg",
    "Michael Dell",
    "Hock Tan",
    "Jayshree Ullal",
    "Charles Liang",
    "Sanjay Mehrotra",
    "C.C. Wei",
    "Jim Keller",
    "Andrew Feldman",
    "Jonathan Ross",
    "Arvind Krishna",
    "Andy Jassy",
    "Matt Garman",
    "Eric Lefkofsky",
    "Elon Musk",
}


TOPIC_FREE_PERSONS = {
    "Marc Andreessen",
    "Ben Horowitz",
    "Chamath Palihapitiya",
    "Gavin Baker",
    "Dylan Patel",
}

STRICT_PERSONS = {
    "Elon Musk",
}


def build_search_batches():
    names = list(PERSONS.keys())
    batches = []

    for i in range(0, len(names), 5):
        group = names[i:i + 5]
        q = "|".join(f'"{x}"' for x in group)
        batches.append((q, True))

    return batches


SEARCH_BATCHES = build_search_batches()


TITLE_BLACKLIST = [
    "shorts",
    "#shorts",
    "reaction",
    "리액션",
    "요약정리",
    "총정리",
    "주식",
    "종목",
    "매수",
    "매도",
    "급등",
    "코인",
    "숏폼",
    "클립모음",
    "ai voice",
    "ai 목소리",
    "성대모사",
    "meme",
    "compilation",
    "fan made",
    "tribute",
    "motivational",
    "동기부여",
    "documentary",
    "다큐",
    "일대기",
    "성공 스토리",
    "성공스토리",
    "success story",
    "biography",
    "전기",
    "생애",
    "인생",
    "wealth secret",
    "roast",
]

CHANNEL_BLACKLIST_PATTERNS = [
    r"주식",
    r"투자",
    r"경제tv",
    r"코인",
    r"클립",
    r"쇼츠",
    r"shorts",
    r"motivation",
    r"quotes",
    r"success",
]

TRUSTED_CHANNELS = [
    "bloomberg",
    "cnbc",
    "bg2 pod",
    "all-in",
    "lex fridman",
    "dwarkesh",
    "nvidia",
    "openai",
    "anthropic",
    "microsoft",
    "google",
    "20vc",
    "no priors",
    "a16z",
    "wsj",
    "financial times",
    "the information",
    "stanford",
    "acquired",
    "bipartisan",
    "cheeky pint",
    "training data",
    "mit",
    "harvard",
    "berkeley",
    "sequoia",
]

INTERVIEW_SIGNALS = [
    "interview",
    "full interview",
    "in conversation",
    "conversation with",
    "fireside chat",
    "keynote",
    "keynote speech",
    "podcast",
    "episode",
    "panel",
    "panel discussion",
    "conference",
    "summit",
    "q&a",
    "qa",
    "talks with",
    "speaks with",
    "discussion",
    "live",
    "hearing",
    "earnings call",
    "fireside",
    "대담",
    "인터뷰",
    "키노트",
    "패널",
    "컨퍼런스",
]

TOPIC_SIGNALS = [
    "ai",
    "artificial intelligence",
    "gpu",
    "accelerator",
    "compute",
    "inference",
    "training",
    "datacenter",
    "data center",
    "data centre",
    "ai infrastructure",
    "capex",
    "capital expenditure",
    "hbm",
    "dram",
    "nand",
    "memory",
    "semiconductor",
    "chip",
    "chips",
    "networking",
    "ethernet",
    "infiniband",
    "optical",
    "silicon photonics",
    "custom silicon",
    "asic",
    "cloud",
    "power",
    "electricity",
    "grid",
    "rack",
    "liquid cooling",
    "token",
    "tokens",
    "inference economics",
]

NEGATIVE_CONTENT_SIGNALS = [
    "according to",
    "what x thinks",
    "why x is wrong",
    "explained",
    "analysis",
    "commentary",
    "reaction",
    "summary",
    "news roundup",
    "news update",
    "daily news",
]


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-5000:], f, ensure_ascii=False)


def yt_search(query, published_after, include_medium=False):
    items = []

    durations = ["long"]

    if include_medium:
        durations.append("medium")

    for d in durations:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "key": YOUTUBE_API_KEY,
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "date",
                "maxResults": 25,
                "publishedAfter": published_after,
                "videoDuration": d,
            },
            timeout=30,
        )

        r.raise_for_status()
        items.extend(r.json().get("items", []))

    return items


def get_video_details(video_ids):
    if not video_ids:
        return {}

    out = {}

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]

        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "key": YOUTUBE_API_KEY,
                "part": "contentDetails,statistics,snippet,status",
                "id": ",".join(batch),
            },
            timeout=30,
        )

        r.raise_for_status()

        for it in r.json().get("items", []):
            out[it["id"]] = it

    return out


def get_channel_stats(channel_ids):
    out = {}
    ids = list(dict.fromkeys(channel_ids))

    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]

        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "statistics,snippet",
                    "id": ",".join(batch),
                },
                timeout=30,
            )

            r.raise_for_status()

            for it in r.json().get("items", []):
                st = it.get("statistics", {})
                sn = it.get("snippet", {})

                out[it["id"]] = {
                    "subs": int(st.get("subscriberCount", 0) or 0),
                    "hidden_subs": st.get("hiddenSubscriberCount", False),
                    "videos": int(st.get("videoCount", 0) or 0),
                    "published": sn.get("publishedAt", ""),
                }

        except Exception as e:
            print(f"[채널조회 실패] {str(e)[:120]}")

    return out


def is_factory_channel(stats):
    if not stats:
        return False, None

    subs = stats["subs"]
    videos = stats["videos"]

    if videos >= 300 and subs < 5000:
        return True, f"공장형(영상{videos}/구독{subs})"

    pub = stats.get("published", "")

    if pub and videos >= 500:
        try:
            created = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days

            if age_days < 365:
                return True, f"신생대량({age_days}일/{videos}편)"
        except Exception:
            pass

    return False, None


def parse_duration(iso):
    m = re.match(
        r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
        iso or "",
    )

    if not m:
        return 0

    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def match_person(text):
    t = (text or "").lower()

    ordered = sorted(
        PERSONS.items(),
        key=lambda x: max(
            [len(x[0])] + [len(a) for a in x[1]]
        ),
        reverse=True,
    )

    for name, aliases in ordered:
        for a in [name.lower()] + aliases:
            if a and a in t:
                return name

    return None


def contains_any(text, words):
    t = (text or "").lower()
    return any(w in t for w in words)


def candidate_score(person, item, detail):
    title = item["snippet"].get("title", "")
    channel = item["snippet"].get("channelTitle", "")
    desc = detail.get("snippet", {}).get("description", "") or ""

    score = 0

    if any(t in channel.lower() for t in TRUSTED_CHANNELS):
        score += 4

    if contains_any(title, INTERVIEW_SIGNALS):
        score += 4

    if contains_any(desc[:1500], INTERVIEW_SIGNALS):
        score += 2

    if contains_any(title, TOPIC_SIGNALS):
        score += 2

    if contains_any(desc[:1500], TOPIC_SIGNALS):
        score += 1

    if contains_any(title, NEGATIVE_CONTENT_SIGNALS):
        score -= 3

    if contains_any(channel, ["news", "daily", "media", "today"]):
        score -= 1

    dur = parse_duration(
        detail.get("contentDetails", {}).get("duration")
    )

    if dur >= PREFERRED_DURATION_SEC:
        score += 2
    elif dur < MIN_DURATION_SEC:
        score -= 5

    if person in CORE_PERSONS:
        score += 1

    aliases = [person.lower()] + PERSONS.get(person, [])

    if any(
        a and a in title.lower()
        for a in aliases
    ):
        score += 3

    return score


def hard_filter(item, detail):
    title = item["snippet"].get("title", "")
    channel = item["snippet"].get("channelTitle", "")
    desc = detail.get("snippet", {}).get("description", "") or ""

    person = match_person(title) or match_person(desc[:1500])

    if not person:
        return None, "인물명 없음"

    if detail.get("status", {}).get("containsSyntheticMedia"):
        return None, "합성미디어 플래그"

    title_l = title.lower()
    channel_l = channel.lower()

    for b in TITLE_BLACKLIST:
        if b in title_l:
            return None, f"제목 블랙리스트: {b}"

    for p in CHANNEL_BLACKLIST_PATTERNS:
        if re.search(p, channel_l):
            return None, f"채널 블랙리스트: {p}"

    real_pub = detail.get("snippet", {}).get("publishedAt", "")

    if real_pub:
        try:
            pub_dt = datetime.fromisoformat(
                real_pub.replace("Z", "+00:00")
            )

            age_hours = (
                datetime.now(timezone.utc) - pub_dt
            ).total_seconds() / 3600

            if age_hours > LOOKBACK_HOURS + 2:
                return None, f"실제게시일 범위밖 ({age_hours / 24:.0f}일 전)"

        except Exception:
            pass

    dur = parse_duration(
        detail.get("contentDetails", {}).get("duration")
    )

    min_dur = 360 if person in CORE_PERSONS else MIN_DURATION_SEC

    if dur < min_dur:
        return None, f"길이 미달 ({dur // 60}분)"

    return person, None


CELEB_PROMPT = r"""
당신은 AI/반도체/데이터센터 산업의 핵심 인물 출연 영상을 찾는
매우 엄격한 검증 에이전트다.

목표는 "그 사람의 이름이 나오는 영상"이 아니다.

목표는:
해당 인물 본인이 실제 영상에 출연하여 직접 말하는 원본 또는
원본에 가까운 영상만 찾아내는 것이다.

반드시 다음을 각각 판정하라.

direct_appearance:
- 해당 인물이 실제 영상에 등장해서 직접 말하는가?
- 인터뷰, 팟캐스트, 대담, 키노트, 컨퍼런스, 패널,
  fireside chat, earnings call, 공식 행사 등은 TRUE 가능.
- 단순 뉴스 진행자가 그 사람의 발언을 전달하는 영상은 FALSE.
- 제3자가 그 사람에 대해 설명하는 영상은 FALSE.
- 사진/영상 클립만 보여주는 것은 FALSE.
- AI 음성/AI 아바타/재구성은 FALSE.

source_originality:
- 원본 방송/원본 인터뷰/공식 행사/공식 팟캐스트/방송사 원본인가?
- 원본 전체 영상 또는 정상적인 원본 재게시라면 TRUE.
- 다른 영상의 짜깁기/클립/번역 재구성/AI 음성은 FALSE.

indirect_content:
- 그 인물이 직접 출연하지 않고 제3자가 설명하거나 분석하는 콘텐츠인가?
- 그렇다면 TRUE.

synthetic_or_reupload:
- AI 음성, AI 아바타, fan-made, 짜깁기, 번역 재구성,
  뉴스 클립 재편집 등인가?

appearance_confidence:
- 실제 직접 출연 판단 확신도 0~10.

relevance_score:
- 이 영상이 AI/반도체/데이터센터/컴퓨팅/메모리/네트워킹/
  전력/AI CAPEX/클라우드/AI 경제성 측면에서 투자자가 볼 가치가
  얼마나 높은지 0~10.

중요한 판정 규칙:

인물 이름이 제목에 있다고 해서 출연으로 판단하지 마라.

예:
"Jensen Huang Says AI Will Change Everything"
→ 실제 인터뷰라는 증거가 없으면 FALSE.

"Why Jensen Huang Is Wrong"
→ FALSE.

"Jensen Huang Explained"
→ FALSE.

"Jensen Huang Interview"
→ 제목만으로 TRUE라고 확정하지 말고
  설명의 프로그램명/진행자/에피소드/방송사/행사 정보를 확인하라.

다음은 강한 TRUE 신호다:
- "full interview"
- "in conversation with"
- "fireside chat"
- "keynote"
- "podcast with"
- "guest: Jensen Huang"
- "Jensen Huang joins Bloomberg"
- "Jensen Huang at GTC"
- "Jensen Huang keynote"
- "Jensen Huang on CNBC"
- 공식 NVIDIA/OpenAI/Microsoft/Google 등 행사
- Bloomberg/CNBC/WSJ/FT 등 원본 인터뷰

그러나 채널이 유명하지 않다고 무조건 FALSE로 판단하지 마라.
새로운 팟캐스트/대학/컨퍼런스/행사 채널에서도 진짜 출연 영상이 나올 수 있다.

반대로 유명 채널이라고 자동 TRUE로 판단하지 마라.
Bloomberg/CNBC라도 뉴스 해설 영상이면 FALSE다.

설명에서 프로그램명, 진행자, 게스트, 행사명, 에피소드 정보가 보이면
출연 여부를 판단하는 강한 근거로 사용하라.

일반적인 관련성:
- AI 수요
- 토큰 소비
- GPU / accelerator
- inference / training
- 데이터센터
- AI CAPEX
- HBM / DRAM / NAND
- 메모리
- 네트워킹 / Ethernet / InfiniBand / optical
- custom ASIC
- cloud / neocloud
- 전력 / grid / cooling
- AI economics

위 주제와 무관한 일반적인 인생 이야기만 있는 경우 relevance_score를 낮춰라.

특별히 중요한 것은 "직접 출연"이다.
직접 출연이 false라면 relevance_score가 높아도 최종적으로 탈락한다.

출력은 반드시 JSON 배열 하나만 출력한다.
마크다운 금지.
입력 개수와 같은 개수로 반환한다.

형식:
[
  {
    "idx": 0,
    "direct_appearance": true,
    "source_originality": true,
    "indirect_content": false,
    "synthetic_or_reupload": false,
    "appearance_confidence": 10,
    "relevance_score": 10,
    "evidence": "Bloomberg가 해당 인물을 인터뷰 게스트로 명시",
    "reason": "실제 인터뷰 원본으로 판단",
    "summary_kr": "AI 데이터센터와 GPU 수요에 대한 핵심 발언 요약"
  }
]

판정 대상:
{items}
"""


def judge_celeb_batch(chunk):
    lines = []

    for i, (person, item, detail, _vid, meta_score) in enumerate(chunk):
        if person in TOPIC_FREE_PERSONS:
            mode = "주제무관"
        elif person in STRICT_PERSONS:
            mode = "엄격"
        else:
            mode = "일반"

        desc = (
            detail.get("snippet", {}).get("description", "")
            or ""
        )

        desc = strip_html(desc[:DESC_CHARS_FOR_GEMINI])

        lines.append(
            f"[{i}] 인물: {person}\n"
            f"모드: {mode}\n"
            f"메타후보점수: {meta_score}\n"
            f"제목: {item['snippet'].get('title', '')}\n"
            f"채널: {item['snippet'].get('channelTitle', '')}\n"
            f"설명: {desc}"
        )

    out = gemini_call(
        CELEB_PROMPT.format(
            items="\n\n".join(lines)
        )
    )

    return parse_json_array(
        out,
        len(chunk),
    )


def send_telegram_celeb(person, item, judge, video_id):
    evidence = judge.get("evidence", "")
    summary = judge.get("summary_kr", "")
    score = judge.get("relevance_score", "?")
    confidence = judge.get("appearance_confidence", "?")

    msg = (
        f"🎙 <b>{html.escape(person)}</b> 직접 출연 감지\n"
        f"📺 {html.escape(item['snippet'].get('channelTitle', ''))}\n"
        f"<b>{html.escape(item['snippet'].get('title', ''))}</b>\n\n"
        f"💡 {html.escape(summary)}\n"
        f"🔎 근거: {html.escape(evidence)}\n"
        f"직접출연 확신도: {confidence}/10\n"
        f"산업 관련성: {score}/10\n"
        f"https://youtu.be/{video_id}"
    )

    send_tg(msg)


def run_celeb_watch():
    seen = load_seen()

    published_after = (
        datetime.now(timezone.utc)
        - timedelta(hours=LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = {}

    for q, inc_med in SEARCH_BATCHES:
        try:
            for it in yt_search(
                q,
                published_after,
                inc_med,
            ):
                vid = it.get("id", {}).get("videoId")

                if not vid:
                    continue

                if vid not in seen:
                    candidates[vid] = it

        except Exception as e:
            print(
                f"[셀럽 검색 실패] {q}: {str(e)[:150]}"
            )

        time.sleep(0.7)

    print(f"[셀럽] 신규 후보: {len(candidates)}건")

    if not candidates:
        save_seen(seen)

        if NOTIFY_WHEN_EMPTY:
            send_tg("🔍 새로운 직접출연 영상 없음")

        return

    try:
        details = get_video_details(
            list(candidates.keys())
        )
    except Exception as e:
        print(f"[셀럽] 영상 상세조회 실패: {e}")
        return

    passed = []

    for vid, item in candidates.items():
        detail = details.get(vid, {})

        person, reject = hard_filter(
            item,
            detail,
        )

        if not person:
            seen.add(vid)

            print(
                f"❌ [{reject}] "
                f"{item['snippet'].get('title', '')[:70]}"
            )

            continue

        meta_score = candidate_score(
            person,
            item,
            detail,
        )

        if meta_score < -1 and person not in CORE_PERSONS:
            seen.add(vid)

            print(
                f"❌ [메타점수 낮음 {meta_score}] "
                f"{item['snippet'].get('title', '')[:60]}"
            )

            continue

        passed.append(
            (
                person,
                item,
                detail,
                vid,
                meta_score,
            )
        )

    if passed:
        ch_ids = [
            it["snippet"].get("channelId", "")
            for _, it, _, _, _ in passed
        ]

        ch_stats = get_channel_stats(
            [c for c in ch_ids if c]
        )

        filtered = []

        for candidate in passed:
            person, item, detail, vid, meta_score = candidate

            cid = item["snippet"].get(
                "channelId",
                "",
            )

            factory, why = is_factory_channel(
                ch_stats.get(cid)
            )

            if factory:
                seen.add(vid)

                print(
                    f"❌ [{why}] "
                    f"{item['snippet'].get('channelTitle', '')[:30]} | "
                    f"{item['snippet'].get('title', '')[:45]}"
                )

                continue

            filtered.append(candidate)

        passed = filtered

    passed.sort(
        key=lambda x: x[4],
        reverse=True,
    )

    # 이번 사이클의 Gemini 예산 안에서 가장 유력한 후보부터 판정.
    passed = passed[:MAX_CELEB_CANDIDATES]

    nb = (
        len(passed) + BATCH_SIZE - 1
    ) // BATCH_SIZE

    print(
        f"[셀럽] Gemini 판정 대상 "
        f"{len(passed)}건 → 배치 {nb}회"
    )

    sent = 0

    for i in range(
        0,
        len(passed),
        BATCH_SIZE,
    ):
        chunk = passed[
            i:i + BATCH_SIZE
        ]

        results = judge_celeb_batch(chunk)

        for candidate, j in zip(
            chunk,
            results,
        ):
            person, item, detail, vid, meta_score = candidate

            if j is None:
                print(
                    f"⚠ 판정실패(다음사이클 재시도): "
                    f"{item['snippet'].get('title', '')[:60]}"
                )
                continue

            seen.add(vid)

            direct = j.get(
                "direct_appearance",
                False,
            )

            original = j.get(
                "source_originality",
                False,
            )

            indirect = j.get(
                "indirect_content",
                True,
            )

            synthetic = j.get(
                "synthetic_or_reupload",
                True,
            )

            evidence = str(
                j.get("evidence", "")
            ).strip()

            try:
                appearance_conf = int(
                    j.get(
                        "appearance_confidence",
                        0,
                    )
                    or 0
                )
            except Exception:
                appearance_conf = 0

            try:
                score = int(
                    j.get(
                        "relevance_score",
                        0,
                    )
                    or 0
                )
            except Exception:
                score = 0

            should_send = (
                direct is True
                and original is True
                and indirect is False
                and synthetic is False
                and bool(evidence)
                and appearance_conf >= 8
                and score >= SCORE_THRESHOLD
            )

            if should_send:
                send_telegram_celeb(
                    person,
                    item,
                    j,
                    vid,
                )

                sent += 1

                print(
                    f"✅ {person} | "
                    f"출연확신 {appearance_conf}/10 | "
                    f"관련성 {score}/10 | "
                    f"{item['snippet'].get('title', '')[:60]}"
                )

            else:
                print(
                    f"❌ [{person}] "
                    f"direct={direct} "
                    f"original={original} "
                    f"indirect={indirect} "
                    f"synthetic={synthetic} "
                    f"confidence={appearance_conf} "
                    f"score={score} | "
                    f"{j.get('reason', '')[:55]}"
                )

        time.sleep(1.5)

        if _gm["dead"]:
            print("[셀럽] Gemini 중단 → 다음 사이클 재시도")
            break

    save_seen(seen)

    if sent == 0 and NOTIFY_WHEN_EMPTY:
        send_tg(
            f"🔍 셀럽 후보 {len(candidates)}건 검토했으나 "
            "직접출연 조건 충족 영상 없음"
        )

    print(f"[셀럽] 완료: {sent}건 전송")


# ============================================================
# PART 3 — 네이버 블로그 감시
# ============================================================

NAVER_BLOG_IDS = [
    "richyun0108",
    "cybermw",
    "hardark",
    "kk_kontemp",
    "tmdejr1267",
    "engineerinvestor",
    "thebeing",
]

SEEN_BLOG_FILE = "seen_twitter_blog.json"
BLOG_MAX_AGE_HOURS = 30


def load_blog_state():
    try:
        with open(
            SEEN_BLOG_FILE,
            encoding="utf-8",
        ) as f:
            d = json.load(f)

        d.setdefault("blog", {})
        return d

    except Exception:
        return {"blog": {}}


def save_blog_state(state):
    with open(
        SEEN_BLOG_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )


def fetch_blog_posts(blog_id):
    try:
        resp = requests.get(
            f"https://rss.blog.naver.com/{blog_id}.xml",
            headers={"User-Agent": UA},
            timeout=20,
        )

        if resp.status_code != 200:
            print(
                f"[블로그 오류] {blog_id}: "
                f"HTTP {resp.status_code}"
            )
            return []

        feed = feedparser.parse(resp.content)

    except Exception as e:
        print(f"[블로그 오류] {blog_id}: {e}")
        return []

    posts = []

    for entry in feed.entries[:30]:
        raw = entry.get("link", "")
        clean = raw.split("?")[0].rstrip("/")

        pub = (
            entry.get("published", "")
            or entry.get("updated", "")
        )

        title = entry.get(
            "title",
            "(제목 없음)",
        )

        pub_dt = None

        tm = (
            entry.get("published_parsed")
            or entry.get("updated_parsed")
        )

        if tm:
            try:
                pub_dt = datetime(
                    *tm[:6],
                    tzinfo=timezone.utc,
                )
            except Exception:
                pub_dt = None

        posts.append(
            {
                "id": clean
                or f"{blog_id}:{title}:{pub}",
                "title": title,
                "url": clean or raw,
                "pub_dt": pub_dt,
            }
        )

    print(
        f"[블로그] {blog_id}: {len(posts)}건"
    )

    return posts


def run_blog_watch():
    try:
        state = load_blog_state()
        seen = state.setdefault(
            "blog",
            {},
        )

        total = 0

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=BLOG_MAX_AGE_HOURS)
        )

        for blog_id in NAVER_BLOG_IDS:
            posts = fetch_blog_posts(blog_id)

            if not posts:
                continue

            first = blog_id not in seen

            already = set(
                seen.get(
                    blog_id,
                    [],
                )
            )

            fresh = [
                p for p in posts
                if p["id"]
                and p["id"] not in already
            ]

            old_but_new = [
                p for p in fresh
                if p["pub_dt"]
                and p["pub_dt"] < cutoff
            ]

            fresh = [
                p for p in fresh
                if not (
                    p["pub_dt"]
                    and p["pub_dt"] < cutoff
                )
            ]

            if old_but_new:
                print(
                    f"  ↳ [{blog_id}] 오래된 글 "
                    f"{len(old_but_new)}건 전송 생략"
                )

            if first:
                print(
                    f"[블로그] {blog_id}: baseline 저장"
                )

            else:
                for p in reversed(fresh):
                    send_tg(
                        f"📝 <b>{html.escape(blog_id)}</b> 새 글\n\n"
                        f"{html.escape(p['title'])}\n\n"
                        f"{p['url']}"
                    )

                    total += 1
                    time.sleep(0.5)

            seen[blog_id] = list(
                dict.fromkeys(
                    [
                        p["id"]
                        for p in posts
                        if p["id"]
                    ]
                    + list(already)
                )
            )[:80]

        print(
            f"[블로그] {total}건 전송"
        )

        save_blog_state(state)

    except Exception as e:
        print(
            f"[블로그 감시 실패] {str(e)[:200]}"
        )


# ============================================================
# PART 4 — 크레딧 / 사모대출 / AI CAPEX 금융 감시
# ============================================================

CREDIT_STATE_FILE = "seen_credit.json"
FEED_CACHE_VERSION = 3

ENABLE_PODCAST = True
ENABLE_CREDIT_YT = False
ENABLE_NEWS = True

PODCAST_MAX_AGE_DAYS = 5
NEWS_LOOKBACK_HOURS = 24

# 뉴스는 "관련 뉴스"가 아니라 "투자 판단을 바꿀 정도의 중요 뉴스"만 전송
NEWS_SCORE_THRESHOLD = 9
NEWS_MAX_CANDIDATES = 10
NEWS_TITLE_SIMILARITY = 0.72

CREDIT_SCORE_THRESHOLD = 7
CREDIT_MAX_SEND = 8
CREDIT_MAX_CANDIDATES = 24

PODCASTS = [
    {
        "name": "The Credit Edge",
        "apple_id": "1674628050",
    },
    {
        "name": "Odd Lots",
        "search": "Odd Lots Bloomberg",
        "verify": "odd lots",
    },
    {
        "name": "Money Stuff",
        "search": "Money Stuff Matt Levine",
        "verify": "money stuff",
    },
    {
        "name": "GS Exchanges",
        "search": "Goldman Sachs Exchanges",
        "verify": "exchanges",
    },
    {
        "name": "Behind the Money",
        "search": "Behind the Money FT",
        "verify": "behind the money",
    },
    {
        "name": "Unhedged",
        "search": "Unhedged Financial Times",
        "verify": "unhedged",
    },
    {
        "name": "BG2Pod",
        "search": "BG2Pod Gerstner Gurley",
        "verify": "bg2",
    },
]

CREDIT_KEYWORDS = [
    "private credit",
    "private debt",
    "direct lending",
    "securitization",
    "securitisation",
    "asset-backed",
    "bond issuance",
    "debt financing",
    "credit spread",
    "spreads",
    "investment grade",
    "high yield",
    "leverage",
    "lender",
    "underwriting",
    "refinanc",
    "duration",
    "issuance",
    "vendor financing",
    "sale-leaseback",
    "covenant",
    "downgrade",
    "rating",
    "data center",
    "datacenter",
    "data centre",
    "hyperscaler",
    "capex",
    "capital expenditure",
    "neocloud",
    "gpu",
    "ai infrastructure",
    "grid",
    "memory",
    "hbm",
    "dram",
    "nand",
    "semiconductor",
    "nvidia",
    "microsoft",
    "amazon",
    "alphabet",
    "google",
    "meta",
    "oracle",
    "coreweave",
    "broadcom",
    "micron",
    "sk hynix",
    "samsung",
    "tsmc",
    "blackstone",
    "apollo",
    "ares",
    "kkr",
    "wellington",
    "pimco",
]

CREDIT_YT_QUERIES = [
    '"private credit" ("data center"|"data centre"|AI)',
    'hyperscaler debt "bond issuance" OR "credit spread"',
]

NEWS_QUERIES = [
    '"private credit" "data center"',
    "hyperscaler bond issuance debt",
    '"data center" debt financing spreads',
    '"AI capex" credit market',
    "HBM pricing contract negotiation",
]

CREDIT_YT_CHANNEL_BLACK = [
    r"주식",
    r"투자",
    r"코인",
    r"경제tv",
    r"클립",
    r"쇼츠",
    r"shorts",
]


def load_credit_state():
    try:
        with open(
            CREDIT_STATE_FILE,
            encoding="utf-8",
        ) as f:
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

    with open(
        CREDIT_STATE_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            s,
            f,
            ensure_ascii=False,
            indent=2,
        )


def keyword_hit(text):
    t = (text or "").lower()

    return any(
        k in t
        for k in CREDIT_KEYWORDS
    )


def resolve_feed(pod, state):
    name = pod["name"]

    if state["feeds"].get(name):
        return state["feeds"][name]

    try:
        if pod.get("apple_id"):
            r = requests.get(
                "https://itunes.apple.com/lookup",
                params={
                    "id": pod["apple_id"],
                    "entity": "podcast",
                },
                timeout=20,
            )

        else:
            r = requests.get(
                "https://itunes.apple.com/search",
                params={
                    "term": pod["search"],
                    "entity": "podcast",
                    "limit": 10,
                },
                timeout=20,
            )

        r.raise_for_status()

        results = [
            x
            for x in r.json().get(
                "results",
                [],
            )
            if x.get("feedUrl")
        ]

        verify = (
            pod.get("verify")
            or ""
        ).lower()

        if verify:
            results = [
                x
                for x in results
                if verify
                in (
                    x.get("collectionName")
                    or ""
                ).lower()
            ]

        if not results:
            print(
                f"[팟캐스트] {name}: 해석 실패"
            )
            return None

        best = results[0]

        print(
            f"[팟캐스트] {name} → "
            f"{best.get('collectionName')}"
        )

        state["feeds"][name] = best["feedUrl"]

        return best["feedUrl"]

    except Exception as e:
        print(
            f"[팟캐스트] {name} "
            f"해석 오류: {str(e)[:150]}"
        )
        return None


def fetch_episodes(feed_url, name):
    try:
        r = requests.get(
            feed_url,
            headers={"User-Agent": UA},
            timeout=25,
        )

        if r.status_code != 200:
            print(
                f"[팟캐스트] {name}: "
                f"HTTP {r.status_code}"
            )
            return []

        d = feedparser.parse(
            r.content
        )

    except Exception as e:
        print(
            f"[팟캐스트] {name}: "
            f"피드 오류 {str(e)[:120]}"
        )
        return []

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=PODCAST_MAX_AGE_DAYS)
    )

    out = []

    for e in d.entries[:15]:
        eid = (
            e.get("id")
            or e.get("guid")
            or e.get("link", "")
        )

        if not eid:
            continue

        pub = (
            e.get("published_parsed")
            or e.get("updated_parsed")
        )

        if (
            pub
            and datetime(
                *pub[:6],
                tzinfo=timezone.utc,
            ) < cutoff
        ):
            continue

        desc = (
            e.get("summary", "")
            or ""
        )

        if e.get("content"):
            desc = e["content"][0].get(
                "value",
                desc,
            )

        out.append(
            {
                "id": eid,
                "title": e.get(
                    "title",
                    "(제목없음)",
                ),
                "url": e.get(
                    "link",
                    "",
                ),
                "desc": desc,
            }
        )

    print(
        f"[팟캐스트] {name}: "
        f"최근 {PODCAST_MAX_AGE_DAYS}일 "
        f"{len(out)}건"
    )

    return out


def collect_podcast(state):
    cands = []

    for pod in PODCASTS:
        name = pod["name"]

        feed = resolve_feed(
            pod,
            state,
        )

        if not feed:
            continue

        eps = fetch_episodes(
            feed,
            name,
        )

        if not eps:
            continue

        first = (
            name
            not in state["podcast"]
        )

        seen = set(
            state["podcast"].get(
                name,
                [],
            )
        )

        state["podcast"][name] = list(
            dict.fromkeys(
                [e["id"] for e in eps]
                + list(seen)
            )
        )

        if first:
            print(
                f"[팟캐스트] {name}: "
                "baseline 저장"
            )
            continue

        for ep in reversed(
            [
                e for e in eps
                if e["id"] not in seen
            ]
        ):
            if not keyword_hit(
                f"{ep['title']} "
                f"{strip_html(ep['desc'])}"
            ):
                print(
                    f"  ⏭ [키워드 없음] "
                    f"{ep['title'][:55]}"
                )
                continue

            cands.append(
                {
                    "kind": "팟캐스트",
                    "icon": "🎧",
                    "source": name,
                    "title": ep["title"],
                    "body": ep["desc"],
                    "url": ep["url"],
                    "seen_key": None,
                }
            )

    return cands


def collect_credit_youtube(state):
    after = (
        datetime.now(timezone.utc)
        - timedelta(hours=LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    seen = set(state["youtube"])
    cand = {}

    for q in CREDIT_YT_QUERIES:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "key": YOUTUBE_API_KEY,
                    "part": "snippet",
                    "q": q,
                    "type": "video",
                    "order": "date",
                    "maxResults": 20,
                    "publishedAfter": after,
                },
                timeout=30,
            )

            r.raise_for_status()

            for it in r.json().get(
                "items",
                [],
            ):
                vid = it["id"]["videoId"]

                if vid not in seen:
                    cand[vid] = it

        except Exception as e:
            print(
                f"[크레딧YT] 검색 실패: "
                f"{str(e)[:120]}"
            )

        time.sleep(1)

    if not cand:
        return []

    try:
        det = get_video_details(
            list(cand.keys())
        )

    except Exception as e:
        print(
            f"[크레딧YT] 상세조회 실패: "
            f"{str(e)[:120]}"
        )
        return []

    out = []

    for vid, it in cand.items():
        title = it["snippet"]["title"]
        ch = it["snippet"]["channelTitle"]

        d = det.get(
            vid,
            {},
        )

        desc = d.get(
            "snippet",
            {},
        ).get(
            "description",
            "",
        )

        if (
            any(
                re.search(
                    p,
                    ch.lower(),
                )
                for p in CREDIT_YT_CHANNEL_BLACK
            )
            or parse_duration(
                d.get(
                    "contentDetails",
                    {},
                ).get(
                    "duration"
                )
            ) < 600
            or not keyword_hit(
                f"{title} {desc[:600]}"
            )
        ):
            state["youtube"].append(
                vid
            )
            continue

        out.append(
            {
                "kind": "유튜브",
                "icon": "📺",
                "source": ch,
                "title": title,
                "body": desc,
                "url": f"https://youtu.be/{vid}",
                "seen_key": (
                    "youtube",
                    vid,
                ),
            }
        )

    print(
        f"[크레딧YT] 판정 대상 "
        f"{len(out)}건"
    )

    return out


def _news_title_key(title):
    """언론사/숫자/기호 차이로 같은 사건이 중복되는 것을 줄인다."""
    t = (title or "").lower()
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"[^a-z0-9가-힣]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


NEWS_CRITICAL_TERMS = [
    # 실제 금융 이벤트
    "bond issuance", "bond sale", "debt issuance", "debt financing",
    "credit facility", "loan facility", "private credit", "private debt",
    "default", "defaults", "bankruptcy", "restructuring", "distress",
    "downgrade", "rating cut", "credit rating", "credit spread",
    "spread widening", "spread widens", "refinancing", "refinance",
    "covenant breach", "covenant", "securitization", "asset-backed",
    "funding", "financing", "capital raise", "liquidity",
    # AI 인프라의 대형 금융/계약 이벤트
    "capex financing", "capex plan", "capital expenditure",
    "data center financing", "data centre financing",
    "data center debt", "data centre debt", "project finance",
    "vendor financing", "prepayment", "prepayment agreement",
    "long-term contract", "supply agreement", "purchase agreement",
    # 메모리 가격/계약의 실제 변화
    "memory price", "dram price", "nand price", "hbm price",
    "contract price", "contract pricing", "price increase", "price hike",
]

NEWS_MAJOR_ENTITIES = [
    "microsoft", "google", "alphabet", "amazon", "meta", "oracle",
    "coreweave", "nvidia", "blackstone", "apollo", "ares", "kkr",
    "pimco", "wellington", "sk hynix", "samsung", "micron", "tsmc",
]

NEWS_NOISE_TERMS = [
    "stock price", "shares rise", "shares fall", "stock rises", "stock falls",
    "analyst", "price target", "buy rating", "sell rating", "upgrade to",
    "downgrade to buy", "market outlook", "investor outlook", "should buy",
    "top stocks", "best stocks", "stock picks", "why investors",
    "earnings preview", "earnings recap", "quarterly results", "ai tool",
    "ai model", "new ai", "launches", "unveils", "introduces",
]


def _news_importance_prefilter(title, body):
    """명백히 중요하지 않은 뉴스는 Gemini에 보내기 전에 제거한다."""
    text = f"{title} {strip_html(body)}".lower()

    if any(x in text for x in NEWS_NOISE_TERMS):
        # 단, 금융/신용 이벤트가 동시에 있으면 살려둔다.
        if not any(x in text for x in NEWS_CRITICAL_TERMS):
            return False

    critical_hits = sum(
        1 for x in NEWS_CRITICAL_TERMS
        if x in text
    )
    entity_hits = sum(
        1 for x in NEWS_MAJOR_ENTITIES
        if x in text
    )

    # 대형 금융 이벤트는 단일 강한 신호도 후보로 허용.
    if critical_hits >= 2:
        return True

    # 주요 기업 + 핵심 금융 이벤트 조합만 허용.
    if critical_hits >= 1 and entity_hits >= 1:
        return True

    return False


def _news_duplicate(title, selected_titles):
    key = _news_title_key(title)
    if not key:
        return True

    for old in selected_titles:
        old_key = _news_title_key(old)
        if not old_key:
            continue

        ratio = difflib.SequenceMatcher(
            None,
            key,
            old_key,
        ).ratio()

        # 제목 표현이 달라도 핵심 단어 집합이 거의 같으면 중복으로 처리
        a = set(key.split())
        b = set(old_key.split())
        jaccard = len(a & b) / max(1, len(a | b))

        if ratio >= NEWS_TITLE_SIMILARITY or jaccard >= 0.62:
            return True

    return False


def collect_news(state):
    seen = set(state["news"])
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=NEWS_LOOKBACK_HOURS)
    )

    raw = []
    url_seen = set()
    title_seen = []

    for q in NEWS_QUERIES:
        try:
            r = requests.get(
                "https://news.google.com/rss/search",
                params={
                    "q": f"{q} when:2d",
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
                headers={"User-Agent": UA},
                timeout=25,
            )
            d = feedparser.parse(r.content)

        except Exception as e:
            print(
                f"[뉴스] 실패 ({q}): "
                f"{str(e)[:120]}"
            )
            continue

        for e in d.entries[:8]:
            link = e.get("link", "")
            title = e.get("title", "")
            body = e.get("summary", "")

            if not link or link in seen or link in url_seen:
                continue

            pub = e.get("published_parsed") or e.get("updated_parsed")
            if pub:
                pub_dt = datetime(
                    *pub[:6],
                    tzinfo=timezone.utc,
                )
                if pub_dt < cutoff:
                    continue

            url_seen.add(link)

            # 명백한 잡음 제거
            if not _news_importance_prefilter(title, body):
                # 다시 검색되지 않도록 URL은 seen에 기록
                state["news"].append(link)
                continue

            # 같은 사건을 여러 언론사가 보도한 경우 대표 기사 하나만 남김
            if _news_duplicate(title, title_seen):
                state["news"].append(link)
                continue

            title_seen.append(title)
            raw.append(
                {
                    "kind": "뉴스",
                    "icon": "📰",
                    "source": (
                        e.get("source", {}) or {}
                    ).get("title", "News"),
                    "title": title,
                    "body": body,
                    "url": link,
                    "seen_key": ("news", link),
                }
            )

        time.sleep(0.5)

    # 후보가 너무 많아져 팟캐스트/다른 콘텐츠를 밀어내지 않도록 제한
    raw = raw[:NEWS_MAX_CANDIDATES]

    print(
        f"[뉴스] 중요뉴스 후보 "
        f"{len(raw)}건"
    )

    return raw


CREDIT_PROMPT = r"""
너는 반도체/AI 인프라 투자자를 위한 콘텐츠 선별 에이전트다.

아래 콘텐츠 각각이 다음 관심사에 실질적으로 부합하는지 엄격히 판정하라.

관심사:
- 하이퍼스케일러(MS/구글/아마존/메타/오라클) 자금조달
- 회사채 발행, 스프레드, 신용등급, 듀레이션
- 데이터센터 사모대출/사모크레딧
- AI CAPEX의 재무적 지속가능성
- 벤더 파이낸싱
- 자산유동화(ABS)
- 메모리(HBM/DRAM/NAND) 가격
- 계약 협상/선급금
- 위 주제로 업계 실무자가 직접 발언하는 인터뷰/대담

뉴스는 특히 엄격하게 판정한다.
뉴스의 경우 9점 이상만 통과시킨다.
9점은 단순히 관련 있는 뉴스가 아니라, 실제 투자 판단이나 시장 구조를 바꿀 가능성이 높은 사건이어야 한다.
예: 대규모 채권/대출/프로젝트 파이낸싱, 신용등급 강등, 디폴트/부실, 신용스프레드 급변, AI CAPEX 자금조달 구조 변화, 대형 데이터센터 금융 문제, 대형 벤더 파이낸싱/선급금/장기공급계약, HBM/DRAM/NAND 실제 계약가격 변화.

뉴스 탈락:
- 일반 AI 기술/제품 소개
- 신제품 출시/모델 발표
- 주가 등락이나 증권사 목표가/투자의견
- 개인투자 채널의 종목추천/시황요약
- 이미 널리 알려진 내용을 반복하는 기사
- 기업 실적 자체가 아니라 단순 실적 요약
- 스치듯 관련 키워드만 포함한 기사
- 광고

출력:
JSON 배열만.
마크다운 금지.
입력과 같은 개수와 순서.

형식:
[
  {
    "idx": 0,
    "relevance_score": 8,
    "summary_kr": "핵심 요약",
    "key_points": ["핵심1", "핵심2"]
  }
]

콘텐츠:
{items}
"""


def judge_credit_batch(chunk):
    blob = "\n\n".join(
        f"[{i}] 종류: {c['kind']}\n"
        f"제목: {c['title']}\n"
        f"출처: {c['source']}\n"
        f"설명: {strip_html(c['body'])[:1000]}"
        for i, c in enumerate(chunk)
    )

    out = gemini_call(
        CREDIT_PROMPT.replace(
            "{items}",
            blob,
        )
    )

    return parse_json_array(
        out,
        len(chunk),
    )


def run_credit_watch():
    try:
        state = load_credit_state()

        cands = []

        if ENABLE_PODCAST:
            print("── 팟캐스트 ──")
            cands += collect_podcast(
                state
            )

        if ENABLE_CREDIT_YT:
            print("── 크레딧 유튜브 ──")
            cands += collect_credit_youtube(
                state
            )

        if ENABLE_NEWS:
            print("── 뉴스 ──")
            cands += collect_news(
                state
            )

        cands = cands[
            :CREDIT_MAX_CANDIDATES
        ]

        nb = (
            len(cands)
            + BATCH_SIZE
            - 1
        ) // BATCH_SIZE

        print(
            f"[크레딧] 판정 대상 "
            f"{len(cands)}건 → "
            f"배치 {nb}회"
        )

        sent = 0

        for i in range(
            0,
            len(cands),
            BATCH_SIZE,
        ):
            chunk = cands[
                i:i + BATCH_SIZE
            ]

            results = judge_credit_batch(
                chunk
            )

            for c, j in zip(
                chunk,
                results,
            ):
                if j is None:
                    print(
                        f"⚠ 판정실패: "
                        f"{c['title'][:50]}"
                    )
                    continue

                if c["seen_key"]:
                    state[
                        c["seen_key"][0]
                    ].append(
                        c["seen_key"][1]
                    )

                try:
                    score = int(
                        j.get(
                            "relevance_score",
                            0,
                        )
                        or 0
                    )
                except Exception:
                    score = 0

                threshold = (
                    NEWS_SCORE_THRESHOLD
                    if c["kind"] == "뉴스"
                    else CREDIT_SCORE_THRESHOLD
                )

                if (
                    score >= threshold
                    and sent < CREDIT_MAX_SEND
                ):
                    pts = "".join(
                        f"\n • {html.escape(str(p))}"
                        for p in (
                            j.get(
                                "key_points"
                            )
                            or []
                        )[:3]
                    )

                    send_tg(
                        f"{c['icon']} "
                        f"<b>{html.escape(c['kind'])}</b> · "
                        f"{html.escape(c['source'])}\n"
                        f"<b>{html.escape(c['title'])}</b>\n\n"
                        f"💡 {html.escape(j.get('summary_kr', ''))}"
                        f"{pts}\n\n"
                        f"점수: {score}/10\n"
                        f"{c['url']}"
                    )

                    sent += 1

                    print(
                        f"✅ [{score}] "
                        f"{c['source']} - "
                        f"{c['title'][:50]}"
                    )

                else:
                    print(
                        f"❌ [{score}] "
                        f"{c['title'][:50]}"
                    )

            time.sleep(1.5)

            if _gm["dead"]:
                break

        save_credit_state(state)

        print(
            f"[크레딧] 전송 {sent}건"
        )

    except Exception as e:
        print(
            f"[크레딧 감시 실패] "
            f"{str(e)[:250]}"
        )


# ============================================================
# MAIN
# ============================================================

def main():
    try:
        run_celeb_watch()
    except Exception as e:
        print(
            f"[셀럽 감시 실패] "
            f"{str(e)[:250]}"
        )

    try:
        run_blog_watch()
    except Exception as e:
        print(
            f"[블로그 감시 실패] "
            f"{str(e)[:250]}"
        )

    try:
        run_credit_watch()
    except Exception as e:
        print(
            f"[크레딧 감시 실패] "
            f"{str(e)[:250]}"
        )

    print(
        f"=== Gemini 총 호출 "
        f"{_gm['n']}회 / 상한 {MAX_GEMINI_CALLS} ==="
    )


if __name__ == "__main__":
    main()
