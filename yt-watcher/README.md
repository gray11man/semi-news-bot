# YouTube AI-인터뷰 감시 봇

AI 인프라/수요·공급/컴퓨팅 부족/버블 관련 유명인사 인터뷰를 자동 감지해 텔레그램으로 한글 제목과 함께 알림.

## 구조 (3축)
- **축1 채널** — 고품질 채널 RSS 전수 감시 (쿼터 0, 놓침 거의 없음)
- **축2 인물** — 25+명 이름 검색, 어디 게스트로 나오든 포착 (강세 + 약세 인사)
- **축3 키워드** — 주제어 검색, 이름·채널 안 박혀도 포착 (강세 + 약세 양방향)

중복은 `seen_videos.json`(video_id)으로 제거, repo 커밋으로 영속.
제목은 Gemini로 한글 번역(원문도 함께 표시).

## 설치 (기존 gray_man_bot repo에 합치기)
1. `yt_watcher/` 폴더와 `.github/workflows/yt-watcher.yml` 을 repo에 복사.
2. Google Cloud Console → **YouTube Data API v3** 사용 설정 → API 키 발급.
3. repo → Settings → Secrets → Actions 에 등록:
   - `YOUTUBE_API_KEY`
   - `GEMINI_API_KEY` (기존 봇 것 재사용)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (기존 봇 것 재사용)
4. **최초 1회** 채널 ID 변환:
   ```
   cd yt_watcher
   YOUTUBE_API_KEY=... python resolve_channels.py
   ```
   출력된 `channel_id` 들을 `watch_config.py` 의 `CHANNELS` 에 채워넣기.
5. push 하면 30분마다 자동 실행. Actions 탭에서 수동 실행도 가능.

## 감시 대상 수정
`watch_config.py` 의 리스트만 고치면 됨 (코드 수정 불필요).
- 인물 추가/삭제 → `PEOPLE`
- 키워드 추가/삭제 → `TOPIC_KEYWORDS`
- 채널 추가 → `CHANNELS` (handle 추가 후 resolve 재실행)

## 노이즈 조절
- 너무 많이 오면: 축3 키워드를 줄이거나, `PEOPLE` 의 tier 2 인물 일부 제거.
- 폴링 주기: 워크플로 cron `*/30` → `0 */2 * * *`(2시간) 등으로 조정.
- `LOOKBACK_HOURS` 는 cron 주기보다 약간 길게 두면 누락 방지(겹쳐도 dedup이 잡음).

## 쿼터
- search.list 호출당 100 units, 무료 10,000/day.
- 인물 30 + 키워드 약 40 = 호출 70개 × 100 = 7,000 units/회.
  → 30분마다 돌리면 하루 한도 초과. **2시간 주기 권장** 또는 키워드 축소.
- 채널 RSS는 쿼터 0이라 자주 돌려도 무방.
