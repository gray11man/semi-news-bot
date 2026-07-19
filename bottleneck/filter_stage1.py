# -*- coding: utf-8 -*-
"""
filter_stage1.py  (v3 - 거시·전업종 구조적 신호판)

v2 → v3 변경:
  - signals.py v3(18개 섹터 축 + 6개 구조 문법 축) 대응
  - 짧은 영문 키워드 단어경계(\b) 매칭 (ban→urban 오탐 방지)
  - 시황/리테일 노이즈 감점 적용 (NOISE_PENALTY)
  - 다양성 분산 개선: 최고점 2건은 점수순 무조건 포함 후 나머지만 라운드로빈
    (큰 사건이 터진 날 핵심 기사가 분산 로직에 밀리는 문제 해결)
  - 통과 조건은 v2와 동일한 2경로 OR 구조 유지

경로 A (섹터): 섹터 점수 >= SCORE_THRESHOLD(6) + 서로 다른 축 2개 이상
경로 B (구조): 구조 문법 점수 >= NOVELTY_THRESHOLD(4)
              + (구조 축 2개 이상 or 섹터 신호 최소 1개 동반)
"""

import re
from collections import defaultdict

from signals import (
    SIGNAL_CATEGORIES, NOVELTY_SIGNALS, NOISE_PENALTY, WORD_BOUNDARY_KWS,
    SCORE_THRESHOLD, NOVELTY_THRESHOLD, MAX_DAILY, CATEGORY_LABELS,
)

# 섹터 경로에서 요구하는 최소 축 개수
MIN_DISTINCT_CATEGORIES = 2
# 점수순 무조건 포함할 상위 건수 (다양성 분산 예외)
TOP_GUARANTEED = 2


def _kw_match(kw, text_low):
    """키워드 매칭. 짧은 영문 키워드는 단어경계로만 매칭해 오탐 방지."""
    k = kw.lower()
    if k in WORD_BOUNDARY_KWS:
        return re.search(r"\b" + re.escape(k) + r"\b", text_low) is not None
    return k in text_low


def _score(text_low, categories):
    """주어진 사전(categories)으로 점수와 매칭 카테고리 반환.
    카테고리당 가중치 1회 + 동일 축 추가 매칭 보너스(최대 +2)."""
    total = 0
    hits = {}
    for cat, (keywords, weight) in categories.items():
        matched = [kw for kw in keywords if _kw_match(kw, text_low)]
        if matched:
            total += weight + min(len(matched) - 1, 2)
            hits[cat] = matched
    return total, hits


def _noise_penalty(text_low):
    """시황/리테일/가십 기사 감점."""
    keywords, penalty = NOISE_PENALTY
    if any(_kw_match(kw, text_low) for kw in keywords):
        return penalty
    return 0


def _primary_category(industry_hits, novelty_hits):
    """대표 주제 하나 결정 (다양성 분산용). 구체적 섹터축 우선."""
    priority = [
        "shipbuilding", "power_grid", "semis_ai", "defense_geo",
        "bio_health", "k_wave", "battery_ev", "robotics_auto",
        "macro_rates", "macro_fx_flow", "energy_shift",
        "commodity_metal", "commodity_soft", "shipping_freight",
        "ai_infra", "infra_construction", "policy_regulation",
        "consumer_shift", "macro_inflation",
    ]
    for cat in priority:
        if cat in industry_hits:
            return cat
    nov_priority = ["supply_demand_flip", "first_ever", "regime_shift",
                    "capacity_race", "abrupt", "new_entry"]
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

        # [v3] 시황/리테일 노이즈 감점 — 양쪽 축 모두에 적용
        penalty = _noise_penalty(text_low)
        ind_score += penalty
        nov_score += penalty

        # 경로 A: 섹터 신호가 충분히 촘촘
        path_industry = (
            ind_score >= SCORE_THRESHOLD
            and len(ind_hits) >= MIN_DISTINCT_CATEGORIES
        )
        # 경로 B: 구조 문법이 강함 + 최소한의 맥락
        path_novelty = (
            nov_score >= NOVELTY_THRESHOLD
            and (len(nov_hits) >= 2 or ind_score > 0)
        )
        # [v3] 경로 C: 단일 섹터 축이라도 매칭 밀도가 높으면 그 주제가
        #   기사의 핵심이라는 뜻 → 통과.
        #   일반 축(가중치 2~3): 키워드 3개 이상 (예: 금리인하+점도표+유동성공급)
        #   핵심 축(가중치 4): 키워드 2개 이상 (예: FDA승인+기술수출, 신조선가+슬롯부족)
        path_dense = False
        if ind_score > 0:
            for cat, matched in ind_hits.items():
                weight = SIGNAL_CATEGORIES[cat][1]
                need = 2 if weight >= 4 else 3
                if len(matched) >= need:
                    path_dense = True
                    break

        if not (path_industry or path_novelty or path_dense):
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

    # ── [v3] 상위 TOP_GUARANTEED건은 점수순 무조건 포함 ──
    # (큰 사건 터진 날 고점수 기사가 라운드로빈에 밀리는 문제 방지)
    guaranteed = passed[:TOP_GUARANTEED]
    rest = passed[TOP_GUARANTEED:]

    # ── 다양성 분산: 같은 대표주제가 몰리지 않게 라운드로빈 ──
    buckets = defaultdict(list)
    for it in rest:
        buckets[it["_primary"]].append(it)

    diversified = list(guaranteed)
    limit = MAX_DAILY * 3   # [v3] Stage2에 후보를 넉넉히 (2배 → 3배)
    while len(diversified) < limit and any(buckets.values()):
        progressed = False
        for cat in list(buckets.keys()):
            if buckets[cat]:
                diversified.append(buckets[cat].pop(0))
                progressed = True
                if len(diversified) >= limit:
                    break
        if not progressed:
            break
    return diversified
