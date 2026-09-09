"""기존 피드를 유지한 수집기. 제목 일치 제거 및 피드별 진단, 성공 전송 이력 저장."""
import os
import re
import json
import html
import hashlib
import datetime
from urllib.parse import urlparse, quote

import feedparser
import requests
import tempfile
from pathlib import Path
from collections import Counter

# ───────────────────────── 설정 ─────────────────────────
FRESH_HOURS = 24            # 1일 이내 뉴스만
URL_DATE_HARD_LIMIT_H = 48  # URL 날짜 기준 하드리밋 (재색인 방어)
SEEN_FILE = str(Path(__file__).with_name("seen_ideas.json"))
FETCH_STATS = []
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
    if lang == "zh":
        return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# 전업종을 넓게 커버하는 소스.
# 산업 중요성은 pick_headlines.py에서 평가한다.
FEEDS = [
    # 한국 경제/산업 헤드라인 토픽
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",
    # 미국 비즈니스 헤드라인 토픽
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    # [v3.2] 중국 비즈니스 헤드라인 토픽 (구글뉴스 중국어판)
    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    # 넓은 그물 쿼리 (구조 변곡이 잘 걸리는 표현 위주)
    _gnews("수주 OR 공급 부족 OR 가격 급등 OR 사상 최대 OR 증설 OR 사상 최초", "ko"),
    _gnews("금리 OR 환율 OR FOMC OR 유가 OR 신조선가 OR 운임 OR 전력망", "ko"),
    _gnews("수출 급증 OR 기술 수출 OR FDA 승인 OR 세계 최초 OR 역대 최대 수출", "ko"),
    _gnews("shortage OR \"supply crunch\" OR \"record high\" OR \"first ever\" OR surge", "en"),
    _gnews("\"rate cut\" OR FOMC OR tariff OR sanction OR \"defense spending\"", "en"),
    _gnews("shipbuilding OR freight OR \"power grid\" OR uranium OR \"data center\"", "en"),
    # [v3.2] 중국어 산업/해운/공급망 쿼리
    #   구글뉴스 한국/영어판에 잘 안 걸리는 중국 현지 업계 심층 소식
    #   (해운 운임, 공급과잉/부족 재평가, 정부 정책, 원자재 등)
    _gnews("集运 OR 运价 OR 缺柜 OR 舱位 OR 港口 拥堵", "zh"),   # 컨테이너 해운/운임/항만 정체
    _gnews("供应过剩 OR 供不应求 OR 涨价 OR 减产 OR 扩产", "zh"),  # 공급과잉/부족, 가격 인상, 증산/감산
    _gnews("关税 OR 出口管制 OR 制裁 OR 补贴政策", "zh"),          # 관세/수출통제/제재/보조금 정책
    _gnews("芯片 OR 半导体 OR 稀土 OR 锂电池 供应链", "zh"),       # 반도체·희토류·배터리 공급망
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
    except (OSError, ValueError) as exc:
        raise RuntimeError("전송 이력 읽기 실패: 파일을 확인하세요") from exc


def _save_json(path, data):
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


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


def _is_similar(a, b):
    # 유사 제목은 진단용. 숫자 변경/반대 방향 뉴스를 자동 탈락시키지 않는다.
    if not a or not b:
        return False
    return a == b


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
    FETCH_STATS.clear()
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    # [v3.1] 과거 전송 기사의 정규화 제목 목록 (유사도 비교용)
    seen_titles = [v.get("nt", "") for v in seen.values() if v.get("nt")]

    items = []
    dup_keys = set()
    run_titles = []   # [v3.1] 이번 실행 내 유사도 비교용

    healthy = 0
    for feed_index, url in enumerate(FEEDS):
        stats = Counter(raw=0, accepted=0)
        FETCH_STATS.append({"feed": feed_index, "url": url, "counts": stats})
        try:
            response = requests.get(url, timeout=(10, 25), headers={"User-Agent": "InvestmentNewsBot/4.0"})
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.get("version"):
                raise ValueError("RSS/Atom 아님")
            if feed.get("bozo"):
                raise ValueError("RSS 파싱 오류")
            healthy += 1
        except (requests.RequestException, ValueError):
            stats["failed"] += 1
            print(f"[fetch] feed={feed_index} 실패")
            continue
        stats["raw"] = len(feed.entries)
        stats["over_limit"] = max(0, len(feed.entries) - RSS_MAX_ENTRIES)
        for entry in feed.entries[:RSS_MAX_ENTRIES]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or urlparse(link).scheme not in ("http", "https"):
                stats["invalid"] += 1
                continue

            # 저신호 매체 차단
            src = _source_name(entry)
            if any(b in src for b in SOURCE_BLACKLIST):
                stats["blacklist"] += 1
                continue

            # ── 신선도 1차: published 24시간 이내 (날짜 없으면 차단) ──
            age = _entry_age_hours(entry)
            if age is None or age > FRESH_HOURS or age < -1:
                stats["date_rejected"] += 1
                continue

            # ── 신선도 2차: URL 날짜 48시간 초과 차단 (재색인 방어) ──
            u_age = _url_date_age_hours(link)
            if u_age is not None and u_age > URL_DATE_HARD_LIMIT_H:
                stats["old_url"] += 1
                continue

            # ── 중복 1: 완전일치 해시 (실행 내 + 실행 간) ──
            key = _title_key(title)
            if key in dup_keys or key in seen:
                stats["exact_duplicate"] += 1
                continue

            # ── 정규화 제목 일치만 제거; 사건 중복은 LLM에서 평가 ──
            nt = _norm_for_sim(title)
            #   (a) 과거 전송 제목과 정규화 후 동일
            if any(_is_similar(nt, s) for s in seen_titles):
                stats["exact_duplicate"] += 1
                continue
            #   (b) 이번 실행 내 동일한 정규화 제목
            if any(_is_similar(nt, s) for s in run_titles):
                stats["exact_duplicate"] += 1
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

            stats["accepted"] += 1
            items.append({
                "feed_id": feed_index,
                "title": html.unescape(title),
                "summary": _clean_summary(entry.get("summary", "")),
                "link": link,
                "source": src,
                "published": pub_iso,
                "_seen_key": key,
                "_nt": nt,
            })

    if healthy == 0:
        raise RuntimeError("모든 피드 수집 실패: 뉴스 0건과 다릅니다")
    items.sort(key=lambda it: it["published"], reverse=True)
    print(f"[fetch] {healthy}/{len(FEEDS)} 피드 정상, {len(items)}건 수집")
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
        seen[key] = {"ts": now_ts, "nt": nt, "title": title,
                     "link": it.get("link", ""), "reason": it.get("reason", "")}
        count += 1
    _save_json(SEEN_FILE, seen)
    print(f"[fetch] seen 기록 {count}건")


if __name__ == "__main__":
    for it in fetch_news()[:10]:
        print(f"- [{it['source']}] {it['title'][:60]}")

def recent_sent():
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    rows = sorted(seen.values(), key=lambda row: row["ts"], reverse=True)
    return [{"title": row.get("title") or row.get("nt", ""),
             "reason": row.get("reason", ""), "sent_ts": row["ts"]}
            for row in rows if row.get("title") or row.get("nt")]
