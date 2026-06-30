# -*- coding: utf-8 -*-
"""
감시 대상 설정 — 3축 구조
  축1) CHANNELS      : 고품질 채널 화이트리스트 (RSS 전수 감시, 쿼터 0)
  축2) PEOPLE        : 인물 이름 검색 (어디 나오든)
  축3) TOPIC_KEYWORDS: 주제 키워드 (이름·채널 몰라도 잡음)

여기 리스트만 고치면 감시 대상이 바뀝니다. 코드는 안 건드려도 됩니다.
"""

# ─────────────────────────────────────────────
# 축1: 채널 화이트리스트 (RSS — API키 불필요, 쿼터 0, 놓침 거의 0%)
#   channel_id 는 채널 페이지 소스에서 "UC..." 형태로 찾을 수 있음.
#   handle(@xxx)만 알면 resolve_channels.py 로 channel_id 자동 변환 가능.
# ─────────────────────────────────────────────
CHANNELS = [
    # {"name": "20VC", "channel_id": "UC..."},
    # 아래는 handle 만 적어두면 resolve 스크립트가 channel_id 채워줌
    {"name": "20VC (Twenty Minute VC)", "handle": "@20vc"},
    {"name": "All-In Podcast",          "handle": "@allin"},
    {"name": "BG2 Pod",                 "handle": "@bg2pod"},
    {"name": "Dwarkesh Patel",          "handle": "@DwarkeshPatel"},
    {"name": "Lex Fridman",             "handle": "@lexfridman"},
    {"name": "Acquired",                "handle": "@AcquiredFM"},
    {"name": "a16z",                    "handle": "@a16z"},
    {"name": "Sequoia Capital",         "handle": "@sequoiacapital"},
    {"name": "Cheeky Pint (Stripe)",    "handle": "@stripe"},
    {"name": "Stratechery / Sharp Tech","handle": "@SharpTech"},
]

# ─────────────────────────────────────────────
# 축2: 인물 검색 (search.list q=이름, order=date)
#   tier 1 = 핵심(시장 움직이는 발언), tier 2 = 보조
#   bear  = True 면 약세/경고 인사 (반대방향 균형용)
# ─────────────────────────────────────────────
PEOPLE = [
    # ── 강세/핵심 (Tier 1) ──
    {"name": "Jensen Huang",     "tier": 1},
    {"name": "Sundar Pichai",    "tier": 1},
    {"name": "Satya Nadella",    "tier": 1},
    {"name": "Sam Altman",       "tier": 1},
    {"name": "Lisa Su",          "tier": 1},
    {"name": "C.C. Wei",         "tier": 1},   # TSMC
    {"name": "Dario Amodei",     "tier": 1},
    {"name": "Demis Hassabis",   "tier": 1},

    # ── 강세/보조 (Tier 2) ──
    {"name": "Mark Zuckerberg",  "tier": 2},
    {"name": "Andy Jassy",       "tier": 2},
    {"name": "Elon Musk",        "tier": 2},
    {"name": "Mustafa Suleyman", "tier": 2},
    {"name": "Alexandr Wang",    "tier": 2},
    {"name": "Greg Brockman",    "tier": 2},
    {"name": "Andrew Feldman",   "tier": 2},   # Cerebras
    {"name": "Sarah Friar",      "tier": 2},   # OpenAI CFO
    {"name": "K.R. Sridhar",     "tier": 2},   # Bloom Energy
    {"name": "Cristiano Amon",   "tier": 2},   # Qualcomm
    {"name": "Jeff Dean",        "tier": 2},
    {"name": "Andrej Karpathy",  "tier": 2},
    {"name": "Ilya Sutskever",   "tier": 2},
    {"name": "Fei-Fei Li",       "tier": 2},
    {"name": "Arthur Mensch",    "tier": 2},   # Mistral

    # ── 약세/경고 인사 (bear) ──
    {"name": "Michael Burry",    "tier": 1, "bear": True},
    {"name": "Jim Chanos",       "tier": 2, "bear": True},
    {"name": "David Cahn",       "tier": 2, "bear": True},   # Sequoia $600B
    {"name": "Torsten Slok",     "tier": 2, "bear": True},   # Apollo
    {"name": "Daron Acemoglu",   "tier": 2, "bear": True},
    {"name": "Gary Marcus",      "tier": 2, "bear": True},
    {"name": "Ed Zitron",        "tier": 2, "bear": True},
]

# ─────────────────────────────────────────────
# 축3: 주제 키워드 (search.list q=키워드)
#   강세(bull) + 약세(bear) 양방향. 이름·채널 안 박혀도 잡음.
# ─────────────────────────────────────────────
TOPIC_KEYWORDS = {
    "bull": [
        # 수요>공급 / 부족
        "compute shortage", "GPU shortage", "HBM shortage",
        "supply constrained AI", "capacity constrained data center",
        "AI compute demand", "data center buildout",
        "AI capex", "AI infrastructure spending",
        "컴퓨팅 부족", "반도체 공급 부족", "메모리 부족",
        "AI 데이터센터 전력", "AI 수요 전망",
        # 병목
        "wafer capacity bottleneck", "data center power constraint",
        "HBM memory bottleneck",
    ],
    "bear": [
        # 거품/과잉/회수의문
        "AI bubble", "infrastructure bubble", "AI capex bubble",
        "AI overbuild", "overcapacity AI", "circular financing AI",
        "AI spending unsustainable", "return on AI investment",
        "AI 거품", "과잉투자 AI", "AI 투자 회수",
        # 공급 정상화 / 수요둔화 (exit 트리거)
        "memory oversupply", "DRAM price drop", "HBM oversupply",
        "AI demand slowdown", "데이터센터 공급 과잉",
        "메모리 공급 과잉", "D램 가격 하락",
    ],
}

# 검색 폴링에서 "최근 N시간 내 업로드"만 신규로 간주
LOOKBACK_HOURS = 4
