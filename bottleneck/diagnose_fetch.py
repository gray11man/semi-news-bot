"""조회 전용: python diagnose_fetch.py. Gemini/Telegram 호출 및 이력 변경 없음."""
import json
from collections import Counter
from fetch_news import fetch_news, FETCH_STATS

if __name__ == '__main__':
    items = fetch_news()
    print('\n--- 피드별 원본/제외 사유/최종 생존 ---')
    for row in FETCH_STATS:
        print(json.dumps(row, ensure_ascii=False))
    print('\n--- 소스별 ---')
    for source, count in Counter(it['source'] for it in items).most_common():
        print(count, source)
    print('\n--- 수집 제목 전체 ---')
    for it in items:
        print(f"[{it['feed_id']}] [{it['source']}] {it['title']}")
