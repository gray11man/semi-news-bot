# -*- coding: utf-8 -*-
"""
filter_stage1.py
Stage 1: 룰 기반 1차 필터 (빡세게).
- 시그널 사전으로 점수 매김
- 임계값(SCORE_THRESHOLD) 통과 + '서로 다른 축 2개 이상' 동시 충족해야 S급
- 점수순 정렬 후 상위 MAX_DAILY*2 만 Stage2로 (LLM이 최종 5개로 좁힘)
"""

from signals import (
    SIGNAL_CATEGORIES, SCORE_THRESHOLD, MAX_DAILY, CATEGORY_LABELS,
)

# core_top은 '축'으로 세지 않고 가산점으로만 취급할지 여부.
# 여기서는 core_top 포함 서로 다른 카테고리 2개 이상을 S급 조건으로 둔다.
# 서로 다른 축 2개 이상 요구 (다시 조임).
# 신호가 너무 많으면 이대로, 너무 적으면 1로 내린다.
MIN_DISTINCT_CATEGORIES = 2


def score_item(text: str):
    text_low = text.lower()
    total = 0
    hits = {}
    for cat, (keywords, weight) in SIGNAL_CATEGORIES.items():
        matched = [kw for kw in keywords if kw.lower() in text_low]
        if matched:
            total += weight + min(len(matched) - 1, 2)
            hits[cat] = matched
    return total, hits


def _primary_category(hits):
    """그 뉴스의 '대표 주제'를 하나 정한다 (다양성 분산용)."""
    # core_top은 너무 흔하니 대표에서 제외하고, 구체적 축을 우선
    priority = ["geopolitics", "material", "policy", "competition",
                "capex", "supply", "pricing", "demand", "cycle", "core_top"]
    for cat in priority:
        if cat in hits:
            return cat
    return next(iter(hits)) if hits else "기타"


def filter_news(news_items):
    passed = []
    for item in news_items:
        text = f"{item.get('title','')} {item.get('summary','')}"
        score, hits = score_item(text)
        if score >= SCORE_THRESHOLD and len(hits) >= MIN_DISTINCT_CATEGORIES:
            enriched = dict(item)
            enriched["score"] = score
            enriched["hits"] = hits
            enriched["categories"] = [CATEGORY_LABELS[c] for c in hits.keys()]
            enriched["_primary"] = _primary_category(hits)
            passed.append(enriched)

    passed.sort(key=lambda x: x["score"], reverse=True)

    # ── 다양성 분산: 같은 대표주제가 몰리지 않게 라운드로빈으로 뽑기 ──
    # 주제별로 묶고, 각 주제에서 점수 높은 것부터 한 개씩 번갈아 뽑는다.
    from collections import defaultdict
    buckets = defaultdict(list)
    for it in passed:
        buckets[it["_primary"]].append(it)

    diversified = []
    # 주제별 리스트를 점수순 유지한 채 라운드로빈
    while len(diversified) < MAX_DAILY * 2 and any(buckets.values()):
        for cat in list(buckets.keys()):
            if buckets[cat]:
                diversified.append(buckets[cat].pop(0))
                if len(diversified) >= MAX_DAILY * 2:
                    break

    return diversified
