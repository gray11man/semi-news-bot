# -*- coding: utf-8 -*-
"""감시 대상 설정 — 인물 검색만 (노이즈 최소화)"""

# 축1 채널: 일단 끔 (나중에 필요하면 추가)
CHANNELS = []

# 축2 인물: 핵심 인사만. 이름이 제목/채널에 실제로 박힌 것만 통과시킴.
PEOPLE = [
    {"name": "Jensen Huang",   "tier": 1},
    {"name": "Sundar Pichai",  "tier": 1},
    {"name": "Sam Altman",     "tier": 1},
    {"name": "Lisa Su",        "tier": 1},
    {"name": "Dario Amodei",   "tier": 1},
    {"name": "Demis Hassabis", "tier": 1},
    {"name": "Satya Nadella",  "tier": 1},
    {"name": "Mark Zuckerberg","tier": 2},
    {"name": "Andrew Feldman", "tier": 2},   # Cerebras
    {"name": "Sarah Friar",    "tier": 2},   # OpenAI CFO
    # 약세/경고 — 균형용
    {"name": "Michael Burry",  "tier": 1, "bear": True},
    {"name": "David Cahn",     "tier": 2, "bear": True},
]

# 축3 키워드: 끔 (잡다한 영상 원인이었음)
TOPIC_KEYWORDS = {"bull": [], "bear": []}

LOOKBACK_HOURS = 4
