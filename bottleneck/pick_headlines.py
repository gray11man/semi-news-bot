import os
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI


# ============================================================
# Industry Study - Deep Dynamic Curriculum
# ============================================================

CURRICULUM_VERSION = 4

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

LESSON_MODEL = os.getenv("LESSON_MODEL", "gpt-5.6-terra")
FAST_MODEL = os.getenv("FAST_MODEL", "gpt-5.6-luna")
NORMAL_LESSON_MODEL = os.getenv("NORMAL_LESSON_MODEL", "gpt-5.6-luna")

STATE_FILE = "study_state.json"
KST = ZoneInfo("Asia/Seoul")


# ============================================================
# 메뉴
# ============================================================

MENU = """
━━━━━━━━━━━━━━
📌 Industry Study

다음공부
심화학습
연기
기업 더 자세히
질문 대답
━━━━━━━━━━━━━━
""".strip()


# ============================================================
# 산업 목록
# ============================================================

INDUSTRIES = [
    "반도체",
    "AI 반도체·가속기",
    "메모리·HBM",
    "반도체 장비",
    "반도체 소재",
    "첨단 패키징·기판",

    "AI 서버",
    "데이터센터",
    "네트워크·스위치",
    "광통신·CPO",
    "클라우드·네오클라우드",
    "스토리지·SSD",

    "전력기기·변압기",
    "송배전망·그리드",
    "가스터빈·발전설비",
    "원자력 발전",
    "SMR",
    "우라늄·핵연료",

    "천연가스",
    "LNG",
    "원유·정유",
    "석유화학",

    "조선",
    "LNG선",
    "탱커·유조선",
    "컨테이너 해운",
    "벌크 해운",
    "항만·물류",

    "방산",
    "우주·위성",

    "로봇",
    "산업자동화",
    "공작기계·산업기계",

    "자동차",
    "전기차",
    "자율주행·ADAS",
    "자동차 부품",

    "배터리",
    "배터리 소재",
    "ESS",

    "태양광",
    "풍력",
    "수소",

    "구리",
    "알루미늄",
    "철강",
    "희토류",
    "리튬",
    "금",
    "광산·자원개발",

    "건설·건자재",
    "시멘트",

    "농업",
    "비료",
    "곡물·농산물",
    "식품",
    "프랜차이즈",

    "유통·리테일",
    "전자상거래",

    "광고",
    "미디어·스트리밍",
    "게임",
    "통신",

    "은행",
    "보험",
    "증권·자산운용",
    "결제·핀테크",

    "바이오테크",
    "제약",
    "의료기기",
    "진단·정밀의료",

    "항공",
    "호텔·여행",

    "폐기물·환경서비스",
    "수처리",

    "데이터·정보서비스",
    "사이버보안",
    "엔터프라이즈 소프트웨어",
]


# ============================================================
# 시간
# ============================================================

def now_kst():
    return datetime.now(KST)


def today():
    return now_kst().strftime("%Y-%m-%d")


def tomorrow():
    return (now_kst() + timedelta(days=1)).strftime("%Y-%m-%d")


# ============================================================
# 상태
# ============================================================

def default_state():
    return {
        "curriculum_version": CURRICULUM_VERSION,
        "industry_index": 0,
        "topic_index": 0,
        "curriculum": None,
        "lesson_count": 0,
        "telegram_offset": 0,
        "pause_until": None,
        "last_auto_lesson_date": None,
        "last_lesson": None,
        "history": [],
        "completed_industries": [],
        "api_usage": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "estimated_text_cost_usd": 0.0
        }
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return default_state()

    # 새 버전으로 교체 시 커리큘럼은 처음부터
    # Telegram offset만 살림
    if old.get("curriculum_version") != CURRICULUM_VERSION:
        new = default_state()
        new["telegram_offset"] = old.get("telegram_offset", 0)
        return new

    base = default_state()

    for key, value in base.items():
        old.setdefault(key, value)

    return old


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 환경변수
# ============================================================

def validate_env():
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if missing:
        raise RuntimeError(
            "필수 환경변수가 없습니다: "
            + ", ".join(missing)
        )


# ============================================================
# OpenAI
# ============================================================

def get_client():
    return OpenAI(
        api_key=OPENAI_API_KEY
    )


# ============================================================
# API 사용량 / 비용 추정
# ============================================================

# 2026-09 기준 텍스트 토큰 단가(USD / 1M tokens)
# 웹 검색 tool-call 자체의 별도 요금은 아래 추정치에 포함하지 않음.
MODEL_PRICING = {
    "gpt-5.6-terra": {
        "input": 2.00,
        "cached_input": 0.20,
        "output": 12.00,
    },
    "gpt-5.6-luna": {
        "input": 0.20,
        "cached_input": 0.02,
        "output": 1.20,
    },
}


def _usage_value(obj, name, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_usage(response):
    usage = getattr(response, "usage", None)

    input_tokens = int(
        _usage_value(usage, "input_tokens", 0) or 0
    )

    output_tokens = int(
        _usage_value(usage, "output_tokens", 0) or 0
    )

    details = _usage_value(
        usage,
        "input_tokens_details",
        None
    )

    cached_tokens = int(
        _usage_value(
            details,
            "cached_tokens",
            0
        ) or 0
    )

    cached_tokens = max(
        0,
        min(cached_tokens, input_tokens)
    )

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
    }


def estimate_text_cost_usd(model, usage):
    pricing = MODEL_PRICING.get(model)

    if not pricing:
        return 0.0

    input_tokens = usage["input_tokens"]
    cached_tokens = usage["cached_input_tokens"]
    output_tokens = usage["output_tokens"]

    uncached_tokens = max(
        0,
        input_tokens - cached_tokens
    )

    return (
        uncached_tokens
        * pricing["input"]
        / 1_000_000
        +
        cached_tokens
        * pricing["cached_input"]
        / 1_000_000
        +
        output_tokens
        * pricing["output"]
        / 1_000_000
    )


def record_usage(state, response, model):
    usage = extract_usage(response)
    cost = estimate_text_cost_usd(
        model,
        usage
    )

    totals = state.setdefault(
        "api_usage",
        {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "estimated_text_cost_usd": 0.0,
        },
    )

    totals["input_tokens"] = (
        int(totals.get("input_tokens", 0))
        + usage["input_tokens"]
    )

    totals["cached_input_tokens"] = (
        int(
            totals.get(
                "cached_input_tokens",
                0
            )
        )
        + usage["cached_input_tokens"]
    )

    totals["output_tokens"] = (
        int(totals.get("output_tokens", 0))
        + usage["output_tokens"]
    )

    totals["estimated_text_cost_usd"] = round(
        float(
            totals.get(
                "estimated_text_cost_usd",
                0.0
            )
        )
        + cost,
        6
    )

    save_state(state)

    return {
        **usage,
        "estimated_text_cost_usd": cost,
        "model": model,
    }


def usage_footer(state, call_usage):
    totals = state.get(
        "api_usage",
        {}
    )

    return (
        "\n\n💳 API 사용량\n"
        f"모델 · {call_usage.get('model', 'unknown')}\n"
        f"이번 호출 · 입력 "
        f"{call_usage['input_tokens']:,} / "
        f"출력 "
        f"{call_usage['output_tokens']:,} tokens\n"
        f"이번 텍스트 토큰 비용 · 약 "
        f"${call_usage['estimated_text_cost_usd']:.4f}\n"
        f"누적 텍스트 토큰 비용 · 약 "
        f"${float(totals.get('estimated_text_cost_usd', 0.0)):.4f}\n"
        "※ 웹 검색 tool-call 별도 요금은 제외한 추정치"
    )


# ============================================================
# 출력 정리
# ============================================================

def clean_output(text):
    if not text:
        return ""

    # Markdown 링크 -> 텍스트만
    text = re.sub(
        r"\[([^\]]+)\]\(https?://[^)]+\)",
        r"\1",
        text
    )

    # 일반 URL 제거
    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    # 빈 괄호
    text = re.sub(
        r"\(\s*\)",
        "",
        text
    )

    # 과도한 빈 줄
    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text
    )

    return text.strip()


# ============================================================
# Telegram 전송
# ============================================================

def split_message(text, limit=3800):
    text = text.strip()

    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    paragraphs = text.split("\n\n")

    for paragraph in paragraphs:
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        candidate = (
            current + "\n\n" + paragraph
            if current
            else paragraph
        )

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        while len(paragraph) > limit:
            cut = paragraph.rfind(". ", 0, limit)

            if cut < limit // 2:
                cut = paragraph.rfind("\n", 0, limit)

            if cut < limit // 2:
                cut = limit

            chunks.append(
                paragraph[:cut].strip()
            )

            paragraph = paragraph[cut:].strip()

        current = paragraph

    if current:
        chunks.append(current)

    return chunks


def send(text, show_menu=True):
    text = clean_output(text)

    if show_menu:
        text = text + "\n\n" + MENU

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    chunks = split_message(text)

    for i, chunk in enumerate(chunks, start=1):

        if len(chunks) > 1:
            chunk = (
                f"[{i}/{len(chunks)}]\n\n"
                + chunk
            )

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True
            },
            timeout=45
        )

        response.raise_for_status()
        time.sleep(0.5)


# ============================================================
# Telegram 메시지 읽기
# ============================================================

def get_updates(offset):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/getUpdates"
    )

    response = requests.get(
        url,
        params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message"])
        },
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    return data.get("result", [])


# ============================================================
# 동적 커리큘럼 생성
# Structured Output 사용
# ============================================================

def generate_curriculum(industry, state):

    prompt = f"""
당신은 산업을 밑바닥 원리부터 가르치는
최상급 산업 리서치 교수다.

산업:
{industry}

이 산업을 투자자가 겉핥기가 아니라
실제 구조와 기술적 병목까지 이해하도록
장기 커리큘럼을 설계한다.

평범한 투자 리포트처럼

시장규모
→ 기업
→ 전망

부터 시작하지 않는다.

먼저

물리적 원리
기술의 존재 이유
제품 구조
제조 또는 운영 과정
핵심 공정
공정별 난제
기술 발전 방향
병목
장비
소재
기업 경쟁력
수요공급
경제성
사이클
현재 업황

순으로 지식이 쌓이게 한다.

반도체라면 필요에 따라

반도체가 전기를 제어하는 원리
실리콘
도핑
PN 접합
MOSFET
트랜지스터
웨이퍼
8대 공정 전체 지도
산화
포토리소그래피
PR
PAG
포토마스크
펠리클
ArF
EUV
식각의 원리
습식식각
건식식각
플라즈마
선택비
HAR 식각
증착
PVD
CVD
ALD
이온주입
CMP
세정
금속배선
High-k
Low-k
FinFET
GAA
DRAM 셀
DRAM 커패시터
리프레시
NAND
3D NAND
HBM
TSV
본딩
첨단패키징
수율
검사·계측
장비산업
소재산업
기업 경쟁구도
산업 사이클
최신 업황

같은 내용을 필요한 만큼 세분화한다.

복잡한 산업은 25~45강까지 괜찮다.

비교적 단순한 산업은
15~25강 정도로 설계한다.

각 강의는 앞 강의를 이해해야
다음 강의가 자연스럽게 이어지는 순서여야 한다.

각 강의는 다음 네 필드를 가진다.

title
수업 제목

goal
이 수업에서 반드시 이해해야 할 핵심

mechanism
실제로 어떤 원리나 구조까지 파고들지

investment_link
왜 이 내용이 기업 경쟁력이나 투자 판단과 연결되는지
"""

    response = get_client().responses.create(

        model=FAST_MODEL,

        reasoning={
            "effort": "medium"
        },

        input=prompt,

        text={
            "format": {
                "type": "json_schema",
                "name": "industry_curriculum",
                "strict": True,

                "schema": {
                    "type": "object",

                    "properties": {

                        "industry": {
                            "type": "string"
                        },

                        "topics": {
                            "type": "array",

                            "items": {
                                "type": "object",

                                "properties": {

                                    "title": {
                                        "type": "string"
                                    },

                                    "goal": {
                                        "type": "string"
                                    },

                                    "mechanism": {
                                        "type": "string"
                                    },

                                    "investment_link": {
                                        "type": "string"
                                    }
                                },

                                "required": [
                                    "title",
                                    "goal",
                                    "mechanism",
                                    "investment_link"
                                ],

                                "additionalProperties": False
                            }
                        }
                    },

                    "required": [
                        "industry",
                        "topics"
                    ],

                    "additionalProperties": False
                }
            }
        },

        max_output_tokens=9000,

        store=False
    )

    record_usage(
        state,
        response,
        FAST_MODEL
    )

    data = json.loads(
        response.output_text
    )

    topics = data.get("topics", [])

    if len(topics) < 15:
        raise RuntimeError(
            f"커리큘럼이 너무 짧습니다: {len(topics)}강"
        )

    return topics


# ============================================================
# 커리큘럼 확보
# ============================================================

def ensure_curriculum(state):

    if state.get("curriculum"):
        return

    industry = INDUSTRIES[
        state["industry_index"]
        % len(INDUSTRIES)
    ]

    print(
        f"{industry} 딥 커리큘럼 생성 중..."
    )

    state["curriculum"] = generate_curriculum(
        industry,
        state
    )

    state["topic_index"] = 0

    save_state(state)


# ============================================================
# 쉬운 설명 모드
# ============================================================

EASY_MODE_GUIDE = """
설명 난이도는 '중간 난이도'로 고정한다.

독자는 산업과 투자 공부를 많이 했고 기본 개념은 빠르게 이해하지만,
공학 전공자 수준의 세부 수식·물리·공정 이론까지는 필요하지 않다고 생각한다.

너무 초보자처럼 설명하지 않는다.
핵심 전문용어와 기술 구조는 그대로 사용하되,
전문용어가 왜 중요한지와 원인·결과를 쉬운 문장으로 풀어준다.

핵심 원칙:

어려운 용어를 먼저 던지지 않는다.
먼저 일상적인 말로 현상을 설명하고,
그 다음에 전문용어 이름을 붙인다.

전문용어가 처음 나오면
바로 뒤에서 한 문장으로 뜻을 풀어준다.

직관적으로 이해하기 어려운 개념에만
문, 수도관, 도로, 자석 같은 익숙한 비유를 사용한다.
이미 이해하기 쉬운 내용까지 억지로 비유하지 않는다.

단, 비유가 실제 원리와 다른 부분이 있으면
그 차이도 짧게 알려준다.

강의의 정보량과 깊이는 충분히 유지한다.
어려운 내용도 빼지 말고, 쉬운 말과 비유로 차근차근 풀어서 설명한다.

수식과 복잡한 계산은
이해에 꼭 필요하지 않으면 쓰지 않는다.

약어를 연속으로 나열하지 않는다.
약어는 처음 나올 때 반드시 한글 뜻과 역할을 설명한다.

'왜 그런가'를 가장 중요하게 설명한다.
정의 암기보다 원인과 결과를 연결한다.

설명 순서는 가능하면

쉬운 한 줄 결론
→ 아주 쉬운 비유
→ 실제 반도체/산업에서는 무슨 일이 일어나는지
→ 왜 문제가 되는지
→ 어떻게 해결하는지
→ 그래서 어느 기업이 유리한지

순서로 간다.

내용의 깊이는 유지하되
문장은 짧고 쉬워야 한다.

독자가 중간에
'그래서 이게 대체 무슨 뜻이지?'
라는 느낌이 들지 않게 쓴다.

전문가에게 보여주기 위한 글이 아니라
사용자가 실제로 이해하기 위한 글을 쓴다.

답변은 반드시 문장과 문단을 완결해서 끝낸다.
출력 한도가 가까워지면 새로운 내용을 더 벌리지 말고,
이미 설명한 내용을 자연스럽게 마무리한 뒤 끝낸다.
절대로 문장 중간이나 단어 중간에서 끝내지 않는다.
"""

# ============================================================
# 수업 프롬프트
# ============================================================

def build_lesson_prompt(
    industry,
    topic,
    topic_index,
    total_topics,
    previous_topics,
    next_topic_title=None
):

    previous = "\n".join(
        f"- {x}"
        for x in previous_topics[-8:]
    )

    return f"""
오늘 날짜는 {today()} 한국 시간 기준이다.

당신은 산업을 밑바닥 원리부터 설명하는
최상급 산업 교수이자 투자 분석가다.

현재 산업:
{industry}

전체 커리큘럼:
{total_topics}강

오늘:
{topic_index + 1}강

오늘의 주제:
{topic["title"]}

오늘 반드시 이해해야 할 것:
{topic["goal"]}

반드시 파고들 메커니즘:
{topic["mechanism"]}

투자와 연결되는 이유:
{topic["investment_link"]}

최근 배운 내용:
{previous or "첫 수업"}

실제 커리큘럼상 다음 강의:
{next_topic_title or "현재 산업의 마지막 강의"}

{EASY_MODE_GUIDE}

━━━━━━━━━━━━━━━━━━

이번 수업은
일반 투자 리포트가 아니다.

독자가 실제로
왜 그렇게 되는지를 이해해야 한다.

항상 다음 흐름을 따른다.

왜 필요한가
→
실제로 내부에서 무슨 일이 일어나는가
→
기존 방식은 왜 한계가 생기는가
→
그 한계를 어떻게 해결하는가
→
그 해결이 또 어떤 새로운 문제를 만드는가
→
그래서 다음 기술이 왜 필요한가
→
그 난도가 어떤 기업의 해자가 되는가

정의만 말하지 않는다.

━━━━━━━━━━━━━━━━━━

예를 들어 식각이면

식각은 깎는 공정이다

에서 끝내면 안 된다.

포토 공정 뒤에 왜 식각이 필요한지,

습식식각은 왜 옆으로도 깎이는지,

미세화에서 왜 수직성이 중요해지는지,

플라즈마에서 이온과 라디칼이
각각 무슨 일을 하는지,

선택비는 왜 중요한지,

구조가 깊고 좁아질수록
왜 HAR 식각이 어려워지는지,

3D NAND와 GAA가
왜 식각 난도를 높이는지,

그 결과 어느 장비와 어느 기업이
유리해지는지까지 연결한다.

━━━━━━━━━━━━━━━━━━

DRAM이면

DRAM은 휘발성 메모리다

에서 끝내지 않는다.

왜 트랜지스터와 커패시터가 필요한지,

전하를 저장한다는 것이
0과 1과 어떻게 연결되는지,

전하가 왜 새는지,

왜 리프레시가 필요한지,

미세화할수록 커패시터 면적이
왜 문제가 되는지,

왜 커패시터 구조가
길고 깊어지는지,

왜 high-k가 필요한지,

그 구조가 수율과 공정 난도,
장비,
소재,
삼성전자,
SK하이닉스,
Micron의 경쟁력과
어떻게 연결되는지까지 설명한다.

━━━━━━━━━━━━━━━━━━

HBM이면

DRAM을 쌓은 메모리다

에서 끝내지 않는다.

GPU가 왜 메모리를 기다리는지,

대역폭 병목이 실제 연산에
무슨 문제를 만드는지,

왜 I/O 폭을 넓혀야 하는지,

왜 DRAM을 GPU 가까이 가져가는지,

TSV가 왜 필요한지,

다이를 많이 쌓으면
왜 발열과 수율 문제가 생기는지,

본딩,
베이스다이,
인터포저,
패키징,
고객 인증이

왜 진입장벽이 되는지까지 설명한다.

━━━━━━━━━━━━━━━━━━

글은 쉬운 산업 기술 책처럼 쓴다.

번호 매기지 않는다.

표를 쓰지 않는다.

불릿 남발하지 않는다.

큰 개념이 바뀔 때만
적은 수의 제목을 사용한다.

첫 문단에서는 오늘 주제의 핵심을
투자자가 바로 이해할 수 있는 수준으로 명확하게 설명한다.

그 다음 기술 구조와 산업적 의미를 차근차근 깊게 설명한다.

쉽게 설명하되
내용 자체는 얕게 만들지 않는다.

핵심 전문용어는 그대로 사용한다.
다만 처음 나올 때 뜻과 역할을 쉬운 말로 바로 설명한다.

한 문장에 전문용어를
여러 개 몰아넣지 않는다.

문장은 짧게 쓴다.

━━━━━━━━━━━━━━━━━━

기업과 투자는
기술 설명 뒤에 반드시 충분히 연결한다.

이번 수업과 직접 관련된 기업을
가능한 한 많이 다룬다.

대형 대표 기업 몇 곳만 말하고 끝내지 않는다.

가능하면 밸류체인 전체에서
10~20개 안팎의 관련 기업을 찾아 설명한다.

다만 억지로 숫자를 채우지는 않는다.
오늘 주제와 실제로 관련 있는 기업만 넣는다.

기업은 가능하면 다음 범위에서 폭넓게 찾는다.

최종 제품 업체
팹리스
파운드리
메모리 업체
장비 업체
소재·화학 업체
부품 업체
패키징·테스트 업체
기판·인터포저 업체
EDA·IP 업체
전력·냉각·네트워크 업체
그 밖의 핵심 공급망 업체

기업 국가는 한쪽에 치우치지 않는다.

오늘 주제와 관련이 있다면 반드시 폭넓게 확인한다.

한국
미국
일본
대만
중국·홍콩
유럽

기업을 모두 후보군에 넣는다.

각 지역에서 실제로 중요한 기업이 있으면 반드시 포함한다.
특정 지역에 관련 기업이 거의 없으면 억지로 끼워 넣지는 않는다.

특히 한국·일본의 소재·부품·장비 업체,
미국의 설계·장비·EDA·인프라 업체,
대만의 파운드리·패키징·기판 업체,
중국의 메모리·파운드리·장비·소재 업체,
유럽의 노광·전력반도체·장비·IP 업체까지
밸류체인에서 중요한 기업을 놓치지 않는다.

대형주만 보지 않는다.
중소형 상장사나 공급망 핵심 기업도
오늘 주제와 직접 연결되면 포함한다.

토큰을 낭비하지 않기 위해
모든 기업을 똑같이 길게 설명하지 않는다.

가장 중요한 핵심 기업은 비교적 자세히 설명하고,
나머지 관련 기업은 투자 판단에 필요한 핵심만 압축해서 설명한다.
기업 이름만 나열하는 것은 금지한다.

각 기업은 이름만 나열하지 않는다.

각 회사마다 최소한

무엇을 파는 회사인지,
오늘 배운 기술과 정확히 어디서 연결되는지,
왜 고객이 그 회사를 쓰는지,
경쟁사 대비 강점이나 진입장벽이 무엇인지,
이 기술이 커질수록 매출이나 이익에 어떤 방향으로 영향을 받을 수 있는지

를 짧고 구체적으로 설명한다.

가능하면
시장점유율,
핵심 고객,
대표 제품,
최근 수주·CAPEX·로드맵,
경쟁사
중 투자 판단에 중요한 사실도 붙인다.

확실하지 않은 점유율이나 숫자는 만들지 않는다.

특히 중요한 기업은
다른 기업보다 조금 더 자세히 설명한다.

수업 후반에는 반드시

"이 기술과 연결된 기업 지도"

라는 제목을 만들고,
오늘 배운 기술의 밸류체인을 따라
관련 기업들을 묶어서 설명한다.

단순 기업 목록이 아니라

누가 가장 직접적인 수혜인지,
누가 병목을 쥐고 있는지,
누가 가격결정력이 있는지,
누가 경쟁이 심한지,
누가 대체되기 어려운지

까지 투자자 관점에서 비교한다.

━━━━━━━━━━━━━━━━━━

미래 방향도 반드시 설명한다.

오늘 배운 기술이

앞으로 더 중요해지는지,

덜 중요해지는지,

병목이 다른 곳으로 이동하는지

설명한다.

미세화,
3D화,
고단화,
고속화,
대역폭 증가,
전력,
열,
수율

같은 변화가
오늘의 기술에 어떤 영향을 주는지 연결한다.

━━━━━━━━━━━━━━━━━━

최신 변화는 반드시 웹 검색으로 확인한다.

오늘 주제와 직접 관련된

최근 기술 변화
최근 기업 발표
제품 로드맵
CAPEX
실적
CEO·CTO 발언
현재 업황

이 있다면 반영한다.

최근 3개월을 가장 중요하게 보고,
필요하면 6~12개월까지 본다.

뉴스를 나열하지 않는다.

오늘 배운 원리와
현재 벌어지는 변화를 연결한다.

중요 사실은 가능하면 교차검증한다.

확실하지 않은 숫자는 만들지 않는다.

최종 Telegram 출력에는

URL
링크
출처목록
각주형 링크

를 넣지 않는다.

━━━━━━━━━━━━━━━━━━

마지막에는 반드시

"결국 이 기술의 진짜 장벽은"

이라는 제목으로
핵심 진입장벽 또는 병목을 정리한다.

그 다음

"투자자 머릿속에 남겨둘 것"

이라는 제목으로
정말 중요한 내용만 짧게 정리한다.

마지막 한두 문장으로 다음 강의를 예고한다.

단, 다음 강의 주제는 절대로 추측하거나 새로 만들지 않는다.
반드시 위에 적힌 "실제 커리큘럼상 다음 강의"만 예고한다.
다음 강의가 "현재 산업의 마지막 강의"로 표시되어 있으면
다른 주제를 임의로 예고하지 말고 이번 산업 학습이 마무리되었다고만 말한다.

충분히 길고 깊게 작성한다.

한국어로 쓴다.
"""


# ============================================================
# 수업 생성
# ============================================================

def generate_deep_lesson(
    industry,
    topic,
    topic_index,
    total_topics,
    previous_topics,
    next_topic_title=None
):

    response = get_client().responses.create(

        model=NORMAL_LESSON_MODEL,

        reasoning={
            "effort": "high"
        },

        tools=[
            {
                "type": "web_search"
            }
        ],

        tool_choice="auto",

        input=build_lesson_prompt(
            industry,
            topic,
            topic_index,
            total_topics,
            previous_topics,
            next_topic_title
        ),

        max_output_tokens=16000,

        store=False
    )

    return (
        clean_output(response.output_text),
        response
    )


# ============================================================
# 다음 수업
# ============================================================

def send_next_lesson(
    state,
    automatic=False
):

    ensure_curriculum(state)

    industry = INDUSTRIES[
        state["industry_index"]
        % len(INDUSTRIES)
    ]

    curriculum = state["curriculum"]
    topic_index = state["topic_index"]


    # 현재 산업 끝
    if topic_index >= len(curriculum):

        if industry not in state["completed_industries"]:
            state["completed_industries"].append(
                industry
            )

        state["industry_index"] += 1

        if state["industry_index"] >= len(INDUSTRIES):
            state["industry_index"] = 0

        state["topic_index"] = 0
        state["curriculum"] = None

        save_state(state)

        ensure_curriculum(state)

        industry = INDUSTRIES[
            state["industry_index"]
        ]

        curriculum = state["curriculum"]
        topic_index = 0


    topic = curriculum[topic_index]

    next_topic_title = None
    if topic_index + 1 < len(curriculum):
        next_topic_title = curriculum[topic_index + 1].get("title", "")


    previous_topics = [
        x.get("topic", "")
        for x in state.get("history", [])
        if x.get("industry") == industry
    ]


    print(
        f"{industry} "
        f"{topic_index + 1}/{len(curriculum)} "
        f"{topic.get('title')}"
    )


    lesson, lesson_response = generate_deep_lesson(
        industry,
        topic,
        topic_index,
        len(curriculum),
        previous_topics,
        next_topic_title
    )

    call_usage = record_usage(
        state,
        lesson_response,
        NORMAL_LESSON_MODEL
    )


    state["lesson_count"] += 1


    state["last_lesson"] = {
        "date": today(),
        "industry": industry,
        "topic": topic.get("title", ""),
        "goal": topic.get("goal", ""),
        "mechanism": topic.get("mechanism", ""),
        "investment_link": topic.get(
            "investment_link",
            ""
        ),
        "text": lesson[-24000:]
    }


    state["history"].append({
        "date": today(),
        "industry": industry,
        "topic": topic.get("title", "")
    })


    state["history"] = state["history"][-500:]


    state["topic_index"] += 1

    save_state(state)


    header = (
        "📚 Industry Study · EASY\n"
        f"{industry}\n"
        f"{topic_index + 1}강 / "
        f"{len(curriculum)}강\n\n"
        f"오늘의 주제 · "
        f"{topic.get('title')}\n\n"
    )


    send(
        header
        + lesson
        + usage_footer(
            state,
            call_usage
        )
    )


# ============================================================
# 심화학습
# ============================================================

def deep_study(state):

    last = state.get("last_lesson")

    if not last:

        send(
            "아직 심화할 수업이 없습니다. "
            "먼저 다음공부를 진행해주세요."
        )
        return


    prompt = f"""
오늘 날짜는 {today()}다.

당신은 산업 기술을
한 단계 더 깊게 파고드는 교수다.

산업:
{last["industry"]}

현재 주제:
{last["topic"]}

수업 목표:
{last.get("goal", "")}

핵심 메커니즘:
{last.get("mechanism", "")}

방금 수업:
{last["text"]}

사용자가 '심화학습'을 요청했다.

{EASY_MODE_GUIDE}

심화학습도 어려운 말투로 바꾸지 않는다.
더 깊게 들어가되 설명은 오히려 더 쉽게 한다.

방금 내용을 반복하지 않는다.

이번에는
한 단계 더 아래의 물리적,
기술적,
공정적,
경제적 원리로 내려간다.

전문용어만 늘어놓지 않는다.

왜 그런 현상이 생기는지를
쉽게 설명한다.

그 난도가

장비
소재
공정
수율
가격결정력
마진
시장점유율

과 어떻게 연결되는지 설명한다.

최신 기술 변화는
웹 검색으로 확인한다.

URL은 출력하지 않는다.

번호 매기지 않는다.
표를 사용하지 않는다.

좋은 기술서의
심화 챕터처럼 자연스럽게 쓴다.

마지막에는

"여기까지 이해하면 보이는 것"

이라는 제목으로
투자 관점에서 무엇이 달라 보이는지 정리한다.
"""


    response = get_client().responses.create(

        model=LESSON_MODEL,

        reasoning={
            "effort": "high"
        },

        tools=[
            {
                "type": "web_search"
            }
        ],

        tool_choice="auto",

        input=prompt,

        max_output_tokens=12000,

        store=False
    )


    call_usage = record_usage(
        state,
        response,
        LESSON_MODEL
    )

    send(
        "🔬 심화학습\n"
        f"{last['industry']} · "
        f"{last['topic']}\n\n"
        + clean_output(
            response.output_text
        )
        + usage_footer(
            state,
            call_usage
        )
    )


# ============================================================
# 기업 더 자세히
# ============================================================

def company_deep_dive(state):

    last = state.get("last_lesson")

    if not last:

        send(
            "아직 연결된 수업이 없습니다. "
            "먼저 다음공부를 진행해주세요."
        )
        return


    prompt = f"""
오늘 날짜는 {today()}다.

당신은 기술과 기업 경쟁력을
연결해서 분석하는 산업 투자 전문가다.

산업:
{last["industry"]}

현재 공부 주제:
{last["topic"]}

방금 공부한 내용:
{last["text"]}

사용자가 '기업 더 자세히'를 요청했다.

{EASY_MODE_GUIDE}

기업 분석에서도
전문 금융용어와 기술용어를 압축해서 쓰지 않는다.
회사가 무엇을 팔고 왜 돈을 버는지부터 쉽게 설명한다.

오늘 배운 기술이나 구조가
실제 어느 회사와 연결되는지 깊게 설명한다.

한국,
미국,
일본,
유럽,
중국 등
지역을 제한하지 않는다.

중요한 회사를 충분히 다룬다.

각 회사가

무엇을 파는지
어디에 쓰이는지
왜 고객이 선택하는지
경쟁사는 누구인지
기술적 해자는 무엇인지
고객이 공급사를 바꾸기 어려운 이유
시장점유율은 어느 정도인지
점유율이 어떻게 변하는지
마진과 가격결정력은 어떤지
CAPEX가 중요한지
최근 실적과 가이던스는 어떤지
현재 업황에서 왜 유리하거나 불리한지

를 자연스럽게 연결한다.

시장점유율,
최근 실적,
가이던스,
CAPEX,
제품 로드맵은
최신 웹 자료를 확인한다.

CEO나 CTO의
최근 중요한 발언이 있으면 반영한다.

확실하지 않은 숫자는 만들지 않는다.

뉴스 나열은 금지한다.

URL과 링크는 출력하지 않는다.

번호 매기지 않는다.
표를 사용하지 않는다.

마지막에는

"결국 누가 가장 강한가"

라는 제목으로
현재 구조에서 어떤 기업이 강한지,
왜 강한지,
무엇이 바뀌면 우위가 흔들리는지 설명한다.
"""


    response = get_client().responses.create(

        model=LESSON_MODEL,

        reasoning={
            "effort": "high"
        },

        tools=[
            {
                "type": "web_search"
            }
        ],

        tool_choice="auto",

        input=prompt,

        max_output_tokens=12000,

        store=False
    )


    call_usage = record_usage(
        state,
        response,
        LESSON_MODEL
    )

    send(
        "🏢 기업 더 자세히\n"
        f"{last['industry']} · "
        f"{last['topic']}\n\n"
        + clean_output(
            response.output_text
        )
        + usage_footer(
            state,
            call_usage
        )
    )


# ============================================================
# 질문 대답
# ============================================================

def answer_question(
    state,
    question
):

    last = state.get("last_lesson")

    if last:

        context = f"""
현재 산업:
{last["industry"]}

최근 주제:
{last["topic"]}

최근 수업:
{last["text"][-12000:]}
"""

    else:

        context = "최근 수업 기록 없음"


    prompt = f"""
당신은 개인 산업공부 교수다.

{context}

사용자의 질문:

{question}

{EASY_MODE_GUIDE}

질문의 핵심 주장부터
아주 쉬운 한두 문장으로 먼저 답한다.

그다음 왜 그런지
밑바닥 원리까지 설명한다.

쉽게 설명하되
내용은 얕게 만들지 않는다.

가능하면

원인
→ 구조
→ 병목
→ 해결
→ 기업 경쟁력
→ 투자 의미

순서로 연결한다.

최신 기술,
업황,
기업,
실적,
시장점유율,
CEO 발언이 관련되면
웹 검색을 사용한다.

확실하지 않은 사실은
사실처럼 말하지 않는다.

URL이나 링크는 출력하지 않는다.

번호 매기지 않는다.
표를 사용하지 않는다.
"""


    response = get_client().responses.create(

        model=LESSON_MODEL,

        reasoning={
            "effort": "medium"
        },

        tools=[
            {
                "type": "web_search"
            }
        ],

        tool_choice="auto",

        input=prompt,

        max_output_tokens=5000,

        store=False
    )


    call_usage = record_usage(
        state,
        response,
        LESSON_MODEL
    )

    send(
        "💬 질문 대답\n\n"
        + clean_output(
            response.output_text
        )
        + usage_footer(
            state,
            call_usage
        )
    )


# ============================================================
# 연기
# ============================================================

def delay_study(state):

    state["pause_until"] = tomorrow()

    save_state(state)

    send(
        "⏸ Industry Study를 하루 연기했습니다.\n\n"
        "자동 수업만 하루 쉬고 "
        "현재 진도는 그대로 유지합니다.\n\n"
        "연기 중에도 '다음공부'라고 보내면 "
        "바로 다음 강의로 진행합니다."
    )


# ============================================================
# Telegram 수집
# ============================================================

def collect_messages(state):

    updates = get_updates(
        state.get(
            "telegram_offset",
            0
        )
    )

    messages = []


    for update in updates:

        update_id = update.get(
            "update_id",
            0
        )

        state["telegram_offset"] = max(
            state.get(
                "telegram_offset",
                0
            ),
            update_id + 1
        )


        message = update.get(
            "message",
            {}
        )


        chat_id = str(
            message.get(
                "chat",
                {}
            ).get(
                "id",
                ""
            )
        )


        if chat_id != str(
            TELEGRAM_CHAT_ID
        ):
            continue


        if message.get(
            "from",
            {}
        ).get(
            "is_bot"
        ):
            continue


        text = message.get(
            "text",
            ""
        ).strip()


        if text:
            messages.append(text)


    return messages


# ============================================================
# 명령 처리
# ============================================================

def process_command(
    state,
    text
):

    command = text.strip()


    if command == "다음공부":

        state["pause_until"] = None

        save_state(state)

        send_next_lesson(
            state,
            automatic=False
        )

        return


    if command in [
        "심화학습",
        "심화 학습"
    ]:

        deep_study(state)
        return


    if command == "연기":

        delay_study(state)
        return


    if command in [
        "기업 더 자세히",
        "기업더자세히"
    ]:

        company_deep_dive(state)
        return


    if command == "질문 대답":

        send(
            "궁금한 내용을 그대로 보내주세요.\n\n"
            "예를 들어\n"
            "'왜 DRAM 커패시터는 길어지는 거야?'\n"
            "'앞으로 식각이 왜 중요해져?'\n"
            "'Lam Research 해자가 정확히 뭐야?'\n\n"
            "처럼 보내시면 됩니다."
        )

        return


    # 그 외 텍스트는 전부 질문
    answer_question(
        state,
        command
    )


# ============================================================
# 새벽 자동 수업
# ============================================================

def morning_mode():

    validate_env()

    state = load_state()


    if (
        state.get(
            "last_auto_lesson_date"
        )
        == today()
    ):

        print(
            "오늘 자동 수업은 이미 전송되었습니다."
        )

        return


    pause_until = state.get(
        "pause_until"
    )


    if (
        pause_until
        and today() <= pause_until
    ):

        print(
            "오늘은 연기 상태입니다."
        )

        return


    state["last_auto_lesson_date"] = today()

    save_state(state)


    send_next_lesson(
        state,
        automatic=True
    )


# ============================================================
# 명령 확인
# ============================================================

def poll_mode():

    validate_env()

    state = load_state()

    messages = collect_messages(
        state
    )


    if not messages:

        save_state(state)

        print(
            "새 Telegram 메시지 없음"
        )

        return


    for text in messages:

        print(
            "Telegram 명령:",
            text
        )

        process_command(
            state,
            text
        )

        state = load_state()


    save_state(state)


# ============================================================
# 오류
# ============================================================

def error_notice(error):

    message = str(error)


    if (
        "credit_balance_exhausted" in message
        or
        "no credits remaining" in message.lower()
        or
        "insufficient_quota" in message
    ):

        text = (
            "💳 Industry Study 중단\n\n"
            "OpenAI API 잔액이 부족합니다.\n"
            "크레딧을 충전하면 "
            "현재 진도부터 계속할 수 있습니다."
        )

    else:

        text = (
            "❌ Industry Study 오류\n\n"
            f"{type(error).__name__}: "
            f"{message[:1800]}"
        )


    print(text)


    try:
        send(text)
    except Exception:
        pass


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":

    MODE = os.getenv(
        "MODE",
        "morning"
    ).lower()


    try:

        if MODE == "morning":

            morning_mode()


        elif MODE == "poll":

            poll_mode()


        else:

            raise ValueError(
                f"잘못된 MODE: {MODE}"
            )


    except Exception as e:

        error_notice(e)

        raise
