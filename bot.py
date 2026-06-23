#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI·반도체·메모리·데이터센터·전력 산업 뉴스 에이전트
- 목표: AI 산업의 자금흐름/공급망/수요/병목 변화에 영향 주는 정보만 선별
- 6시간 이내 뉴스, 5개국(미/한/일/중/대만), 기업·인물·병목 추적
- Gemini로 한국어 번역+요약+중요도(S/A/B/C)+병목/유동성 라벨(참고용)
- 12건/회 제한, 초과분은 다음 회차 이월
- Gemini 한도 소진/실패 시: 제목 + 링크만 전송
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

# ───────────────────────── 환경변수 ─────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip()

# ───────────────────────── 설정 ─────────────────────────
SEEN_FILE = "seen.json"
QUEUE_FILE = "queue.json"          # 12건 초과분 이월 저장
SEEN_RETENTION_DAYS = 7
MAX_SEND_PER_RUN = 12              # 회당 최대 발송
NEWS_WINDOW_HOURS = 6              # 최근 N시간 이내 뉴스만
SIMILARITY_THRESHOLD = 0.68
REQUEST_TIMEOUT = 25
SEND_DELAY = 1.0
GEMINI_MIN_INTERVAL = 6.5          # 보수적: 무료 10RPM 가정 → 6.5초 간격
GEMINI_MAX_CALLS_PER_RUN = 30      # 일일 한도(보수적 250RPD) 보호: 회당 상한
GEMINI_RETRY_MAX = 0               # 503 재시도 끔(즉시 폴백) — 쿼터 절약 우선
GEMINI_RETRY_BASE = 2.0            # 지수 백오프 기준 대기(초): 2, 4 ...
GEMINI_CONSEC_FAIL_STOP = 1        # 503 등 실패 1회만에 즉시 중단 → 쿼터 절약
RSS_MAX_ENTRIES = 30


# ───────────────────────── RSS 소스 ─────────────────────────
def gnews(query, lang="en", hours=NEWS_WINDOW_HOURS):
    """구글뉴스 검색 RSS. when:Nh 로 최근 N시간 제한."""
    q = quote(f"{query} when:{hours}h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    if lang == "zh":
        return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# 기업/주제 키워드 (영문)
CORE_EN = (
    "OpenAI OR Anthropic OR xAI OR \"Google DeepMind\" OR \"Meta AI\" OR Mistral OR "
    "Nvidia OR AMD OR Broadcom OR Marvell OR TSMC OR Samsung OR \"SK hynix\" OR Micron OR "
    "HBM OR DRAM OR NAND OR CXL OR CoWoS OR \"data center\" OR datacenter OR "
    "CoreWeave OR \"power grid\" OR \"gas turbine\" OR nuclear"
)
# 인물 (인터뷰/발언 관련 뉴스)
PEOPLE_EN = (
    "\"Jensen Huang\" OR \"Sam Altman\" OR \"Dario Amodei\" OR \"Ilya Sutskever\" OR "
    "\"Demis Hassabis\" OR \"Elon Musk\" OR \"Lisa Su\" OR \"Satya Nadella\" OR "
    "\"Sundar Pichai\" OR \"Hock Tan\""
)
# 유동성/CAPEX
MONEY_EN = (
    "AI capex OR AI funding OR \"data center investment\" OR \"GPU order\" OR "
    "\"HBM contract\" OR \"cloud deal\" OR AI acquisition OR semiconductor investment"
)
# 한국어 핵심
CORE_KO = (
    "엔비디아 OR HBM OR DRAM OR 낸드 OR SK하이닉스 OR 삼성전자 반도체 OR "
    "데이터센터 OR AI 투자 OR AI 인프라 OR 반도체 증설 OR 전력 OR 가스터빈 OR CXL OR 패키징"
)

FEEDS = [
    # 미국/영문 핵심
    gnews(CORE_EN, "en"),
    gnews(PEOPLE_EN, "en"),
    gnews(MONEY_EN, "en"),
    # 한국
    gnews(CORE_KO, "ko"),
    gnews("AI 데이터센터 OR HBM 공급 OR 반도체 수주 OR AI 전력 OR 원전 데이터센터", "ko"),
    # 일본
    gnews("AI半導体 OR HBM OR データセンター OR ラピダス OR 電力 AI", "ja"),
    # 중국
    gnews("人工智能 芯片 OR 数据中心 OR HBM OR 算力 OR 英伟达", "zh"),
    # 대만
    gnews("台積電 OR CoWoS OR AI 伺服器 OR 半導體 產能", "zh"),
]

# ───────────────────────── 필터 키워드 ─────────────────────────
INCLUDE = [
    # 영문
    "ai", "gpu", "hbm", "dram", "nand", "cxl", "cowos", "packaging", "wafer",
    "data center", "datacenter", "nvidia", "amd", "tsmc", "samsung", "hynix",
    "micron", "broadcom", "marvell", "openai", "anthropic", "xai", "deepmind",
    "capex", "funding", "investment", "acquisition", "power", "grid", "turbine",
    "nuclear", "transformer", "optical", "transceiver", "inference",
    # 한글
    "인공지능", "반도체", "엔비디아", "메모리", "데이터센터", "고대역폭",
    "전력", "원전", "가스터빈", "패키징", "투자", "수주", "증설", "공급",
    # 일/중 핵심
    "半導体", "データセンター", "人工智能", "芯片", "数据中心", "算力", "台積電",
]
EXCLUDE = [
    "할인", "쿠폰", "이벤트", "광고", "분양", "운세", "로또",
    "casino", "porn", "coupon", "discount", "giveaway",
]

# 병목 키워드 (중요도 1단계 상향)
BOTTLENECK = [
    "hbm", "cowos", "packaging", "gpu", "dram", "nand", "optical", "transceiver",
    "power", "grid", "turbine", "substation", "cooling", "전력", "송전", "변전",
    "가스터빈", "냉각", "패키징", "고대역폭", "capacity", "shortage", "증설", "감산",
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


# 같은 사건 판정용: 핵심 주체(인물/기업) — 둘 다 공유하면 동일 사건 가능성↑
SAME_EVENT_ACTORS = {
    "이재용", "이재명", "젠슨", "황", "올트먼", "머스크", "저커버그", "아모데이",
    "삼성전자", "하이닉스", "sk하이닉스", "마이크론", "엔비디아", "tsmc", "인텔",
    "openai", "anthropic", "삼성", "구글", "메타", "broadcom", "amd",
}
# 같은 사건 판정용: 사건 행위어 — 같은 행위면 동일 사건 가능성↑
SAME_EVENT_ACTIONS = {
    "점검", "현장", "방문", "찾았다", "공급계약", "계약", "수주", "체결",
    "인수", "투자", "증설", "착공", "양산", "출시", "발표", "공개", "돌파",
    "선정", "협력", "파트너십", "상향", "하향", "목표가", "목표주가",
    "1위", "등극", "제치고", "추월",
}

# 의미가 사실상 같은 행위어 묶음(동의어). 같은 그룹이면 동일 행위로 간주.
ACTION_SYNONYMS = [
    {"점검", "현장", "방문", "찾았다", "둘러", "행보"},      # 현장 점검류
    {"공급계약", "계약", "수주", "체결", "납품", "공급"},      # 계약류
    {"인수", "합병", "지분", "m&a"},                          # M&A류
    {"투자", "증설", "착공", "신설", "구축", "확대"},          # 투자/증설류
    {"양산", "출시", "공개", "발표", "선보", "상용화"},        # 출시/발표류
    {"1위", "등극", "제치고", "추월", "역전", "왕좌"},         # 순위역전류
    {"상향", "하향", "목표가", "목표주가", "투자의견"},        # 리포트류
]


def _action_group(action_set):
    """행위어 집합을 동의어 그룹 번호 집합으로 변환."""
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
    # 핵심 주체와 사건 행위어(동의어 그룹 기준)를 동시에 공유하면 동일 사건
    aa_actor, aa_action = _key_entities(a)
    bb_actor, bb_action = _key_entities(b)
    shared_actor = aa_actor & bb_actor
    shared_group = _action_group(aa_action) & _action_group(bb_action)
    if shared_actor and shared_group:
        if _jaccard(a, b) >= 0.12:   # 과묶음 방지 최소 겹침
            return True
    return False


def passes_filter(title, summary):
    text = f"{title} {summary}".lower()
    if any(k in text for k in EXCLUDE):
        return False
    return any(k in text for k in INCLUDE)


def base_score(title, summary):
    """1차 중요도(키워드 기반). 병목/유동성 신호 가중."""
    text = f"{title} {summary}".lower()
    score = 0
    # 유동성/규모 신호
    for kw in ["capex", "billion", "investment", "funding", "수주", "계약", "조 원",
               "억 달러", "acquisition", "deal", "contract", "투자", "발주", "증설"]:
        if kw in text:
            score += 3
    # 병목 신호 (1단계 상향)
    if any(k in text for k in BOTTLENECK):
        score += 2
    # 단신 감점
    for kw in ["주가", "시총", "장중", "마감", "shares", "stock rises", "stock falls",
               "급등", "급락", "보합"]:
        if kw in text:
            score -= 2
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
    return age <= NEWS_WINDOW_HOURS + 1   # 약간의 여유


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


# ───────────────────────── Gemini ─────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_gemini_state = {"calls": 0, "disabled": False, "last": 0.0, "consec_fail": 0}


def gemini_analyze(title, summary, source):
    """
    한국어 번역+요약+중요도+병목/유동성 라벨.
    반환 dict 또는 None(실패/한도). None이면 호출부가 제목+링크만 처리.
    503/5xx/타임아웃은 지수 백오프로 재시도, 429/4xx는 즉시 폴백.
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

    # 분당 제한 보호
    elapsed = time.time() - _gemini_state["last"]
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)

    prompt = (
        "너는 AI·반도체·메모리·데이터센터·전력 산업 분석가다. "
        "아래 뉴스를 한국 투자자 관점에서 분석하라. 과장/추측 금지, 사실 기반.\n"
        "아래 7줄 형식으로만 답하라. 각 줄 라벨 그대로, 값만 채워라. 다른 말 금지.\n"
        "제목: (한국어 번역 제목)\n"
        "요약: (핵심 1~2문장 한국어)\n"
        "중요도: (S=산업구조 영향 / A=대규모 투자·계약·증설 / B=산업영향 존재 / C=참고용 중 하나)\n"
        "분야: (AI,GPU,HBM,DRAM,NAND,패키징,광통신,데이터센터,전력,원전,가스터빈 중 해당)\n"
        "병목: (악화 / 완화 / 무관 중 하나)\n"
        "유동성: (유입 / 유출 / 중립 중 하나)\n"
        "왜중요: (한 문장)\n\n"
        f"[원문 제목] {title}\n[원문 요약] {summary}\n[출처] {source}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    url = GEMINI_URL.format(model=GEMINI_MODEL)

    # 총 (1 + GEMINI_RETRY_MAX) 회 시도. 503/5xx/타임아웃만 재시도.
    for attempt in range(GEMINI_RETRY_MAX + 1):
        try:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=REQUEST_TIMEOUT)
            _gemini_state["calls"] += 1     # 503 재시도도 쿼터에 카운트되므로 매번 증가
            _gemini_state["last"] = time.time()

            if r.status_code == 200:
                data = r.json()
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                parts = cand.get("content", {}).get("parts", [])
                text = parts[0]["text"] if parts and "text" in parts[0] else ""
                if not text.strip():
                    print(f"[WARN] Gemini empty (finish={finish}) → 제목+링크 폴백")
                    _gemini_state["consec_fail"] += 1
                    return None
                _gemini_state["consec_fail"] = 0   # 성공 시 연속실패 초기화
                return _parse_lines(text)

            # 429: 한도 → 재시도 무의미, 이후 전체 중단
            if r.status_code == 429:
                _gemini_state["disabled"] = True
                print("[WARN] Gemini 429(한도) → 이후 제목+링크만")
                return None

            # 503/500/502/504: 일시적 → 재시도 대상
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

            # 400/403/404 등: 코드/키/모델 문제 → 재시도 무의미
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
    """'라벨: 값' 7줄 파싱."""
    m = {"제목": "title_ko", "요약": "summary_ko", "중요도": "grade",
         "분야": "sector", "병목": "bottleneck", "유동성": "liquidity", "왜중요": "why"}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        if k in m and v.strip():
            out[m[k]] = v.strip()
    # 중요도는 첫 글자만(S/A/B/C)
    if out.get("grade"):
        g = out["grade"].strip().upper()[:1]
        out["grade"] = g if g in "SABC" else ""
    return out if out.get("title_ko") or out.get("summary_ko") else None


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
            # 수집 단계 1차 중복 제거
            if any(is_similar(nt, s) for s in seen_titles):
                continue
            seen_titles.append(nt)
            items.append({
                "title": html.unescape(title),
                "link": link,
                "summary": clean_summary(raw_sum),
                "source": source_name(entry),
                "ntitle": nt,
                "score": base_score(title, raw_sum),
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
    """Gemini 분석 성공 시 풀 포맷."""
    title_ko = a.get("title_ko") or it["title"]
    grade = GRADE_EMOJI.get(str(a.get("grade", "")).upper().strip(), "")
    lines = []
    if grade:
        lines.append(f"{grade}")
    lines.append(f"<b>{esc(title_ko)}</b>")
    if a.get("summary_ko"):
        lines.append(esc(a["summary_ko"]))
    meta = []
    if a.get("sector"):
        meta.append(f"분야 {esc(a['sector'])}")
    if a.get("bottleneck"):
        meta.append(f"병목 {esc(a['bottleneck'])}")
    if a.get("liquidity"):
        meta.append(f"유동성 {esc(a['liquidity'])}")
    if meta:
        lines.append("· " + " | ".join(meta))
    if a.get("why"):
        lines.append(f"💡 {esc(a['why'])}")
    src = f" · {esc(it['source'])}" if it["source"] else ""
    lines.append(f'🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}')
    return "\n".join(lines)


_gt_state = {"obj": None, "disabled": False}
_gt_cache = {}


def google_translate_ko(text):
    """
    제미나이 실패 시 보조 번역(deep-translator/구글).
    영문 등 비한글을 한국어로. 실패하면 원문 반환(봇 안 멈춤).
    """
    if not text or not text.strip():
        return text
    if has_korean(text):          # 이미 한글이면 그대로
        return text
    if _gt_state["disabled"]:
        return text
    if text in _gt_cache:
        return _gt_cache[text]
    # 지연 로딩
    if _gt_state["obj"] is None:
        try:
            from deep_translator import GoogleTranslator
            _gt_state["obj"] = GoogleTranslator(source="auto", target="ko")
        except Exception as e:
            print(f"[WARN] google translate init fail: {e}")
            _gt_state["disabled"] = True
            return text
    try:
        out = _gt_state["obj"].translate(text[:4500])   # 5천자 제한 안전선
        if out and out.strip():
            _gt_cache[text] = out
            time.sleep(0.4)        # 연속 호출 차단 방지(검증된 권장)
            return out
    except Exception as e:
        print(f"[WARN] google translate fail: {e}")
    return text


def build_min(it):
    """Gemini 미사용/실패 시: 제목+링크. 영문이면 구글번역으로 한글화."""
    title = it["title"]
    if not has_korean(title):
        title = google_translate_ko(title)   # 영문 → 한글 보조 번역
    src = f" · {esc(it['source'])}" if it["source"] else ""
    return f'<b>{esc(title)}</b>\n🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}'


# ───────────────────────── 메인 ─────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("[FATAL] TELEGRAM 토큰/챗ID 없음")

    seen = prune_seen(load_json(SEEN_FILE, {}))
    queue = load_json(QUEUE_FILE, [])   # 이월분 (이미 dedupe된 raw item들)

    fresh = collect()
    fresh = dedupe_against_seen(fresh, seen)

    # 이월분 + 신규 합치고, 이월분 우선 + 점수순
    pool = queue + fresh
    # pool 내부 중복 제거
    uniq, seen_nt = [], []
    for it in pool:
        if any(is_similar(it["ntitle"], s) for s in seen_nt):
            continue
        seen_nt.append(it["ntitle"])
        uniq.append(it)
    uniq.sort(key=lambda x: x.get("score", 0), reverse=True)

    if not uniq:
        print("[INFO] 신규 없음 - 전송 생략")
        save_json(SEEN_FILE, seen)
        save_json(QUEUE_FILE, [])
        return

    to_send = uniq[:MAX_SEND_PER_RUN]
    leftover = uniq[MAX_SEND_PER_RUN:]

    # 헤더
    kst = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    tg_send(f"📡 <b>AI·반도체 산업 브리핑</b>\n🗓 {kst} KST · {len(to_send)}건"
            + (f" · 대기 {len(leftover)}건" if leftover else ""))
    time.sleep(SEND_DELAY)

    sent = 0
    for it in to_send:
        a = gemini_analyze(it["title"], it["summary"], it["source"])
        msg = build_full(it, a) if a else build_min(it)
        if tg_send(msg):
            sent += 1
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
        time.sleep(SEND_DELAY)

    # 이월분 저장 (다음 회차 우선 발송) - seen 처리 안 함(아직 안 보냄)
    save_json(QUEUE_FILE, leftover[:50])   # 과적재 방지
    save_json(SEEN_FILE, seen)
    print(f"[DONE] {sent}건 전송, 이월 {len(leftover)}건, Gemini호출 {_gemini_state['calls']}")


if __name__ == "__main__":
    main()
