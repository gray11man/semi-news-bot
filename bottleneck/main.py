"""뉴스 봇 실행 진입점 v5."""
import contextlib
import os
import sys
import time
from pathlib import Path

from fetch_news import fetch_news, mark_sent
from pick_headlines import pick_critical
from telegram_send import send_results

LOCK_FILE = Path(os.getenv("RUN_LOCK_FILE", str(Path(__file__).with_name("news_bot.lock"))))
LOCK_STALE_SECONDS = int(os.getenv("RUN_LOCK_STALE_SECONDS", "3600"))


@contextlib.contextmanager
def single_instance_lock():
    """스케줄/수동 실행이 겹쳐 같은 뉴스를 이중 전송하는 것을 막는다."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    fd = None
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} ts={time.time()}\n".encode())
            os.fsync(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except OSError:
                age = 0
            if age > LOCK_STALE_SECONDS:
                try:
                    LOCK_FILE.unlink()
                    print("[lock] 오래된 실행 잠금 제거")
                    continue
                except OSError:
                    pass
            raise RuntimeError("이미 다른 뉴스 봇 실행이 진행 중입니다")
    if not acquired:
        raise RuntimeError("실행 잠금을 만들 수 없습니다")
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def run(news_items=None):
    if os.getenv("DRY_RUN") != "1":
        missing = [
            key for key in ("GEMINI_KEY", "BOTTLENECK_TOKEN", "BOTTLENECK_CHAT_ID")
            if not os.getenv(key)
        ]
        if missing:
            raise RuntimeError("환경변수 누락: " + ", ".join(missing))

    with single_instance_lock():
        items = fetch_news() if news_items is None else news_items
        print(f"[fetch] 최종 입력 {len(items)}건")
        results = pick_critical(items)
        if results is None:
            raise RuntimeError("API 판단 실패 — 뉴스 0건이 아닙니다")
        print(f"[pick] {len(results)}건")

        if os.getenv("DRY_RUN") == "1":
            for result in results:
                print(
                    f"[{result.get('sector', result.get('sector_hint', ''))}] "
                    f"{result.get('headline', result.get('title', ''))} | {result.get('reason', '')}"
                )
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
        # 토큰/URL 등 민감한 네트워크 예외 원문을 그대로 출력하지 않는다.
        message = str(exc) if isinstance(exc, RuntimeError) else "실행 실패"
        print(f"[error] {type(exc).__name__}: {message}")
        sys.exit(1)
