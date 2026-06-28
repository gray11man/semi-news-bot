# -*- coding: utf-8 -*-
"""
fetch_news.py
구글 뉴스 RSS로 미국+한국 뉴스를 긁는 수집기.
signals.py의 핵심 키워드를 그대로 검색어로 사용 → 긁는 기준과 거르는 기준 일치.
키 발급 불필요, 무료. GitHub Actions에서 바로 작동.

반환 형식(main.py가 기대하는 모양):
  [{"title", "summary", "url", "source"}, ...]
"""

import urllib.parse
import xml.etree.ElementTree as ET
import requests
import time

from signals import SIGNAL_CATEGORIES

# 구글 뉴스 RSS 엔드포인트.
# hl=언어, gl=국가, ceid=국가:언어 로 한국판/미국판을 나눠 긁는다.
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

# 한국 뉴스: 한국어 키워드로 검색
KO_PARAMS = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}
# 미국 뉴스: 영어 키워드로 검색
US_PARAMS = {"hl": "en-US", "gl": "US", "ceid": "US:en"}

# 검색에 쓸 키워드 고르기:
# 핵심신호(core_top) + 쇼티지/원자재(material) + 지정학(geopolitics) 위주로,
# 너무 흔해서 노이즈만 잔뜩 끌어오는 단어는 제외하고 '조준 키워드'만 추린다.
def build_search_terms():
    ko_terms, us_terms = [], []
    for cat in ("core_top", "material", "geopolitics", "supply"):
        keywords = SIGNAL_CATEGORIES.get(cat, ([], 0))[0]
        for kw in keywords:
            # 영어/한글 구분: 알파벳이 들어간 건 미국 검색, 한글은 한국 검색
            if any(ord(c) < 128 and c.isalpha() for c in kw):
                us_terms.append(kw)
            else:
                ko_terms.append(kw)
    # 중복 제거, 너무 짧은 단어(1글자) 제외
    ko_terms = sorted(set(t for t in ko_terms if len(t) >= 2))
    us_terms = sorted(set(t for t in us_terms if len(t) >= 3))
    return ko_terms, us_terms


def fetch_rss(query, params, source_label):
    """구글 뉴스 RSS 1건 검색 → 기사 리스트."""
    q = urllib.parse.quote(query)
    url = GOOGLE_NEWS.format(q=q, **params)
    items = []
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()
            # description은 HTML 태그가 섞여있어 거칠게 정리
            desc = desc.replace("<", " <").replace(">", "> ")
            import re
            desc = re.sub(r"<[^>]+>", "", desc).strip()
            src = (it.findtext("source") or source_label).strip()
            if title:
                items.append({
                    "title": title,
                    "summary": desc[:300],
                    "url": link,
                    "source": src,
                })
    except Exception as e:
        print(f"[fetch] RSS 실패 ({query}): {e}")
    return items


from difflib import SequenceMatcher

# 같은 사건 묶기용: 핵심 주체 + 주제
_DEDUP_ACTORS = {
    "마이크론", "micron", "sk하이닉스", "하이닉스", "hynix", "삼성전자", "삼성",
    "samsung", "엔비디아", "nvidia", "tsmc", "퀄컴", "qualcomm", "amd", "인텔",
    "intel", "브로드컴", "broadcom", "한화엔진", "두산", "효성",
}
_DEDUP_TOPICS = {
    "실적": {"실적", "매출", "이익", "earnings", "revenue", "분기", "사상 최대",
             "record", "가이던스", "전망", "최고", "급등", "깜짝", "경신"},
    "수주": {"수주", "계약", "공급계약", "발주", "contract", "deal", "수주잔고"},
    "증설": {"증설", "투자", "공장", "capex", "설비", "착공", "ipo", "상장", "adr"},
    "hbm": {"hbm", "고대역폭", "메모리", "dram", "슈퍼사이클", "supercycle"},
    "전력": {"전력", "데이터센터", "전력망", "원전", "가스터빈", "power", "datacenter"},
    "쇼티지": {"쇼티지", "shortage", "부족", "품귀", "공급난", "병목", "tight"},
    "원자재": {"구리", "copper", "유가", "원유", "리튬", "우라늄", "희토류", "원자재"},
    "지정학": {"전쟁", "war", "호르무즈", "제재", "방산", "지정학", "분쟁"},
}


def _norm(t):
    import re as _re
    t = _re.sub(r"\[[^\]]*\]", " ", t or "")
    t = _re.sub(r"[^\w가-힣 ]", " ", t)
    return _re.sub(r"\s+", " ", t).strip().lower()


def _actors_topics(t):
    low = _norm(t)
    actors = {a for a in _DEDUP_ACTORS if a in low}
    topics = {tp for tp, kws in _DEDUP_TOPICS.items() if any(k in low for k in kws)}
    return actors, topics


def _same_event(a, b):
    na, nb = _norm(a), _norm(b)
    if SequenceMatcher(None, na, nb).ratio() >= 0.6:
        return True
    aa, at = _actors_topics(a)
    ba, bt = _actors_topics(b)
    # 같은 주체 + 같은 주제 → 같은 사건으로 간주
    if (aa & ba) and (at & bt):
        return True
    return False


def dedup(items):
    """유사도 + 주체/주제 기반 중복 제거 (강화판)."""
    kept = []
    for it in items:
        if any(_same_event(it["title"], k["title"]) for k in kept):
            continue
        kept.append(it)
    return kept


def fetch_news():
    """
    한국+미국 뉴스를 키워드별로 긁어 모아 중복 제거 후 반환.
    검색어가 많으면 호출이 늘어나므로, 키워드를 OR로 묶어 호출 수를 줄인다.
    """
    ko_terms, us_terms = build_search_terms()
    all_items = []

    # 한국: 키워드를 5개씩 OR로 묶어 검색 (호출 수 절감)
    for i in range(0, len(ko_terms), 5):
        chunk = ko_terms[i:i + 5]
        query = " OR ".join(chunk) + " when:1d"   # 최근 1일 기사
        all_items += fetch_rss(query, KO_PARAMS, "구글뉴스(KR)")
        time.sleep(1)

    # 미국: 동일
    for i in range(0, len(us_terms), 5):
        chunk = us_terms[i:i + 5]
        query = " OR ".join(chunk) + " when:1d"
        all_items += fetch_rss(query, US_PARAMS, "GoogleNews(US)")
        time.sleep(1)

    deduped = dedup(all_items)
    print(f"[fetch] 수집 {len(all_items)}건 → 중복제거 {len(deduped)}건")
    return deduped


if __name__ == "__main__":
    # 단독 테스트용
    news = fetch_news()
    for n in news[:10]:
        print(f"- [{n['source']}] {n['title']}")
