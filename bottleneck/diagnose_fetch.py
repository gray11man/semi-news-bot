"""조회 전용: python diagnose_fetch.py
Gemini/Telegram 호출 및 전송 이력 변경 없음.
"""
import json
from collections import Counter

from fetch_news import FETCH_STATS, fetch_news


if __name__ == "__main__":
    items = fetch_news()

    print("\n--- 피드별 원본/제외 사유/최종 생존 ---")
    for row in FETCH_STATS:
        printable = dict(row)
        printable["counts"] = dict(printable["counts"])
        print(json.dumps(printable, ensure_ascii=False))

    print("\n--- 업종별 최종 생존 ---")
    for sector, count in Counter(it.get("sector_hint", "종합") for it in items).most_common():
        print(f"{count:4d}  {sector}")

    print("\n--- 소스별 최종 생존 ---")
    for source, count in Counter(it["source"] for it in items).most_common():
        print(f"{count:4d}  {source}")

    print("\n--- 수집 제목 전체 ---")
    for it in items:
        print(
            f"[{it['feed_id']:02d}] [{it.get('sector_hint','')}] "
            f"[{it['source']}] {it['title']}"
        )
