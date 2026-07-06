# -*- coding: utf-8 -*-
"""
fetch_news.py  (v2 - 전업종 무키워드 수집판)

기존: signals.py 키워드로 검색 → 그 키워드 산업만 잡힘 (범위 좁음).
변경: 미국/한국/일본/대만 4개 권역의 '경제·비즈니스 섹션 헤드라인'을
      키워드 없이 넓게 긁는다. 무엇이 '신호'인지는 Stage1/Stage2가 판정.

  1단(여기): 넓게 수집 + dedup  → 수백 건
  2단(filter_stage1): 신규성/산업 OR 필터 → 수십 건
  3단(evaluate_stage2): LLM variant perception 판정 → 최종 소수

반환 형식(main.py가 기대하는 모양):
  [{"title", "summary", "url", "source"}, ...]
"""

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

import requests

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

# 구글 뉴스는 UA에 민감. 기존 봇과 동일한 브라우저 UA로 403 회피.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

REGIONS = {
    "US": {"hl": "en-US", "gl": "US", "ceid": "US:en", "label": "GoogleNews(US)"},
    "KR": {"hl": "ko",    "gl": "KR", "ceid": "KR:ko", "label": "구글뉴스(KR)"},
    "JP": {"hl": "ja",    "gl": "JP", "ceid": "JP:ja", "label": "GoogleNews(JP)"},
    "TW": {"hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant", "label": "GoogleNews(TW)"},
}

BROAD_QUERIES = {
    "US": ["business when:1d", "economy market when:1d", "industry supply when:1d"],
    "KR": ["경제 when:1d", "산업 기업 when:1d", "증시 시장 when:1d"],
    "JP": ["経済 when:1d", "産業 企業 when:1d"],
    "TW": ["經濟 when:1d", "產業 企業 when:1d"],
}

RSS_MAX_ENTRIES = 40
REQUEST_TIMEOUT = 20
FETCH_DELAY = 1.0


def fetch_rss(url, source_label):
    items = []
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": UA})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for it in list(root.iter("item"))[:RSS_MAX_ENTRIES]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            desc = (it.findtext("description") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", desc).strip()
            src = (it.findtext("source") or source_label).strip()
            if title:
                items.append({
                    "title": title,
                    "summary": desc[:300],
                    "url": link,
                    "source": src,
                })
    except Exception as e:
        print(f"[fetch] RSS 실패 ({source_label}): {e}")
    return items


def _norm(t):
    t = t or ""
    t = re.sub(r"\s*-\s*[^-]+$", "", t)
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    t = re.sub(r"[^\w가-힣 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _canon_url(u):
    try:
        p = urllib.parse.urlparse(u)
        return (p.netloc.replace("www.", "") + p.path).rstrip("/").lower()
    except Exception:
        return u or ""


_SIM_THRESHOLD = 0.80


def _same_event(a, b):
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= _SIM_THRESHOLD


def dedup(items):
    kept = []
    seen_url = set()
    for it in items:
        cu = _canon_url(it.get("url", ""))
        if cu and cu in seen_url:
            continue
        if any(_same_event(it["title"], k["title"]) for k in kept):
            continue
        if cu:
            seen_url.add(cu)
        kept.append(it)
    return kept


def fetch_news():
    """4개 권역 경제/산업 헤드라인을 무키워드로 넓게 수집 후 dedup."""
    all_items = []

    for region, params in REGIONS.items():
        for q in BROAD_QUERIES.get(region, []):
            qq = urllib.parse.quote(q)
            search_url = GOOGLE_NEWS_SEARCH.format(
                q=qq, hl=params["hl"], gl=params["gl"], ceid=params["ceid"])
            all_items += fetch_rss(search_url, params["label"])
            time.sleep(FETCH_DELAY)

    deduped = dedup(all_items)
    print(f"[fetch] 수집 {len(all_items)}건 → 중복제거 {len(deduped)}건")
    return deduped


if __name__ == "__main__":
    news = fetch_news()
    for n in news[:15]:
        print(f"- [{n['source']}] {n['title']}")
