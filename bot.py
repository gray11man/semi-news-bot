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
MAX_ITEMS_PER_CATEGORY = 10        # 카테고리당 최대 (알림 폭주 방지)
SIMILARITY_THRESHOLD = 0.82
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
        google_news_rss("AI 반도체 OR 엔비디아 OR 데이터센터 OR 생성형AI", "ko"),
        google_news_rss("AI chip OR Nvidia OR datacenter GPU OR LLM OR OpenAI", "en"),
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
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
        "ai", "인공지능", "nvidia", "엔비디아", "gpu", "데이터센터", "datacenter",
        "data center", "llm", "openai", "오픈ai", "추론", "inference", "학습", "training",
        "생성형", "generative", "blackwell", "tpu", "anthropic", "구글", "google",
        "microsoft", "메타", "meta",
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
    t = re.sub(r"\s*-\s*[^-]+$", "", t)
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
