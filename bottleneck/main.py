# -*- coding: utf-8 -*-
"""
main.py (단순화판)
fetch_news() → pick_critical() → telegram 전송
filter_stage1.py, signals.py, evaluate_stage2.py는 더 이상 사용하지 않음(삭제 가능).

[변경 사항]
  - pick_critical()이 None을 반환하면 "API 자체가 실패한 것"으로 간주,
    빈 리스트([])와 구분해서 로그 출력 (조용히 0건으로 넘어가지 않음)
"""
from fetch_news import fetch_news, mark_sent
from pick_headlines import pick_critical
from telegram_send import send_results


def run(news_items=None):
    if news_items is None:
        news_items = fetch_news()
    print(f"[fetch] {len(news_items)}건 수집")

    results = pick_critical(news_items)

    if results is None:
        # API 호출 자체가 실패한 경우 (크레딧 부족, 키 누락, HTTP 에러 등)
        # "오늘은 뉴스가 없다"와 절대 혼동되면 안 되므로 명확히 별도 표시
        print("=" * 50)
        print("⚠️  경고: pick 단계 API 호출 실패로 이번 회차는 스킵됩니다.")
        print("⚠️  '크리티컬 뉴스 0건'이 아니라 '판단 자체를 못한 것'입니다.")
        print("⚠️  위 [pick_headlines] 로그의 에러 메시지를 확인하세요.")
        print("=" * 50)
        return None

    print(f"[pick] {len(results)}건 선별")

    sent = send_results(results)
    print(f"[send] {sent}건 전송 완료")

    mark_sent(results)
    return results


if __name__ == "__main__":
    run()
