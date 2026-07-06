# -*- coding: utf-8 -*-
"""
filter_stage1.py  (v2 - 전업종 신규성 확장판)

기존: 산업 키워드 2축 이상 겹쳐야 통과 → 반도체 우물만 잡힘.
변경: 두 경로를 OR로 본다.
  경로 A (산업): 기존 키워드 점수 + 서로 다른 축 2개 이상  → 반도체/공급망 신호 유지
  경로 B (신규성): NOVELTY 점수만 높아도 통과            → 전업종 '신박한 것' 포착
둘 중 하나만 만족하면 Stage2(LLM)로 넘긴다.

Stage2가 어차피 variant perception으로 최종 선별하므로,
Stage1은 '재료를 넓게 통과시키되 완전 노이즈만 거르는' 역할로 재정의.
"""

from collections import defaultdict

from signals import (
    SIGNAL_CATEGORIES, NOVELTY_SIGNALS,
    SCORE_THRESHOLD, NOVELTY_THRESHOLD, MAX_DAILY, CATEGORY_LABELS,
)

# 산업 경로에서 요구하는 최소 축 개수
MIN_DISTINCT_CATEGORIES = 2


def _score(text_low, categories):
    """주어진 사전(categories)으로 점수와 매칭 카테고리 반환."""
    total = 0
    hits = {}
    for cat, (keywords, weight) in categories.items():
        matched = [kw for kw in keywords if kw.lower() in text_low]
        if matched:
            total += weight + min(len(matched) - 1, 2)
            hits[cat] = matched
    return total, hits


def _primary_category(industry_hits, novelty_hits):
    """대표 주제 하나 결정 (다양성 분산용). 구체적 산업축 우선, 없으면 신규성축."""
    priority = ["geopolitics", "material", "policy", "competition",
                "capex", "supply", "pricing", "demand", "cycle", "core_top"]
    for cat in priority:
        if cat in industry_hits:
            return cat
    # 산업축이 없으면 신규성축을 대표로
    nov_priority = ["first_ever", "policy_shock", "regime_shift",
                    "abrupt", "new_entry"]
    for cat in nov_priority:
        if cat in novelty_hits:
            return cat
    if industry_hits:
        return next(iter(industry_hits))
    if novelty_hits:
        return next(iter(novelty_hits))
    return "기타"


def filter_news(news_items):
    passed = []
    for item in news_items:
        text_low = f"{item.get('title','')} {item.get('summary','')}".lower()

        ind_score, ind_hits = _score(text_low, SIGNAL_CATEGORIES)
        nov_score, nov_hits = _score(text_low, NOVELTY_SIGNALS)

        # 경로 A: 산업 키워드가 충분히 촘촘 (기존 반도체/공급망 신호 유지)
        path_industry = (
            ind_score >= SCORE_THRESHOLD
            and len(ind_hits) >= MIN_DISTINCT_CATEGORIES
        )
        # 경로 B: 신규성 신호가 강함 (전업종 '신박한 것')
        #   단, 신규성만 있고 산업 맥락이 아예 없으면 노이즈일 수 있어
        #   '신규성 축 자체가 2개 이상'이거나 '신규성+산업키워드 최소 1개' 요구
        path_novelty = (
            nov_score >= NOVELTY_THRESHOLD
            and (len(nov_hits) >= 2 or ind_score > 0)
        )

        if not (path_industry or path_novelty):
            continue

        all_hits = {}
        all_hits.update(ind_hits)
        all_hits.update(nov_hits)

        enriched = dict(item)
        enriched["score"] = ind_score + nov_score
        enriched["ind_score"] = ind_score
        enriched["nov_score"] = nov_score
        enriched["hits"] = all_hits
        enriched["categories"] = [CATEGORY_LABELS.get(c, c) for c in all_hits.keys()]
        enriched["_primary"] = _primary_category(ind_hits, nov_hits)
        # Stage2가 신규성 여부를 참고하도록 플래그
        enriched["is_novelty"] = path_novelty and not path_industry
        passed.append(enriched)

    passed.sort(key=lambda x: x["score"], reverse=True)

    # ── 다양성 분산: 같은 대표주제가 몰리지 않게 라운드로빈 ──
    buckets = defaultdict(list)
    for it in passed:
        buckets[it["_primary"]].append(it)

    diversified = []
    limit = MAX_DAILY * 2
    while len(diversified) < limit and any(buckets.values()):
        for cat in list(buckets.keys()):
            if buckets[cat]:
                diversified.append(buckets[cat].pop(0))
                if len(diversified) >= limit:
                    break
    return diversified
