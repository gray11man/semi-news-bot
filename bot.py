#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI·반도체·메모리·데이터센터·전력·AI수요 산업 뉴스 에이전트 (v2)

핵심 변경점(v1 → v2):
  1) 발송 필터 강화: 점수 기준 상향 + 단신/주가류 감점 강화 + AI수요/관심종목 가점.
  2) 본문 읽기: 기사 URL 본문을 추출(trafilatura)해 Gemini에 전달 → 10~15문장 한국어 요약.
  3) 다국어 본문 번역: Gemini가 영/중/일/대만어 본문을 직접 읽고 한국어로 요약(언어 무관).
  4) AI 수요 뉴스 추가: 토큰소비·추론수요·캐파부족·가동률·매출가이던스 등 수요측 피드/키워드 신설.
  5) 구조 수정: '제목 → 요약' 순서로 출력(v1은 요약이 먼저 나오던 버그).

필요 패키지: requests, feedparser, trafilatura, deep-translator
  (GitHub Actions: pip install requests feedparser trafilatura deep-translator)

Gemini 최저가 모델: gemini-2.5-flash-lite ($0.10/1M in, $0.40/1M out, 2026.06 기준 최저가).
"""

import os
import re
import json
import time
import html
import random
import hashlib
import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse, quote

import requests
import feedparser

# 본문 추출(없어도 봇은 동작 — 요약 입력이 RSS 요약으로 축소될 뿐)
try:
    import trafilatura
    _HAS_TRAFI = True
except Exception:
    _HAS_TRAFI = False

# ───────────────────────── 환경변수 ─────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()

# ───────────────────────── 설정 ─────────────────────────
SEEN_FILE = "seen.json"
QUEUE_FILE = "queue.json"
NEWS_FILE = "news.json"        # 대시보드(thesis-tracker)가 읽는 파일
NEWS_MAX_ITEMS = 150           # news.json 누적 상한(오래된 것부터 제거)
SEEN_RETENTION_DAYS = 7
MAX_SEND_PER_RUN = 10
MIN_SCORE_TO_SEND = 5          # ↑ 상향(v1=3): 중요한 것만 통과
NEWS_WINDOW_HOURS = 6
SIMILARITY_THRESHOLD = 0.68
REQUEST_TIMEOUT = 25
SEND_DELAY = 1.0

GEMINI_MIN_INTERVAL = 6.5
GEMINI_MAX_CALLS_PER_RUN = 30
GEMINI_RETRY_MAX = 0
GEMINI_RETRY_BASE = 2.0
GEMINI_CONSEC_FAIL_STOP = 1
RSS_MAX_ENTRIES = 30

# 본문 추출 설정
FETCH_BODY = True             # 기사 본문 가져오기 on/off
BODY_FETCH_TIMEOUT = 12
BODY_MAX_CHARS = 6000         # Gemini 입력 비용 보호: 본문 앞 N자만 사용
BODY_FETCH_DELAY = 0.5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ───────────────────────── RSS 소스 ─────────────────────────
def gnews(query, lang="en", hours=NEWS_WINDOW_HOURS):
    q = quote(f"{query} when:{hours}h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    if lang == "zh":
        return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


CORE_EN = (
    "OpenAI OR Anthropic OR xAI OR \"Google DeepMind\" OR \"Meta AI\" OR Mistral OR "
    "Nvidia OR AMD OR Broadcom OR Marvell OR TSMC OR Samsung OR \"SK hynix\" OR Micron OR "
    "HBM OR DRAM OR NAND OR CXL OR CoWoS OR \"data center\" OR datacenter OR "
    "CoreWeave OR \"power grid\" OR \"gas turbine\" OR nuclear"
)
PEOPLE_EN = (
    "\"Jensen Huang\" OR \"Sam Altman\" OR \"Dario Amodei\" OR \"Ilya Sutskever\" OR "
    "\"Demis Hassabis\" OR \"Elon Musk\" OR \"Lisa Su\" OR \"Satya Nadella\" OR "
    "\"Sundar Pichai\" OR \"Hock Tan\""
)
MONEY_EN = (
    "AI capex OR AI funding OR \"data center investment\" OR \"GPU order\" OR "
    "\"HBM contract\" OR \"cloud deal\" OR AI acquisition OR semiconductor investment"
)
# ── AI 수요(신설): 토큰소비/추론수요/가동률/매출가이던스/캐파부족 등 수요측 신호 ──
DEMAND_EN = (
    "\"AI demand\" OR \"inference demand\" OR \"token usage\" OR \"compute demand\" OR "
    "\"GPU shortage\" OR \"capacity sold out\" OR \"AI revenue\" OR \"AI adoption\" OR "
    "\"AI workload\" OR \"datacenter utilization\" OR \"AI agent\" OR \"enterprise AI\" OR "
    "\"AI guidance\" OR \"backlog\" OR \"order backlog\""
)
CORE_KO = (
    "엔비디아 OR HBM OR DRAM OR 낸드 OR SK하이닉스 OR 삼성전자 반도체 OR "
    "데이터센터 OR AI 투자 OR AI 인프라 OR 반도체 증설 OR 전력 OR 가스터빈 OR CXL OR 패키징"
)
# ── AI 수요(한국어 신설) ──
DEMAND_KO = (
    "AI 수요 OR 추론 수요 OR AI 토큰 OR 연산 수요 OR GPU 부족 OR 캐파 부족 OR "
    "AI 매출 OR AI 가동률 OR AI 에이전트 OR 기업용 AI OR 수주잔고 OR AI 채택"
)

FEEDS = [
    # 미국/영문 핵심
    gnews(CORE_EN, "en"),
    gnews(PEOPLE_EN, "en"),
    gnews(MONEY_EN, "en"),
    gnews(DEMAND_EN, "en"),          # ← AI 수요 추가
    # 한국
    gnews(CORE_KO, "ko"),
    gnews("AI 데이터센터 OR HBM 공급 OR 반도체 수주 OR AI 전력 OR 원전 데이터센터", "ko"),
    gnews(DEMAND_KO, "ko"),          # ← AI 수요 추가
    # 일본
    gnews("AI半導体 OR HBM OR データセンター OR ラピダス OR 電力 AI OR AI需要 OR 推論需要", "ja"),
    # 중국
    gnews("人工智能 芯片 OR 数据中心 OR HBM OR 算力 OR 英伟达 OR AI需求 OR 推理需求", "zh"),
    # 대만
    gnews("台積電 OR CoWoS OR AI 伺服器 OR 半導體 產能 OR AI 需求", "zh"),

    # ── 사용자 관심 테마 ──
    gnews("한화엔진 OR 4행정 중속엔진 OR 데이터센터 발전엔진 OR 힘센엔진 OR 선박엔진 발전", "ko"),
    gnews("한화엔진 OR STX엔진 OR HD현대마린엔진 OR 데이터센터 엔진 OR 가스엔진 발전", "ko"),
    gnews("조선주 OR HD현대중공업 OR 삼성중공업 OR 한화오션 OR 조선 수주 OR LNG선 발주", "ko"),
    gnews("Tempus AI OR \"TEM stock\" OR Tempus oncology OR Tempus FDA", "en"),
    gnews("Tempus AI OR 템퍼스", "ko"),
]

# ───────────────────────── 필터 키워드 ─────────────────────────
INCLUDE = [
    "ai", "gpu", "hbm", "dram", "nand", "cxl", "cowos", "packaging", "wafer",
    "data center", "datacenter", "nvidia", "amd", "tsmc", "samsung", "hynix",
    "micron", "broadcom", "marvell", "openai", "anthropic", "xai", "deepmind",
    "capex", "funding", "investment", "acquisition", "power", "grid", "turbine",
    "nuclear", "transformer", "optical", "transceiver", "inference",
    # AI 수요
    "demand", "token", "workload", "backlog", "utilization", "adoption", "sold out",
    "인공지능", "반도체", "엔비디아", "메모리", "데이터센터", "고대역폭",
    "전력", "원전", "가스터빈", "패키징", "투자", "수주", "증설", "공급",
    "수요", "추론", "가동률", "토큰", "수주잔고",
    "半導体", "データセンター", "人工智能", "芯片", "数据中心", "算力", "台積電",
    "需要", "需求", "推論", "推理",
    "한화엔진", "4행정", "중속엔진", "힘센", "선박엔진", "조선", "hd현대중공업",
    "삼성중공업", "한화오션", "stx엔진", "lng선", "발전엔진", "가스엔진",
    "tempus", "템퍼스",
]
EXCLUDE = [
    "할인", "쿠폰", "이벤트", "광고", "분양", "운세", "로또",
    "casino", "porn", "coupon", "discount", "giveaway",
]

BOTTLENECK = [
    "hbm", "cowos", "packaging", "gpu", "dram", "nand", "optical", "transceiver",
    "power", "grid", "turbine", "substation", "cooling", "전력", "송전", "변전",
    "가스터빈", "냉각", "패키징", "고대역폭", "capacity", "shortage", "증설", "감산",
]
# AI 수요 신호(가점용)
DEMAND_SIGNALS = [
    "ai demand", "inference demand", "token usage", "compute demand",
    "sold out", "utilization", "backlog", "ai revenue", "ai adoption",
    "ai workload", "enterprise ai", "ai agent", "guidance",
    "ai 수요", "추론 수요", "연산 수요", "캐파 부족", "gpu 부족", "가동률",
    "ai 매출", "수주잔고", "ai 에이전트", "기업용 ai", "需求", "需要",
]


# ───────────────────────── 유틸 ─────────────────────────
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def prune_seen(seen):
    cutoff = (now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    return {k: v for k, v in seen.items() if v.get("ts", 0) >= cutoff}


def has_korean(t):
    return bool(re.search(r"[가-힣]", t or ""))


def norm_title(title):
    t = html.unescape(title or "")
    t = re.sub(r"\s*[-|·]\s*[^-|·]+$", "", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[\[\](){}<>·…“”\"'’‘|!?.,~―—\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def title_key(title):
    compact = re.sub(r"\s+", "", norm_title(title))
    return hashlib.md5(compact.encode("utf-8")).hexdigest()


def _tokens(s):
    return {w for w in norm_title(s).split() if len(w) >= 2}


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


SAME_EVENT_ACTORS = {
    "이재용", "이재명", "젠슨", "황", "올트먼", "머스크", "저커버그", "아모데이",
    "삼성전자", "하이닉스", "sk하이닉스", "마이크론", "엔비디아", "tsmc", "인텔",
    "openai", "anthropic", "삼성", "구글", "메타", "broadcom", "amd",
}
SAME_EVENT_ACTIONS = {
    "점검", "현장", "방문", "찾았다", "공급계약", "계약", "수주", "체결",
    "인수", "투자", "증설", "착공", "양산", "출시", "발표", "공개", "돌파",
    "선정", "협력", "파트너십", "상향", "하향", "목표가", "목표주가",
    "1위", "등극", "제치고", "추월",
}
ACTION_SYNONYMS = [
    {"점검", "현장", "방문", "찾았다", "둘러", "행보"},
    {"공급계약", "계약", "수주", "체결", "납품", "공급"},
    {"인수", "합병", "지분", "m&a"},
    {"투자", "증설", "착공", "신설", "구축", "확대"},
    {"양산", "출시", "공개", "발표", "선보", "상용화"},
    {"1위", "등극", "제치고", "추월", "역전", "왕좌"},
    {"상향", "하향", "목표가", "목표주가", "투자의견"},
]


def _action_group(action_set):
    groups = set()
    for a in action_set:
        for gi, grp in enumerate(ACTION_SYNONYMS):
            if a in grp:
                groups.add(gi)
    return groups


def _key_entities(s):
    toks = set(norm_title(s).split())
    actors = {a for a in SAME_EVENT_ACTORS if a in s.lower() or a in toks}
    actions = {a for a in SAME_EVENT_ACTIONS if a in s.lower() or a in toks}
    return actors, actions


def is_similar(a, b):
    if SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD:
        return True
    if _jaccard(a, b) >= 0.45:
        return True
    aa_actor, aa_action = _key_entities(a)
    bb_actor, bb_action = _key_entities(b)
    shared_actor = aa_actor & bb_actor
    shared_group = _action_group(aa_action) & _action_group(bb_action)
    if shared_actor and shared_group:
        if _jaccard(a, b) >= 0.12:
            return True
    return False


def passes_filter(title, summary):
    text = f"{title} {summary}".lower()
    if any(k in text for k in EXCLUDE):
        return False
    return any(k in text for k in INCLUDE)


def base_score(title, summary):
    """1차 중요도. 유동성/규모 이벤트(+3), 병목(+2), AI수요(+2), 관심종목(+2), 단신(-3)."""
    text = f"{title} {summary}".lower()
    score = 0
    strong = [
        "capex", "billion", "investment", "funding", "수주", "계약", "조 원",
        "억 달러", "acquisition", "deal", "contract", "투자", "발주", "증설",
        "fda", "approval", "승인", "양산", "출시", "공급계약", "파트너십",
        "partnership", "돌파", "신고가", "목표가", "상향", "수주잔고",
        "ipo", "인수", "합병", "기록적", "사상 최대", "record",
        "collaboration", "협업", "제휴", "협력", "선정", "채택", "공급",
        "launch", "unveil", "secures", "wins", "공개",
    ]
    for kw in strong:
        if kw in text:
            score += 3
            break
    if any(k in text for k in BOTTLENECK):
        score += 2
    # AI 수요 신호 가점(+2) — 수요측 변화는 산업 방향성에 직결
    if any(k in text for k in DEMAND_SIGNALS):
        score += 2
    watchlist = [
        "한화엔진", "tempus", "템퍼스", "hd현대중공업", "삼성중공업", "한화오션",
        "stx엔진", "sk하이닉스", "하이닉스", "삼성전자", "엔비디아", "nvidia",
        "tsmc", "micron", "마이크론", "4행정", "힘센", "조선",
    ]
    if any(k in text for k in watchlist):
        score += 2
    # 단신 감점 강화(v1=-2 → -3): 주가 등락성 단신은 더 강하게 배제
    for kw in ["주가", "시총", "장중", "마감", "shares", "stock rises", "stock falls",
               "급등", "급락", "보합", "상한가", "하한가", "약세", "강세",
               "오늘의", "특징주", "이 시각"]:
        if kw in text:
            score -= 3
            break
    return score


def entry_age_hours(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    try:
        published = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    return (now_utc() - published).total_seconds() / 3600.0


def is_fresh(entry):
    age = entry_age_hours(entry)
    if age is None:
        return True
    return age <= NEWS_WINDOW_HOURS + 1


def source_name(entry):
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        return entry.source.title
    return urlparse(entry.get("link", "")).netloc.replace("www.", "")


def clean_summary(raw):
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", " ", raw)
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


# ───────────────────────── 본문 추출 ─────────────────────────
def resolve_final_url(url):
    """구글뉴스 RSS 링크는 리다이렉트 → 실제 기사 URL 확보."""
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         timeout=BODY_FETCH_TIMEOUT, allow_redirects=True)
        return r.url, r.text
    except Exception:
        return url, None


def fetch_article_body(url):
    """
    기사 본문 텍스트 추출. 언어 무관(영/중/일/대만 모두 원문 그대로 반환).
    실패 시 빈 문자열 → 호출부가 RSS 요약으로 대체.
    """
    if not (FETCH_BODY and _HAS_TRAFI):
        return ""
    final_url, prefetched = resolve_final_url(url)
    body = ""
    try:
        downloaded = prefetched
        if not downloaded:
            downloaded = trafilatura.fetch_url(final_url)
        if downloaded:
            body = trafilatura.extract(
                downloaded, include_comments=False, include_tables=False,
                no_fallback=False, favor_precision=True,
            ) or ""
    except Exception as e:
        print(f"[WARN] body extract fail: {e}")
        body = ""
    body = re.sub(r"\s+", " ", body).strip()
    return body[:BODY_MAX_CHARS]


# ───────────────────────── Gemini ─────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_gemini_state = {"calls": 0, "disabled": False, "last": 0.0, "consec_fail": 0}


def gemini_analyze(title, summary, source, body=""):
    """
    본문(있으면) 기반 한국어 번역+10~15문장 요약+중요도+병목/유동성/수요 라벨.
    반환 dict 또는 None(실패/한도). None이면 제목+링크만 처리.
    """
    if not GEMINI_KEY or _gemini_state["disabled"]:
        return None
    if _gemini_state["calls"] >= GEMINI_MAX_CALLS_PER_RUN:
        _gemini_state["disabled"] = True
        print("[INFO] Gemini 회당 호출 상한 도달 → 이후 제목+링크만")
        return None
    if _gemini_state["consec_fail"] >= GEMINI_CONSEC_FAIL_STOP:
        _gemini_state["disabled"] = True
        print(f"[INFO] Gemini 연속 실패 {GEMINI_CONSEC_FAIL_STOP}회 → 이후 제목+링크만")
        return None

    elapsed = time.time() - _gemini_state["last"]
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)

    # 본문이 있으면 본문을, 없으면 RSS 요약을 분석 대상으로
    content_for_model = body.strip() if body.strip() else summary
    content_label = "본문" if body.strip() else "요약(본문 추출 실패)"

    prompt = (
        "너는 AI·반도체·메모리·데이터센터·전력·AI수요 산업 분석가다. "
        "아래 기사를 한국 투자자 관점에서 분석하라. 과장/추측 금지, 사실 기반.\n"
        "원문이 영어/중국어/일본어/대만어(번체)든 모두 한국어로 옮겨라.\n"
        "아래 7개 라벨 형식으로만 답하라. 각 줄 라벨 그대로, 값만 채워라. 다른 말 금지.\n"
        "제목: (한국어 번역 제목, 한 줄)\n"
        "요약: (반드시 10~15문장의 한국어. 기사 본문의 사실/숫자/맥락을 충실히 담되 "
        "한 문단으로 자연스럽게. 추측 금지)\n"
        "중요도: (S=산업구조 영향 / A=대규모 투자·계약·증설 / B=산업영향 존재 / C=참고용 중 하나)\n"
        "분야: (AI,GPU,HBM,DRAM,NAND,패키징,광통신,데이터센터,전력,원전,가스터빈,AI수요 중 해당)\n"
        "병목: (악화 / 완화 / 무관 중 하나)\n"
        "유동성: (유입 / 유출 / 중립 중 하나)\n"
        "왜중요: (한 문장)\n\n"
        f"[원문 제목] {title}\n[출처] {source}\n[{content_label}]\n{content_for_model}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,   # 10~15문장 요약 수용
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    url = GEMINI_URL.format(model=GEMINI_MODEL)

    for attempt in range(GEMINI_RETRY_MAX + 1):
        try:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=REQUEST_TIMEOUT)
            _gemini_state["calls"] += 1
            _gemini_state["last"] = time.time()

            if r.status_code == 200:
                data = r.json()
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                parts = cand.get("content", {}).get("parts", [])
                text = parts[0]["text"] if parts and "text" in parts[0] else ""
                if not text.strip():
                    print(f"[WARN] Gemini empty (finish={finish}) → 폴백")
                    _gemini_state["consec_fail"] += 1
                    return None
                _gemini_state["consec_fail"] = 0
                return _parse_lines(text)

            if r.status_code == 429:
                _gemini_state["disabled"] = True
                print("[WARN] Gemini 429(한도) → 이후 제목+링크만")
                return None

            if r.status_code in (500, 502, 503, 504):
                _gemini_state["consec_fail"] += 1
                if attempt < GEMINI_RETRY_MAX:
                    wait = GEMINI_RETRY_BASE * (2 ** attempt) + random.uniform(0, 1.0)
                    print(f"[WARN] Gemini {r.status_code} 과부하 "
                          f"(재시도 {attempt+1}/{GEMINI_RETRY_MAX}, {wait:.1f}초 후)")
                    time.sleep(wait)
                    continue
                print(f"[WARN] Gemini {r.status_code} → 이후 전체 제목+링크 전환")
                _gemini_state["disabled"] = True
                return None

            print(f"[WARN] Gemini {r.status_code}: {r.text[:200]} → 폴백")
            return None

        except requests.exceptions.Timeout:
            _gemini_state["consec_fail"] += 1
            if attempt < GEMINI_RETRY_MAX:
                wait = GEMINI_RETRY_BASE * (2 ** attempt) + random.uniform(0, 1.0)
                print(f"[WARN] Gemini timeout (재시도 {attempt+1}/{GEMINI_RETRY_MAX}, "
                      f"{wait:.1f}초 후)")
                time.sleep(wait)
                continue
            print("[WARN] Gemini timeout → 이후 전체 제목+링크 전환")
            _gemini_state["disabled"] = True
            return None
        except Exception as e:
            print(f"[WARN] Gemini fail: {e} → 폴백")
            _gemini_state["consec_fail"] += 1
            return None
    return None


def _parse_lines(text):
    """'라벨: 값' 파싱. 요약은 여러 줄일 수 있어 다음 라벨 전까지 이어붙임."""
    label_map = {"제목": "title_ko", "요약": "summary_ko", "중요도": "grade",
                 "분야": "sector", "병목": "bottleneck", "유동성": "liquidity",
                 "왜중요": "why"}
    labels = list(label_map.keys())
    out = {}
    cur_key = None
    buf = []

    def flush():
        if cur_key and buf:
            out[label_map[cur_key]] = " ".join(x.strip() for x in buf).strip()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = None
        for lb in labels:
            if line.startswith(lb + ":") or line.startswith(lb + "："):
                matched = lb
                break
        if matched:
            flush()
            cur_key = matched
            v = line.split(":", 1)[-1] if ":" in line else line.split("：", 1)[-1]
            buf = [v.strip()]
        else:
            if cur_key:   # 요약 등 멀티라인 이어쓰기
                buf.append(line)
    flush()

    if out.get("grade"):
        g = out["grade"].strip().upper()[:1]
        out["grade"] = g if g in "SABC" else ""
    return out if (out.get("title_ko") or out.get("summary_ko")) else None


# ───────────────────────── 수집 ─────────────────────────
def collect():
    items = []
    seen_titles = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] feed fail: {e}")
            continue
        for entry in feed.entries[:RSS_MAX_ENTRIES]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            raw_sum = entry.get("summary", "")
            if not title or not link:
                continue
            if not is_fresh(entry):
                continue
            if not passes_filter(title, raw_sum):
                continue
            nt = norm_title(title)
            if any(is_similar(nt, s) for s in seen_titles):
                continue
            seen_titles.append(nt)
            # published 시각(ISO) — 대시보드 news.json용
            pub_iso = ""
            tm = entry.get("published_parsed") or entry.get("updated_parsed")
            if tm:
                try:
                    pub_iso = datetime.datetime(*tm[:6],
                        tzinfo=datetime.timezone.utc).isoformat()
                except Exception:
                    pub_iso = ""
            items.append({
                "title": html.unescape(title),
                "link": link,
                "summary": clean_summary(raw_sum),
                "source": source_name(entry),
                "ntitle": nt,
                "score": base_score(title, raw_sum),
                "published": pub_iso,
            })
    print(f"[INFO] 수집 {len(items)}건 (필터/1차중복 후)")
    return items


def dedupe_against_seen(items, seen):
    seen_norm = [v["ntitle"] for v in seen.values() if "ntitle" in v]
    out = []
    for it in items:
        if title_key(it["title"]) in seen:
            continue
        if any(is_similar(it["ntitle"], s) for s in seen_norm):
            continue
        out.append(it)
    return out


# ───────────────────────── 텔레그램 ─────────────────────────
def esc(s):
    return html.escape(s or "")


def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"[ERROR] telegram {r.status_code}: {r.text[:120]}")
    return r.status_code == 200


GRADE_EMOJI = {"S": "🔴 S", "A": "🟠 A", "B": "🟡 B", "C": "⚪ C"}


def build_full(it, a):
    """
    Gemini 분석 성공 시 풀 포맷.
    구조 수정: [등급] → 제목 → 요약 → 메타 → 왜중요 → 링크  (제목이 요약보다 먼저!)
    """
    title_ko = a.get("title_ko") or it["title"]
    grade = GRADE_EMOJI.get(str(a.get("grade", "")).upper().strip(), "")
    lines = []
    # 1) 등급 + 제목 (먼저)
    if grade:
        lines.append(f"{grade}")
    lines.append(f"<b>{esc(title_ko)}</b>")
    # 2) 요약 (제목 다음)
    if a.get("summary_ko"):
        lines.append(esc(a["summary_ko"]))
    # 3) 메타
    meta = []
    if a.get("sector"):
        meta.append(f"분야 {esc(a['sector'])}")
    if a.get("bottleneck"):
        meta.append(f"병목 {esc(a['bottleneck'])}")
    if a.get("liquidity"):
        meta.append(f"유동성 {esc(a['liquidity'])}")
    if meta:
        lines.append("· " + " | ".join(meta))
    # 4) 왜중요
    if a.get("why"):
        lines.append(f"💡 {esc(a['why'])}")
    # 5) 링크
    src = f" · {esc(it['source'])}" if it["source"] else ""
    lines.append(f'🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}')
    return "\n".join(lines)


_gt_state = {"obj": None, "disabled": False}
_gt_cache = {}


def google_translate_ko(text):
    if not text or not text.strip():
        return text
    if has_korean(text):
        return text
    if _gt_state["disabled"]:
        return text
    if text in _gt_cache:
        return _gt_cache[text]
    if _gt_state["obj"] is None:
        try:
            from deep_translator import GoogleTranslator
            _gt_state["obj"] = GoogleTranslator(source="auto", target="ko")
        except Exception as e:
            print(f"[WARN] google translate init fail: {e}")
            _gt_state["disabled"] = True
            return text
    try:
        out = _gt_state["obj"].translate(text[:4500])
        if out and out.strip():
            _gt_cache[text] = out
            time.sleep(0.4)
            return out
    except Exception as e:
        print(f"[WARN] google translate fail: {e}")
    return text


def build_min(it):
    """Gemini 미사용/실패 시: 제목+링크. 비한글이면 구글번역으로 한글화."""
    title = it["title"]
    if not has_korean(title):
        title = google_translate_ko(title)
    src = f" · {esc(it['source'])}" if it["source"] else ""
    return f'<b>{esc(title)}</b>\n🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}'


# ───────────────────────── news.json (대시보드 연동) ─────────────────────────
def make_news_item(it, a):
    """
    대시보드(thesis-tracker)가 읽는 형식.
    summary에는 Gemini 한국어 요약(있으면)을, 없으면 build_min 로직처럼 보조 처리.
    """
    if a:   # Gemini 분석 성공
        title = a.get("title_ko") or it["title"]
        summary = a.get("summary_ko") or ""
        # 메타(분야/병목/유동성/왜중요)도 요약 뒤에 붙여 대시보드에서 맥락 보강
        tail = []
        if a.get("sector"):
            tail.append(f"[분야 {a['sector']}]")
        if a.get("bottleneck"):
            tail.append(f"[병목 {a['bottleneck']}]")
        if a.get("liquidity"):
            tail.append(f"[유동성 {a['liquidity']}]")
        if a.get("why"):
            tail.append(f"왜중요: {a['why']}")
        if tail:
            summary = (summary + " " + " ".join(tail)).strip()
    else:   # Gemini 미사용/실패 → 제목 한글화, 요약은 RSS 요약
        title = it["title"]
        if not has_korean(title):
            title = google_translate_ko(title)
        summary = it.get("summary", "")
        if summary and not has_korean(summary):
            summary = google_translate_ko(summary)
    return {
        "title": title,
        "url": it["link"],
        "source": it.get("source", ""),
        "published": it.get("published", ""),
        "summary": summary,
    }


def save_news_json(new_items):
    """
    기존 news.json에 신규 항목을 앞쪽에 누적(중복 url 제거), 상한 유지.
    신규가 없어도 기존 파일은 그대로 보존(절대 빈 파일로 덮어쓰지 않음).
    """
    prev = load_json(NEWS_FILE, {"updated": "", "items": []})
    prev_items = prev.get("items", []) if isinstance(prev, dict) else []
    seen_urls = {x.get("url") for x in new_items if x.get("url")}
    merged = new_items + [x for x in prev_items if x.get("url") not in seen_urls]
    merged = merged[:NEWS_MAX_ITEMS]
    save_json(NEWS_FILE, {
        "updated": now_utc().isoformat(),
        "items": merged,
    })
    print(f"[INFO] news.json 저장: 신규 {len(new_items)}건 + 기존 → 총 {len(merged)}건")


# ───────────────────────── 메인 ─────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("[FATAL] TELEGRAM 토큰/챗ID 없음")

    seen = prune_seen(load_json(SEEN_FILE, {}))
    queue = load_json(QUEUE_FILE, [])

    fresh = collect()
    fresh = dedupe_against_seen(fresh, seen)

    pool = queue + fresh
    uniq, seen_nt = [], []
    for it in pool:
        if any(is_similar(it["ntitle"], s) for s in seen_nt):
            continue
        seen_nt.append(it["ntitle"])
        uniq.append(it)
    uniq.sort(key=lambda x: x.get("score", 0), reverse=True)

    before = len(uniq)
    uniq = [it for it in uniq if it.get("score", 0) >= MIN_SCORE_TO_SEND]
    print(f"[INFO] 중요도 필터: {before}건 → {len(uniq)}건 (기준 {MIN_SCORE_TO_SEND}점 이상)")

    if not uniq:
        print("[INFO] 신규 없음 - 전송 생략")
        save_json(SEEN_FILE, seen)
        save_json(QUEUE_FILE, [])
        # news.json은 건드리지 않음(기존 누적 보존)
        return

    to_send = uniq[:MAX_SEND_PER_RUN]
    leftover = uniq[MAX_SEND_PER_RUN:]

    kst = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    tg_send(f"📡 <b>AI·반도체 산업 브리핑</b>\n🗓 {kst} KST · {len(to_send)}건"
            + (f" · 대기 {len(leftover)}건" if leftover else ""))
    time.sleep(SEND_DELAY)

    sent = 0
    news_batch = []   # 대시보드 news.json 누적용
    for it in to_send:
        # 1) 본문 추출 → 2) Gemini가 본문 읽고 10~15문장 요약
        body = fetch_article_body(it["link"]) if FETCH_BODY else ""
        if FETCH_BODY:
            time.sleep(BODY_FETCH_DELAY)
        a = gemini_analyze(it["title"], it["summary"], it["source"], body=body)
        msg = build_full(it, a) if a else build_min(it)
        if tg_send(msg):
            sent += 1
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
            # 텔레그램 발송 성공분만 news.json에도 적재
            news_batch.append(make_news_item(it, a))
        time.sleep(SEND_DELAY)

    save_json(QUEUE_FILE, leftover[:50])
    save_json(SEEN_FILE, seen)
    if news_batch:
        save_news_json(news_batch)   # 대시보드 연동
    print(f"[DONE] {sent}건 전송, 이월 {len(leftover)}건, Gemini호출 {_gemini_state['calls']}")


if __name__ == "__main__":
    main()
