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
MAX_PICK = int(os.environ.get("MAX_PICK", "5"))  # 하루 최종 전송 상한

SYSTEM = """너는 냉철한 투자 뉴스 큐레이터다. 사용자는 반도체/조선/방산/전력·에너지/
바이오/원자재 등 전업종을 넓게 보며 구조적 변화(공급부족, 가격전환, 정책충격, 대규모 수주,
금리·환율 전환 등)를 먼저 잡으려는 개인투자자다.

아래 뉴스 목록(번호가 매겨져 있음) 중에서 "투자 의사결정에 실제로 영향을 줄 만큼 중요한" 것만
최대 {max_pick}개 골라라. 시황 반복, 단순 실적 발표, 잡다한 가십, 홍보성 기사는 제외한다.
아무리 봐도 크리티컬한 게 없으면 빈 리스트를 반환해도 된다. 억지로 개수를 채우지 마라.

출력은 JSON 배열만. 마크다운, 설명, 코드펜스 없이 순수 JSON.
각 원소 형식:
{{"index": 원본 번호(정수), "reason": "왜 중요한지 한 줄 (25자 내외)"}}
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
