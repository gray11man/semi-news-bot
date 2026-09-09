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
from urllib.parse import urlparse, parse_qs

import requests
import feedparser


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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
    """텔레그램 전송 성공 여부를 반환한다. 실패한 항목은 seen 처리하지 않는다."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": msg[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json=payload,
                timeout=30,
            )

            # Telegram rate limit
            if r.status_code == 429:
                try:
                    retry_after = int(
                        r.json().get("parameters", {}).get("retry_after", 3)
                    )
                except Exception:
                    retry_after = 3

                print(f"[텔레그램 429] {retry_after}초 후 재시도")
                time.sleep(min(max(retry_after, 1), 30))
                continue

            r.raise_for_status()

            data = r.json()
            if data.get("ok") is True:
                return True

            print(f"[텔레그램 실패] 응답 ok=false | {str(data)[:250]}")

        except Exception as e:
            print(f"[텔레그램 실패] attempt={attempt + 1} | {str(e)[:200]}")

        if attempt < 2:
            time.sleep(2 ** attempt)

    return False


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


def atomic_json_dump(path, data, *, indent=None):
    """중간 종료로 seen 파일이 깨지는 것을 줄이기 위한 원자적 저장."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=indent,
        )
    os.replace(tmp, path)


def _retry_delay(body):
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)', body or "")
    return int(m.group(1)) if m else None


def gemini_call(prompt, max_retry=2):
    if _gm["dead"]:
        return None

    for model in GEMINI_MODELS:
        for attempt in range(max_retry):
            if _gm["n"] >= MAX_GEMINI_CALLS:
                print(f"[Gemini] 예산 {MAX_GEMINI_CALLS}회 소진")
                _gm["dead"] = True
                _notify_gemini_dead(f"호출 예산 {MAX_GEMINI_CALLS}회 소진")
                return None

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
                    body = r.text[:500].replace("\\n", " ")
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

                data = r.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    print(f"[Gemini] {model} 응답에 candidates 없음")
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                txt = "".join(
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict)
                ).strip()

                if not txt:
                    print(f"[Gemini] {model} 빈 텍스트 응답")
                    continue

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
        cleaned = re.sub(r"```(?:json)?|```", "", str(out), flags=re.I).strip()

        # 모델이 앞뒤에 문장을 붙여도 첫 JSON 배열만 복구한다.
        if not cleaned.startswith("["):
            m = re.search(r"\[.*\]", cleaned, flags=re.S)
            if m:
                cleaned = m.group(0)

        arr = json.loads(cleaned)

        if not isinstance(arr, list):
            return [None] * n

        res = [None] * n
        used = set()

        # idx가 정상적으로 들어온 항목 우선 배치
        for pos, j in enumerate(arr):
            if not isinstance(j, dict):
                continue

            i = j.get("idx")
            if isinstance(i, int) and 0 <= i < n and i not in used:
                res[i] = j
                used.add(i)

        # idx 누락 시 입력 순서로 보완
        if len(arr) == n:
            for i, j in enumerate(arr):
                if res[i] is None and isinstance(j, dict):
                    res[i] = j

        return res

    except Exception as e:
        print(f"[배치 파싱 실패] {str(e)[:150]} | {str(out)[:250]}")
        return [None] * n


# ============================================================
# PART 2 — AI / 반도체 / 데이터센터 핵심인물 유튜브 감시
# ============================================================

SEEN_FILE = os.path.join(BASE_DIR, "seen_celeb_ids.json")

LOOKBACK_HOURS = 72

# 기본 검색은 YouTube API 단계에서 20분 초과(long)만 받는다.
# 4~20분(medium)은 최핵심 인물만 예외적으로 추가 검색한다.
MIN_DURATION_SEC = 1200
CORE_MEDIUM_MIN_SEC = 240

# 비핵심 인물은 6개 조로 나눠 6시간 슬롯마다 순환 검색한다.
# LOOKBACK_HOURS=72라 36시간에 한 번 검색해도 최근 업로드를 놓치지 않는다.
NONCORE_ROTATION_SHARDS = 6
SEARCH_GROUP_SIZE = 8

# 후보 우선순위용.
PREFERRED_DURATION_SEC = 1200

# 최종 알림은 매우 엄격하게.
SCORE_THRESHOLD = 8
MAX_CELEB_CANDIDATES = 32

# Gemini가 읽는 설명 길이.
DESC_CHARS_FOR_GEMINI = 2200


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

    # Oracle / enterprise AI / cloud infrastructure
    "Larry Ellison": ["larry ellison", "lawrence ellison", "oracle cto"],
    "Safra Catz": ["safra catz"],
    "Clay Magouyrk": ["clay magouyrk", "oracle cloud ceo"],
    "Mike Sicilia": ["mike sicilia", "oracle industries ceo"],
    "T.K. Anand": ["t.k. anand", "tk anand oracle"],

    # Arm / CPU architecture
    "Rene Haas": ["rene haas", "rené haas", "arm ceo"],
    "Mohamed Awad": ["mohamed awad arm"],

    # EDA / semiconductor design software
    "Sassine Ghazi": ["sassine ghazi", "synopsys ceo"],
    "Aart de Geus": ["aart de geus", "synopsys"],
    "Anirudh Devgan": ["anirudh devgan", "cadence ceo"],

    # Semiconductor equipment
    "Christophe Fouquet": ["christophe fouquet", "asml ceo"],
    "Roger Dassen": ["roger dassen", "asml cfo"],
    "Gary Dickerson": ["gary dickerson", "applied materials ceo"],
    "Prabu Raja": ["prabu raja", "applied materials semiconductor"],
    "Tim Archer": ["tim archer", "timothy archer lam research"],
    "Rick Wallace": ["rick wallace", "kla ceo"],

    # Memory / storage
    "Manish Bhatia": ["manish bhatia micron"],
    "Scott DeBoer": ["scott deboer", "micron cto"],
    "David Goeckeler": ["david goeckeler", "sandisk ceo"],
    "Irving Tan": ["irving tan", "western digital ceo"],

    # AI interconnect / optics / connectivity
    "Jitendra Mohan": ["jitendra mohan", "astera labs ceo"],
    "Sanjay Gajendra": ["sanjay gajendra", "astera labs"],
    "Sandeep Bharathi": ["sandeep bharathi", "marvell data center"],
    "Will Chu": ["will chu marvell", "marvell custom cloud"],
    "Noam Mizrahi": ["noam mizrahi marvell"],
    "Jim Anderson": ["jim anderson coherent", "coherent ceo"],
    "Alan Lowe": ["alan lowe lumentum", "lumentum ceo"],
    "Bill Brennan": ["bill brennan credo", "credo semiconductor ceo"],
    "Matthew Prince": ["matthew prince", "cloudflare ceo"],

    # Data center / power / cooling / energy
    "Olivier Blum": ["olivier blum", "schneider electric ceo"],
    "Adaire Fox-Martin": ["adaire fox-martin", "equinix ceo"],
    "Andy Power": ["andy power digital realty", "andrew power digital realty"],
    "Joe Dominguez": ["joe dominguez constellation energy"],
    "Jim Burke": ["jim burke vistra", "vistra ceo"],
    "Arkady Volozh": ["arkady volozh", "nebius ceo"],

    # Frontier AI / research / model ecosystem
    "Fidji Simo": ["fidji simo", "openai applications"],
    "Brad Lightcap": ["brad lightcap", "openai coo"],
    "Wojciech Zaremba": ["wojciech zaremba", "openai"],
    "Andrej Karpathy": ["andrej karpathy"],
    "Fei-Fei Li": ["fei-fei li", "fei fei li", "world labs"],
    "Alexandr Wang": ["alexandr wang", "alexander wang ai"],
    "Nat Friedman": ["nat friedman", "nathaniel friedman ai"],
    "Daniel Gross": ["daniel gross ai"],
    "David Luan": ["david luan ai"],
    "Barret Zoph": ["barret zoph", "thinking machines"],

    # Robotics / embodied AI
    "Karol Hausman": ["karol hausman", "physical intelligence"],
    "Sergey Levine": ["sergey levine", "physical intelligence"],
    "Chelsea Finn": ["chelsea finn", "physical intelligence"],
    "Deepak Pathak": ["deepak pathak", "skild ai"],
    "Abhinav Gupta": ["abhinav gupta", "skild ai"],
    "Bernt Bornich": ["bernt bornich", "bernt børnich", "1x technologies"],
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
    "Larry Ellison",
    "Clay Magouyrk",
    "Rene Haas",
    "Sassine Ghazi",
    "Anirudh Devgan",
    "Christophe Fouquet",
    "Gary Dickerson",
    "Tim Archer",
    "Rick Wallace",
    "Manish Bhatia",
    "David Goeckeler",
    "Jitendra Mohan",
    "Sandeep Bharathi",
    "Jim Anderson",
    "Olivier Blum",
    "Adaire Fox-Martin",
    "Andy Power",
    "Arkady Volozh",
    "Fidji Simo",
    "Andrej Karpathy",
    "Fei-Fei Li",
}


# 4~20분짜리까지 매 실행마다 따로 검색할 최핵심 인물.
# medium 검색은 search.list를 추가로 소모하므로 정말 중요한 인물만 둔다.
ULTRA_CORE_MEDIUM = {
    "Jensen Huang",
    "Sam Altman",
    "Dario Amodei",
    "Demis Hassabis",
    "Sundar Pichai",
    "Satya Nadella",
    "Lisa Su",
    "Mark Zuckerberg",
    "Elon Musk",
    "Hock Tan",
    "Sanjay Mehrotra",
    "C.C. Wei",
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


def _make_name_batches(names, duration):
    batches = []
    for i in range(0, len(names), SEARCH_GROUP_SIZE):
        group = names[i:i + SEARCH_GROUP_SIZE]
        if not group:
            continue
        q = "|".join(f'"{x}"' for x in group)
        batches.append((q, duration))
    return batches


def build_search_batches(now=None):
    """
    초절약형 검색 계획.

    - 핵심 인물 전체: 매 실행마다 20분 초과(long) 검색
    - 최핵심 인물만: 4~20분(medium) 추가 검색
    - 비핵심 인물: 6개 조로 순환하며 해당 조만 long 검색

    LOOKBACK_HOURS가 72시간이므로 비핵심은 36시간 주기로 돌아도
    정상적인 6시간 스케줄에서는 최근 업로드를 다시 잡을 수 있다.
    """
    now = now or datetime.now(timezone.utc)
    slot = int(now.timestamp() // (6 * 3600)) % NONCORE_ROTATION_SHARDS

    names = list(PERSONS.keys())
    core_names = [n for n in names if n in CORE_PERSONS]
    medium_names = [n for n in core_names if n in ULTRA_CORE_MEDIUM]
    noncore_names = [n for n in names if n not in CORE_PERSONS]
    rotated_noncore = [
        n for idx, n in enumerate(noncore_names)
        if idx % NONCORE_ROTATION_SHARDS == slot
    ]

    batches = []
    batches += _make_name_batches(core_names, "long")
    batches += _make_name_batches(medium_names, "medium")
    batches += _make_name_batches(rotated_noncore, "long")

    return (
        batches, slot, len(core_names), len(medium_names),
        len(rotated_noncore), len(noncore_names)
    )


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
    "oracle",
    "arm",
    "asml",
    "applied materials",
    "lam research",
    "kla",
    "synopsys",
    "cadence",
    "micron",
    "sandisk",
    "western digital",
    "astera labs",
    "marvell",
    "broadcom",
    "coherent",
    "lumentum",
    "credo",
    "cloudflare",
    "equinix",
    "digital realty",
    "vertiv",
    "schneider electric",
    "ge vernova",
    "nebius",
    "constellation energy",
    "vistra",
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
    atomic_json_dump(
        SEEN_FILE,
        sorted(seen)[-5000:],
    )


class YouTubeQuotaError(RuntimeError):
    pass


def yt_search(query, published_after, duration="long"):
    # search.list 단계에서 길이를 먼저 거른다.
    # long   = 20분 초과
    # medium = 4~20분 (핵심 인물 예외 검색에만 사용)
    if duration not in {"long", "medium"}:
        duration = "long"

    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "key": YOUTUBE_API_KEY,
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": published_after,
            "videoDuration": duration,
        },
        timeout=30,
    )

    if r.status_code in (403, 429):
        body = r.text[:600].lower()
        if "quota" in body or "rate" in body:
            raise YouTubeQuotaError(r.text[:500])

    r.raise_for_status()
    return r.json().get("items", [])


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


DIRECT_GUEST_SIGNALS = [
    "guest", "guest:", "our guest", "joined by", "joins us", "joins ",
    "sit down with", "sits down with", "in conversation with",
    "conversation with", "interview with", "speaks with", "talks with",
    "fireside chat with", "featuring", "welcomes", "keynote by",
    "keynote from", "panelist", "대담", "인터뷰", "출연", "초대",
]


def _person_aliases(person):
    return list(dict.fromkeys(
        [person.lower()] + [a.lower() for a in PERSONS.get(person, []) if a]
    ))


def _person_mentioned(person, text):
    t = (text or "").lower()
    return any(a in t for a in _person_aliases(person))


def _guest_context(person, text, window=140):
    """인물명 근처에 '게스트/인터뷰/키노트' 표현이 실제로 붙어 있는지 확인."""
    t = re.sub(r"\\s+", " ", (text or "").lower())

    for alias in _person_aliases(person):
        start = 0
        while True:
            idx = t.find(alias, start)
            if idx < 0:
                break

            left = max(0, idx - window)
            right = min(len(t), idx + len(alias) + window)
            ctx = t[left:right]

            if any(sig in ctx for sig in DIRECT_GUEST_SIGNALS):
                return True

            start = idx + max(1, len(alias))

    return False


def direct_metadata_evidence(person, item, detail):
    """
    Gemini가 제목만 보고 출연을 상상하지 못하게 하는 코드측 안전장치.
    TRUE여도 최종 판정은 Gemini가 다시 한다.
    """
    title = item.get("snippet", {}).get("title", "") or ""
    channel = item.get("snippet", {}).get("channelTitle", "") or ""
    desc = detail.get("snippet", {}).get("description", "") or ""

    name_in_title = _person_mentioned(person, title)
    format_in_title = contains_any(title, INTERVIEW_SIGNALS)
    guest_in_desc = _guest_context(person, desc[:3000])
    guest_in_title = _guest_context(person, title)
    trusted = any(t in channel.lower() for t in TRUSTED_CHANNELS)

    # 제목이 명백한 해설/분석이면 직접출연 근거로 승격하지 않는다.
    if contains_any(title, NEGATIVE_CONTENT_SIGNALS):
        strong_direct_title = contains_any(
            title,
            [
                "full interview",
                "in conversation",
                "fireside chat",
                "keynote",
                "podcast",
                "panel discussion",
                "인터뷰",
                "키노트",
                "대담",
            ],
        )
        if not strong_direct_title:
            return False, "제목이 해설/분석형 콘텐츠"

    # 가장 강한 신호: 제목 자체가 '인물 + 인터뷰/키노트/패널/팟캐스트'
    if name_in_title and (format_in_title or guest_in_title):
        return True, "제목에 인물명과 직접출연 형식이 함께 있음"

    # 설명에 게스트/진행자/행사 문맥과 인물명이 근접해 있음
    if guest_in_desc:
        return True, "설명에 인물명이 게스트/인터뷰/행사 문맥으로 명시됨"

    # 신뢰 채널이라도 이름만 있으면 부족하다. 설명에 출연 형식이 함께 있어야 한다.
    if trusted and name_in_title and contains_any(desc[:2500], INTERVIEW_SIGNALS):
        return True, "신뢰 채널 + 제목 인물명 + 설명의 출연 형식"

    return False, "직접출연을 뒷받침하는 메타데이터 근거 부족"


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

    direct_meta, _ = direct_metadata_evidence(person, item, detail)
    if direct_meta:
        score += 5
    else:
        score -= 1

    return score


def hard_filter(item, detail):
    title = item["snippet"].get("title", "")
    channel = item["snippet"].get("channelTitle", "")
    desc = detail.get("snippet", {}).get("description", "") or ""

    person = match_person(title) or match_person(desc[:3000])

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

    if dur < MIN_DURATION_SEC:
        # 20분 이하 영상은 원칙적으로 탈락.
        # 단, 핵심 인물의 4~20분짜리 원본 인터뷰/키노트/패널은 예외 허용한다.
        if person not in CORE_PERSONS or dur < CORE_MEDIUM_MIN_SEC:
            return None, f"길이 미달 ({dur // 60}분)"

        direct_meta, direct_reason = direct_metadata_evidence(person, item, detail)
        if not direct_meta:
            return None, f"20분 이하 직접출연 근거 부족 ({dur // 60}분)"

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

        direct_meta, direct_meta_reason = direct_metadata_evidence(
            person,
            item,
            detail,
        )

        lines.append(
            f"[{i}] 인물: {person}\n"
            f"모드: {mode}\n"
            f"메타후보점수: {meta_score}\n"
            f"코드측 직접출연근거: {direct_meta}\n"
            f"코드측 근거설명: {direct_meta_reason}\n"
            f"제목: {item['snippet'].get('title', '')}\n"
            f"채널: {item['snippet'].get('channelTitle', '')}\n"
            f"설명: {desc}"
        )

    # CELEB_PROMPT 안의 JSON 예시 중괄호를 str.format이 변수로 오인하지 않도록
    # 단순 replace로 판정 대상 자리만 치환한다.
    out = gemini_call(
        CELEB_PROMPT.replace(
            "{items}",
            "\n\n".join(lines),
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

    return send_tg(msg)


def run_celeb_watch():
    seen = load_seen()

    published_after = (
        datetime.now(timezone.utc)
        - timedelta(hours=LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = {}
    quota_stopped = False

    (
        search_batches, rotation_slot, core_count, medium_count,
        rotated_count, noncore_count
    ) = build_search_batches()
    print(
        f"[셀럽] 검색계획: 핵심 {core_count}명 long 매회, "
        f"최핵심 {medium_count}명만 medium 추가, "
        f"비핵심 {rotated_count}/{noncore_count}명 순환조 "
        f"{rotation_slot + 1}/{NONCORE_ROTATION_SHARDS}, "
        f"search.list 최대 {len(search_batches)}회"
    )

    for q, duration in search_batches:
        try:
            for it in yt_search(
                q,
                published_after,
                duration,
            ):
                vid = it.get("id", {}).get("videoId")

                if not vid:
                    continue

                if vid not in seen:
                    candidates[vid] = it

        except YouTubeQuotaError:
            print("[셀럽] YouTube API 쿼터 소진/제한 감지 → 남은 검색 중단")
            quota_stopped = True
            break

        except Exception as e:
            print(
                f"[셀럽 검색 실패] {q}: {str(e)[:150]}"
            )

        time.sleep(0.5)

    print(
        f"[셀럽] 신규 후보: {len(candidates)}건"
        + (" (쿼터로 검색 조기종료)" if quota_stopped else "")
    )

    if not candidates:
        save_seen(seen)

        if NOTIFY_WHEN_EMPTY and not quota_stopped:
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
                direct_meta, _ = direct_metadata_evidence(
                    person,
                    item,
                    detail,
                )

                # 작은 컨퍼런스/대학 채널도 진짜 인터뷰를 올릴 수 있으므로
                # 직접출연 근거가 있으면 하드 탈락시키지 않는다.
                if not direct_meta:
                    seen.add(vid)

                    print(
                        f"❌ [{why}] "
                        f"{item['snippet'].get('channelTitle', '')[:30]} | "
                        f"{item['snippet'].get('title', '')[:45]}"
                    )
                    continue

                candidate = (
                    person,
                    item,
                    detail,
                    vid,
                    meta_score - 2,
                )

            filtered.append(candidate)

        passed = filtered

    passed.sort(
        key=lambda x: x[4],
        reverse=True,
    )

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

            direct_meta, direct_meta_reason = direct_metadata_evidence(
                person,
                item,
                detail,
            )

            should_send = (
                direct_meta is True
                and direct is True
                and original is True
                and indirect is False
                and synthetic is False
                and bool(evidence)
                and appearance_conf >= 8
                and score >= SCORE_THRESHOLD
            )

            if should_send:
                ok = send_telegram_celeb(
                    person,
                    item,
                    j,
                    vid,
                )

                if ok:
                    seen.add(vid)
                    sent += 1

                    print(
                        f"✅ {person} | "
                        f"출연확신 {appearance_conf}/10 | "
                        f"관련성 {score}/10 | "
                        f"{item['snippet'].get('title', '')[:60]}"
                    )
                else:
                    print(
                        f"⚠ 텔레그램 전송실패 → seen 미처리/다음사이클 재시도 | "
                        f"{person} | {item['snippet'].get('title', '')[:55]}"
                    )

            else:
                # 정상적으로 판정해 탈락한 항목만 seen 처리
                seen.add(vid)

                print(
                    f"❌ [{person}] "
                    f"meta={direct_meta}({direct_meta_reason}) "
                    f"direct={direct} "
                    f"original={original} "
                    f"indirect={indirect} "
                    f"synthetic={synthetic} "
                    f"confidence={appearance_conf} "
                    f"score={score} | "
                    f"{j.get('reason', '')[:55]}"
                )

        time.sleep(1.2)

        if _gm["dead"]:
            print("[셀럽] Gemini 중단 → 미판정 항목은 다음 사이클 재시도")
            break

    save_seen(seen)

    if sent == 0 and NOTIFY_WHEN_EMPTY and not quota_stopped:
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

SEEN_BLOG_FILE = os.path.join(BASE_DIR, "seen_twitter_blog.json")
BLOG_MAX_AGE_HOURS = 72
BLOG_FIRST_RUN_SEND_HOURS = 8


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
    atomic_json_dump(
        SEEN_BLOG_FILE,
        state,
        indent=2,
    )


def _canonical_blog_link(entry, blog_id):
    """
    네이버 RSS가 PostView.naver?blogId=...&logNo=... 형태를 줄 때
    쿼리를 통째로 버리면 모든 글 ID가 같은 주소가 되는 치명적 문제가 생긴다.
    logNo를 보존한 정규 URL로 바꾼다.
    """
    raw = (entry.get("link", "") or "").strip()

    if not raw:
        eid = (
            entry.get("id")
            or entry.get("guid")
            or ""
        )
        return str(eid), str(eid)

    try:
        u = urlparse(raw)
        qs = parse_qs(u.query)

        q_blog = (
            (qs.get("blogId") or [blog_id])[0]
            or blog_id
        )
        log_no = (
            (qs.get("logNo") or [None])[0]
        )

        if not log_no:
            m = re.search(
                r"/(?:[^/]+)/([0-9]{6,})(?:/)?$",
                u.path or "",
            )
            if m:
                log_no = m.group(1)

        if log_no:
            canonical = f"https://blog.naver.com/{q_blog}/{log_no}"
            return canonical, canonical

        # 알려진 추적 파라미터만 제거. 식별에 필요한 쿼리는 함부로 버리지 않는다.
        clean = raw.split("#", 1)[0].rstrip("/")
        eid = (
            entry.get("id")
            or entry.get("guid")
            or clean
        )
        return str(eid), clean

    except Exception:
        eid = (
            entry.get("id")
            or entry.get("guid")
            or raw
        )
        return str(eid), raw


def fetch_blog_posts(blog_id):
    try:
        resp = requests.get(
            f"https://rss.blog.naver.com/{blog_id}.xml",
            headers={
                "User-Agent": UA,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            timeout=20,
        )

        if resp.status_code != 200:
            print(
                f"[블로그 오류] {blog_id}: "
                f"HTTP {resp.status_code}"
            )
            return []

        feed = feedparser.parse(resp.content)

        if getattr(feed, "bozo", False):
            print(
                f"[블로그 경고] {blog_id}: RSS 파싱 경고 "
                f"{str(getattr(feed, 'bozo_exception', ''))[:120]}"
            )

    except Exception as e:
        print(f"[블로그 오류] {blog_id}: {e}")
        return []

    posts = []

    for entry in feed.entries[:40]:
        post_id, clean_url = _canonical_blog_link(
            entry,
            blog_id,
        )

        pub = (
            entry.get("published", "")
            or entry.get("updated", "")
        )

        title = strip_html(
            entry.get(
                "title",
                "(제목 없음)",
            )
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
                "id": post_id
                or f"{blog_id}:{title}:{pub}",
                "title": title,
                "url": clean_url,
                "pub_dt": pub_dt,
            }
        )

    # RSS가 중복 엔트리를 줄 때도 한 번만 처리
    deduped = []
    ids = set()

    for p in posts:
        if not p["id"] or p["id"] in ids:
            continue
        ids.add(p["id"])
        deduped.append(p)

    print(
        f"[블로그] {blog_id}: {len(deduped)}건"
    )

    return deduped


def run_blog_watch():
    try:
        state = load_blog_state()
        seen_map = state.setdefault(
            "blog",
            {},
        )

        total = 0
        now = datetime.now(timezone.utc)
        normal_cutoff = now - timedelta(
            hours=BLOG_MAX_AGE_HOURS
        )
        first_cutoff = now - timedelta(
            hours=BLOG_FIRST_RUN_SEND_HOURS
        )

        for blog_id in NAVER_BLOG_IDS:
            posts = fetch_blog_posts(blog_id)

            if not posts:
                continue

            first = blog_id not in seen_map
            already = set(
                seen_map.get(
                    blog_id,
                    [],
                )
            )

            # 구버전은 PostView.naver의 ? 뒤를 잘라 모든 글을 같은 ID로 만들 수 있었다.
            legacy_collapsed = any(
                str(x).rstrip("/") == "https://blog.naver.com/PostView.naver"
                for x in already
            )

            fresh = [
                p for p in posts
                if p["id"]
                and p["id"] not in already
            ]

            newly_seen = set()
            send_cutoff = (
                first_cutoff
                if first or legacy_collapsed
                else normal_cutoff
            )

            if first:
                print(
                    f"[블로그] {blog_id}: 첫 실행 — "
                    f"최근 {BLOG_FIRST_RUN_SEND_HOURS}시간 글만 알림"
                )

            if legacy_collapsed:
                print(
                    f"[블로그] {blog_id}: 구버전 PostView ID 충돌 감지 — "
                    "최근 글만 복구 알림"
                )

            # 오래된 신규 항목은 과거 backlog로 보고 전송 없이 seen 처리
            eligible = []
            for p in fresh:
                if (
                    p["pub_dt"] is not None
                    and p["pub_dt"] < send_cutoff
                ):
                    newly_seen.add(p["id"])
                    continue

                eligible.append(p)

            # RSS는 보통 최신순이므로 오래된 것부터 알림
            for p in reversed(eligible):
                ok = send_tg(
                    f"📝 <b>{html.escape(blog_id)}</b> 새 글\n\n"
                    f"{html.escape(p['title'])}\n\n"
                    f"{p['url']}"
                )

                if ok:
                    newly_seen.add(p["id"])
                    total += 1
                    print(
                        f"✅ [블로그] {blog_id} | "
                        f"{p['title'][:60]}"
                    )
                else:
                    print(
                        f"⚠ [블로그] 전송실패 → seen 미처리/다음사이클 재시도 | "
                        f"{blog_id} | {p['title'][:55]}"
                    )

                time.sleep(0.4)

            # 현재 RSS에서 확인된 기존 글 + 정상 처리한 신규 글만 저장.
            # 전송 실패 신규 글은 일부러 넣지 않아 다음 주기에 재시도한다.
            current_existing = [
                p["id"]
                for p in posts
                if p["id"] in already
            ]

            merged = list(
                dict.fromkeys(
                    list(newly_seen)
                    + current_existing
                    + list(already)
                )
            )

            seen_map[blog_id] = merged[:200]

            # 한 블로그 처리 후 즉시 저장해 중간 종료에도 상태 보존
            save_blog_state(state)

        print(
            f"[블로그] {total}건 전송"
        )

    except Exception as e:
        print(
            f"[블로그 감시 실패] {str(e)[:200]}"
        )


# ============================================================
# PART 4 — 크레딧 / 사모대출 / AI CAPEX 금융 감시
# ============================================================

CREDIT_STATE_FILE = os.path.join(BASE_DIR, "seen_credit.json")
FEED_CACHE_VERSION = 4

ENABLE_PODCAST = True
ENABLE_CREDIT_YT = False
ENABLE_NEWS = True

PODCAST_MAX_AGE_DAYS = 5
NEWS_LOOKBACK_HOURS = 48

# 뉴스는 "관련 뉴스"가 아니라 "투자 판단을 바꿀 정도의 중요 뉴스"만 전송
NEWS_SCORE_THRESHOLD = 9
NEWS_MAX_CANDIDATES = 10
NEWS_TITLE_SIMILARITY = 0.72
NEWS_EVENT_TTL_DAYS = 7

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
    '"private credit" ("data center" OR "data centre" OR "AI infrastructure")',
    '("data center" OR "data centre") ("debt financing" OR "project finance" OR "credit spread")',
    '("AI capex" OR "AI infrastructure") (bond OR debt OR financing OR prepayment)',
    '(HBM OR DRAM OR NAND) ("contract price" OR pricing OR prepayment OR "supply agreement")',
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
    s.setdefault("news_events", {})

    if s.get("feed_ver") != FEED_CACHE_VERSION:
        print("[캐시] 피드 캐시 재해석")
        s["feeds"] = {}
        s["feed_ver"] = FEED_CACHE_VERSION

    # 오래된 사건 중복키는 자동 정리
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    cleaned = {}

    for k, v in (s.get("news_events") or {}).items():
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if dt >= cutoff:
                cleaned[k] = v
        except Exception:
            continue

    s["news_events"] = cleaned
    return s


def save_credit_state(s):
    s["youtube"] = s["youtube"][-1500:]
    s["news"] = s["news"][-1500:]

    for k in s["podcast"]:
        s["podcast"][k] = s["podcast"][k][:100]

    # 사건키는 최근 것만 유지
    cutoff = datetime.now(timezone.utc) - timedelta(days=10)
    cleaned = {}

    for k, v in (s.get("news_events") or {}).items():
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if dt >= cutoff:
                cleaned[k] = v
        except Exception:
            continue

    s["news_events"] = cleaned

    atomic_json_dump(
        CREDIT_STATE_FILE,
        s,
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

        if first:
            # 첫 실행은 과거 에피소드 폭탄 방지용 baseline
            state["podcast"][name] = [
                e["id"] for e in eps
            ][:100]

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
                state["podcast"].setdefault(name, []).append(ep["id"])
                continue

            cands.append(
                {
                    "kind": "팟캐스트",
                    "icon": "🎧",
                    "source": name,
                    "title": ep["title"],
                    "body": ep["desc"],
                    "url": ep["url"],
                    "seen_key": (
                        "podcast",
                        name,
                        ep["id"],
                    ),
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
    "bond issuance", "bond sale", "debt issuance", "debt financing",
    "credit facility", "loan facility", "private credit", "private debt",
    "default", "defaults", "bankruptcy", "restructuring", "distress",
    "downgrade", "rating cut", "credit rating", "credit spread",
    "spread widening", "spread widens", "refinancing", "refinance",
    "covenant breach", "covenant", "securitization", "asset-backed",
    "funding", "financing", "capital raise", "liquidity",
    "capex financing", "capital expenditure", "project finance",
    "vendor financing", "prepayment", "prepayment agreement",
    "long-term contract", "supply agreement", "purchase agreement",
    "contract price", "contract pricing", "price increase", "price hike",
]

NEWS_MAJOR_ENTITIES = [
    "microsoft", "google", "alphabet", "amazon", "aws", "meta", "oracle",
    "coreweave", "nvidia", "blackstone", "apollo", "ares", "kkr",
    "pimco", "wellington", "sk hynix", "samsung", "micron", "tsmc",
]

NEWS_HYPERSCALERS = [
    "microsoft", "google", "alphabet", "amazon", "aws", "meta", "oracle",
]

NEWS_AI_INFRA_TERMS = [
    "ai capex", "ai infrastructure", "data center", "datacenter",
    "data centre", "gpu", "accelerator", "compute capacity",
    "compute cluster", "training cluster", "inference cluster",
    "cloud infrastructure", "server capacity", "hbm",
]

NEWS_MEMORY_TERMS = [
    "hbm", "dram", "nand", "memory",
]

NEWS_MEMORY_EVENT_TERMS = [
    "contract price", "contract pricing", "pricing",
    "price increase", "price hike", "prepayment",
    "supply agreement", "purchase agreement",
]

NEWS_GENERIC_FINANCE_TERMS = [
    "bond issuance", "bond sale", "debt issuance", "debt financing",
    "funding", "financing", "capital raise", "refinancing", "refinance",
]

NEWS_DISTRESS_TERMS = [
    "default", "defaults", "bankruptcy", "restructuring", "distress",
    "downgrade", "rating cut", "covenant breach", "liquidity",
]

NEWS_PRIVATE_CREDIT_TERMS = [
    "private credit", "private debt", "direct lending",
    "asset-backed", "securitization", "securitisation",
    "project finance", "vendor financing",
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
    """
    뉴스는 '관련 있음'이 아니라 'AI 인프라 투자 판단을 바꿀 사건'만 남긴다.
    특히 하이퍼스케일러의 일반 회사채는 AI CAPEX/데이터센터 연결이 없으면 탈락.
    """
    text = f"{title} {strip_html(body)}".lower()

    critical = any(x in text for x in NEWS_CRITICAL_TERMS)
    ai_context = any(x in text for x in NEWS_AI_INFRA_TERMS)
    memory_context = any(x in text for x in NEWS_MEMORY_TERMS)
    memory_event = any(x in text for x in NEWS_MEMORY_EVENT_TERMS)
    hyperscaler = any(x in text for x in NEWS_HYPERSCALERS)
    generic_finance = any(x in text for x in NEWS_GENERIC_FINANCE_TERMS)
    distress = any(x in text for x in NEWS_DISTRESS_TERMS)
    private_credit = any(x in text for x in NEWS_PRIVATE_CREDIT_TERMS)
    major_entity = any(x in text for x in NEWS_MAJOR_ENTITIES)

    if any(x in text for x in NEWS_NOISE_TERMS):
        if not (critical or (memory_context and memory_event)):
            return False

    # 메모리 가격/계약은 그 자체로 핵심 관심사
    if memory_context and memory_event:
        return True

    # CoreWeave 부실/신용 이벤트는 AI 인프라 자체의 자금조달 문제라 직접 통과
    if "coreweave" in text and distress:
        return True

    # 사모크레딧/프로젝트 파이낸싱도 데이터센터·AI 인프라 연결이 있어야 한다.
    if private_credit:
        return ai_context

    # 핵심 수정:
    # Amazon/MS/Google/Meta/Oracle의 일반 채권 발행은 AI 용도 연결이 없으면 버린다.
    if hyperscaler and generic_finance and not ai_context:
        return False

    # 나머지 금융 이벤트는 AI 인프라 문맥 + 주요 주체가 같이 있어야 후보
    if critical and ai_context and major_entity:
        return True

    # 매우 명백한 AI 인프라 금융 이벤트는 회사명이 없어도 후보 허용
    strong_ai_finance = sum(
        1 for x in (
            "data center financing",
            "data centre financing",
            "project finance",
            "vendor financing",
            "capex financing",
            "prepayment",
        )
        if x in text
    )
    if ai_context and strong_ai_finance >= 1:
        return True

    return False


def _news_event_key(title, body):
    """같은 사건을 다른 언론사가 매일 다시 써도 며칠간 한 사건으로 묶는다."""
    text = f"{title} {strip_html(body)}".lower()

    entity = next(
        (x for x in NEWS_MAJOR_ENTITIES if x in text),
        "unknown",
    )

    if any(x in text for x in NEWS_MEMORY_EVENT_TERMS) and any(
        x in text for x in NEWS_MEMORY_TERMS
    ):
        bucket = "memory_contract_price"
    elif "private credit" in text or "private debt" in text:
        bucket = "private_credit"
    elif "project finance" in text or "data center financing" in text or "data centre financing" in text:
        bucket = "data_center_financing"
    elif any(x in text for x in NEWS_DISTRESS_TERMS):
        bucket = "distress_rating"
    elif "prepayment" in text:
        bucket = "prepayment"
    elif "supply agreement" in text or "purchase agreement" in text:
        bucket = "supply_agreement"
    elif any(x in text for x in ("bond issuance", "bond sale", "debt issuance")):
        bucket = "bond"
    elif any(x in text for x in NEWS_GENERIC_FINANCE_TERMS):
        bucket = "financing"
    else:
        bucket = "other"

    amount = ""
    m = re.search(
        r"(?:\$|£|€)?\s*\d+(?:\.\d+)?\s*(?:billion|million|bn|mn|b|m)\b",
        text,
    )
    if m:
        amount = re.sub(r"\s+", "", m.group(0))

    currency = next(
        (
            x for x in (
                "gbp", "sterling", "pound", "usd", "dollar",
                "eur", "euro", "yen", "jpy"
            )
            if x in text
        ),
        "",
    )

    # 금액이 없으면 제목의 핵심어 일부를 보조키로 사용
    if not amount:
        title_key = _news_title_key(title)
        stop = {
            "the", "a", "an", "to", "of", "for", "in", "on", "and",
            "with", "as", "at", "from", "says", "said",
        }
        words = [
            w for w in title_key.split()
            if w not in stop
        ]
        tail = "_".join(words[:5])
    else:
        tail = amount

    return f"{entity}|{bucket}|{currency}|{tail}"[:240]


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


def _news_event_recent(state, event_key):
    ts = (state.get("news_events") or {}).get(event_key)
    if not ts:
        return False

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (
            datetime.now(timezone.utc) - dt
        ) < timedelta(days=NEWS_EVENT_TTL_DAYS)
    except Exception:
        return False


def _mark_news_event(state, event_key):
    if not event_key:
        return

    state.setdefault("news_events", {})[event_key] = (
        datetime.now(timezone.utc).isoformat()
    )


def collect_news(state):
    seen = set(state["news"])
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(hours=NEWS_LOOKBACK_HOURS)
    )

    raw = []
    url_seen = set()
    title_seen = []
    cycle_events = set()

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
            r.raise_for_status()
            d = feedparser.parse(r.content)

        except Exception as e:
            print(
                f"[뉴스] 실패 ({q}): "
                f"{str(e)[:120]}"
            )
            continue

        for e in d.entries[:12]:
            link = e.get("link", "")
            title = strip_html(e.get("title", ""))
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
            event_key = _news_event_key(title, body)

            # 과거 며칠 내 같은 사건을 이미 처리했으면 다른 언론사 기사도 차단
            if _news_event_recent(state, event_key):
                state["news"].append(link)
                continue

            # 같은 실행 안에서 같은 사건 중복
            if event_key in cycle_events:
                state["news"].append(link)
                continue

            # 일반 회사채 등 명백한 잡음은 Gemini까지 보내지 않는다.
            if not _news_importance_prefilter(title, body):
                state["news"].append(link)
                _mark_news_event(state, event_key)
                print(
                    f"  ⏭ [뉴스 사전탈락] {title[:75]}"
                )
                continue

            # 제목 기반 중복도 한 번 더 제거
            if _news_duplicate(title, title_seen):
                state["news"].append(link)
                _mark_news_event(state, event_key)
                continue

            cycle_events.add(event_key)
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
                    "event_key": event_key,
                }
            )

        time.sleep(0.4)

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
- Amazon/MS/Google/Meta/Oracle의 일반 회사채 발행·통화별 채권 조달처럼
  AI CAPEX, 데이터센터, GPU/서버 조달, 전력 인프라와 직접 연결되지 않은 일반 재무 뉴스
- 특히 영국 파운드화/유로화/달러화 채권 발행이라는 이유만으로 높은 점수를 주지 마라

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


def _mark_credit_seen(state, c):
    key = c.get("seen_key")

    if key:
        kind = key[0]

        if kind == "podcast" and len(key) >= 3:
            _, name, eid = key[:3]
            arr = state["podcast"].setdefault(name, [])
            if eid not in arr:
                arr.append(eid)

        elif kind in ("youtube", "news") and len(key) >= 2:
            value = key[1]
            arr = state.setdefault(kind, [])
            if value not in arr:
                arr.append(value)

    if c.get("kind") == "뉴스":
        _mark_news_event(
            state,
            c.get("event_key"),
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
                        f"⚠ 판정실패(다음사이클 재시도): "
                        f"{c['title'][:50]}"
                    )
                    continue

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

                if score >= threshold:
                    if sent >= CREDIT_MAX_SEND:
                        print(
                            f"⏸ 전송상한 도달 → seen 미처리/다음사이클 재시도 | "
                            f"[{score}] {c['title'][:50]}"
                        )
                        continue

                    pts = "".join(
                        f"\n • {html.escape(str(p))}"
                        for p in (
                            j.get(
                                "key_points"
                            )
                            or []
                        )[:3]
                    )

                    ok = send_tg(
                        f"{c['icon']} "
                        f"<b>{html.escape(c['kind'])}</b> · "
                        f"{html.escape(c['source'])}\n"
                        f"<b>{html.escape(c['title'])}</b>\n\n"
                        f"💡 {html.escape(j.get('summary_kr', ''))}"
                        f"{pts}\n\n"
                        f"점수: {score}/10\n"
                        f"{c['url']}"
                    )

                    if ok:
                        _mark_credit_seen(
                            state,
                            c,
                        )
                        sent += 1

                        print(
                            f"✅ [{score}] "
                            f"{c['source']} - "
                            f"{c['title'][:50]}"
                        )
                    else:
                        print(
                            f"⚠ 텔레그램 전송실패 → seen 미처리/다음사이클 재시도 | "
                            f"[{score}] {c['title'][:50]}"
                        )

                else:
                    # 판정이 정상 완료된 탈락 항목만 seen 처리
                    _mark_credit_seen(
                        state,
                        c,
                    )

                    print(
                        f"❌ [{score}] "
                        f"{c['title'][:50]}"
                    )

            save_credit_state(state)
            time.sleep(1.2)

            if _gm["dead"]:
                print("[크레딧] Gemini 중단 → 미판정 항목은 다음 사이클 재시도")
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
