# -*- coding: utf-8 -*-
"""감시 대상 설정 — 양질 채널만 (쇼츠·음모론 원천 차단)"""

# 축1 채널: 양질 인터뷰 채널만. handle 또는 channel_id 둘 다 지원.
CHANNELS = [
    {"name": "20VC (Harry Stebbings)", "channel_id": "UCf0PBRjhf0rF8fWBIxTuoWA"},
    {"name": "All-In Podcast",         "handle": "@allin"},
    {"name": "BG2 Pod",                "handle": "@bg2pod"},
    {"name": "Dwarkesh Patel",         "handle": "@DwarkeshPatel"},
    {"name": "Lex Fridman",            "handle": "@lexfridman"},
    {"name": "a16z",                   "handle": "@a16z"},
    {"name": "Latent Space",           "handle": "@LatentSpacePod"},
    {"name": "No Priors",              "handle": "@NoPriorsPod"},
    {"name": "Acquired",               "handle": "@AcquiredFM"},
    {"name": "Cheeky Pint (Stripe)",   "handle": "@stripe"},
]

# 축2 인물: 끔 (쇼츠·음모론 원인이었음)
PEOPLE = []

# 축3 키워드: 끔
TOPIC_KEYWORDS = {"bull": [], "bear": []}

LOOKBACK_HOURS = 4
