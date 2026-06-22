#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
메모리 반도체 / AI 데일리 브리핑 텔레그램 봇
- RSS 다중 소스 수집
- 키워드 필터 (메모리 반도체 / AI)
- 제목 유사도 기반 중복 제거 (이미 보낸 것 + 이번 회차 내부 중복)
- 카테고리별로 묶어서 텔레그램 전송
- 보낸 기록은 seen.json 에 저장 (GitHub Actions가 커밋해서 영속화)
"""

import os
import re
import json
import time
import html
import hashlib
import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
import feedparser

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SEEN_FILE = "seen.json"
SEEN_RETENTION_DAYS = 14          # 14일 지난 기록은 자동 정리
MAX_ITEMS_PER_CATEGORY = 12       # 카테고리당 최대 전송 수
SIMILARITY_THRESHOLD = 0.82       # 제목 유사도 중복 판정 임계값
REQUEST_TIMEOUT = 20

# ─────────────────────────────────────────────────────────────
# RSS 소스 (카테고리별)
#   - 구글 뉴스 RSS는 키워드 쿼리로 한국어/영어 둘 다 수집 가능
#   - 매체 RSS도 섞어서 커버리지 확보
# ─────────────────────────────────────────────────────────────
def google_news_rss(query, lang="ko", country="KR"):
    from urllib.parse import quote
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
        google_news_rss("AI 반도체 OR 엔비디아 OR 데이터센터 OR 생성형AI", "ko"),
        google_news_rss("AI chip OR Nvidia OR datacenter GPU OR LLM OR OpenAI", "en"),
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    ],
}

# 카테고리별 양성 키워드 (제목/요약에 하나라도 있어야 통과)
INCLUDE_KEYWORDS = {
    "🧠 메모리 반도체": [
        "hbm", "dram", "nand", "낸드", "디램", "메모리", "memory chip", "lpddr",
        "ddr5", "ddr4", "socamm", "sk하이닉스", "하이닉스", "hynix", "micron", "마이크론",
        "삼성전자", "samsung", "wafer", "웨이퍼", "cowos", "패키징", "packaging",
        "비트", "감산", "증설", "공급", "수요", "가격", "고대역폭",
    ],
    "🤖 AI": [
        "ai", "인공지능", "nvidia", "엔비디아", "gpu", "데이터센터", "datacenter",
        "data center", "llm", "openai", "오픈ai", "추론", "inference", "학습", "training",
        "생성형", "generative", "blackwell", "tpu", "anthropic", "구글", "google",
        "microsoft", "메타", "meta",
    ],
}

# 노이즈 컷용 음성 키워드 (있으면 제외)
EXCLUDE_KEYWORDS = [
    "할인", "쿠폰", "이벤트 당첨", "광고", "분양", "운세", "로또",
    "casino", "porn", "coupon",
]


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────
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
    """오래된 기록 정리"""
    cutoff = (now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    return {k: v for k, v in seen.items() if v.get("ts", 0) >= cutoff}


def norm_title(title):
    """제목 정규화: 매체명/특수문자/공백 제거, 소문자화"""
    t = html.unescape(title or "")
    t = re.sub(r"\s*-\s*[^-]+$", "", t)          # 구글뉴스 ' - 매체명' 꼬리 제거
    t = re.sub(r"[\[\](){}<>·…“”\"'’‘|!?.,~―—\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def title_key(title):
    return hashlib.md5(norm_title(title).encode("utf-8")).hexdigest()


def is_similar(a, b):
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD


def passes_filter(category, title, summary):
    text = f"{title} {summary}".lower()
    if any(k.lower() in text for k in EXCLUDE_KEYWORDS):
        return False
    incl = INCLUDE_KEYWORDS.get(category, [])
    return any(k.lower() in text for k in incl)


def source_name(entry, feed):
    # 구글뉴스는 source 태그가 있는 경우가 많음
    src = ""
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        src = entry.source.title
    if not src:
        link = entry.get("link", "")
        host = urlparse(link).netloc.replace("www.", "")
        src = host
    return src


# ─────────────────────────────────────────────────────────────
# 수집
# ─────────────────────────────────────────────────────────────
def collect():
    results = {}
    for category, urls in FEEDS.items():
        items = []
        for url in urls:
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print(f"[WARN] feed parse fail: {url} ({e})")
                continue
            for entry in feed.entries[:40]:
                title = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue
                if not passes_filter(category, title, summary):
                    continue
                items.append({
                    "title": html.unescape(title),
                    "link": link,
                    "summary": summary[:160],
                    "source": source_name(entry, feed),
                    "ntitle": norm_title(title),
                })
        results[category] = items
        print(f"[INFO] {category}: {len(items)} raw items collected")
    return results


# ─────────────────────────────────────────────────────────────
# 중복 제거
# ─────────────────────────────────────────────────────────────
def dedupe(category_items, seen):
    """seen(과거) + 회차 내부 유사도로 중복 제거"""
    seen_norm = [v["ntitle"] for v in seen.values() if "ntitle" in v]
    out = []
    accepted_norm = []

    for it in category_items:
        key = title_key(it["title"])
        if key in seen:
            continue
        # 과거 보낸 것과 유사하면 제외
        if any(is_similar(it["ntitle"], s) for s in seen_norm):
            continue
        # 이번 회차 내부 중복 제외
        if any(is_similar(it["ntitle"], s) for s in accepted_norm):
            continue
        out.append(it)
        accepted_norm.append(it["ntitle"])
    return out


# ─────────────────────────────────────────────────────────────
# 텔레그램 전송
# ─────────────────────────────────────────────────────────────
def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"[ERROR] telegram {r.status_code}: {r.text}")
    return r.status_code == 200


def esc(s):
    return html.escape(s or "")


def build_messages(briefing):
    """텔레그램 4096자 제한 고려해 카테고리별로 메시지 분할"""
    today = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d (%a)")
    messages = []
    header = f"📡 <b>반도체·AI 데일리 브리핑</b>\n🗓 {today} KST\n"
    messages.append(header)

    for category, items in briefing.items():
        if not items:
            continue
        block = f"\n<b>{esc(category)}</b>  ({len(items)}건)\n"
        for i, it in enumerate(items, 1):
            line = f'{i}. <a href="{esc(it["link"])}">{esc(it["title"])}</a>'
            if it["source"]:
                line += f' <i>· {esc(it["source"])}</i>'
            line += "\n"
            if len(block) + len(line) > 3500:
                messages.append(block)
                block = f"<b>{esc(category)}</b> (계속)\n"
            block += line
        messages.append(block)
    return messages


# ─────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("[FATAL] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.")

    seen = prune_seen(load_seen())
    raw = collect()

    briefing = {}
    new_count = 0
    for category, items in raw.items():
        deduped = dedupe(items, seen)[:MAX_ITEMS_PER_CATEGORY]
        briefing[category] = deduped
        new_count += len(deduped)
        # seen 기록
        for it in deduped:
            seen[title_key(it["title"])] = {
                "ntitle": it["ntitle"],
                "ts": now_utc().timestamp(),
            }

    if new_count == 0:
        print("[INFO] 신규 뉴스 없음 — 전송 생략")
        save_seen(seen)
        return

    messages = build_messages(briefing)
    for msg in messages:
        if msg.strip():
            tg_send(msg)
            time.sleep(0.6)  # rate limit 회피

    save_seen(seen)
    print(f"[DONE] 신규 {new_count}건 전송 완료")


if __name__ == "__main__":
    main()
