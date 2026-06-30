# -*- coding: utf-8 -*-
"""감시 대상 설정 — 3축 (핵심만 추려 슬림화)"""

# 축1: 채널 화이트리스트 (RSS, 쿼터 0)
CHANNELS = [
    {"name": "20VC (Twenty Minute VC)", "handle": "@20vc"},
    {"name": "All-In Podcast",          "handle": "@allin"},
    {"name": "BG2 Pod",                 "handle": "@bg2pod"},
    {"name": "Dwarkesh Patel",          "handle": "@DwarkeshPatel"},
    {"name": "Lex Fridman",             "handle": "@lexfridman"},
    {"name": "a16z",                    "handle": "@a16z"},
]

# 축2: 인물 검색 — 핵심 인사만
PEOPLE = [
    {"name": "Jensen Huang",   "tier": 1},
    {"name": "Sundar Pichai",  "tier": 1},
    {"name": "Sam Altman",     "tier": 1},
    {"name": "Lisa Su",        "tier": 1},
    {"name": "Dario Amodei",   "tier": 1},
    {"name": "Demis Hassabis", "tier": 1},
    {"name": "Michael Burry",  "tier": 1, "bear": True},
    {"name": "David Cahn",     "tier": 2, "bear": True},
]

# 축3: 주제 키워드 — 핵심만
TOPIC_KEYWORDS = {
    "bull": [
        "AI compute shortage",
        "AI data center buildout",
        "AI capex",
        "HBM memory demand",
    ],
    "bear": [
        "AI bubble",
        "AI capex bubble",
        "memory oversupply",
    ],
}

LOOKBACK_HOURS = 4
