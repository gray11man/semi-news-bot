# -*- coding: utf-8 -*-
"""
patch_people.py  —  gray_man_bot(메인 뉴스봇)에 '인물 발언 전용 경로' 추가 패치

목적:
  젠슨황·올트먼·아모데이·곽노정·전영현 등 핵심 인물의 발언/인터뷰 기사가
  passes_filter(산업 키워드 강제)에서 죽고, when:4h 창이 좁아 놓치던 문제 해결.

방식:
  1) 인물 전용 피드(PEOPLE_FEEDS)를 24시간 창으로 별도 수집.
  2) 이 피드로 들어온 기사는 passes_filter를 '건너뛰고' 인물명만 맞으면 통과.
  3) 기존 산업 뉴스 로직 / dedup / seen / 점수 체계는 그대로.

적용법 (원본 파일에 아래 3곳만 반영):
  [A] 상수/피드/사전 추가       → 파일 상단 FEEDS 정의 부근에 붙여넣기
  [B] collect() 함수 교체        → 기존 collect()를 이 버전으로 교체
  [C] base_score()에 인물 가산   → 기존 base_score() 안에 두 줄 추가 (선택)

※ 인물 명단은 2026-07-06 기준 교차검증 완료.
  CTO 공석(OpenAI)·퇴사자(Mira Murati) 제외. 직함 변동 잦아 실명+회사 병행.
"""

from urllib.parse import quote

# 원본과 동일한 기본 시간창(산업용)
NEWS_WINDOW_HOURS = 4
# 인물 발언은 하루 종일 퍼지므로 넓게
PEOPLE_WINDOW_HOURS = 24


def gnews(query, lang="en", hours=NEWS_WINDOW_HOURS):
    """원본 gnews와 동일 시그니처. hours만 인물용으로 넘겨 재사용."""
    q = quote(f"{query} when:{hours}h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    if lang == "zh":
        return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


# ─────────────────────────────────────────────────────────────
# [A] 인물 전용 검색어 (2026-07-06 교차검증)
# ─────────────────────────────────────────────────────────────
# 영어권 인물 (AI 빅랩 + 반도체/파운드리 수장)
PEOPLE_EN = (
    '"Sam Altman" OR "Greg Brockman" OR "Sarah Friar" OR "Jason Kwon" OR '
    '"Dario Amodei" OR "Krishna Rao" OR "Rahul Patil" OR '
    '"Demis Hassabis" OR "Sundar Pichai" OR "Elon Musk" OR '
    '"Jensen Huang" OR "Lisa Su" OR "Satya Nadella" OR "Hock Tan" OR '
    '"Sanjay Mehrotra" OR "C.C. Wei" OR "Cristiano Amon"'
)
# 인물 발언·인터뷰 성격을 강화하는 보조어(영어)
PEOPLE_EN_VERB = (
    '(says OR said OR interview OR warns OR predicts OR comments OR '
    'remarks OR earnings call OR keynote)'
)

# 한국·현지 인물 (메모리·조선·엔진)
PEOPLE_KO = (
    '곽노정 OR 전영현 OR 젠슨 황 OR 올트먼 OR 아모데이 OR 피차이 OR '
    '황산더 OR 김동관 OR 정기선'
)
PEOPLE_KO_VERB = '(발언 OR 인터뷰 OR 간담회 OR 컨퍼런스콜 OR 기자회견 OR 강조 OR 전망)'

# 인물 전용 피드: 24시간 창으로 넓게
PEOPLE_FEEDS = [
    gnews(f"{PEOPLE_EN} {PEOPLE_EN_VERB}", "en", hours=PEOPLE_WINDOW_HOURS),
    gnews(PEOPLE_EN, "en", hours=PEOPLE_WINDOW_HOURS),          # 보조어 없이도 한 번
    gnews(f"{PEOPLE_KO} {PEOPLE_KO_VERB}", "ko", hours=PEOPLE_WINDOW_HOURS),
    gnews(PEOPLE_KO, "ko", hours=PEOPLE_WINDOW_HOURS),
]

# 인물명 매칭 사전 (제목/요약에 이 중 하나만 있으면 인물기사로 인정)
PEOPLE_NAMES = [
    # 영어
    "sam altman", "altman", "greg brockman", "brockman", "sarah friar", "friar",
    "jason kwon", "dario amodei", "amodei", "krishna rao", "rahul patil",
    "demis hassabis", "hassabis", "sundar pichai", "pichai", "elon musk", "musk",
    "jensen huang", "jensen", "lisa su", "satya nadella", "nadella", "hock tan",
    "sanjay mehrotra", "mehrotra", "c.c. wei", "cristiano amon",
    # 한국어
    "곽노정", "전영현", "젠슨", "황", "올트먼", "아모데이", "피차이",
    "머스크", "리사 수", "황산더", "김동관", "정기선",
]


def is_people_article(title, summary):
    """제목/요약에 핵심 인물명이 들어있으면 True."""
    text = f"{title} {summary}".lower()
    return any(name in text for name in PEOPLE_NAMES)


# ─────────────────────────────────────────────────────────────
# [B] collect() 교체본
#     - 산업 피드(FEEDS)는 기존대로 passes_filter 적용
#     - 인물 피드(PEOPLE_FEEDS)는 passes_filter 건너뛰고 인물명만 확인
# ─────────────────────────────────────────────────────────────
"""
아래 collect_with_people()로 원본 collect()를 교체하세요.
원본에 이미 있는 함수들(is_fresh, passes_filter, norm_title, is_similar,
source_name, clean_summary, base_score, html, feedparser, RSS_MAX_ENTRIES,
datetime)을 그대로 사용합니다. import는 원본 상단 것을 씁니다.
"""

COLLECT_REPLACEMENT = r'''
def collect():
    items = []
    seen_titles = []

    # 피드를 (url, is_people) 쌍으로 순회. 인물 피드는 필터 완화.
    feed_plan = [(u, False) for u in FEEDS] + [(u, True) for u in PEOPLE_FEEDS]

    for url, is_people in feed_plan:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] feed fail: {e}")
            continue
        for entry in feed.entries[:RSS_MAX_ENTRIES]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            raw_sum = entry.get("summary", "")
            if not title or not link:
                continue

            # 신선도: 인물 기사는 24h까지 허용
            if is_people:
                age = entry_age_hours(entry)
                if age is not None and age > PEOPLE_WINDOW_HOURS + 1:
                    continue
            else:
                if not is_fresh(entry):
                    continue

            # 필터: 산업 기사만 passes_filter 강제.
            #       인물 기사는 인물명만 맞으면 통과(산업 키워드 불필요).
            if is_people:
                if not is_people_article(title, raw_sum):
                    continue
            else:
                if not passes_filter(title, raw_sum):
                    continue

            nt = norm_title(title)
            if any(is_similar(nt, s) for s in seen_titles):
                continue
            seen_titles.append(nt)

            pub_iso = ""
            tm = entry.get("published_parsed") or entry.get("updated_parsed")
            if tm:
                try:
                    pub_iso = datetime.datetime(*tm[:6],
                        tzinfo=datetime.timezone.utc).isoformat()
                except Exception:
                    pub_iso = ""

            sc = base_score(title, raw_sum)
            if is_people:
                sc += 3  # 인물 발언 가산점(전송 문턱 통과 도움)

            items.append({
                "title": html.unescape(title),
                "link": link,
                "summary": clean_summary(raw_sum),
                "source": source_name(entry),
                "ntitle": nt,
                "score": sc,
                "published": pub_iso,
                "is_people": is_people,
            })
    print(f"[INFO] 수집 {len(items)}건 (산업+인물, 필터/1차중복 후)")
    return items
'''

if __name__ == "__main__":
    # 인물 피드 URL 및 매칭 동작 간단 점검
    print("=== PEOPLE_FEEDS ===")
    for u in PEOPLE_FEEDS:
        print(u[:110], "...")
    print("\n=== is_people_article 테스트 ===")
    samples = [
        ("Jensen Huang says AI demand is insatiable", ""),
        ("곽노정 SK하이닉스 사장 \"HBM 수요 폭발\"", ""),
        ("삼성전자 3분기 영업이익 발표", ""),  # 인물명 없음 → False 기대
    ]
    for t, s in samples:
        print(f"  [{is_people_article(t, s)}] {t}")
