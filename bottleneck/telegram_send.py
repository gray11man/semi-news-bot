# -*- coding: utf-8 -*-
"""
telegram_send.py (단순화판)
pick_headlines.pick_critical()이 뽑은 제목만 텔레그램으로 전송.
GitHub Actions에서는 실제 전송을 하지 않는다.
       - GITHUB_ACTIONS=true 는 GitHub Actions 러너에 자동 설정되는 환경변수.
       - 강제로 켜고 싶으면 FORCE_SEND=1 환경변수를 주면 전송한다.
"""
import os
import requests

TELEGRAM_TOKEN = os.environ.get("BOTTLENECK_TOKEN", "")
BOTTLENECK_CHAT_ID = os.environ.get("BOTTLENECK_CHAT_ID", "")

IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
FORCE_SEND = os.environ.get("FORCE_SEND", "") == "1"
SEND_DISABLED = IS_GITHUB_ACTIONS and not FORCE_SEND


def format_message(result):
    """선별 1건을 텔레그램 메시지(HTML)로 포맷."""
    title = result.get("title", "")
    source = result.get("source", "")
    reason = result.get("reason", "")
    link = result.get("link", "")

    lines = [
        f"📌 <b>{title}</b>",
        f"<i>{source}</i>",
    ]
    if reason:
        lines.append(f"💡 {reason}")
    if link:
        lines.append(f"🔗 <a href=\"{link}\">원문</a>")
    return "\n".join(lines)


def send_message(text):
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
    """선별된 제목 전체를 전송. GitHub Actions면 전송 생략하고 0 리턴."""
    if SEND_DISABLED:
        print(f"[telegram] GitHub Actions 환경 → 전송 생략 (선별 {len(results)}건)")
        return 0
    if not results:
        send_message("📭 오늘은 크리티컬한 투자 뉴스 없음.")
        return 0

    sent = 0
    send_message(f"📡 <b>오늘의 크리티컬 투자 뉴스</b> ({len(results)}건)")
    for result in results:
        if send_message(format_message(result)):
            sent += 1
    return sent
