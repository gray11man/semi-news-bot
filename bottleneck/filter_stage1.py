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


def filter_news(news_items):
    passed = []
    for item in news_items:
        text = f"{item.get('title','')} {item.get('summary','')}"
        score, hits = score_item(text)
        # 빡센 조건: 점수 + 서로 다른 축 개수
        if score >= SCORE_THRESHOLD and len(hits) >= MIN_DISTINCT_CATEGORIES:
            enriched = dict(item)
            enriched["score"] = score
            enriched["hits"] = hits
            enriched["categories"] = [CATEGORY_LABELS[c] for c in hits.keys()]
            passed.append(enriched)
    passed.sort(key=lambda x: x["score"], reverse=True)
    # Stage2로는 여유분(상한의 2배)만 넘겨 LLM이 최종 압축
    return passed[: MAX_DAILY * 2]
