"""Telegram 전송. 성공이 확인된 개별 메시지만 콜백으로 영구 기록한다."""
import html
import os
import time
from urllib.parse import urlparse

import requests


def format_message(result):
    def esc(value, limit):
        return html.escape(str(value or "")[:limit], quote=True)

    title = esc(result.get("headline") or result.get("title", ""), 180)
    source = esc(result.get("source", ""), 80)
    sector = esc(result.get("sector") or result.get("sector_hint", ""), 50)

    lines = [f"📌 <b>{title}</b>"]
    if sector:
        lines.append(f"🏭 <b>{sector}</b>")
    if source:
        lines.append(f"<i>{source}</i>")
    if result.get("reason"):
        lines.append("💡 " + esc(result["reason"], 260))
    if result.get("evidence"):
        lines.append("근거: " + esc(result["evidence"], 220))

    link = result.get("link", "")
    if urlparse(link).scheme in ("http", "https"):
        lines.append(f'<a href="{html.escape(link, quote=True)}">원문</a>')
    return "\n".join(lines)


def send_message(text):
    if os.getenv("DRY_RUN") == "1":
        print("[telegram] DRY_RUN: 미전송")
        return False

    token = os.getenv("BOTTLENECK_TOKEN")
    chat = os.getenv("BOTTLENECK_CHAT_ID")
    if not token or not chat:
        print("[telegram] 토큰/채팅 ID 누락")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=(10, 25),
            )
            data = r.json()
        except (requests.RequestException, ValueError):
            # 수신 여부가 불명확할 때 즉시 재전송하면 중복 메시지가 생길 수 있으므로 중단.
            print("[telegram] 응답 확인 불가: 자동 즉시 재전송 중단")
            return False

        if r.status_code == 200 and data.get("ok") is True and data.get("result", {}).get("message_id"):
            return True

        if r.status_code == 429 or data.get("error_code") == 429:
            delay = data.get("parameters", {}).get("retry_after", 5)
            if isinstance(delay, (int, float)) and 0 <= delay <= 60 and attempt < 2:
                time.sleep(float(delay) + 0.5)
                continue

        print(f"[telegram] 전송 실패 HTTP {r.status_code}")
        return False
    return False


def send_results(results, on_sent=None):
    sent = []
    for result in results:
        if send_message(format_message(result)):
            if on_sent:
                on_sent([result])
            sent.append(result)
        time.sleep(1.1)
    return sent
