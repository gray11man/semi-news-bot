#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI·반도체·메모리·데이터센터·전력·AI수요 산업 뉴스 에이전트 (v2.7)

v2.6 → v2.7 변경점:
  [과거 뉴스 차단 — 3중 검증]
  - 구글 재색인으로 published가 "방금"으로 찍힌 옛 기사 차단
  - 1차: URL 경로에 박힌 날짜(/2026/06/18/ 등)로 조기 차단 (collect 단계)
  - 2차: 기사 원문 HTML의 실제 발행일(article:published_time) 추출 검증 (전송 직전)
  - 3차: Gemini 프롬프트에 "명백한 과거 사건 보도는 C 판정" 지시 → C 차단 로직이 최종 안전망
  - STALE_HARD_LIMIT_H = 48 (실제 발행일 기준 48시간 초과 시 폐기)
  - SEEN_RETENTION_DAYS 7 → 30 (재색인 주기가 7일보다 길어 같은 기사 반복 유입되던 문제)

  [메시지 폭주 억제 — 중요도 기준 상향]
  - MIN_SCORE_TO_SEND 5 → 8
  - MAX_SEND_PER_RUN 20 → 10
  - POLICY_SIGNALS / ROI_SIGNALS 가점 +5 → +3 (점수 인플레 원인이던 과다 가점 축소)
  - Gemini 등급 필터 강화: C 차단(기존) + B등급도 점수 10 미만이면 차단
    → 실질적으로 S/A 위주 + 고점수 B만 전송
"""

import os
import re
import json
import time
import html
import random
import hashlib
import datetime
from difflib import SequenceMatcher
from urllib.parse import urlparse, quote

import requests
import feedparser

try:
    import trafilatura
    _HAS_TRAFI = True
except Exception:
    _HAS_TRAFI = False

# ───────────────────────── 환경변수 ─────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite").strip()

# ───────────────────────── 설정 ─────────────────────────
SEEN_FILE = "seen.json"
QUEUE_FILE = "queue.json"
NEWS_FILE = "news.json"
NEWS_MAX_ITEMS = 150
SEEN_RETENTION_DAYS = 30              # [v2.7] 7 → 30 (재색인 재유입 방지)
MAX_SEND_PER_RUN = 10                 # [v2.7] 20 → 10
MIN_SCORE_TO_SEND = 5                 # [v2.7] 5 → 8
NEWS_WINDOW_HOURS = 3
PEOPLE_WINDOW_HOURS = 24
STALE_HARD_LIMIT_H = 48               # [v2.7] 실제 발행일 기준 최대 허용 나이
GRADE_B_MIN_SCORE = 7                 # [v2.7] B등급은 이 점수 이상만 전송
SIMILARITY_THRESHOLD = 0.55
REQUEST_TIMEOUT = 25
SEND_DELAY = 1.0

GEMINI_MIN_INTERVAL = 4.0
GEMINI_MAX_CALLS_PER_RUN = 45
GEMINI_RETRY_MAX = 2
GEMINI_RETRY_BASE = 2.0
GEMINI_CONSEC_FAIL_STOP = 4
RSS_MAX_ENTRIES = 30

FETCH_BODY = True
BODY_FETCH_TIMEOUT = 12
BODY_MAX_CHARS = 6000
BODY_FETCH_DELAY = 0.5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ───────────────────────── RSS 소스 ─────────────────────────
def gnews(query, lang="en", hours=NEWS_WINDOW_HOURS):
    q = quote(f"{query} when:{hours}h")
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    if lang == "ja":
        return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    if lang == "zh":
        return f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


CORE_EN = (
    "OpenAI OR Anthropic OR xAI OR \"Google DeepMind\" OR \"Meta AI\" OR Mistral OR "
    "Nvidia OR AMD OR Broadcom OR Marvell OR TSMC OR Samsung OR \"SK hynix\" OR Micron OR "
    "Amazon OR AWS OR Microsoft OR Alphabet OR Google OR Meta OR Oracle OR "
    "HBM OR DRAM OR NAND OR CXL OR CoWoS OR \"data center\" OR datacenter OR "
    "CoreWeave OR \"power grid\" OR \"gas turbine\" OR nuclear"
)
MONEY_EN = (
    "AI capex OR AI funding OR \"data center investment\" OR \"GPU order\" OR "
    "\"HBM contract\" OR \"cloud deal\" OR AI acquisition OR semiconductor investment"
)
DEMAND_EN = (
    "\"AI demand\" OR \"inference demand\" OR \"token usage\" OR \"compute demand\" OR "
    "\"GPU shortage\" OR \"capacity sold out\" OR \"AI revenue\" OR \"AI adoption\" OR "
    "\"AI workload\" OR \"datacenter utilization\" OR \"AI agent\" OR \"enterprise AI\" OR "
    "\"AI guidance\" OR \"backlog\" OR \"order backlog\""
)
CORE_KO = (
    "엔비디아 OR HBM OR DRAM OR 낸드 OR SK하이닉스 OR 삼성전자 반도체 OR "
    "데이터센터 OR AI 투자 OR AI 인프라 OR 반도체 증설 OR 전력 OR 가스터빈 OR CXL OR 패키징"
)
DEMAND_KO = (
    "AI 수요 OR 추론 수요 OR AI 토큰 OR 연산 수요 OR GPU 부족 OR 캐파 부족 OR "
    "AI 매출 OR AI 가동률 OR AI 에이전트 OR 기업용 AI OR 수주잔고 OR AI 채택"
)
FUNDING_EN = (
    "(Amazon OR Microsoft OR Alphabet OR Google OR Meta OR Oracle OR Nvidia OR "
    "OpenAI OR Anthropic OR xAI OR CoreWeave OR hyperscaler) "
    "(bond OR debt OR \"capital raise\" OR \"equity sale\" OR \"share sale\" OR "
    "financing OR \"credit rating\" OR CDS OR leverage OR \"free cash flow\" OR "
    "downgrade OR \"credit spread\" OR \"bond yield\" OR \"funding squeeze\" OR "
    "\"debt burden\" OR \"struggling to raise\" OR oversubscribed)"
)
FUNDING_KO = (
    "하이퍼스케일러 자금조달 OR 하이퍼스케일러 채권 OR 오라클 회사채 OR "
    "빅테크 부채 OR AI 설비투자 조달 OR AI 자금조달 OR 빅테크 신용등급 OR "
    "데이터센터 프로젝트파이낸싱 OR AI 부채 OR 자금조달 난항 OR 조달 실패 OR "
    "회사채 스프레드 OR 신용등급 하향"
)
POLICY_EN = (
    "(hyperscaler OR \"data center\" OR OpenAI OR Anthropic OR Nvidia OR TSMC OR "
    "Samsung OR \"SK hynix\" OR Micron OR Intel OR \"AI infrastructure\") "
    "(subsidy OR subsidies OR \"tax credit\" OR \"tax break\" OR \"CHIPS Act\" OR "
    "\"government support\" OR \"state support\" OR \"sovereign AI\" OR "
    "\"national AI\" OR \"government funding\" OR \"loan guarantee\" OR "
    "\"executive order\" OR \"export control\" OR tariff)"
)
POLICY_KO = (
    "AI 정부 지원 OR 반도체 보조금 OR 데이터센터 세액공제 OR 소버린 AI OR "
    "국가 AI 인프라 OR 반도체 특별법 OR AI 기본법 OR 정부 AI 투자 OR "
    "대출 보증 OR 정책금융 OR 칩스법"
)
ROI_EN = (
    "(AI OR hyperscaler OR \"data center\" OR GPU OR capex) "
    "(ROI OR \"return on investment\" OR monetization OR \"AI bubble\" OR "
    "depreciation OR \"payback period\" OR overbuild OR overcapacity OR "
    "\"write-down\" OR impairment OR \"capex cut\" OR \"spending cut\" OR "
    "\"capex guidance\" OR unprofitable OR \"burn rate\")"
)
ROI_KO = (
    "AI 투자 회수 OR AI ROI OR AI 수익화 OR AI 버블 OR AI 거품 OR "
    "데이터센터 과잉 OR 과잉투자 OR 감가상각 OR capex 축소 OR 설비투자 축소 OR "
    "가이던스 하향 OR 손상차손"
)

FEEDS = [
    gnews(CORE_EN, "en"),
    gnews(MONEY_EN, "en"),
    gnews(DEMAND_EN, "en"),
    gnews(FUNDING_EN, "en"),
    gnews(POLICY_EN, "en"),
    gnews(ROI_EN, "en"),
    gnews(CORE_KO, "ko"),
    gnews("AI 데이터센터 OR HBM 공급 OR 반도체 수주 OR AI 전력 OR 원전 데이터센터", "ko"),
    gnews(DEMAND_KO, "ko"),
    gnews(FUNDING_KO, "ko"),
    gnews(POLICY_KO, "ko"),
    gnews(ROI_KO, "ko"),
    gnews("AI半導体 OR HBM OR データセンター OR ラピダス OR 電力 AI OR AI需要 OR 推論需要", "ja"),
    gnews("人工智能 芯片 OR 数据中心 OR HBM OR 算力 OR 英伟达 OR AI需求 OR 推理需求", "zh"),
    gnews("台積電 OR CoWoS OR AI 伺服器 OR 半導體 產能 OR AI 需求", "zh"),
    gnews("한화엔진 OR 4행정 중속엔진 OR 데이터센터 발전엔진 OR 힘센엔진 OR 선박엔진 발전", "ko"),
    gnews("한화엔진 OR STX엔진 OR HD현대마린엔진 OR 데이터센터 엔진 OR 가스엔진 발전", "ko"),
    gnews("조선주 OR HD현대중공업 OR 삼성중공업 OR 한화오션 OR 조선 수주 OR LNG선 발주", "ko"),
    gnews("Tempus AI OR \"TEM stock\" OR Tempus oncology OR Tempus FDA", "en"),
    gnews("Tempus AI OR 템퍼스", "ko"),
]

# ───────────────────────── 인물 발언 전용 경로 ─────────────────────────
PEOPLE_NAMED_EN = (
    '"Jensen Huang" OR "Sam Altman" OR "Dario Amodei" OR "Elon Musk" OR '
    '"Demis Hassabis" OR "Sundar Pichai" OR "Lisa Su" OR "Satya Nadella"'
)
PEOPLE_TITLE_EN = (
    '"OpenAI CEO" OR "OpenAI CFO" OR "OpenAI CTO" OR "OpenAI president" OR '
    '"Anthropic CEO" OR "Anthropic CFO" OR "Anthropic CTO" OR '
    '"Nvidia CEO" OR "Nvidia CFO" OR "TSMC CEO" OR "Micron CEO" OR '
    '"SK hynix CEO" OR "Samsung CEO" OR "AMD CEO" OR "Broadcom CEO" OR '
    '"Qualcomm CEO"'
)
PEOPLE_EN_VERB = (
    '(says OR said OR interview OR warns OR predicts OR comments OR '
    'remarks OR "earnings call" OR keynote)'
)

PEOPLE_NAMED_KO = (
    '곽노정 OR 전영현 OR "젠슨 황" OR 올트먼 OR 아모데이 OR 피차이 OR '
    '김동관 OR 정기선'
)
PEOPLE_TITLE_KO = (
    '"SK하이닉스 사장" OR "SK하이닉스 대표" OR "삼성전자 사장" OR "삼성전자 부회장" OR '
    '"마이크론 CEO" OR "엔비디아 CEO" OR "TSMC CEO"'
)
PEOPLE_KO_VERB = '(발언 OR 인터뷰 OR 간담회 OR 컨퍼런스콜 OR 기자회견 OR 강조 OR 전망)'

PEOPLE_FEEDS = [
    gnews(f"({PEOPLE_NAMED_EN}) {PEOPLE_EN_VERB}", "en", hours=PEOPLE_WINDOW_HOURS),
    gnews(PEOPLE_NAMED_EN, "en", hours=PEOPLE_WINDOW_HOURS),
    gnews(f"({PEOPLE_TITLE_EN}) {PEOPLE_EN_VERB}", "en", hours=PEOPLE_WINDOW_HOURS),
    gnews(f"({PEOPLE_NAMED_KO}) {PEOPLE_KO_VERB}", "ko", hours=PEOPLE_WINDOW_HOURS),
    gnews(PEOPLE_NAMED_KO, "ko", hours=PEOPLE_WINDOW_HOURS),
    gnews(f"({PEOPLE_TITLE_KO}) {PEOPLE_KO_VERB}", "ko", hours=PEOPLE_WINDOW_HOURS),
]

PEOPLE_NAMES = [
    "jensen huang", "jensen", "sam altman", "altman", "dario amodei", "amodei",
    "elon musk", "musk", "demis hassabis", "hassabis", "sundar pichai", "pichai",
    "lisa su", "satya nadella", "nadella",
    "곽노정", "전영현", "젠슨", "올트먼", "아모데이", "피차이", "머스크",
    "김동관", "정기선",
]
PEOPLE_ORGS = [
    "openai", "anthropic", "nvidia", "엔비디아", "tsmc", "micron", "마이크론",
    "sk hynix", "sk하이닉스", "하이닉스", "samsung", "삼성전자", "amd",
    "broadcom", "qualcomm", "퀄컴",
]
PEOPLE_TITLES = [
    "ceo", "cfo", "cto", "president", "사장", "대표", "부회장", "회장",
]


def is_people_article(title, summary):
    text = f"{title} {summary}".lower()
    if any(name in text for name in PEOPLE_NAMES):
        return True
    if any(org in text for org in PEOPLE_ORGS) and any(t in text for t in PEOPLE_TITLES):
        return True
    return False


# ───────────────────────── 필터 키워드 ─────────────────────────
INCLUDE = [
    "ai", "gpu", "hbm", "dram", "nand", "cxl", "cowos", "packaging", "wafer",
    "data center", "datacenter", "nvidia", "amd", "tsmc", "samsung", "hynix",
    "micron", "broadcom", "marvell", "openai", "anthropic", "xai", "deepmind",
    "capex", "funding", "investment", "acquisition", "power", "grid", "turbine",
    "nuclear", "transformer", "optical", "transceiver", "inference",
    "demand", "token", "workload", "backlog", "utilization", "adoption", "sold out",
    "인공지능", "반도체", "엔비디아", "메모리", "데이터센터", "고대역폭",
    "전력", "원전", "가스터빈", "패키징", "투자", "수주", "증설", "공급",
    "수요", "추론", "가동률", "토큰", "수주잔고",
    "半導体", "データセンター", "人工智能", "芯片", "数据中心", "算力", "台積電",
    "需要", "需求", "推論", "推理",
    "한화엔진", "4행정", "중속엔진", "힘센", "선박엔진", "조선", "hd현대중공업",
    "삼성중공업", "한화오션", "stx엔진", "lng선", "발전엔진", "가스엔진",
    "tempus", "템퍼스",
    "hyperscaler", "bond", "debt issuance", "credit rating", "leverage",
    "오라클", "oracle", "채권", "회사채", "신용등급", "부채", "자금조달",
    "amazon", "aws", "아마존", "microsoft", "마이크로소프트", "alphabet",
    "google", "구글", "meta", "메타",
    "subsidy", "tax credit", "chips act", "sovereign", "loan guarantee",
    "보조금", "세액공제", "정부 지원", "국가 지원", "정책금융", "소버린", "칩스법",
    "roi", "monetization", "depreciation", "bubble", "overcapacity", "overbuild",
    "write-down", "impairment", "downgrade",
    "수익화", "감가상각", "버블", "거품", "과잉투자", "과잉공급", "공급과잉",
    "손상차손", "등급 하향", "투자 회수",
]
EXCLUDE = [
    "할인", "쿠폰", "이벤트", "광고", "분양", "운세", "로또",
    "casino", "porn", "coupon", "discount", "giveaway",
]
SOURCE_BLACKLIST = [
    "인벤", "루리웹", "디스이즈게임", "게임메카", "디스패치", "위키트리",
    "인사이트", "허프포스트",
]

BOTTLENECK = [
    "hbm", "cowos", "packaging", "gpu", "dram", "nand", "optical", "transceiver",
    "power", "grid", "turbine", "substation", "cooling", "전력", "송전", "변전",
    "가스터빈", "냉각", "패키징", "고대역폭", "capacity", "shortage", "증설", "감산",
    "부족", "품귀", "수급", "병목", "tight", "sold out", "공급난", "대란",
    "리드타임", "lead time", "backlog", "수주잔고", "capex", "설비투자",
    "전력난", "부족분", "공급부족", "수급난", "물량부족", "증설 경쟁",
]
DEMAND_SIGNALS = [
    "ai demand", "inference demand", "token usage", "compute demand",
    "sold out", "utilization", "backlog", "ai revenue", "ai adoption",
    "ai workload", "enterprise ai", "ai agent", "guidance",
    "ai 수요", "추론 수요", "연산 수요", "캐파 부족", "gpu 부족", "가동률",
    "ai 매출", "수주잔고", "ai 에이전트", "기업용 ai", "需求", "需要",
    "수요 급증", "수요 폭증", "토큰 사용", "토큰 소비", "추론 폭증",
    "연산 폭증", "ai 채택", "도입 확대", "트래픽 급증", "사용량 폭증",
    "컴퓨팅 수요", "데이터센터 수요",
    "gigawatt", "기가와트", "gw", "double compute", "computing capacity",
    "컴퓨팅 인프라", "인프라 확대", "인프라 두 배", "capacity expansion",
    "용량 확대", "용량 두 배",
]
# 논제 무효화 신호 — 놓치면 안 되는 최우선 경보 (+5 유지)
THESIS_ALERT = [
    "capex cut", "capex reduction", "spending cut", "guidance cut",
    "lower capex", "reduce capex", "slash spending", "pause construction",
    "cancel order", "order cancellation", "oversupply", "hbm price decline",
    "hbm asp", "asp decline", "memory price fall", "downgrade", "write-down",
    "impairment", "overcapacity", "overbuild", "ai bubble", "bubble burst",
    "data center delay", "data center cancel", "funding squeeze",
    "struggling to raise", "failed to raise", "unable to raise",
    "capex 축소", "설비투자 축소", "설비투자 삭감", "가이던스 하향",
    "투자 축소", "투자 보류", "발주 취소", "주문 취소", "공급과잉", "과잉공급",
    "가격 하락", "asp 하락", "신용등급 하향", "손상차손", "감액",
    "버블 붕괴", "거품 붕괴", "과잉투자", "데이터센터 취소", "데이터센터 연기",
    "착공 연기", "자금조달 난항", "조달 실패", "조달 차질",
]
# [v2.7] 정부/국가 지원 신호 +5 → +3 (점수 인플레 축소)
POLICY_SIGNALS = [
    "subsidy", "subsidies", "tax credit", "tax break", "chips act",
    "government support", "state support", "sovereign ai", "national ai",
    "government funding", "loan guarantee", "executive order", "export control",
    "보조금", "세액공제", "정부 지원", "국가 지원", "정책금융", "정책 지원",
    "소버린 ai", "국가 ai", "칩스법", "특별법", "대출 보증", "수출 통제",
    "정부 투자", "국비", "재정 지원",
]
# [v2.7] ROI/수익성 논쟁 신호 +5 → +3
ROI_SIGNALS = [
    "return on investment", "monetization", "payback period",
    "depreciation", "ai revenue growth", "unprofitable", "burn rate",
    "free cash flow", "capex to revenue",
    "투자 회수", "수익화", "수익성", "감가상각", "잉여현금흐름", "회수 기간",
    "적자", "흑자 전환",
]


# ───────────────────────── 유틸 ─────────────────────────
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def prune_seen(seen):
    cutoff = (now_utc() - datetime.timedelta(days=SEEN_RETENTION_DAYS)).timestamp()
    return {k: v for k, v in seen.items() if v.get("ts", 0) >= cutoff}


def has_korean(t):
    return bool(re.search(r"[가-힣]", t or ""))


def norm_title(title):
    t = html.unescape(title or "")
    t = re.sub(r"\s*[-|·]\s*[^-|·]+$", "", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[\[\](){}<>·…“”\"'’‘|!?.,~―—\-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def title_key(title):
    compact = re.sub(r"\s+", "", norm_title(title))
    return hashlib.md5(compact.encode("utf-8")).hexdigest()


def _tokens(s):
    return {w for w in norm_title(s).split() if len(w) >= 2}


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


SAME_EVENT_ACTORS = {
    "이재용", "이재명", "젠슨", "황", "올트먼", "머스크", "저커버그", "아모데이",
    "삼성전자", "하이닉스", "sk하이닉스", "마이크론", "micron", "엔비디아", "nvidia",
    "tsmc", "인텔", "openai", "anthropic", "삼성", "구글", "메타", "broadcom",
    "amd", "퀄컴", "qualcomm",
    "amazon", "아마존", "aws", "microsoft", "마이크로소프트", "alphabet",
    "google", "meta", "oracle", "오라클",
}
SAME_EVENT_ACTIONS = {
    "점검", "현장", "방문", "찾았다", "공급계약", "계약", "수주", "체결",
    "인수", "투자", "증설", "착공", "양산", "출시", "발표", "공개", "돌파",
    "선정", "협력", "파트너십", "상향", "하향", "목표가", "목표주가",
    "1위", "등극", "제치고", "추월",
    "채권", "회사채", "bond", "debt", "발행", "조달", "issuance",
    "보조금", "세액공제", "subsidy",
}
ACTION_SYNONYMS = [
    {"점검", "현장", "방문", "찾았다", "둘러", "행보"},
    {"공급계약", "계약", "수주", "체결", "납품", "공급"},
    {"인수", "합병", "지분", "m&a"},
    {"투자", "증설", "착공", "신설", "구축", "확대"},
    {"양산", "출시", "공개", "발표", "선보", "상용화"},
    {"1위", "등극", "제치고", "추월", "역전", "왕좌"},
    {"상향", "하향", "목표가", "목표주가", "투자의견"},
    {"채권", "회사채", "bond", "debt", "발행", "조달", "issuance"},
    {"보조금", "세액공제", "subsidy", "지원"},
]

TOPIC_KEYWORDS = {
    "실적": {"실적", "매출", "이익", "어닝", "earnings", "revenue", "분기", "quarter",
             "사상 최대", "record", "가이던스", "guidance", "전망", "최고", "급등",
             "깜짝", "예상치", "경신", "호조", "어닝서프라이즈"},
    "수주": {"수주", "계약", "공급계약", "발주", "contract", "deal", "수주잔고", "backlog"},
    "증설": {"증설", "투자", "공장", "capex", "설비", "착공", "클러스터", "ipo", "상장", "adr"},
    "주가": {"주가", "급락", "목표가", "신고가", "shares", "stock", "rally", "surge"},
    "hbm": {"hbm", "고대역폭", "메모리", "dram", "슈퍼사이클", "supercycle", "공급 부족", "공급부족"},
    "전력": {"전력", "데이터센터", "전력망", "원전", "가스터빈", "power", "grid", "datacenter"},
    "인수": {"인수", "합병", "m&a", "acquisition", "지분"},
    "자금조달": {"채권", "회사채", "bond", "debt", "발행", "조달", "issuance",
               "capital raise", "equity sale", "financing"},
    "칩공개": {"칩 공개", "칩공개", "프로세서", "드래곤플라이", "cpu 공개", "신제품"},
    "정책지원": {"보조금", "세액공제", "subsidy", "tax credit", "chips act",
               "칩스법", "소버린", "sovereign", "정책금융", "loan guarantee"},
    "roi버블": {"roi", "버블", "거품", "bubble", "과잉투자", "overcapacity",
              "수익화", "monetization", "감가상각", "depreciation"},
}


def _action_group(action_set):
    groups = set()
    for a in action_set:
        for gi, grp in enumerate(ACTION_SYNONYMS):
            if a in grp:
                groups.add(gi)
    return groups


def _key_entities(s):
    toks = set(norm_title(s).split())
    actors = {a for a in SAME_EVENT_ACTORS if a in s.lower() or a in toks}
    actions = {a for a in SAME_EVENT_ACTIONS if a in s.lower() or a in toks}
    return actors, actions


def _topic_groups(text):
    low = text.lower()
    return {t for t, kws in TOPIC_KEYWORDS.items() if any(k in low for k in kws)}


def is_similar(a, b):
    if SequenceMatcher(None, a, b).ratio() >= SIMILARITY_THRESHOLD:
        return True
    if _jaccard(a, b) >= 0.45:
        return True

    aa_actor, aa_action = _key_entities(a)
    bb_actor, bb_action = _key_entities(b)
    shared_actor = aa_actor & bb_actor

    shared_group = _action_group(aa_action) & _action_group(bb_action)
    if shared_actor and shared_group:
        if _jaccard(a, b) >= 0.12:
            return True

    if shared_actor:
        shared_topic = _topic_groups(a) & _topic_groups(b)
        if shared_topic and _jaccard(a, b) >= 0.08:
            return True

    return False


def passes_filter(title, summary):
    text = f"{title} {summary}".lower()
    if any(k in text for k in EXCLUDE):
        return False
    return any(k in text for k in INCLUDE)


def base_score(title, summary):
    text = f"{title} {summary}".lower()
    score = 0
    strong = [
        "capex", "billion", "investment", "funding", "수주", "계약", "조 원",
        "억 달러", "acquisition", "deal", "contract", "투자", "발주", "증설",
        "fda", "approval", "승인", "양산", "출시", "공급계약", "파트너십",
        "partnership", "돌파", "신고가", "목표가", "상향", "수주잔고",
        "ipo", "인수", "합병", "기록적", "사상 최대", "record",
        "collaboration", "협업", "제휴", "협력", "선정", "채택", "공급",
        "launch", "unveil", "secures", "wins", "공개",
        "mass production", "into production", "in-house chip", "자체 칩",
        "자체 개발", "custom chip",
    ]
    for kw in strong:
        if kw in text:
            score += 3
            break
    if any(k in text for k in BOTTLENECK):
        score += 2
    if any(k in text for k in DEMAND_SIGNALS):
        score += 3
    # 논제 무효화 신호는 최우선 (+5 유지) — 단독으로도 전송 기준 통과
    if any(k in text for k in THESIS_ALERT):
        score += 5
    # [v2.7] 정부/국가 지원 신호 +5 → +3
    if any(k in text for k in POLICY_SIGNALS):
        score += 3
    # [v2.7] ROI/수익성 논쟁 신호 +5 → +3 (단어경계 매칭으로 오탐 방지)
    if any(k in text for k in ROI_SIGNALS) or re.search(r"\broi\b", text):
        score += 3
    watchlist = [
        "한화엔진", "tempus", "템퍼스", "hd현대중공업", "삼성중공업", "한화오션",
        "stx엔진", "sk하이닉스", "하이닉스", "삼성전자", "엔비디아", "nvidia",
        "tsmc", "micron", "마이크론", "4행정", "힘센", "조선",
        "hyperscaler", "오라클", "oracle", "bond", "회사채", "채권", "cds",
        "credit rating", "신용등급", "leverage", "부채", "debt",
        "free cash flow", "잉여현금흐름",
        "meta", "메타", "broadcom", "브로드컴", "mtia", "샌디스크", "sandisk",
    ]
    if any(k in text for k in watchlist):
        score += 2
    for kw in ["주가", "시총", "장중", "마감", "shares", "stock rises", "stock falls",
               "급등", "급락", "보합", "상한가", "하한가", "약세", "강세",
               "오늘의", "특징주", "이 시각"]:
        if kw in text:
            score -= 3
            break
    for kw in ["20대", "30대", "2030", "청년", "개미", "서학개미", "동학개미",
               "투자했을까", "순매수 1위", "인기 종목", "매수 상위", "계좌 잔고",
               "retail investor", "young investor"]:
        if kw in text:
            score -= 5
            break
    for kw in ["방한기", "3박4일", "3박 4일", "먹방", "치맥", "인증샷", "팬미팅",
               "사인회", "왜 이렇게 바빠", "일거수일투족", "동선", "화제",
               "만난 이유는", "만났다", "회동", "포착", "목격"]:
        if kw in text:
            score -= 4
            break
    return score


def entry_age_hours(entry):
    tm = entry.get("published_parsed")
    if not tm:
        return None
    try:
        published = datetime.datetime(*tm[:6], tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    return (now_utc() - published).total_seconds() / 3600.0


def published_age_hours(pub_iso):
    """저장된 published ISO 문자열로 나이(시간) 계산. 파싱 실패 시 None."""
    if not pub_iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(pub_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (now_utc() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def is_stale_item(it):
    """전송 직전 나이 재검사. queue 이월분 포함."""
    age = published_age_hours(it.get("published", ""))
    if age is None:
        return True
    limit = PEOPLE_WINDOW_HOURS if it.get("is_people") else NEWS_WINDOW_HOURS
    return age > limit + 1


# ───────────────────────── [v2.7] 실제 발행일 검증 ─────────────────────────
def url_date_age_hours(url):
    """[v2.7] URL 경로에 박힌 날짜(/2026/06/18/, 2026-06-18, 20260618)로 나이 계산.
    구글 재색인으로 published가 조작된 옛 기사를 URL로 잡아낸다."""
    if not url:
        return None
    m = re.search(r"/(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})(?:[/\-?#]|$)", url)
    if not m:
        m = re.search(r"[/\-_](20\d{2})(\d{2})(\d{2})[/\-_.]", url)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        dt = datetime.datetime(y, mo, d, tzinfo=datetime.timezone.utc)
        return (now_utc() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def real_date_age_hours(real_date_str):
    """[v2.7] trafilatura가 추출한 원문 발행일("2026-06-18" 등)로 나이 계산."""
    if not real_date_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(real_date_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (now_utc() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def source_name(entry):
    if hasattr(entry, "source") and getattr(entry.source, "title", None):
        return entry.source.title
    return urlparse(entry.get("link", "")).netloc.replace("www.", "")


def clean_summary(raw):
    if not raw:
        return ""
    s = re.sub(r"<[^>]+>", " ", raw)
    s = html.unescape(s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


# ───────────────────────── 본문 추출 ─────────────────────────
def resolve_final_url(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA},
                         timeout=BODY_FETCH_TIMEOUT, allow_redirects=True)
        return r.url, r.text
    except Exception:
        return url, None


def fetch_article_body(url):
    """[v2.7] (본문, 실제발행일나이(시간) or None) 튜플 반환.
    실제발행일나이 = 원문 메타태그 발행일과 URL 날짜 중 더 오래된 쪽."""
    if not (FETCH_BODY and _HAS_TRAFI):
        return "", None
    final_url, prefetched = resolve_final_url(url)
    body = ""
    real_age = None
    try:
        downloaded = prefetched
        if not downloaded:
            downloaded = trafilatura.fetch_url(final_url)
        if downloaded:
            body = trafilatura.extract(
                downloaded, include_comments=False, include_tables=False,
                no_fallback=False, favor_precision=True,
            ) or ""
            # [v2.7] 원문 HTML의 article:published_time 등 실제 발행일 추출
            try:
                meta = trafilatura.extract_metadata(downloaded)
                if meta and getattr(meta, "date", None):
                    real_age = real_date_age_hours(meta.date)
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] body extract fail: {e}")
        body = ""
    # [v2.7] URL 날짜 보조 검증 — 둘 중 더 오래된 쪽 채택
    ua_age = url_date_age_hours(final_url)
    if ua_age is not None:
        real_age = ua_age if real_age is None else max(real_age, ua_age)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:BODY_MAX_CHARS], real_age


# ───────────────────────── Gemini ─────────────────────────
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_gemini_state = {"calls": 0, "disabled": False, "last": 0.0, "consec_fail": 0}


def gemini_analyze(title, summary, source, body="", _model=None, _is_fallback=False):
    if not GEMINI_KEY or _gemini_state["disabled"]:
        return None
    if _gemini_state["calls"] >= GEMINI_MAX_CALLS_PER_RUN:
        _gemini_state["disabled"] = True
        print("[INFO] Gemini 회당 호출 상한 도달 → 이후 제목+링크만")
        return None
    if _gemini_state["consec_fail"] >= GEMINI_CONSEC_FAIL_STOP:
        _gemini_state["disabled"] = True
        print(f"[INFO] Gemini 연속 실패 {GEMINI_CONSEC_FAIL_STOP}회 → 이후 제목+링크만")
        return None

    elapsed = time.time() - _gemini_state["last"]
    if elapsed < GEMINI_MIN_INTERVAL:
        time.sleep(GEMINI_MIN_INTERVAL - elapsed)

    content_for_model = body.strip() if body.strip() else summary
    content_label = "본문" if body.strip() else "요약(본문 추출 실패)"
    today_kst = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")

    prompt = (
        "너는 AI·반도체·메모리·데이터센터·전력·AI수요 산업 분석가다. "
        "아래 기사를 한국 투자자 관점에서 분석하라. 과장/추측 금지, 사실 기반.\n"
        f"오늘 날짜는 {today_kst}(KST)다.\n"
        "원문이 영어/중국어/일본어/대만어(번체)든 모두 한국어로 옮겨라.\n"
        "[중요] 기사 내용이 명백히 수 주~수 개월 이상 지난 과거 사건의 보도이거나, "
        "이미 널리 알려진 옛 뉴스의 재탕이면 반드시 중요도를 C로 판정하라.\n"
        "[중요] 단순 주가 등락, 리테일 투자 동향, 인물 동정/가십 기사도 C로 판정하라.\n"
        "아래 7개 라벨 형식으로만 답하라. 각 줄 라벨 그대로, 값만 채워라. 다른 말 금지.\n"
        "제목: (한국어 번역 제목, 한 줄)\n"
        "요약: (반드시 5~7문장의 한국어. 기사 본문의 사실/숫자/맥락을 충실히 담되 "
        "한 문단으로 자연스럽게. 추측 금지)\n"
        "중요도: (S=산업구조 영향 / A=대규모 투자·계약·증설 / B=산업영향 존재 / C=참고용 중 하나)\n"
        "분야: (AI,GPU,HBM,DRAM,NAND,패키징,광통신,데이터센터,전력,원전,가스터빈,AI수요,"
        "정책지원,ROI수익성,자금조달 중 해당)\n"
        "병목: (악화 / 완화 / 무관 중 하나)\n"
        "유동성: (유입 / 유출 / 중립 중 하나)\n"
        "왜중요: (한 문장)\n\n"
        f"[원문 제목] {title}\n[출처] {source}\n[{content_label}]\n{content_for_model}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    active_model = _model or GEMINI_MODEL
    url = GEMINI_URL.format(model=active_model)

    for attempt in range(GEMINI_RETRY_MAX + 1):
        try:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=REQUEST_TIMEOUT)
            _gemini_state["calls"] += 1
            _gemini_state["last"] = time.time()

            if r.status_code == 200:
                data = r.json()
                cand = data["candidates"][0]
                finish = cand.get("finishReason", "")
                parts = cand.get("content", {}).get("parts", [])
                text = parts[0]["text"] if parts and "text" in parts[0] else ""
                if not text.strip():
                    print(f"[WARN] Gemini empty (finish={finish}) → 폴백")
                    _gemini_state["consec_fail"] += 1
                    return None
                _gemini_state["consec_fail"] = 0
                return _parse_lines(text)

            if r.status_code == 429:
                _gemini_state["disabled"] = True
                print("[WARN] Gemini 429(한도) → 이후 제목+링크만")
                return None

            if r.status_code in (500, 502, 503, 504):
                _gemini_state["consec_fail"] += 1
                if attempt < GEMINI_RETRY_MAX:
                    wait = GEMINI_RETRY_BASE * (2 ** attempt) + random.uniform(0, 1.0)
                    print(f"[WARN] Gemini {r.status_code} 과부하 "
                          f"(재시도 {attempt+1}/{GEMINI_RETRY_MAX}, {wait:.1f}초 후)")
                    time.sleep(wait)
                    continue
                if not _is_fallback and GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != active_model:
                    print(f"[WARN] Gemini {r.status_code} 재시도 소진 → 폴백 모델"
                          f"({GEMINI_FALLBACK_MODEL})로 전환 시도")
                    _gemini_state["consec_fail"] = 0
                    return gemini_analyze(title, summary, source, body=body,
                                          _model=GEMINI_FALLBACK_MODEL, _is_fallback=True)
                print(f"[WARN] Gemini {r.status_code} 재시도/폴백 소진 → 이 기사만 제목+링크")
                return None

            print(f"[WARN] Gemini {r.status_code}: {r.text[:200]} → 폴백")
            return None

        except requests.exceptions.Timeout:
            _gemini_state["consec_fail"] += 1
            if attempt < GEMINI_RETRY_MAX:
                wait = GEMINI_RETRY_BASE * (2 ** attempt) + random.uniform(0, 1.0)
                print(f"[WARN] Gemini timeout (재시도 {attempt+1}/{GEMINI_RETRY_MAX}, "
                      f"{wait:.1f}초 후)")
                time.sleep(wait)
                continue
            print("[WARN] Gemini timeout 재시도 소진 → 이 기사만 제목+링크")
            return None
        except Exception as e:
            print(f"[WARN] Gemini fail: {e} → 폴백")
            _gemini_state["consec_fail"] += 1
            return None
    return None


def _parse_lines(text):
    label_map = {"제목": "title_ko", "요약": "summary_ko", "중요도": "grade",
                 "분야": "sector", "병목": "bottleneck", "유동성": "liquidity",
                 "왜중요": "why"}
    labels = list(label_map.keys())
    out = {}
    cur_key = None
    buf = []

    def flush():
        if cur_key and buf:
            out[label_map[cur_key]] = " ".join(x.strip() for x in buf).strip()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = None
        for lb in labels:
            if line.startswith(lb + ":") or line.startswith(lb + "："):
                matched = lb
                break
        if matched:
            flush()
            cur_key = matched
            v = line.split(":", 1)[-1] if ":" in line else line.split("：", 1)[-1]
            buf = [v.strip()]
        else:
            if cur_key:
                buf.append(line)
    flush()

    if out.get("grade"):
        g = out["grade"].strip().upper()[:1]
        out["grade"] = g if g in "SABC" else ""
    return out if (out.get("title_ko") or out.get("summary_ko")) else None


# ───────────────────────── 수집 ─────────────────────────
def collect():
    items = []
    seen_titles = []

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

            src = source_name(entry)
            if any(b in src for b in SOURCE_BLACKLIST) or \
               any(b in title for b in SOURCE_BLACKLIST):
                continue

            age = entry_age_hours(entry)
            if age is None:
                continue
            limit = PEOPLE_WINDOW_HOURS if is_people else NEWS_WINDOW_HOURS
            if age > limit + 1:
                continue

            # [v2.7] 1차 방어: URL 경로 날짜로 재색인된 옛 기사 조기 차단
            u_age = url_date_age_hours(link)
            if u_age is not None and u_age > STALE_HARD_LIMIT_H:
                continue

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
            tm = entry.get("published_parsed")
            if tm:
                try:
                    pub_iso = datetime.datetime(*tm[:6],
                        tzinfo=datetime.timezone.utc).isoformat()
                except Exception:
                    pub_iso = ""

            sc = base_score(title, raw_sum)
            if is_people:
                sc += 3

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


def dedupe_against_seen(items, seen):
    seen_norm = [v["ntitle"] for v in seen.values() if "ntitle" in v]
    out = []
    for it in items:
        if title_key(it["title"]) in seen:
            continue
        if any(is_similar(it["ntitle"], s) for s in seen_norm):
            continue
        out.append(it)
    return out


# ───────────────────────── 텔레그램 ─────────────────────────
def esc(s):
    return html.escape(s or "")


def tg_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"[ERROR] telegram {r.status_code}: {r.text[:120]}")
    return r.status_code == 200


GRADE_EMOJI = {"S": "🔴 S", "A": "🟠 A", "B": "🟡 B", "C": "⚪ C"}


def build_full(it, a):
    title_ko = a.get("title_ko") or it["title"]
    grade = GRADE_EMOJI.get(str(a.get("grade", "")).upper().strip(), "")
    lines = []
    if grade:
        lines.append(f"{grade}")
    lines.append(f"<b>{esc(title_ko)}</b>")
    if a.get("summary_ko"):
        lines.append(esc(a["summary_ko"]))
    meta = []
    if a.get("sector"):
        meta.append(f"분야 {esc(a['sector'])}")
    if a.get("bottleneck"):
        meta.append(f"병목 {esc(a['bottleneck'])}")
    if a.get("liquidity"):
        meta.append(f"유동성 {esc(a['liquidity'])}")
    if meta:
        lines.append("· " + " | ".join(meta))
    if a.get("why"):
        lines.append(f"💡 {esc(a['why'])}")
    src = f" · {esc(it['source'])}" if it["source"] else ""
    lines.append(f'🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}')
    return "\n".join(lines)


_gt_state = {"obj": None, "disabled": False}
_gt_cache = {}


def has_chinese(t):
    return bool(re.search(r"[\u4e00-\u9fff]", t or ""))


def google_translate_ko(text):
    if not text or not text.strip():
        return text
    if has_korean(text):
        return text
    if _gt_state["disabled"]:
        return text
    if text in _gt_cache:
        return _gt_cache[text]
    if _gt_state["obj"] is None:
        try:
            from deep_translator import GoogleTranslator
            _gt_state["obj"] = GoogleTranslator(source="auto", target="ko")
            _gt_state["zh"] = GoogleTranslator(source="zh-CN", target="ko")
        except Exception as e:
            print(f"[WARN] google translate init fail: {e}")
            _gt_state["disabled"] = True
            return text
    snippet = text[:4500]
    try:
        out = _gt_state["obj"].translate(snippet)
        if out and out.strip() and out.strip() != snippet.strip():
            _gt_cache[text] = out
            time.sleep(0.4)
            return out
    except Exception as e:
        print(f"[WARN] google translate(auto) fail: {e}")
    if has_chinese(snippet) and _gt_state.get("zh"):
        try:
            out = _gt_state["zh"].translate(snippet)
            if out and out.strip():
                _gt_cache[text] = out
                time.sleep(0.4)
                return out
        except Exception as e:
            print(f"[WARN] google translate(zh) fail: {e}")
    return text


def build_min(it):
    title = it["title"]
    if not has_korean(title) or has_chinese(title):
        translated = google_translate_ko(title)
        if translated:
            title = translated
    src = f" · {esc(it['source'])}" if it["source"] else ""
    return f'<b>{esc(title)}</b>\n🔗 <a href="{esc(it["link"])}">기사 보기</a>{src}'


# ───────────────────────── news.json ─────────────────────────
def make_news_item(it, a):
    if a:
        title = a.get("title_ko") or it["title"]
        summary = a.get("summary_ko") or ""
        tail = []
        if a.get("sector"):
            tail.append(f"[분야 {a['sector']}]")
        if a.get("bottleneck"):
            tail.append(f"[병목 {a['bottleneck']}]")
        if a.get("liquidity"):
            tail.append(f"[유동성 {a['liquidity']}]")
        if a.get("why"):
            tail.append(f"왜중요: {a['why']}")
        if tail:
            summary = (summary + " " + " ".join(tail)).strip()
    else:
        title = it["title"]
        if not has_korean(title) or has_chinese(title):
            t = google_translate_ko(title)
            if t:
                title = t
        summary = it.get("summary", "")
        if summary and not has_korean(summary):
            summary = google_translate_ko(summary)
    return {
        "title": title,
        "url": it["link"],
        "source": it.get("source", ""),
        "published": it.get("published", ""),
        "summary": summary,
    }


def save_news_json(new_items):
    prev = load_json(NEWS_FILE, {"updated": "", "items": []})
    prev_items = prev.get("items", []) if isinstance(prev, dict) else []
    seen_urls = {x.get("url") for x in new_items if x.get("url")}
    merged = new_items + [x for x in prev_items if x.get("url") not in seen_urls]
    merged = merged[:NEWS_MAX_ITEMS]
    save_json(NEWS_FILE, {
        "updated": now_utc().isoformat(),
        "items": merged,
    })
    print(f"[INFO] news.json 저장: 신규 {len(new_items)}건 + 기존 → 총 {len(merged)}건")


# ───────────────────────── 메인 ─────────────────────────
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("[FATAL] TELEGRAM 토큰/챗ID 없음")

    seen = prune_seen(load_json(SEEN_FILE, {}))
    queue = load_json(QUEUE_FILE, [])

    fresh = collect()
    fresh = dedupe_against_seen(fresh, seen)

    pool = queue + fresh

    before_stale = len(pool)
    pool = [it for it in pool if not is_stale_item(it)]
    if before_stale != len(pool):
        print(f"[INFO] 오래된 기사 폐기: {before_stale - len(pool)}건")

    uniq, seen_nt = [], []
    for it in pool:
        if any(is_similar(it["ntitle"], s) for s in seen_nt):
            continue
        seen_nt.append(it["ntitle"])
        uniq.append(it)
    uniq.sort(key=lambda x: x.get("score", 0), reverse=True)

    before = len(uniq)
    uniq = [it for it in uniq if it.get("score", 0) >= MIN_SCORE_TO_SEND]
    print(f"[INFO] 중요도 필터: {before}건 → {len(uniq)}건 (기준 {MIN_SCORE_TO_SEND}점 이상)")

    if not uniq:
        print("[INFO] 신규 없음 - 전송 생략")
        save_json(SEEN_FILE, seen)
        save_json(QUEUE_FILE, [])
        return

    to_send = uniq[:MAX_SEND_PER_RUN]
    leftover = uniq[MAX_SEND_PER_RUN:]

    kst = (now_utc() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    header_sent = False

    sent = 0
    news_batch = []
    for it in to_send:
        if FETCH_BODY:
            body, real_age = fetch_article_body(it["link"])
            time.sleep(BODY_FETCH_DELAY)
        else:
            body, real_age = "", None

        # [v2.7] 2차 방어: 원문 실제 발행일 / URL 날짜 기준 48시간 초과 시 폐기
        if real_age is not None and real_age > STALE_HARD_LIMIT_H:
            print(f"[SKIP] 재색인된 과거 기사 폐기({real_age:.0f}h): {it['title'][:50]}")
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
            continue

        a = gemini_analyze(it["title"], it["summary"], it["source"], body=body)

        # [v2.7] 3차 방어 + 노이즈 억제: C 차단, B는 고점수만 통과
        if a and a.get("grade") == "C":
            print(f"[SKIP] C등급 차단: {it['title'][:50]}")
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
            continue
        if a and a.get("grade") == "B" and it.get("score", 0) < GRADE_B_MIN_SCORE:
            print(f"[SKIP] B등급 저점수 차단({it.get('score', 0)}점): {it['title'][:50]}")
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
            continue

        # [v2.7] 헤더는 실제 전송할 기사가 확정된 뒤 1회만 전송
        if not header_sent:
            tg_send(f"📡 <b>AI·반도체 산업 브리핑</b>\n🗓 {kst} KST"
                    + (f" · 대기 {len(leftover)}건" if leftover else ""))
            time.sleep(SEND_DELAY)
            header_sent = True

        msg = build_full(it, a) if a else build_min(it)
        if tg_send(msg):
            sent += 1
            seen[title_key(it["title"])] = {"ntitle": it["ntitle"],
                                            "ts": now_utc().timestamp()}
            news_batch.append(make_news_item(it, a))
        time.sleep(SEND_DELAY)

    save_json(QUEUE_FILE, leftover[:50])
    save_json(SEEN_FILE, seen)
    if news_batch:
        save_news_json(news_batch)
    print(f"[DONE] {sent}건 전송, 이월 {len(leftover)}건, Gemini호출 {_gemini_state['calls']}")


if __name__ == "__main__":
    main()
