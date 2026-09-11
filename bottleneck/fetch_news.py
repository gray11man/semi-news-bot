"""산업 뉴스 수집기 v5.

- 최근 기사만 수집
- 제목 완전/고유사 중복 제거
- 숫자 변화·방향 반전은 새 업데이트로 보호
- 업종별 Google News RSS를 넓게 구성해 산업 다양성 확보
- 성공 전송 이력만 seen_ideas.json에 저장
"""
import datetime
import hashlib
import html
import json
import os
import re
import tempfile
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse

import feedparser
import requests

# ───────────────────────── 설정 ─────────────────────────
FRESH_HOURS = int(os.getenv("FRESH_HOURS", "24"))
URL_DATE_HARD_LIMIT_H = int(os.getenv("URL_DATE_HARD_LIMIT_H", "48"))
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "30"))
HISTORY_LLM_DAYS = int(os.getenv("HISTORY_LLM_DAYS", "10"))
HISTORY_LLM_LIMIT = int(os.getenv("HISTORY_LLM_LIMIT", "300"))
RSS_MAX_ENTRIES = int(os.getenv("RSS_MAX_ENTRIES", "25"))
SUMMARY_MAX = int(os.getenv("SUMMARY_MAX", "700"))
MAX_TOTAL_ITEMS = int(os.getenv("MAX_TOTAL_ITEMS", "480"))
SEEN_FILE = os.getenv("SEEN_FILE", str(Path(__file__).with_name("seen_ideas.json")))
FETCH_STATS = []

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


def _spec(label, sector, query=None, lang="ko", url=None):
    return {"label": label, "sector": sector, "url": url or _gnews(query, lang)}


# 섹터 피드를 먼저 둔다. 같은 기사가 종합 피드에도 있을 경우 섹터 라벨을 보존하기 위함.
FEED_SPECS = [
    # 한국
    _spec("KR-반도체", "반도체·전자", "반도체 OR HBM OR DRAM OR NAND OR 파운드리 OR 디스플레이", "ko"),
    _spec("KR-전력", "전력·원전·유틸리티", "전력망 OR 변압기 OR 원전 OR 가스터빈 OR 발전소 OR 전력기기", "ko"),
    _spec("KR-에너지", "에너지·자원", "LNG OR 천연가스 OR 원유 OR 우라늄 OR 정유 OR 광산", "ko"),
    _spec("KR-조선물류", "조선·해운·물류", "조선 OR 선박 OR 해운 OR 운임 OR 항만 OR 물류", "ko"),
    _spec("KR-소재", "철강·화학·소재", "철강 OR 석유화학 OR 화학 OR 구리 OR 알루미늄 OR 시멘트", "ko"),
    _spec("KR-자동차기계", "자동차·배터리·기계", "자동차 OR 전기차 OR 배터리 OR 로봇 OR 공작기계 OR 산업기계", "ko"),
    _spec("KR-방산우주", "방산·항공우주", "방산 OR 미사일 OR 항공우주 OR 위성 OR 드론", "ko"),
    _spec("KR-헬스케어", "제약·바이오·헬스케어", "제약 OR 바이오 OR 의료기기 OR 병원 OR 보험수가", "ko"),
    _spec("KR-금융", "금융·보험·부동산", "은행 OR 보험 OR 증권 OR 카드 OR 대출 OR 부동산 PF", "ko"),
    _spec("KR-농식품", "농업·식품", "곡물 OR 비료 OR 농업 OR 축산 OR 설탕 OR 커피 OR 식품", "ko"),
    _spec("KR-소비여행", "소비재·유통·여행", "유통 OR 소매 OR 호텔 OR 항공 OR 여행 OR 화장품 OR 의류", "ko"),
    _spec("KR-디지털인프라", "통신·클라우드·데이터센터", "통신 OR 클라우드 OR 데이터센터 OR 광통신 OR 서버 OR 네트워크", "ko"),
    _spec("KR-소프트웨어", "소프트웨어·인터넷", "소프트웨어 OR SaaS OR 플랫폼 OR 광고시장 OR 게임산업 OR 사이버보안", "ko"),
    _spec("KR-산업재", "산업재·건설·인프라", "건설기계 OR 인프라 OR 철도 OR 플랜트 OR HVAC OR 냉각 OR 산업자동화", "ko"),
    _spec("KR-환경", "환경·수처리·재활용", "폐기물 OR 수처리 OR 재활용 OR 탄소포집 OR 환경설비", "ko"),

    # 영어권
    _spec("EN-semiconductor", "반도체·전자", "semiconductor OR DRAM OR NAND OR HBM OR foundry OR display", "en"),
    _spec("EN-power", "전력·원전·유틸리티", '"power grid" OR transformer OR nuclear OR "gas turbine" OR utility', "en"),
    _spec("EN-energy", "에너지·자원", "LNG OR natural gas OR oil OR uranium OR refinery OR mining", "en"),
    _spec("EN-shipping", "조선·해운·물류", "shipbuilding OR shipping OR freight OR port OR logistics", "en"),
    _spec("EN-materials", "철강·화학·소재", "steel OR petrochemical OR chemical OR copper OR aluminum OR cement", "en"),
    _spec("EN-auto", "자동차·배터리·기계", 'automotive OR EV OR battery OR robotics OR "industrial machinery"', "en"),
    _spec("EN-defense", "방산·항공우주", "defense OR aerospace OR missile OR satellite OR drone", "en"),
    _spec("EN-health", "제약·바이오·헬스케어", 'pharma OR biotech OR "medical device" OR hospital OR reimbursement', "en"),
    _spec("EN-finance", "금융·보험·부동산", 'bank OR insurance OR credit OR "commercial real estate"', "en"),
    _spec("EN-agri", "농업·식품", "grain OR fertilizer OR agriculture OR livestock OR cocoa OR coffee", "en"),
    _spec("EN-consumer", "소비재·유통·여행", "retail OR airline OR hotel OR travel OR cosmetics OR apparel", "en"),
    _spec("EN-digital", "통신·클라우드·데이터센터", 'telecom OR cloud OR "data center" OR optical OR network OR server', "en"),
    _spec("EN-software", "소프트웨어·인터넷", 'software OR SaaS OR platform OR advertising OR gaming OR cybersecurity', "en"),
    _spec("EN-industrials", "산업재·건설·인프라", '"construction equipment" OR infrastructure OR rail OR HVAC OR cooling OR automation', "en"),
    _spec("EN-environment", "환경·수처리·재활용", '"waste management" OR "water treatment" OR recycling OR "carbon capture"', "en"),

    # 중국
    _spec("ZH-chip", "반도체·전자", "芯片 OR 半导体 OR 存储 OR 晶圆厂 OR 显示面板", "zh"),
    _spec("ZH-logistics", "조선·해운·물류", "集运 OR 运价 OR 舱位 OR 港口 OR 造船", "zh"),
    _spec("ZH-materials", "철강·화학·소재", "钢铁 OR 石化 OR 稀土 OR 铜 OR 铝 OR 水泥", "zh"),
    _spec("ZH-auto", "자동차·배터리·기계", "新能源汽车 OR 锂电池 OR 机器人 OR 工业设备", "zh"),
    _spec("ZH-policy", "정책·공급망", "关税 OR 出口管制 OR 制裁 OR 补贴政策 OR 减产 OR 扩产", "zh"),

    # 종합 헤드라인은 마지막에 둔다.
    _spec("KR-business", "종합", url="https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko"),
    _spec("EN-business", "종합", url="https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    _spec("ZH-business", "종합", url="https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
]
FEEDS = [spec["url"] for spec in FEED_SPECS]
_SPEC_BY_URL = {spec["url"]: spec for spec in FEED_SPECS}

# ───────────────────────── 유틸 ─────────────────────────
def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _env_int(name, default, minimum=None, maximum=None):
    raw = os.getenv(name)
    value = default if raw in (None, "") else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}은 {minimum} 이상")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name}은 {maximum} 이하")
    return value


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON 최상위 형식")
        return data
    except (OSError, ValueError) as exc:
        raise RuntimeError("전송 이력 읽기 실패: 파일을 확인하세요") from exc


def _save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _strip_media_tail(title):
    t = html.unescape(title or "")
    return re.sub(r"\s+[-–—|]\s+[\w가-힣 .이코노미]{1,25}$", "", t).strip()


def _title_key(title):
    t = _strip_media_tail(title)
    t = re.sub(r"[^\w가-힣]+", "", t).lower()
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def _norm_for_sim(title):
    t = _strip_media_tail(title)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[^\w가-힣%.$€£¥兆亿美元만원억원조 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _number_tokens(text):
    text = html.unescape(text or "").lower()
    # 30%, $5.2b, 1.5조, 2026 등 숫자 변화가 있는 업데이트를 보호한다.
    return tuple(re.findall(r"(?:[$€£¥]\s*)?\d+(?:[.,]\d+)*(?:\s*(?:%|bp|bps|배|x|조|억|만|천|b|bn|m|mn|t|兆|亿|万))?", text))


_UP_WORDS = ("증가", "인상", "상승", "확대", "증설", "증산", "급등", "늘", "increase", "raise", "rise", "expand", "ramp", "surge", "扩产", "上涨", "增加")
_DOWN_WORDS = ("감소", "인하", "하락", "축소", "감산", "폐쇄", "급락", "줄", "decrease", "cut", "fall", "reduce", "close", "shutdown", "减产", "下跌", "减少")


def _direction(text):
    t = (text or "").lower()
    up = any(w in t for w in _UP_WORDS)
    down = any(w in t for w in _DOWN_WORDS)
    if up and not down:
        return 1
    if down and not up:
        return -1
    return 0


def _token_jaccard(a, b):
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_similar(a, b):
    """매우 비슷한 제목만 중복 처리한다.

    숫자가 달라졌거나 증가↔감소처럼 방향이 바뀐 경우는 새 업데이트로 보호한다.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    na, nb = _number_tokens(a), _number_tokens(b)
    if (na or nb) and na != nb:
        return False
    da, db = _direction(a), _direction(b)
    if da and db and da != db:
        return False
    ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    jac = _token_jaccard(a, b)
    return ratio >= 0.94 or (ratio >= 0.90 and jac >= 0.82)


def _entry_age_hours(entry):
    tm = entry.get("published_parsed") or entry.get("updated_parsed")
    if not tm:
        return None
    try:
        published = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    return (_now_utc() - published).total_seconds() / 3600.0


def _url_date_age_hours(url):
    if not url:
        return None
    m = re.search(r"/(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})(?:[/\-?#]|$)", url)
    if not m:
        m = re.search(r"[/\-_](20\d{2})(\d{2})(\d{2})[/\-_.]", url)
    if not m:
        return None
    try:
        y, mo, d = map(int, m.groups())
        dt = datetime.datetime(y, mo, d, tzinfo=datetime.timezone.utc)
        return (_now_utc() - dt).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _source_name(entry):
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        return str(entry.source.title).strip()
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return str(source["title"]).strip()
    return urlparse(entry.get("link", "")).netloc.replace("www.", "")


def _clean_summary(raw):
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(raw))
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:SUMMARY_MAX]


def _prune_seen(seen):
    cutoff = (_now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    out = {}
    for k, v in seen.items():
        if isinstance(v, dict):
            ts = v.get("ts", 0)
            if isinstance(ts, (int, float)) and ts >= cutoff:
                out[k] = v
        elif isinstance(v, (int, float)) and v >= cutoff:
            out[k] = {"ts": v, "nt": ""}
    return out


def _balanced_trim(items, limit):
    """수집량이 너무 많을 때 최신순을 유지하면서 섹터별 round-robin으로 자른다."""
    if limit <= 0 or len(items) <= limit:
        return items
    groups = defaultdict(deque)
    for item in items:
        groups[item.get("sector_hint") or "종합"].append(item)
    sectors = sorted(groups, key=lambda s: (s == "종합", s))
    out = []
    while len(out) < limit:
        progressed = False
        for sector in sectors:
            if groups[sector] and len(out) < limit:
                out.append(groups[sector].popleft())
                progressed = True
        if not progressed:
            break
    out.sort(key=lambda it: it.get("published", ""), reverse=True)
    return out


# ───────────────────────── 메인 수집 ─────────────────────────
def fetch_news():
    FETCH_STATS.clear()
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    seen_titles = [v.get("nt", "") for v in seen.values() if isinstance(v, dict) and v.get("nt")]

    items = []
    dup_keys = set()
    run_titles = []
    healthy = 0

    # 테스트가 FEEDS만 monkeypatch해도 동작하도록 URL 기반으로 메타데이터를 찾는다.
    for feed_index, url in enumerate(FEEDS):
        spec = _SPEC_BY_URL.get(url, {"label": f"feed-{feed_index}", "sector": "종합", "url": url})
        stats = Counter(raw=0, accepted=0)
        FETCH_STATS.append({
            "feed": feed_index,
            "label": spec["label"],
            "sector": spec["sector"],
            "url": url,
            "counts": stats,
        })
        try:
            response = requests.get(
                url,
                timeout=(10, 25),
                headers={"User-Agent": "InvestmentNewsBot/5.0"},
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.get("version"):
                raise ValueError("RSS/Atom 아님")
            # 일부 정상 RSS도 경미한 bozo 경고가 날 수 있어 entries가 있으면 살린다.
            if feed.get("bozo") and not feed.entries:
                raise ValueError("RSS 파싱 오류")
            healthy += 1
        except (requests.RequestException, ValueError):
            stats["failed"] += 1
            print(f"[fetch] feed={feed_index} {spec['label']} 실패")
            continue

        stats["raw"] = len(feed.entries)
        stats["over_limit"] = max(0, len(feed.entries) - RSS_MAX_ENTRIES)

        for entry in feed.entries[:RSS_MAX_ENTRIES]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title or urlparse(link).scheme not in ("http", "https"):
                stats["invalid"] += 1
                continue

            src = _source_name(entry)
            if any(blocked.lower() in src.lower() for blocked in SOURCE_BLACKLIST):
                stats["blacklist"] += 1
                continue

            age = _entry_age_hours(entry)
            if age is None or age > FRESH_HOURS or age < -1:
                stats["date_rejected"] += 1
                continue

            u_age = _url_date_age_hours(link)
            if u_age is not None:
                stats["url_date_found"] += 1
                if u_age > URL_DATE_HARD_LIMIT_H:
                    stats["old_url"] += 1
                    continue

            key = _title_key(title)
            if key in dup_keys or key in seen:
                stats["exact_duplicate"] += 1
                continue

            nt = _norm_for_sim(title)
            if any(_is_similar(nt, old) for old in seen_titles):
                stats["similar_sent_duplicate"] += 1
                continue
            if any(_is_similar(nt, old) for old in run_titles):
                stats["similar_run_duplicate"] += 1
                continue

            tm = entry.get("published_parsed") or entry.get("updated_parsed")
            try:
                pub_iso = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc).isoformat() if tm else ""
            except Exception:
                pub_iso = ""

            dup_keys.add(key)
            run_titles.append(nt)
            stats["accepted"] += 1
            items.append({
                "feed_id": feed_index,
                "feed_label": spec["label"],
                "sector_hint": spec["sector"],
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

    items.sort(key=lambda it: it.get("published", ""), reverse=True)
    before_trim = len(items)
    items = _balanced_trim(items, MAX_TOTAL_ITEMS)
    if len(items) < before_trim:
        print(f"[fetch] 과다 수집 {before_trim}건 → 섹터 균형 상한 {len(items)}건")
    print(f"[fetch] {healthy}/{len(FEEDS)} 피드 정상, {len(items)}건 수집")
    return items


def mark_sent(items):
    """Telegram 전송 성공이 확인된 기사만 영구 이력에 기록한다."""
    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    now_ts = _now_utc().timestamp()
    count = 0
    for r in items:
        it = r.get("item", r) if isinstance(r, dict) else r
        if not isinstance(it, dict):
            continue
        title = it.get("title", "")
        if not title:
            continue
        key = it.get("_seen_key") or _title_key(title)
        nt = it.get("_nt") or _norm_for_sim(title)
        seen[key] = {
            "ts": now_ts,
            "nt": nt,
            "title": title,
            "link": it.get("link", ""),
            "reason": it.get("reason", ""),
            "sector": it.get("sector", it.get("sector_hint", "")),
        }
        count += 1
    _save_json(SEEN_FILE, seen)
    print(f"[fetch] seen 기록 {count}건")


def recent_sent(limit=None, days=None):
    """LLM 사건 중복 판단용 이력. 파일 보관기간과 별도로 최근 일부만 반환한다."""
    if limit is None:
        limit = HISTORY_LLM_LIMIT
    if days is None:
        days = HISTORY_LLM_DAYS
    if type(limit) is not int or limit < 0:
        raise ValueError("history limit은 0 이상 정수")
    if type(days) is not int or days < 0:
        raise ValueError("history days는 0 이상 정수")

    seen = _prune_seen(_load_json(SEEN_FILE, {}))
    cutoff = (_now_utc() - datetime.timedelta(days=days)).timestamp()
    rows = [
        row for row in seen.values()
        if isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and row["ts"] >= cutoff
    ]
    rows.sort(key=lambda row: row.get("ts", 0), reverse=True)
    return [{
        "title": row.get("title") or row.get("nt", ""),
        "reason": row.get("reason", ""),
        "sector": row.get("sector", ""),
        "sent_ts": row.get("ts", 0),
    } for row in rows[:limit] if row.get("title") or row.get("nt")]


if __name__ == "__main__":
    for it in fetch_news()[:20]:
        print(f"- [{it['sector_hint']}] [{it['source']}] {it['title'][:80]}")
