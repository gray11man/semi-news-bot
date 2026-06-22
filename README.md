# 반도체·AI 데일리 브리핑 텔레그램 봇

메모리 반도체 / AI 뉴스를 매일 자동 수집해서 텔레그램으로 보냅니다.
중복 기사는 제목 유사도 기반으로 걸러내고, 카테고리별로 묶어서 전송합니다.
GitHub Actions가 무료 cron으로 돌려주므로 PC를 켜둘 필요가 없습니다.

---

## 동작 방식
- RSS 다중 소스(구글뉴스 한/영 + 매체 RSS) 수집
- 키워드 필터로 메모리 반도체 / AI 관련만 통과
- `seen.json`에 보낸 기록을 남겨, 과거에 보낸 기사 + 같은 회차 중복 제거
- 텔레그램으로 카테고리별 묶어서 전송 (4096자 제한 자동 분할)

---

## 설치 (5분, 한 번만)

### 1. 텔레그램 봇 토큰 발급
1. 텔레그램에서 **@BotFather** 검색 → `/newbot` → 봇 이름 지정
2. 받은 토큰 복사 (형식: `8123456789:AAH...`)

### 2. 내 chat_id 확인
1. 방금 만든 봇에게 아무 메시지나 한 번 보내기
2. 브라우저에서 아래 주소 열기 (토큰 끼워넣기):
   `https://api.telegram.org/bot<토큰>/getUpdates`
3. 결과에서 `"chat":{"id":숫자}` 의 숫자가 chat_id

### 3. GitHub 레포 만들고 업로드
1. GitHub에서 새 레포 생성 (private 권장)
2. 이 폴더의 모든 파일 업로드 (`bot.py`, `seen.json`, `requirements.txt`, `.github/`)

### 4. Secrets 등록
레포 → **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_TOKEN` : 위 봇 토큰
- `TELEGRAM_CHAT_ID` : 위 chat_id

### 5. 실행
- 자동: 매일 오전 8시 / 오후 6시(KST)에 실행됨
- 수동 테스트: 레포 → **Actions → Semi-AI Daily Briefing → Run workflow**

---

## 자주 바꾸는 설정 (`bot.py` 상단)
| 항목 | 의미 |
|------|------|
| `FEEDS` | RSS 소스 / 검색 키워드 추가·삭제 |
| `INCLUDE_KEYWORDS` | 카테고리별 통과 키워드 |
| `EXCLUDE_KEYWORDS` | 노이즈 컷 키워드 |
| `SIMILARITY_THRESHOLD` | 중복 판정 민감도 (높일수록 덜 묶음) |
| `MAX_ITEMS_PER_CATEGORY` | 카테고리당 최대 전송 건수 |

발송 시각은 `.github/workflows/briefing.yml`의 `cron`에서 변경 (UTC 기준, KST-9시간).

---

## 주의
- 첫 실행은 누적 뉴스가 많을 수 있음 (이후 자동으로 중복 걸러짐)
- 구글뉴스 RSS는 무료지만 가끔 일시적으로 비는 경우가 있어 매체 RSS를 함께 사용
