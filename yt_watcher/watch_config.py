# -*- coding: utf-8 -*-
"""감시 대상 설정 — 양질 채널만 (쇼츠·음모론 원천 차단)"""

# ── 축1: 채널 화이트리스트 ──
# NVIDIA·Invest Like the Best는 동명 사칭/딥페이크 채널이 실제로 존재해서
# handle 자동조회 대신 channel_id를 직접 박아 리스크 차단.
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
    {"name": "Training Data (Sequoia)","handle": "@sequoiacapital"},
    {"name": "Decoder (The Verge)",    "handle": "@DecoderwithNilayPatel"},
    {"name": "Cognitive Revolution",   "handle": "@CognitiveRevolutionPodcast"},
    {"name": "Bloomberg Technology",   "channel_id": "UCrM7B7SL_g1edFOnmj-SDKg"},
    {"name": "SemiAnalysis",           "handle": "@semianalysis"},
    {"name": "NVIDIA",                 "handle": "@NVIDIA"},
    {"name": "Invest Like the Best",   "channel_id": "UCpQBb0fToph3jrDulwz1iUQ"},
]

# ── 축2: 인물/직함 기반 검색 (채널 화이트리스트 밖 깜짝 출연 잡기) ──
# 별도 스케줄(하루 1회 권장, quota 100단위/건)로 돌릴 것.
#
# 설계 원칙:
#   - "이름"으로 검색하면 그 사람이 교체되는 순간 영구히 죽은 키워드가 됨.
#   - 그래서 후임자가 생겨도 계속 작동하도록, 기본은 "직함" 기반 쿼리로 둔다.
#     (한국 언론/외신은 교체 이후에도 항상 직함을 붙여서 보도하기 때문에
#      직함 검색은 사람이 바뀌어도 자동으로 새 인물을 잡아낸다.)
#   - 단, 이름 자체가 고유명사로 워낙 강력하게 검색되고(검색 정확도↑) /
#     교체 가능성이 낮은 인물(창업자 겸 CEO 등)은 "이름+회사" 유지.
#   - 직함이 통째로 바뀌는 조직개편이 일어나면 그건 그 자체로 중요 뉴스이므로
#     이 리스트도 그때 같이 손보면 됨 (자주 있는 일 아님).
PEOPLE = [
    # 이름 기반 (창업자/장기 재임 CEO — 교체 리스크 낮음, 고유명사 검색력 ↑)
    "Jensen Huang NVIDIA",
    "Lisa Su AMD",
    "Sam Altman OpenAI",
    "Dario Amodei Anthropic",
    "Demis Hassabis DeepMind",
    "Mark Zuckerberg Meta",
    "Andy Jassy Amazon",

    # 직함 기반 (피선임직 — 교체돼도 계속 자동 추적)
    "Microsoft CEO",
    "Alphabet CEO",
    "Intel CEO",
    "Cerebras CEO",
    "OpenAI CFO",
    "Microsoft CFO capex",
    "Alphabet CFO capex",
    "Meta CFO capex",
    "NVIDIA CFO",

    # 한국 — 전부 직함 기반 (인사이동 잦음, 이름 박아두면 금방 죽음)
    "삼성전자 DS부문장",
    "SK하이닉스 대표이사",
    "삼성전자 회장",
    "SK그룹 회장",
]

# 인물 검색 결과 안전 필터
MIN_SUBSCRIBERS   = 100_000   # 이 미만 채널은 결과에서 제외 (듣보잡/리액션 채널 차단)
MIN_DURATION_SEC  = 180       # 3분 미만 = 쇼츠/클립으로 간주, 제외
BLOCK_KEYWORDS = [
    # 클릭베이트/음모론성 제목 차단용. 필요시 계속 추가.
    "충격", "폭로", "shocking", "exposed",
    "they don't want you to know", "wake up", "deep state",
    "conspiracy", "secret agenda",
]
BLOCKED_CHANNEL_IDS = [
    # 과거에 노이즈/오인 채널로 확인된 channel_id를 여기 추가
]
PERSON_SEARCH_LOOKBACK_HOURS = 26   # 하루 1회 스케줄이라 lookback도 그에 맞게

# 축3 키워드: 끔
TOPIC_KEYWORDS = {"bull": [], "bear": []}
LOOKBACK_HOURS = 4
