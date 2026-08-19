# -*- coding: utf-8 -*-
"""
diagnose_fetch.py
fetch_news()가 실제로 어떤 피드에서 몇 건씩, 어떤 제목을 물어오는지 확인하는 진단 스크립트.
"인위적 조정"을 하기 전에, 편향이 fetch 단계(그물)에서 나는지
pick 단계(LLM 선택)에서 나는지부터 눈으로 확인하기 위한 용도.

사용법 (bottleneck 폴더 안에서):
    python diagnose_fetch.py
"""

from fetch_news import fetch_news, FEEDS

items = fetch_news()

print(f"\n=== 전체 수집: {len(items)}건 ===\n")

# 1. 소스(언론사)별 분포
from collections import Counter
src_counter = Counter(it["source"] for it in items)
print("--- 소스별 건수 (상위 15) ---")
for src, cnt in src_counter.most_common(15):
    print(f"  {cnt:3d}건  {src}")

# 2. 제목에 특정 키워드가 얼마나 섞여있는지 대략적 분류
IT_KEYWORDS = ["반도체", "HBM", "DRAM", "AI", "칩", "엔비디아", "TSMC", "삼성전자", "SK하이닉스",
               "chip", "semiconductor", "nvidia", "data center", "GPU"]
OTHER_KEYWORDS = {
    "조선/방산": ["조선", "방산", "함정", "전투기", "미사일", "shipbuilding", "defense"],
    "에너지": ["원전", "전력망", "우라늄", "LNG", "유가", "power grid", "uranium", "oil"],
    "금융/금리": ["금리", "환율", "FOMC", "국채", "rate cut", "tariff"],
    "바이오": ["FDA", "임상", "바이오", "제약", "biotech", "pharma"],
    "화학/철강": ["철강", "화학", "steel", "chemical"],
}

def classify(title):
    t = title.lower()
    if any(k.lower() in t for k in IT_KEYWORDS):
        return "IT/반도체"
    for label, kws in OTHER_KEYWORDS.items():
        if any(k.lower() in t for k in kws):
            return label
    return "기타/미분류"

cls_counter = Counter(classify(it["title"]) for it in items)
print("\n--- 대략적 주제 분류 (키워드 매칭, 참고용) ---")
for label, cnt in cls_counter.most_common():
    pct = cnt / len(items) * 100 if items else 0
    print(f"  {cnt:3d}건 ({pct:4.1f}%)  {label}")

# 3. 피드별로 몇 건이 최종 수집에 살아남았는지 (중복 제거 전 원본 대비)
print("\n--- 참고: 전체 제목 목록 (섹션 3 상위 30건) ---")
for it in items[:30]:
    print(f"  [{it['source']}] {it['title']}")
