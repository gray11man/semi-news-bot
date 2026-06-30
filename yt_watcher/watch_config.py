# -*- coding: utf-8 -*-
"""감시 대상 설정 — 양질 채널만 (쇼츠·음모론·주식리딩방 원천 차단)"""

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
    {"name": "No Priors",              "channel_id": "UCSI7h9hydQ40K5MJHnCrQvw"},
    {"name": "Acquired",               "handle": "@AcquiredFM"},
    {"name": "Cheeky Pint (Stripe)",   "handle": "@stripe"},
    {"name": "Training Data (Sequoia)","channel_id": "UCWrF0oN6unbXrWsTN7RctTw"},
    {"name": "Decoder (The Verge)",    "handle": "@DecoderwithNilayPatel"},
    {"name": "Cognitive Revolution",   "handle": "@CognitiveRevolutionPodcast"},
    {"name": "Bloomberg Technology",   "channel_id": "UCrM7B7SL_g1edFOnmj-SDKg"},
    {"name": "SemiAnalysis",           "channel_id": "UCf_KhBXw5TIV0A7butjgFhg"},
    {"name": "NVIDIA",                 "channel_id": "UCHuiy8bXnmK5nisYHUd1J5g"},
    {"name": "Invest Like the Best",   "channel_id": "UCpQBb0fToph3jrDulwz1iUQ"},
    # ── 한국 임원진(삼성/SK) 관련 뉴스는 오픈검색 대신 검증된 언론사 채널로 ──
    # (오픈검색 시도해보니 "삼성전자 회장" 류 쿼리에 주식 리딩방/매집포착
    #  채널이 100% 매칭돼서 직함검색은 폐기. 대신 공영/증권전문 채널 구독.)
    {"name": "연합뉴스TV",              "channel_id": "UCTHCOPwqNfZ0uiKOvFyhGwg"},
    {"name": "SBS Biz",                "channel_id": "UCbMjg2EvXs_RUGW-KrdM3pw"},
]

# ── 축2: 인물/직함 기반 검색 (채널 화이트리스트 밖 깜짝 출연 잡기) ──
# 별도 스케줄(하루 1회, quota 100단위/건)로 돌릴 것.
#
# 주의: 한국 임원진(전영현/곽노정/이재용 등)은 직함으로 검색해도
# "삼성전자 회장", "SK하이닉스 대표이사" 같은 쿼리가 주식 리딩방 SEO 타겟과
# 정확히 겹쳐서 안전필터(구독자수)를 통과한 매집/리딩 채널이 다수 섞이는 게
# 실측으로 확인됨. 그래서 한국 인물은 이 축에서 제외하고 CHANNELS의
# 연합뉴스TV/SBS Biz가 다루도록 위임. 글로벌 인물은 동일 리스크가 상대적으로
# 낮지만(영어권은 클릭베이트 SEO 경쟁이 한국 주식판만큼 치열하지 않음) 0은 아니므로
# BLOCK_KEYWORDS를 계속 보강해야 함.
PEOPLE = [
    "Jensen Huang NVIDIA",
    "Lisa Su AMD",
    "Sam Altman OpenAI",
    "Dario Amodei Anthropic",
    "Demis Hassabis DeepMind",
    "Mark Zuckerberg Meta",
    "Andy Jassy Amazon",
    "Microsoft CEO",
    "Alphabet CEO",
    "Intel CEO",
    "Cerebras CEO",
    "OpenAI CFO",
    "Microsoft CFO capex",
    "Alphabet CFO capex",
    "Meta CFO capex",
    "NVIDIA CFO",
]

# 인물 검색 결과 안전 필터
MIN_SUBSCRIBERS   = 300_000   # 10만→30만으로 상향. 다만 이것만으론 한계가 있음(아래 참고).
MIN_DURATION_SEC  = 180       # 3분 미만 = 쇼츠/클립으로 간주, 제외

# 블랙워드: title + description 둘 다 검사함 (watch.py 쪽 로직).
# 한국 주식 리딩방/매집방 특유의 어휘를 대거 추가. 일반 클릭베이트 + 한국 주식판 SEO 단어.
BLOCK_KEYWORDS = [
    # 일반 클릭베이트
    "충격", "폭로", "shocking", "exposed",
    "they don't want you to know", "wake up", "deep state",
    "conspiracy", "secret agenda",
    # 한국 주식 리딩방/매집방 특유 어휘 (실측 노이즈 사례 기반 추가)
    "매집포착", "매집", "초VIP", "VIP가입", "긴급속보", "결국 이렇게",
    "세력들도", "난리난", "타점", "구독자를 위한 보답", "문자로 알려",
    "문자 남기고", "무료방송", "파트너스", "유사투자", "수익률 대회",
    "캐시충전", "1599-", "010-",
]
BLOCKED_CHANNEL_IDS = [
    # 과거에 노이즈/리딩방/오인 채널로 확인된 channel_id를 여기 추가
]
PERSON_SEARCH_LOOKBACK_HOURS = 26   # 하루 1회 스케줄이라 lookback도 그에 맞게

# ── 관련성 필터 (축1+축2 공통) ──
# 채널축은 지금까지 필터가 전혀 없어서, 연합뉴스TV/SBS Biz 같은 종합뉴스 채널을
# 추가한 뒤로 날씨/정치/스포츠 등 전부 다 왔음. 이제 제목에 아래 키워드 중
# 하나라도 있어야만 전송하도록 강제. (Lex Fridman 등 AI 전문 팟캐스트는
# 어차피 거의 다 걸리니 손해 없음, 종합뉴스 채널의 무관한 영상만 걸러짐)
RELEVANCE_KEYWORDS = [
    # 회사명
    "NVIDIA", "엔비디아", "AMD", "Intel", "인텔", "삼성전자", "삼성", "SK하이닉스",
    "하이닉스", "OpenAI", "오픈AI", "Anthropic", "앤트로픽", "Google", "구글",
    "Alphabet", "Microsoft", "마이크로소프트", "Meta", "메타", "Amazon", "아마존",
    "Cerebras", "세레브라스", "TSMC", "Tempus",
    # 인물 (PEOPLE 리스트와 중복되도 무방)
    "Jensen Huang", "젠슨 황", "Lisa Su", "Sam Altman", "샘 올트먼",
    "Dario Amodei", "Demis Hassabis", "Mark Zuckerberg", "저커버그",
    "Andy Jassy", "Satya Nadella", "나델라", "Sundar Pichai", "피차이",
    # 기술/산업 키워드
    "AI", "반도체", "HBM", "GPU", "메모리", "데이터센터", "data center",
    "semiconductor", "chip", "캐펙스", "capex", "파운드리", "DRAM", "낸드",
]

# 축3 키워드: 끔
TOPIC_KEYWORDS = {"bull": [], "bear": []}
LOOKBACK_HOURS = 26   # 하루 1회 통합 스케줄로 전환 (기존 4시간 → 26시간)
