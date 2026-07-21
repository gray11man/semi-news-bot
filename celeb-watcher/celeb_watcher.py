name: celeb-watcher
on:
  schedule:
    - cron: "0 */6 * * *"   # 6시간마다 (쿼터 안전)
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: celeb-watcher
  cancel-in-progress: false
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install requests feedparser
      - name: Run watcher
        env:
          YOUTUBE_API_KEY: ${{ secrets.YOUTUBE_API_KEY }}
          GEMINI_KEY: ${{ secrets.GEMINI_KEY }}
          TELEGRAM_TOKEN: ${{ secrets.CELEB_TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.CELEB_TELEGRAM_CHAT_ID }}
        working-directory: celeb-watcher
        run: python celeb_watcher.py
      - name: Commit seen ids
        run: |
          git config user.name "bot"
          git config user.email "bot@users.noreply.github.com"

          # 이번 실행이 만든 최신 상태 파일을 백업 (이 파일들이 항상 최신본)
          cp celeb-watcher/seen_celeb_ids.json /tmp/seen_celeb_ids.json 2>/dev/null || true
          cp celeb-watcher/seen_twitter_blog.json /tmp/seen_twitter_blog.json 2>/dev/null || true

          PUSHED=0
          for i in 1 2 3 4 5; do
            # 원격 최신 상태로 맞춘 뒤 (충돌 원천 차단)
            git fetch origin main
            git reset --hard origin/main

            # 백업해둔 최신 상태 파일 복원
            cp /tmp/seen_celeb_ids.json celeb-watcher/seen_celeb_ids.json 2>/dev/null || true
            cp /tmp/seen_twitter_blog.json celeb-watcher/seen_twitter_blog.json 2>/dev/null || true

            git add celeb-watcher/seen_celeb_ids.json celeb-watcher/seen_twitter_blog.json || true
            if git diff --cached --quiet; then
              echo "변경사항 없음 - 커밋 생략"
              PUSHED=1
              break
            fi
            git commit -m "update seen ids"

            if git push; then
              echo "push 성공 (시도 $i)"
              PUSHED=1
              break
            fi
            echo "push 실패, 재시도 ($i/5)"
            sleep $((RANDOM % 5 + 2))
          done

          if [ "$PUSHED" -ne 1 ]; then
            echo "::error::seen 상태 저장 실패 - 5회 재시도 모두 실패"
            exit 1
          fi
