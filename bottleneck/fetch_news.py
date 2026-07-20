# -*- coding: utf-8 -*-
"""
fetch_news.py  (v3.1 - 유사기사 중복 차단판)

v3 → v3.1 변경:
  - [핵심] 같은 사건을 다른 제목으로 쓴 기사 차단 (유사도 판정 추가)
    예: "미-이란 확전 우려로 유가 급등" vs "중동 확전에 유가 급등"
    → 아침에 보낸 사건을 저녁에 또 해석하던 문제 해결
  - seen 구조 확장: 해시키 → {ts, nt(정규화 제목)} (기존 파일과 호환)
  - 실행 내 중복도 유사도로 판정 (같은 회차에 비슷한 기사 2건 방지)

신선도 3중 검증 (v3과 동일):
  1) RSS published 기준 24시간 이내만 수집
  2) URL 경로 날짜 48시간 초과 차단 (구글 재색인 방어)
  3) seen_ideas.json 30일 보존

반환 형식: [{"title", "summary", "link", "source", "published"}, ...]
"""

import os
import re
import json
import html
import hashlib
import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse, quote

import feedparser

# ───────────────────────── 설정 ─────────────────────────
FRESH_HOURS = 24            # 1일 이내 뉴스만
URL_DATE_HARD_LIMIT_H = 48  # URL 날짜 기준 하드리밋 (재색인 방어)
SEEN_FILE = "seen_ideas.json"
SEEN_RETENTION_DAYS = 30
RSS_MAX_ENTRIES = 40
SUMMARY_MAX = 300
SIMILARITY_THRESHOLD = 0.65  # [v3.1] 제목 유사도 이 이상이면 같은 사건 취급

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
    t = re.sub(r"\s+[-–—|]\s+[\w가-힣 .이코노미]{1,25}$", "", t)          # 꼬리 매체명 제거
    t = re.sub(r"[^\w가-힣]+", "", t).lower()
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def _norm_for_sim(title):
    """[v3.1] 유사도 비교용 제목 정규화."""
    t = html.unescape(title or "")
    t = re.sub(r"\s+[-–—|]\s+[\w가-힣 .이코노미]{1,25}$", "", t)          # 꼬리 매체명 제거
    t = re.sub(r"\[[^\]]*\]", " ", t)                  # [단독] 등 말머리 제거
    t = re.sub(r"[^\w가-힣 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _tokens(s):
    return {w for w in s.split() if len(w) >= 2}


# [v3.1] 한국어 조사 제거 (확전에→확전, 우려로→우려 등을 같은 토큰으로)
_PARTICLES = ("에서", "으로", "이라", "라고", "에는", "에도", "까지", "부터",
              "에", "은", "는", "이", "가", "을", "를", "로", "의", "와", "과", "도")


def _strip_particle(w):
    for p in _PARTICLES:
        if len(w) > len(p) + 1 and w.endswith(p):
            return w[:-len(p)]
    return w


def _event_tokens(s):
    return {_strip_particle(w) for w in s.split() if len(_strip_particle(w)) >= 2}


def _is_similar(a, b):
    """[v3.1] 같은 사건 판정 3단:
    1) 문자열 유사도  2) 토큰 자카드  3) 조사 제거 후 핵심토큰 3개 이상 공유."""
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.5:
        return True
    # 조사 제거 후 사건 핵심토큰 비교 (예: 확전+유가+급등 3개 공유 → 같은 사건)
    ea, eb = _event_tokens(a), _event_tokens(b)
    if ea and eb:
        shared = ea & eb
        if len(shared) >= 3:
            return True
    return False


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
    """[v3.1] 구버전(값=timestamp 숫자)과 신버전(값={ts, nt}) 모두 호환."""
    cutoff = (_now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    out = {}
    for k, v in seen.items():
        if isinstance(v, dict):
            ts = v.get("ts", 0)
            if ts >= cutoff:
                out[k] = v
        else:  # 구버전 숫자값 → 신형식으로 변환
            if v >= cutoff:
                out[k] = {"ts": v, "nt": ""}
    return out


# ───────────────────────── 메인 수집 ─────────────────────────
def fetch_news():
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    # [v3.1] 과거 전송 기사의 정규화 제목 목록 (유사도 비교용)
    seen_titles = [v.get("nt", "") for v in seen.values() if v.get("nt")]

    items = []
    dup_keys = set()
    run_titles = []   # [v3.1] 이번 실행 내 유사도 비교용

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

            # ── 중복 1: 완전일치 해시 (실행 내 + 실행 간) ──
            key = _title_key(title)
            if key in dup_keys or key in seen:
                continue

            # ── 중복 2 [v3.1]: 유사기사 판정 ──
            nt = _norm_for_sim(title)
            #   (a) 과거에 전송한 사건과 유사 → 차단 (같은 사건 재해석 방지)
            if any(_is_similar(nt, s) for s in seen_titles):
                continue
            #   (b) 이번 실행 내 유사 기사 → 차단
            if any(_is_similar(nt, s) for s in run_titles):
                continue

            dup_keys.add(key)
            run_titles.append(nt)

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
                "_nt": nt,
            })

    print(f"[fetch] {len(items)}건 수집 (24h 이내, 완전일치+유사 중복 제거 후)")
    return items


def mark_sent(items):
    """전송 완료된 기사를 seen에 기록 (정규화 제목 포함 → 유사기사 차단에 사용).
    main.py에서 전송 후 호출:
        from fetch_news import mark_sent
        mark_sent(results)
    주의: results가 {"item": {...}, "verdict": {...}} 형태면 item을 꺼내 기록한다.
    """
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    now_ts = _now_utc().timestamp()
    count = 0
    for r in items:
        # evaluate_stage2 결과 형태({"item": ...})와 원본 기사 형태 모두 지원
        it = r.get("item", r) if isinstance(r, dict) else r
        if not isinstance(it, dict):
            continue
        title = it.get("title", "")
        if not title:
            continue
        key = it.get("_seen_key") or _title_key(title)
        nt = it.get("_nt") or _norm_for_sim(title)
        seen[key] = {"ts": now_ts, "nt": nt}
        count += 1
    _save_json(SEEN_FILE, seen)
    print(f"[fetch] seen 기록 {count}건")


if __name__ == "__main__":
    for it in fetch_news()[:10]:
        print(f"- [{it['source']}] {it['title'][:60]}")
