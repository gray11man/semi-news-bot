import os
import sys
from fetch_news import fetch_news, mark_sent
from pick_headlines import pick_critical
from telegram_send import send_results


def run(news_items=None):
    if os.getenv("DRY_RUN") != "1":
        missing = [k for k in ("GEMINI_KEY", "BOTTLENECK_TOKEN", "BOTTLENECK_CHAT_ID") if not os.getenv(k)]
        if missing:
            raise RuntimeError("환경변수 누락: " + ", ".join(missing))
    items = fetch_news() if news_items is None else news_items
    print(f"[fetch] {len(items)}건")
    results = pick_critical(items)
    if results is None:
        raise RuntimeError("API 판단 실패 — 뉴스 0건이 아닙니다")
    print(f"[pick] {len(results)}건")
    if os.getenv("DRY_RUN") == "1":
        for result in results:
            print(result.get("headline", result["title"]), result["reason"])
        print("[dry-run] 전송 및 이력 기록 안 함")
        return results
    sent = send_results(results, on_sent=mark_sent)
    print(f"[send] {len(sent)}/{len(results)}건 성공")
    if len(sent) != len(results):
        raise RuntimeError("일부 전송 실패: 성공 건만 기록했습니다")
    return sent


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: " + (str(exc) if isinstance(exc, RuntimeError) else "실행 실패"))
        sys.exit(1)
