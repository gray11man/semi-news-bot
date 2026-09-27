
                if score >= threshold:
                    if sent >= CREDIT_MAX_SEND:
                        print(
                            f"⏸ 전송상한 도달 → seen 미처리/다음사이클 재시도 | "
                            f"[{score}] {c['title'][:50]}"
                        )
                        continue

                    pts = "".join(
                        f"\n • {html.escape(str(p))}"
                        for p in (
                            j.get(
                                "key_points"
                            )
                            or []
                        )[:3]
                    )

                    ok = send_tg(
                        f"{c['icon']} "
                        f"<b>{html.escape(c['kind'])}</b> · "
                        f"{html.escape(c['source'])}\n"
                        f"<b>{html.escape(c['title'])}</b>\n\n"
                        f"💡 {html.escape(j.get('summary_kr', ''))}"
                        f"{pts}\n\n"
                        f"점수: {score}/10\n"
                        f"{c['url']}"
                    )

                    if ok:
                        _mark_credit_seen(
                            state,
                            c,
                        )
                        save_credit_state(state)
                        sent += 1

                        print(
                            f"✅ [{score}] "
                            f"{c['source']} - "
                            f"{c['title'][:50]}"
                        )
                    else:
                        print(
                            f"⚠ 텔레그램 전송실패 → seen 미처리/다음사이클 재시도 | "
                            f"[{score}] {c['title'][:50]}"
                        )

                else:
                    # 판정이 정상 완료된 탈락 항목만 seen 처리
                    _mark_credit_seen(
                        state,
                        c,
                    )

                    print(
                        f"❌ [{score}] "
                        f"{c['title'][:50]}"
                    )

            save_credit_state(state)
            time.sleep(1.2)

            if _gm["dead"]:
                print("[크레딧] Gemini 중단 → 미판정 항목은 다음 사이클 재시도")
                break

        save_credit_state(state)

        print(
            f"[크레딧] 전송 {sent}건"
        )

    except Exception as e:
        print(
            f"[크레딧 감시 실패] "
            f"{str(e)[:250]}"
        )


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"[버전] {BOT_VERSION}")
    try:
        run_celeb_watch()
    except Exception as e:
        _status_issue('youtube', '처리 실패: ' + type(e).__name__)
        print(
            f"[셀럽 감시 실패] "
            f"{str(e)[:250]}"
        )

    if CELEB_DRY_RUN:
        print("[미리보기 완료] 유튜브만 점검; 텔레그램 전송 안 함")
        return

    try:
        run_blog_watch()
    except Exception as e:
        _status_issue('blog', '처리 실패: ' + type(e).__name__)
        print(
            f"[블로그 감시 실패] "
            f"{str(e)[:250]}"
        )

    try:
        run_credit_watch()
    except Exception as e:
        print(
            f"[크레딧 감시 실패] "
            f"{str(e)[:250]}"
        )

    if not _send_run_report():
        print("[실행 보고] 텔레그램 상태 알림 전송 실패")

    print(
        f"=== Gemini 총 호출 "
        f"{_gm['n']}회 / 기본 {BASE_GEMINI_CALLS} · 최핵심 최대 {MAX_GEMINI_CALLS} · 총토큰 {_gm['tokens']} ==="
    )


if __name__ == "__main__":
    main()
