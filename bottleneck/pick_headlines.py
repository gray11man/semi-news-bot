# -*- coding: utf-8 -*-
"""
pick_headlines.py
파이프라인 단순화판: filter_stage1 / signals / evaluate_stage2(등급판) 전부 제거.
fetch_news()로 모은 기사 중 "투자에 크리티컬한 것"만 LLM이 한 번에 골라
제목(+ 왜 중요한지 한 줄, 링크)만 반환한다.

사용법:
    from pick_headlines import pick_critical
    from fetch_news import fetch_news
    results = pick_critical(fetch_news())
"""

import json
import os
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("PICK_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1200
MAX_PICK = int(os.environ.get("MAX_PICK", "3"))  # 실행당 최종 전송 상한 (진짜 크리티컬한 것만, 목표 아닌 상한)

SYSTEM = """너는 극도로 까다로운 투자 뉴스 게이트키퍼다. 사용자는 업종 불문 구조적 변화
(공급부족, 가격전환, 정책충격, 대규모 수주, 금리·환율 전환 등)를 컨센서스 형성 전에 잡으려는
숙련된 개인투자자다. 업종을 골고루 섞으려 하지 마라 — 업종은 결과일 뿐, 기준이 아니다.

판단 기준은 오직 하나: "이 뉴스가 실제로 포지션을 열거나, 닫거나, 재검토하게 만드는가?"
아래를 모두 통과해야만 채택한다:
1. 일회성이 아니라 구조적 변화(수급/가격/정책/경쟁구도)다.
2. 수혜 또는 피해가 특정 산업·기업에 명확히 걸린다.
3. 이미 다 아는 이야기의 반복이 아니라 새로운 정보다 (실적 발표 자체, 주가 등락 설명, 일반 시황은 탈락).
4. 지금 당장 몰라도 사는 데 지장 없다면 탈락. "알아두면 좋다" 수준은 전부 버려라.

애매하면 무조건 버린다. 하루에 아무것도 없으면 빈 리스트가 정답이다.
개수를 채우려는 압박을 갖지 마라 — {max_pick}개는 상한일 뿐 목표가 아니다.
0개, 1개인 날이 5개인 날보다 훨씬 흔해야 정상이다.

출력은 JSON 배열만. 마크다운, 설명, 코드펜스 없이 순수 JSON.
각 원소 형식:
{{"index": 원본 번호(정수), "reason": "왜 크리티컬한지 한 줄 (25자 내외)"}}
"""


def _headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _build_list_text(items):
    lines = []
    for i, it in enumerate(items):
        lines.append(f"{i}. [{it.get('source','')}] {it.get('title','')} — {it.get('summary','')[:120]}")
    return "\n".join(lines)


def pick_critical(news_items, max_pick=None):
    """뉴스 리스트를 받아 LLM이 크리티컬하다고 판단한 것만 골라 반환.
    반환: [{"title", "link", "source", "reason"}, ...]
    """
    if not news_items:
        return []

    max_pick = max_pick or MAX_PICK
    list_text = _build_list_text(news_items)
    system_text = SYSTEM.format(max_pick=max_pick)

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": list_text}],
    }

    try:
        resp = requests.post(CLAUDE_URL, headers=_headers(), json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        raw = raw.replace("```json", "").replace("```", "").strip()
        picks = json.loads(raw)
    except Exception as e:
        print(f"[pick_headlines] 실패: {e}")
        return []

    results = []
    for p in picks[:max_pick]:
        idx = p.get("index")
        if idx is None or not (0 <= idx < len(news_items)):
            continue
        it = news_items[idx]
        results.append({
            "title": it.get("title", ""),
            "link": it.get("link", ""),
            "source": it.get("source", ""),
            "reason": p.get("reason", ""),
        })
    print(f"[pick_headlines] {len(news_items)}건 중 {len(results)}건 선별")
    return results
