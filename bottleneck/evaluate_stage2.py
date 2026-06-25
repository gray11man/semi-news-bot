# -*- coding: utf-8 -*-
"""
evaluate_stage2.py
Stage 2: Stage 1을 통과한 뉴스만 Claude Opus 4.8로 평가.
형님 프레임(bottleneck migration / SOTP / variant perception)으로
'아이디어 + 파생 사슬 + 진짜 수혜 + 반대 논거'를 항상 같이 생성.
할루시네이션 억제: 추측 금지, 모르면 모른다고, JSON만 출력.

비용 절감: effort='low'로 호출 (코멘트 용도엔 충분, 토큰 절약).
"""

import json
import os
import requests

# Anthropic API
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-opus-4-8"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
# 비용·품질 다이얼. 코멘트 용도엔 low로 충분. 더 깊게 원하면 "high"/"xhigh"/"max".
EFFORT = "low"

# 형님 프레임을 주입하는 시스템 프롬프트.
SYSTEM_FRAME = """너는 냉철한 투자 분석 보조다. 사용자는 'bottleneck migration'(AI 시대의 구조적 병목은 사라지지 않고 HBM→server DRAM→NAND→LPDDR→optical로 순차 이동한다) 프레임과 SOTP, variant perception 프레임으로 투자하는 숙련된 개인투자자다.

너의 임무: 주어진 뉴스 한 건이 'S급 투자 시그널'인지 빡세게 평가한다.

S급 기준 (모두 충족해야 S):
- 일회성 호재가 아니라 구조적(수급/가격/경쟁/CAPEX/정책)으로 의미 있는 변화다.
- 수혜자 또는 피해자가 명확히 특정된다.
- 단순 주가·실적 발표 반복이 아니라 '병목·쇼티지·점유율·가격결정력' 등 판을 바꾸는 신호다.
하나라도 애매하면 S가 아니다. 등급은 S / B / C 중 하나로 매긴다. 의심스러우면 B 이하로 내려라.

엄격한 규칙:
1. 추측·과장 금지. 뉴스에 없는 사실을 지어내지 마라. 모르면 "불명확"이라고 써라.
2. 반드시 강세 논거(why_idea)와 함께 반대/무효화 논거(counter)를 같이 제시한다. 한쪽만 쓰면 실패다.
3. ★핵심: 1차 수혜에서 멈추지 말고 '파생 효과 사슬'을 단계별로 풀어라.
   예시 사고방식: "K-POP 흥행 → 방한 외국인 관광 급증 → 호텔/항공/면세 수요 → 그중 객실 공급이 묶인 호텔이 가격결정력" /
   "전쟁 → 유가 급등 → 정유 정제마진 → 동시에 에너지비용 상승으로 화학 피해 → 방산 수주 장기화".
   2차·3차로 번지는 산업을 짚고, 그 사슬 끝에서 '진짜로 마진을 가져가는 병목/독과점 지점'이 어디인지 지목하라.
4. 종목 단정 추천이 아니라 '어느 산업으로 효과가 번지고, 누가 구조적 수혜·피해인지'를 사슬로 설명한다.
5. 과한 확신 표현(반드시, 확실히) 금지. 방향성과 타이밍을 구분한다. 사슬이 길수록 불확실성도 커짐을 인지하라.
6. 출력은 아래 JSON 형식만. 마크다운·설명·코드펜스 없이 순수 JSON.

JSON 형식:
{
  "relevant": true 또는 false,
  "grade": "S" 또는 "B" 또는 "C",
  "signal_type": "기회" 또는 "위험" 또는 "혼재",
  "headline": "핵심을 한 줄로 (15자~40자)",
  "why_idea": "왜 S급 신호인지 (2~3문장, 병목/수급 구조 중심)",
  "chain": "파생 효과 사슬을 화살표로. 예: 'A 흥행 → B 수요 증가 → C 산업 수혜 → D가 공급 병목이라 가격결정력 보유'. 2~3단계 이상.",
  "winner": "사슬 끝에서 구조적으로 마진을 가져갈 진짜 수혜 지점 (산업/포지션, 한 줄)",
  "counter": "반대·무효화 논거 (1~2문장, 반드시 작성)",
  "watch": "확인해야 할 후속 트리거 1개"
}"""


def evaluate_item(item):
    """
    Stage 1 통과 뉴스 1건을 Claude Opus 4.8로 평가. 실패 시 None.
    """
    user_text = (
        f"[제목] {item.get('title','')}\n"
        f"[요약] {item.get('summary','')}\n"
        f"[출처] {item.get('source','')}\n"
        f"[1차 필터 카테고리] {', '.join(item.get('categories', []))}\n\n"
        f"위 뉴스를 평가해 JSON만 출력해라."
    )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "effort-2025-11-24",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2048,
        "system": SYSTEM_FRAME,
        "messages": [{"role": "user", "content": user_text}],
        "thinking": {"type": "adaptive", "display": "omitted"},  # 사고과정 출력 생략, 결과만
        "output_config": {"effort": EFFORT},       # 비용 다이얼: low/medium/high/xhigh/max
    }

    try:
        resp = requests.post(CLAUDE_URL, headers=headers, json=payload, timeout=60)
        if resp.status_code in (429, 503, 529):
            # 과부하/속도제한: 기존 봇 정책대로 재시도 없이 조용히 스킵
            return None
        resp.raise_for_status()
        data = resp.json()
        # content는 블록 배열. text 블록만 모은다.
        raw = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return parsed
    except Exception as e:
        print(f"[stage2] 평가 실패: {e}")
        return None


def evaluate_all(passed_items, max_daily=5):
    """
    Stage 1 통과분을 평가하되, 최종 전송은 max_daily개로 빡세게 제한.
    relevant=true & grade='S' 인 것만 채택. max_daily 채우면 중단(비용 절감).
    """
    results = []
    for item in passed_items:
        verdict = evaluate_item(item)
        if not verdict or not verdict.get("relevant"):
            continue
        if verdict.get("grade", "").upper() != "S":
            continue
        results.append({"item": item, "verdict": verdict})
        if len(results) >= max_daily:
            break
    return results
