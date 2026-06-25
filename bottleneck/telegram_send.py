# -*- coding: utf-8 -*-
"""
telegram_send.py
평가 통과한 아이디어를 '별도' 텔레그램 채널로 전송.
기존 뉴스봇과 다른 채팅방을 쓰도록 BOTTLENECK_CHAT_ID를 따로 둔다.
"""

import os
import requests

# 새로 만든 아이디어봇 전용 토큰 (기존 뉴스봇 TELEGRAM_TOKEN과 분리)
TELEGRAM_TOKEN = os.environ.get("BOTTLENECK_TOKEN", "")
# 핵심: 기존 뉴스 채널과 분리된 '아이디어 전용' 채널 ID
BOTTLENECK_CHAT_ID = os.environ.get("BOTTLENECK_CHAT_ID", "")

SIGNAL_EMOJI = {"기회": "🟢", "위험": "🔴", "혼재": "🟡"}


def format_message(result):
    """평가 1건을 텔레그램 메시지(HTML)로 포맷."""
    item = result["item"]
    v = result["verdict"]
    emoji = SIGNAL_EMOJI.get(v.get("signal_type", ""), "⚪")
    cats = " · ".join(item.get("categories", []))
    url = item.get("url", "")

    lines = [
        f"{emoji} <b>{v.get('headline','')}</b>",
        f"<i>{cats}</i>  (score {item.get('score','')})",
        "",
        f"💡 <b>아이디어</b>\n{v.get('why_idea','')}",
        "",
        f"🔗 <b>파생 사슬</b>\n{v.get('chain','')}",
        "",
        f"🎯 <b>진짜 수혜</b>\n{v.get('winner','')}",
        "",
        f"⚠️ <b>반대 논거</b>\n{v.get('counter','')}",
        "",
        f"👁 <b>지켜볼 것</b>: {v.get('watch','')}",
    ]
    if url:
        lines.append(f"\n📰 <a href=\"{url}\">원문</a>")
    return "\n".join(lines)


def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": BOTTLENECK_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] 전송 실패: {e}")
        return False


def send_results(results):
    """평가 통과 전체를 전송. 아무것도 없으면 조용히 패스(또는 '오늘 없음' 한 줄)."""
    if not results:
        send_message("📭 오늘은 임계값을 넘는 크리티컬 신호 없음.")
        return 0
    sent = 0
    # 헤더
    send_message(f"📡 <b>오늘의 병목·쇼티지·지정학 신호</b> ({len(results)}건)")
    for result in results:
        if send_message(format_message(result)):
            sent += 1
    return sent
