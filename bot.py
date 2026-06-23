#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
메모리 반도체 / AI 데일리 브리핑 텔레그램 봇 (v2)
- 1 뉴스 = 1 메시지
- 제목 + 간단 요약(RSS 본문 발췌 정리) + 링크
- 키워드 필터 + 제목 유사도 중복 제거 + 과거 발송분 제외
"""

import os
import re
import json
import time
import html
import hashlib
import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse, quote

import requests
import feedparser

# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip()        # 없으면 번역/요약 생략
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()

SEEN_FILE = "seen.json"
SEEN_RETENTION_DAYS = 14
MAX_ITEMS_PER_CATEGORY = 8         # 카테고리당 최대 (알림 폭주 방지)
SIMILARITY_THRESHOLD = 0.68        # 낮을수록 중복을 더 적극적으로 묶음
SUMMARY_MAX_CHARS = 180            # 요약 최대 길이
MAX_AGE_HOURS = 36                 # 이 시간보다 오래된 기사는 제외 (날짜 필터)
REQUEST_TIMEOUT = 20
SEND_DELAY = 1.0                   # 메시지 간 간격(초) - rate limit 회피


def google_news_rss(query, lang="ko"):
    q = quote(query)
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


FEEDS = {
    "🧠 메모리 반도체": [
        google_news_rss("HBM OR DRAM OR 낸드 OR 메모리반도체 OR SK하이닉스 OR 삼성전자 반도체", "ko"),
        google_news_rss("HBM OR DRAM OR NAND OR memory chip OR SK Hynix OR Micron", "en"),
        google_news_rss("LPDDR OR SOCAMM OR HBM4 OR DDR5 server", "en"),
        "https://www.tomshardware.com/feeds/all",
    ],
    "🤖 AI": [
        google_news_rss("OpenAI OR Anthropic OR 구글 제미나이 OR 메타 AI OR 샘 올트먼", "ko"),
        google_news_rss("OpenAI OR Anthropic OR Google DeepMind OR Meta AI OR xAI", "en"),
        google_news_rss("Sam Altman OR Dario Amodei OR Sundar Pichai OR Zuckerberg AI", "en"),
        google_news_rss("AI model OR GPT OR Claude OR Gemini OR Llama release", "en"),
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    ],
    "🌐 해외 소식": [
        google_news_rss("Micron OR \"SK Hynix\" OR Samsung HBM partnership OR deal OR contract", "en"),
        google_news_rss("Nvidia OR TSMC OR Broadcom AI deal OR investment OR supply agreement", "en"),
        google_news_rss("memory chip shortage OR HBM capacity OR DRAM price hike", "en"),
        google_news_rss("OpenAI OR Anthropic infrastructure OR compute deal OR data center", "en"),
    ],
    "📊 리포트/투자의견": [
        google_news_rss("SK하이닉스 OR 삼성전자 목표주가 OR 투자의견 OR 상향 OR 하향", "ko"),
        google_news_rss("Micron OR Nvidia price target OR upgrade OR downgrade analyst", "en"),
        google_news_rss("HBM OR DRAM analyst forecast OR rating", "en"),
    ],
}

INCLUDE_KEYWORDS = {
    "🧠 메모리 반도체": [
        "hbm", "dram", "nand", "낸드", "디램", "메모리", "memory chip", "lpddr",
        "ddr5", "ddr4", "socamm", "sk하이닉스", "하이닉스", "hynix", "micron", "마이크론",
        "삼성전자", "samsung", "wafer", "웨이퍼", "cowos", "패키징", "packaging",
        "비트", "감산", "증설", "공급", "수요", "가격", "고대역폭",
    ],
    "🤖 AI": [
        # 기업
        "openai", "오픈ai", "anthropic", "앤트로픽", "엔트로픽", "deepmind", "딥마인드",
        "google", "구글", "gemini", "제미나이", "제미니", "meta", "메타", "xai", "그록", "grok",
        "microsoft", "마이크로소프트", "코파일럿", "copilot", "mistral", "perplexity", "퍼플렉시티",
        "스케일ai", "엔비디아", "nvidia",
        # 인물
        "altman", "올트먼", "amodei", "아모데이", "pichai", "피차이", "hassabis", "허사비스",
        "zuckerberg", "저커버그", "musk", "머스크", "황", "젠슨", "huang",
        # 모델/기술
        "gpt", "chatgpt", "챗gpt", "claude", "클로드", "llama", "라마", "llm",
        "생성형", "generative", "인공지능", "ai 모델", "추론모델", "에이전트", "agent",
        "오픈소스", "벤치마크", "agi", "파운데이션 모델",
        # 업계 동향
        "투자", "기업가치", "valuation", "라운드", "ipo", "발표", "출시", "공개", "데이터센터",
    ],
    "🌐 해외 소식": [
        "memory", "dram", "nand", "hbm", "lpddr", "micron", "hynix", "samsung",
        "tsmc", "nvidia", "amd", "broadcom", "semiconductor", "chip", "datacenter",
        "data center", "supply", "demand", "wafer", "foundry",
    ],
    "📊 리포트/투자의견": [
        "목표주가", "투자의견", "상향", "하향", "매수", "비중확대", "리포트", "증권",
        "price target", "upgrade", "downgrade", "analyst", "rating", "outperform",
        "overweight", "buy", "forecast", "estimate", "초과", "리서치",
    ],
}

EXCLUDE_KEYWORDS = [
    "할인", "쿠폰", "이벤트 당첨", "광고", "분양", "운세", "로또",
    "casino", "porn", "coupon",
]

# 사건성(중요) 신호어 — 있으면 점수↑, 위로 올림
EVENT_SIGNALS = [
    # 한글
    "파트너십", "협력", "계약", "공급계약", "수주", "인수", "합병", "지분", "투자",
    "증설", "신설", "착공", "양산", "출시", "공개", "발표", "도입", "채택", "선정",
    "독점", "최초", "세계 최초", "신제품", "개발 성공", "양산 돌입", "공장", "거점",
    "조달", "납품", "확보", "체결", "맞손", "동맹", "출하", "상용화", "구축",
    # 영문
    "partnership", "deal", "agreement", "contract", "acquire", "acquisition",
    "merger", "stake", "invest", "investment", "launch", "unveil", "announce",
    "release", "expansion", "build", "supply deal", "secures", "partners with",
    "collaboration", "rollout", "deploy", "mass production", "ramp",
]

# 단신/저신호 — 사건어가 없고 이게 있으면 점수↓ (주가·등락 단신)
WEAK_SIGNALS = [
    "주가", "시총", "상승", "하락", "강세", "약세", "급등", "급락", "오름세", "내림세",
    "마감", "장중", "개장", "종가", "전일", "보합", "거래량", "외국인 순매수",
    "기관 순매수", "수급", "shares", "stock", "rises", "falls", "rose", "fell",
    "gains", "drops", "rally", "slips", "closing", "premarket",
]


def importance_score(category, title, summary):
    """사건성 신호 기반 가중치. 높을수록 위로."""
    text = f"{title} {summary}".lower()
    score = 0
    for kw in EVENT_SIGNALS:
        if kw.lower() in text:
            score += 3
    # 주가 단신성 신호는 감점 (단, 리포트 카테고리는 주가가 본질이라 제외)
    if category != "📊 리포트/투자의견":
        for kw in WEAK_SIGNALS:
            if kw.lower() in text:
                score -= 1
    return score


def _has_korean(text):
    return bool(re.search(r"[가-힣]", text or ""))


_gemini_cache = {}
_gemini_calls = [0]   # 호출 횟수 추적 (rate 보호용)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

def gemini_summarize_ko(title, summary):
    """
    영문 기사 제목+요약을 받아 한국어로 '번역+한줄요약'.
    반환: (한글제목, 한글요약). 실패하면 (원문제목, 원문요약).
    """
    if not GEMINI_KEY:
        return title, summary
    cache_key = title
    if cache_key in _gemini_cache:
        return _gemini_cache[cache_key]

    src = f"제목: {title}"
    if summary:
        src += f"\n요약: {summary}"
    prompt = (
        "다음은 해외 반도체/AI 뉴스입니다. 한국 투자자가 빠르게 파악할 수 있도록 "
        "한국어로 처리하세요.\n"
        "1) 제목을 자연스러운 한국어로 번역\n"
        "2) 핵심을 1문장으로 요약(투자 관점에서 중요한 사실 위주, 과장 금지)\n"
        "반드시 아래 형식으로만 답하세요. 다른 말 금지.\n"
        "제목: <한국어 제목>\n요약: <한국어 한 문장>\n\n"
        f"{src}"
    )
    try:
        r = requests.post(
            GEMINI_URL.format(model=GEMINI_MODEL),
            headers={
                "x-goog-api-key": GEMINI_KEY,
                "Content-Type": "application/json",
            },
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 512,
                    # 핵심: 2.5-flash는 thinking이 기본 ON이라 추론토큰이
                    # maxOutputTokens를 먹어 빈 응답이 됨 → thinking 끔
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            },
            timeout=REQUEST_TIMEOUT,
        )
        _gemini_calls[0] += 1
        time.sleep(4.5)   # 분당 요청 제한(무료 Flash ~13RPM) 회피
        if r.status_code == 200:
            data = r.json()
            try:
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                parts = cand.get("content", {}).get("parts", [])
                text = parts[0]["text"] if parts and "text" in parts[0] else ""
            except (KeyError, IndexError):
                text, finish = "", "PARSE_ERR"
            if not text.strip():
                # 빈 응답이면 원인을 로그에 남김 (thinking이 토큰 먹은 경우 등)
                print(f"[WARN] Gemini empty text (finish={finish}); fallback to original")
                return title, summary
            ko_title, ko_summary = _parse_gemini(text, title, summary)
            _gemini_cache[cache_key] = (ko_title, ko_summary)
            return ko_title, ko_summary
        else:
            print(f"[WARN] Gemini {r.status_code}: {r.text[:150]}")
    except Exception as e:
        print(f"[WARN] Gemini fail: {e}")
    return title, summary


def _parse_gemini(text, fallback_title, fallback_summary):
    """'제목: ... / 요약: ...' 형식 파싱. 실패 시 원문 폴백."""
    ko_title, ko_summary = fallback_title, fallback_summary
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("제목:"):
            v = line[3:].strip()
            if v:
                ko_title = v
        elif line.startswith("요약:"):
            v = line[3:].strip()
            if v:
                ko_summary = v
    return ko_title, ko_summary


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def prune_seen(seen):
    cutoff = (now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    return {k: v for k, v in seen.items() if v.get("ts", 0) >= cutoff}


def norm_title(title):
    t = html.unescape(title or "")
    t = re.sub(r"\s*[-|·]\s*[^-|·]+$", "", t)   # 끝의 ' - 매체명' 제거
    t = re.sub(r"\[[^\]]*\]", " ", t)            # [속보][단독] 등 대괄호 토큰 제거
    t = re.sub(r"[\[\](){}<>·…“”\"'’‘|!?.,~―—\-]+", " ", t)
    t = re.sub(r"[\"'%·,…]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def title_key(title):
    # 공백까지 제거한 형태로 키 생성 → 띄어쓰기만 다른 제목을 같은 키로
    compact = re.sub(r"\s+", "", norm_title(title))
    return hashlib.md5(compact.encode("utf-8")).hexdigest()


def _tokens(s):
    # 2글자 이상 토큰만(조사/짧은 단어 노이즈 제거)
    return {w for w in norm_title(s).split() if len(w) >= 2}


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_similar(a, b):
    # 1) 문자열 시퀀스 유사도
    if SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD:
        return True
    # 2) 핵심 단어 겹침(자카드)
    if _jaccard(a, b) >= 0.4:
        return True
    # 3) 같은 핵심 주체 + 같은 사건 신호어를 공유하면 동일 사건으로 간주
    ta, tb = _tokens(a), _tokens(b)
    shared = ta & tb
    actors = {"sk하이닉스", "하이닉스", "삼성전자", "마이크론", "엔비디아",
              "hynix", "samsung", "micron", "nvidia", "tsmc"}
    signals = {"시총", "왕좌", "대장주", "1위", "제치고", "제쳤다", "넘은", "추월",
               "역전", "목표주가", "상향", "하향", "급등", "급락", "신고가"}
    if (shared & actors) and (shared & signals):
        return True
    return False


def passes_filter(category, title, summary):
    text = f"{title} {summary}".lower()
    if any(k.lower() in text for k in EXCLUDE_KEYWORDS):
        return False
    return any(k.lower() in text for k in INCLUDE_KEYWORDS.get(category, []))


def clean_summary(raw):
    """RSS 본문에서 태그/공백/잡음 제거 후 한 덩어리 텍스트로"""
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", " ", raw)          # 태그 제거
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)         # URL 제거
    s = re.sub(r"&[a-z]+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 구글뉴스가 매체명만 던지는 경우 등 너무 짧으면 버림
    if len(s) < 25:
        return ""
    if len(s) > SUMMARY_MAX_CHARS:
        s = s[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return s


def source_name(entry):
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        return entry.source.title
    return urlparse(entry.get("link", "")).netloc.replace("www.", "")


def entry_age_hours(entry):
    """기사 발행 후 경과 시간(시간). 시각 정보 없으면 None."""
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    try:
        published = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    delta = now_utc() - published
    return delta.total_seconds() / 3600.0


def is_fresh(entry):
    """MAX_AGE_HOURS 이내면 True. 시각을 못 읽으면 보수적으로 통과(놓침 방지)."""
    age = entry_age_hours(entry)
    if age is None:
        return True          # 날짜 불명 기사는 일단 통과 (중복필터가 한번 더 거름)
    return age <= MAX_AGE_HOURS


def collect():
    results = {}
    for category, urls in FEEDS.items():
        items = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"[WARN] feed fail: {url} ({e})")
                continue
            for entry in feed.entries[:40]:
                title = entry.get("title", "").strip()
                raw_sum = entry.get("summary", "")
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue
                if not is_fresh(entry):          # 오래된 기사 제외
                    continue
                summary = clean_summary(raw_sum)
                if not passes_filter(category, title, raw_sum):
                    continue
                items.append({
                    "title": html.unescape(title),
                    "link": link,
                    "summary": summary,
                    "source": source_name(entry),
                    "ntitle": norm_title(title),
                    "score": importance_score(category, title, raw_sum),
                })
        # 카테고리별 최소 점수 기준 (해외는 사건성 뉴스만 엄격히)
        if category == "📊 리포트/투자의견":
            min_score = None              # 리포트는 점수 필터 미적용 (주가가 본질)
        elif category == "🌐 해외 소식":
            min_score = 3                 # 해외는 사건 신호어가 실제 있는 것만
        else:
            min_score = 0                 # 메모리/AI는 단신(음수)만 제외
        if min_score is not None:
            items = [it for it in items if it["score"] >= min_score]
        # 중요도 높은 순으로 정렬 (같으면 원래 순서 유지)
        items.sort(key=lambda x: x["score"], reverse=True)
        results[category] = items
        print(f"[INFO] {category}: {len(items)} items (after importance filter)")
    return results


def dedupe(items, seen):
    seen_norm = [v["ntitle"] for v in seen.values() if "ntitle" in v]
    out, accepted = [], []
    for it in items:
        if title_key(it["title"]) in seen:
            continue
        if any(is_similar(it["ntitle"], s) for s in seen_norm):
            continue
        if any(is_similar(it["ntitle"], s) for s in accepted):
            continue
        out.append(it)
        accepted.append(it["ntitle"])
    return out


def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,   # 1뉴스 1메시지라 링크 미리보기 켬
    }
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"[ERROR] telegram {r.status_code}: {r.text}")
    return r.status_code == 200


def esc(s):
    return html.escape(s or "")


def strip_source_suffix(title):
    """제목 끝의 ' - 매체명' / ' | 매체명' 꼬리 제거 (번역 전후 모두 사용)"""
    if not title:
        return title
    t = re.sub(r"\s*[-|]\s*[^-|]{2,40}$", "", title).strip()
    # 너무 짧게 잘렸으면 원복
    return t if len(t) >= 6 else title.strip()


def build_message(category, it):
    """1 뉴스 = 1 메시지. 영문은 한글 번역을 함께 표시."""
    lines = [f"{esc(category)}"]

    title = strip_source_suffix(it["title"])
    summary = it["summary"]

    if _has_korean(title):
        # 한글 기사: 그대로 (제미나이 호출 안 함 - 한도 절약)
        lines.append(f'<b>{esc(title)}</b>')
        if summary:
            lines.append(f'{esc(summary)}')
    else:
        # 영문 기사: 제미나이로 번역+요약 한 번에
        ko_title, ko_summary = gemini_summarize_ko(title, summary)
        ko_title = strip_source_suffix(ko_title) if ko_title else ko_title
        lines.append(f'<b>{esc(ko_title if ko_title else title)}</b>')
        if ko_summary:
            lines.append(f'{esc(ko_summary)}')

    src = f' · {esc(it["source"])}' if it["source"] else ""
    lines.append(f'🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}')
    return "\n".join(lines)


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("[FATAL] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 없음")

    seen = prune_seen(load_seen())
    raw = collect()

    # 날짜 헤더 1개만 먼저 (구분용)
    today = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d (%a)")

    new_total = 0
    to_send = []
    for category, items in raw.items():
        deduped = dedupe(items, seen)[:MAX_ITEMS_PER_CATEGORY]
        for it in deduped:
            to_send.append((category, it))
            # 직후 카테고리에서 동일/유사 기사가 다시 잡히지 않도록 즉시 seen 반영
            seen[title_key(it["title"])] = {
                "ntitle": it["ntitle"], "ts": now_utc().timestamp(),
            }
        new_total += len(deduped)

    if new_total == 0:
        print("[INFO] 신규 없음 - 전송 생략")
        save_seen(seen)
        return

    # 헤더
    tg_send(f"📡 <b>반도체·AI 데일리 브리핑</b>\n🗓 {today} KST · 신규 {new_total}건")
    time.sleep(SEND_DELAY)

    # 1뉴스 1메시지
    for category, it in to_send:
        tg_send(build_message(category, it))
        time.sleep(SEND_DELAY)

    save_seen(seen)
    print(f"[DONE] {new_total}건 전송 완료")


if __name__ == "__main__":
    main()
