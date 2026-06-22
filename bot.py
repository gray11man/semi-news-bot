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

SEEN_FILE = "seen.json"
SEEN_RETENTION_DAYS = 14
MAX_ITEMS_PER_CATEGORY = 8         # 카테고리당 최대 (알림 폭주 방지)
SIMILARITY_THRESHOLD = 0.68        # 낮을수록 중복을 더 적극적으로 묶음
SUMMARY_MAX_CHARS = 180            # 요약 최대 길이
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
        google_news_rss("Micron OR SK Hynix OR Samsung memory OR TSMC HBM", "en"),
        google_news_rss("Nvidia OR AMD OR Broadcom datacenter OR memory market", "en"),
        google_news_rss("semiconductor memory demand OR HBM supply", "en"),
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
                summary = clean_summary(raw_sum)
                if not passes_filter(category, title, raw_sum):
                    continue
                items.append({
                    "title": html.unescape(title),
                    "link": link,
                    "summary": summary,
                    "source": source_name(entry),
                    "ntitle": norm_title(title),
                })
        results[category] = items
        print(f"[INFO] {category}: {len(items)} raw")
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


def build_message(category, it):
    """1 뉴스 = 1 메시지"""
    lines = [f"{esc(category)}"]
    lines.append(f'<b>{esc(it["title"])}</b>')
    if it["summary"]:
        lines.append(f'{esc(it["summary"])}')
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
