# -*- coding: utf-8 -*-
"""
main.py
전체 파이프라인:
  수집된 뉴스 → Stage1 룰 필터 → Stage2 LLM 평가 → 텔레그램 별도 채널 전송

기존 gray_man_bot에서 '수집·dedup된 뉴스 리스트'만 넘겨받으면 된다.
fetch_news()를 기존 봇의 수집 함수로 교체해서 쓰면 끝.
"""

from filter_stage1 import filter_news
from evaluate_stage2 import evaluate_all
from telegram_send import send_results
from signals import MAX_DAILY
from fetch_news import fetch_news   # 구글 뉴스 RSS 수집기 (미국+한국)


def run(news_items=None):
    if news_items is None:
        news_items = fetch_news()

    # Stage 1: 빡센 룰 필터
    passed = filter_news(news_items)
    print(f"[stage1] {len(news_items)}건 중 {len(passed)}건 통과")

    # Stage 2: LLM이 S급만 골라 최종 MAX_DAILY개로 압축
    results = evaluate_all(passed, max_daily=MAX_DAILY)
    print(f"[stage2] S급 {len(results)}건 확정")

    sent = send_results(results)
    print(f"[send] {sent}건 전송 완료")
    return results


if __name__ == "__main__":
    run()
