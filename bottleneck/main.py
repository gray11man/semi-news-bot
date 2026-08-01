# -*- coding: utf-8 -*-
"""
telegram_send.py
평가 통과한 아이디어를 '별도' 텔레그램 채널로 전송.
기존 뉴스봇과 다른 채팅방을 쓰도록 BOTTLENECK_CHAT_ID를 따로 둔다.

[변경] GitHub Actions에서는 실제 전송을 하지 않는다.
       (전송은 다른 트리거가 담당. GitHub은 수집·평가만.)
       - GITHUB_ACTIONS=true 는 GitHub Actions 러너에 자동 설정되는 환경변수.
       - 강제로 켜고 싶으면 FORCE_SEND=1 환경변수를 주면 전송한다.
"""
import os
import requests

TELEGRAM_TOKEN = os.environ.get("BOTTLENECK_TOKEN", "")
BOTTLENECK_CHAT_ID = os.environ.get("BOTTLENECK_CHAT_ID", "")

# GitHub Actions 여부 감지 (러너에 자동으로 GITHUB_ACTIONS=true 설정됨)
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
# 예외적으로 GitHub에서도 전송하고 싶을 때 FORCE_SEND=1
FORCE_SEND = os.environ.get("FORCE_SEND", "") == "1"
# 최종 전송 차단 여부
SEND_DISABLED = IS_GITHUB_ACTIONS and not FORCE_SEND

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
    # GitHub Actions에서는 실제 전송 차단
    if SEND_DISABLED:
        print("[telegram] GitHub Actions 환경 → 전송 생략")
        return False
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
    """평가 통과 전체를 전송. GitHub Actions면 전송 생략하고 건수만 리턴."""
    if SEND_DISABLED:
        print(f"[telegram] GitHub Actions 환경 → 전송 생략 (평가 통과 {len(results)}건)")
        return 0

    if not results:
        send_message("📭 오늘은 임계값을 넘는 크리티컬 신호 없음.")
        return 0
    sent = 0
    send_message(f"📡 <b>오늘의 병목·쇼티지·지정학 신호</b> ({len(results)}건)")
    for result in results:
        if send_message(format_message(result)):
            sent += 1
    return sent
