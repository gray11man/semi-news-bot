# -*- coding: utf-8 -*-
"""
fetch_news.py  (v3 - 24시간 신선도 검증판)

역할: 구글 뉴스 RSS(한국+미국)에서 전업종 뉴스를 넓게 수집해
      main.py → filter_stage1로 넘긴다.
      섹터 선별은 signals.py가 하므로 여기서는 '넓게 + 신선하게'만 담당.

신선도 3중 검증 (bot.py v2.7과 동일한 방어 구조):
  1) RSS published 기준 24시간 이내만 수집
  2) URL 경로에 박힌 날짜(/2026/06/18/ 등)가 48시간 초과면 차단
     → 구글 재색인으로 published가 "방금"으로 조작된 옛 기사 방어
  3) seen.json으로 실행 간 중복 전송 방지 (30일 보존)

반환 형식: [{"title", "summary", "link", "source", "published"}, ...]
"""

import os
import re
import json
import html
import hashlib
import datetime
from urllib.parse import urlparse, quote

import feedparser

# ───────────────────────── 설정 ─────────────────────────
FRESH_HOURS = 24            # 1일 이내 뉴스만
URL_DATE_HARD_LIMIT_H = 48  # URL 날짜 기준 하드리밋 (재색인 방어)
SEEN_FILE = "seen_ideas.json"
SEEN_RETENTION_DAYS = 30
RSS_MAX_ENTRIES = 40
SUMMARY_MAX = 300

# 저신호 매체 차단 (게임/연예/커뮤니티)
SOURCE_BLACKLIST = [
    "인벤", "루리웹", "디스이즈게임", "게임메카", "디스패치", "위키트리",
    "인사이트", "허프포스트",
]


def _gnews(query, lang="ko"):
    q = quote(f"{query} when:{FRESH_HOURS}h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# 전업종을 넓게 커버하는 소스.
# 섹터 선별은 signals.py 몫이므로 쿼리는 넓은 그물이면 충분하다.
FEEDS = [
    # 한국 경제/산업 헤드라인 토픽
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
    # 미국 비즈니스 헤드라인 토픽
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    # 넓은 그물 쿼리 (구조 변곡이 잘 걸리는 표현 위주)
    _gnews("수주 OR 공급 부족 OR 가격 급등 OR 사상 최대 OR 증설 OR 사상 최초", "ko"),
    _gnews("금리 OR 환율 OR FOMC OR 유가 OR 신조선가 OR 운임 OR 전력망", "ko"),
    _gnews("수출 급증 OR 기술 수출 OR FDA 승인 OR 세계 최초 OR 역대 최대 수출", "ko"),
    _gnews("shortage OR \"supply crunch\" OR \"record high\" OR \"first ever\" OR surge", "en"),
    _gnews("\"rate cut\" OR FOMC OR tariff OR sanction OR \"defense spending\"", "en"),
    _gnews("shipbuilding OR freight OR \"power grid\" OR uranium OR \"data center\"", "en"),
]


# ───────────────────────── 유틸 ─────────────────────────
def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _title_key(title):
    t = html.unescape(title or "")
    t = re.sub(r"\s*[-|·]\s*[^-|·]+$", "", t)          # 꼬리 매체명 제거
    t = re.sub(r"[^\w가-힣]+", "", t).lower()
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def _entry_age_hours(entry):
    tm = entry.get("published_parsed")
    if not tm:
        return None
    try:
        published = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    return (_now_utc() - published).total_seconds() / 3600.0


def _url_date_age_hours(url):
    """URL 경로의 날짜(/2026/06/18/, 2026-06-18, 20260618)로 나이 계산.
    구글 재색인으로 published가 조작된 옛 기사를 URL로 잡는다."""
    if not url:
        return None
    m = re.search(r"/(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})(?:[/\-?#]|$)", url)
    if not m:
        m = re.search(r"[/\-_](20\d{2})(\d{2})(\d{2})[/\-_.]", url)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        dt = datetime.datetime(y, mo, d, tzinfo=datetime.timezone.utc)
        return (_now_utc() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _source_name(entry):
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        return entry.source.title
    return urlparse(entry.get("link", "")).netloc.replace("www.", "")


def _clean_summary(raw):
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", " ", raw)
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:SUMMARY_MAX]


def _prune_seen(seen):
    cutoff = (_now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    return {k: v for k, v in seen.items() if v >= cutoff}


# ───────────────────────── 메인 수집 ─────────────────────────
def fetch_news():
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    items = []
    dup_keys = set()

    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] feed fail: {e}")
            continue
        for entry in feed.entries[:RSS_MAX_ENTRIES]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or not link:
                continue

            # 저신호 매체 차단
            src = _source_name(entry)
            if any(b in src for b in SOURCE_BLACKLIST):
                continue

            # ── 신선도 1차: published 24시간 이내 (날짜 없으면 차단) ──
            age = _entry_age_hours(entry)
            if age is None or age > FRESH_HOURS + 1:
                continue

            # ── 신선도 2차: URL 날짜 48시간 초과 차단 (재색인 방어) ──
            u_age = _url_date_age_hours(link)
            if u_age is not None and u_age > URL_DATE_HARD_LIMIT_H:
                continue

            # ── 중복: 실행 내 + 실행 간(seen.json) ──
            key = _title_key(title)
            if key in dup_keys or key in seen:
                continue
            dup_keys.add(key)

            pub_iso = ""
            tm = entry.get("published_parsed")
            if tm:
                try:
                    pub_iso = datetime.datetime(
                        *tm[:6], tzinfo=datetime.timezone.utc).isoformat()
                except Exception:
                    pub_iso = ""

            items.append({
                "title": html.unescape(title),
                "summary": _clean_summary(entry.get("summary", "")),
                "link": link,
                "source": src,
                "published": pub_iso,
                "_seen_key": key,
            })

    print(f"[fetch] {len(items)}건 수집 (24h 이내, 중복 제거 후)")
    return items


def mark_sent(items):
    """전송 완료된 기사를 seen에 기록. main.py에서 전송 후 호출 권장:
        from fetch_news import mark_sent
        mark_sent(results)
    호출하지 않으면 같은 기사가 다음 실행에서 재전송될 수 있다."""
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    now_ts = _now_utc().timestamp()
    for it in items:
        key = it.get("_seen_key") or _title_key(it.get("title", ""))
        seen[key] = now_ts
    _save_json(SEEN_FILE, seen)
    print(f"[fetch] seen 기록 {len(items)}건")


if __name__ == "__main__":
    for it in fetch_news()[:10]:
        print(f"- [{it['source']}] {it['title'][:60]}")
