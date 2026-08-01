# -*- coding: utf-8 -*-
"""
main.py (단순화판)
fetch_news() → pick_critical() → telegram 전송
filter_stage1.py, signals.py, evaluate_stage2.py는 더 이상 사용하지 않음(삭제 가능).
"""
from fetch_news import fetch_news, mark_sent
from pick_headlines import pick_critical
from telegram_send import send_results


def run(news_items=None):
    if news_items is None:
        news_items = fetch_news()
    print(f"[fetch] {len(news_items)}건 수집")

    results = pick_critical(news_items)
    print(f"[pick] {len(results)}건 선별")

    sent = send_results(results)
    print(f"[send] {sent}건 전송 완료")

    mark_sent(results)
    return results


if __name__ == "__main__":
    run()
