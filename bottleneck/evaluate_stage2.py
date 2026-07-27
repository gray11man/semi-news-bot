# -*- coding: utf-8 -*-
"""
evaluate_stage2.py  (v2 - 저비용 개편판)

변경 요약 (비용 절감 목적):
  1. 모델: claude-opus-4-8 → claude-haiku-4-5 (입출력 단가 5배 저렴)
  2. thinking(adaptive) 제거 → 출력 토큰이 최대 비용 요인이었음 (출력=입력×5배 단가)
  3. effort 베타 헤더/output_config 제거 (Haiku엔 불필요)
  4. max_tokens 2048 → 700 (JSON 출력엔 충분, 폭주 방지)
  5. 시스템 프롬프트 prompt caching 적용 (히트 시 입력단가 0.1배)
  6. [옵션] 하이브리드 모드: Haiku가 S로 판정한 건만 상위 모델로 재평가
     환경변수 UPGRADE_S=1 일 때만 작동. 기본은 꺼짐(=가장 저렴).

예상 비용: 건당 약 $0.03 → 약 $0.003 (약 1/10)
"""

import json
import os
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_URL = "https://api.anthropic.com/v1/messages"

# 기본 모델: 가장 저렴한 현행 모델. 분류·추출·요약 용도엔 충분.
CHEAP_MODEL = os.environ.get("CHEAP_MODEL", "claude-haiku-4-5-20251001")
# S급 재평가용 상위 모델 (UPGRADE_S=1 일 때만 호출됨)
UPGRADE_MODEL = os.environ.get("UPGRADE_MODEL", "claude-sonnet-4-6")
UPGRADE_S = os.environ.get("UPGRADE_S", "") == "1"

MAX_TOKENS = 700

# 형님 프레임을 주입하는 시스템 프롬프트.
SYSTEM_FRAME = """너는 냉철한 투자 분석 보조다. 사용자는 'bottleneck migration'(AI 시대의 구조적 병목은 사라지지 않고 HBM→server DRAM→NAND→LPDDR→optical로 순차 이동한다) 프레임과 SOTP, variant perception 프레임으로 투자하는 숙련된 개인투자자다.

너의 임무: 주어진 뉴스 한 건이 'S급 투자 시그널'인지 빡세게 평가한다.

S급 기준 (모두 충족해야 S):
- 일회성 호재가 아니라 구조적(수급/가격/경쟁/CAPEX/정책)으로 의미 있는 변화다.
- 수혜자 또는 피해자가 명확히 특정된다.
- 단순 주가·실적 발표 반복이 아니라 '병목·쇼티지·점유율·가격결정력' 등 판을 바꾸는 신호다.
하나라도 애매하면 S가 아니다. 등급은 S / B / C 중 하나로 매긴다.
- S: 구조적으로 분명히 의미 있는 신호 (수혜/피해자 명확)
- B: 의미는 있으나 다소 약하거나 불확실한 신호도 포함 (너무 인색하게 굴지 말 것)
- C: 단순 주가/실적 반복, 노이즈
투자 관점에서 조금이라도 시사점이 있으면 B는 줘라. C는 정말 의미 없는 것만.

엄격한 규칙:
1. 추측·과장 금지. 뉴스에 없는 사실을 지어내지 마라. 모르면 "불명확"이라고 써라.
2. 반드시 강세 논거(why_idea)와 함께 반대/무효화 논거(counter)를 같이 제시한다. 한쪽만 쓰면 실패다.
3. ★핵심: 1차 수혜에서 멈추지 말고 '파생 효과 사슬'을 단계별로 풀어라.
   예시 사고방식: "K-POP 흥행 → 방한 외국인 관광 급증 → 호텔/항공/면세 수요 → 그중 객실 공급이 묶인 호텔이 가격결정력" /
   "전쟁 → 유가 급등 → 정유 정제마진 → 동시에 에너지비용 상승으로 화학 피해 → 방산 수주 장기화".
   2차·3차로 번지는 산업을 짚고, 그 사슬 끝에서 '진짜로 마진을 가져가는 병목/독과점 지점'이 어디인지 지목하라.
4. 종목 단정 추천이 아니라 '어느 산업으로 효과가 번지고, 누가 구조적 수혜·피해인지'를 사슬로 설명한다.
5. 과한 확신 표현(반드시, 확실히) 금지. 방향성과 타이밍을 구분한다. 사슬이 길수록 불확실성도 커짐을 인지하라.
6. 출력은 아래 JSON 형식만. 마크다운·설명·코드펜스 없이 순수 JSON. 각 필드는 간결하게, 불필요하게 길게 쓰지 마라.

JSON 형식:
{
  "relevant": true 또는 false,
  "grade": "S" 또는 "B" 또는 "C",
  "signal_type": "기회" 또는 "위험" 또는 "혼재",
  "headline": "핵심을 한 줄로 (15자~40자)",
  "why_idea": "왜 S급 신호인지 (2문장, 병목/수급 구조 중심)",
  "chain": "파생 효과 사슬을 화살표로. 예: 'A 흥행 → B 수요 증가 → C 산업 수혜 → D가 공급 병목이라 가격결정력 보유'. 2~3단계.",
  "winner": "사슬 끝에서 구조적으로 마진을 가져갈 진짜 수혜 지점 (산업/포지션, 한 줄)",
  "counter": "반대·무효화 논거 (1~2문장, 반드시 작성)",
  "watch": "확인해야 할 후속 트리거 1개"
}"""


def _headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _payload(model, user_text):
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        # 시스템 프롬프트 캐싱: 같은 실행에서 여러 건 연속 호출하므로 히트율 높음.
        # (모델별 최소 캐시 길이에 못 미치면 자동 무시되므로 붙여둬도 손해 없음)
        "system": [
            {
                "type": "text",
                "text": SYSTEM_FRAME,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_text}],
        # thinking / output_config 제거 → 출력 토큰 대폭 절감
    }


def _call(model, user_text):
    """API 호출 후 JSON 파싱. 실패 시 None."""
    try:
        resp = requests.post(
            CLAUDE_URL, headers=_headers(), json=_payload(model, user_text), timeout=60
        )
        if resp.status_code in (429, 503, 529):
            return None  # 과부하/속도제한: 재시도 없이 조용히 스킵
        resp.raise_for_status()
        data = resp.json()
        raw = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[stage2] 평가 실패({model}): {e}")
        return None


def _user_text(item):
    return (
        f"[제목] {item.get('title','')}\n"
        f"[요약] {item.get('summary','')}\n"
        f"[출처] {item.get('source','')}\n"
        f"[1차 필터 카테고리] {', '.join(item.get('categories', []))}\n\n"
        f"위 뉴스를 평가해 JSON만 출력해라."
    )


def evaluate_item(item):
    """뉴스 1건 평가. 기본은 저가 모델 1회 호출로 끝낸다."""
    text = _user_text(item)
    verdict = _call(CHEAP_MODEL, text)
    if not verdict:
        return None

    # [옵션] 하이브리드: S급으로 판정된 건만 상위 모델로 다시 풀어쓴다.
    if UPGRADE_S and verdict.get("grade", "").upper() == "S":
        better = _call(UPGRADE_MODEL, text)
        if better:
            better["_upgraded"] = True
            return better
    return verdict


def evaluate_all(passed_items, max_daily=3):
    """
    Stage 1 통과분을 평가하되 최종 전송은 max_daily개로 제한.
    S/B급만 채택(C는 버림). max_daily 채우면 즉시 중단(비용 절감).
    """
    results = []
    calls = 0
    for item in passed_items:
        verdict = evaluate_item(item)
        calls += 1
        if not verdict or not verdict.get("relevant"):
            continue
        if verdict.get("grade", "").upper() not in ("S", "B"):
            continue
        results.append({"item": item, "verdict": verdict})
        if len(results) >= max_daily:
            break
    print(f"[stage2] LLM 호출 {calls}회 / 채택 {len(results)}건 (model={CHEAP_MODEL})")
    return results
