# 병목·쇼티지·지정학 아이디어 봇 (bottleneck-bot)

기존 gray_man_bot 뒤에 얹는 **2단계 하이브리드 아이디어 발굴** 모듈.
일반 뉴스가 아니라 **S급 신호만**(공급 병목 / 쇼티지 / 원자재 급등락 /
지정학·전쟁→방산 / 병목 이동) 골라서, **Claude Opus 4.8의 코멘트**(아이디어 +
파생 효과 사슬 + 진짜 수혜 지점 + 반대 논거)를 달아 **텔레그램으로** 쏜다.
하루 최대 3건.

## 파일 구조 (총 7개)
- `signals.py` — 시그널 사전 + 임계값/상한 (튜닝은 여기서만)
- `filter_stage1.py` — 룰 기반 1차 필터 (공짜, 노이즈 제거)
- `evaluate_stage2.py` — 통과분만 Claude Opus 4.8 평가 (effort=low)
- `telegram_send.py` — 텔레그램 전송 (파생 사슬 포함 포맷)
- `main.py` — 전체 묶음
- `requirements.txt` — 의존성
- `bottleneck.yml` — GitHub Actions 워크플로 (★ .github/workflows/ 폴더에)

## GitHub Secrets (이 봇이 쓰는 것)
새로 추가한 3개만 사용. 기존 뉴스봇 Secret은 안 건드림.
```
ANTHROPIC_API_KEY     # Anthropic API 키 (sk-ant-...)
BOTTLENECK_TOKEN      # 새로 만든 아이디어봇 토큰 (기존 TELEGRAM_TOKEN과 분리)
BOTTLENECK_CHAT_ID    # 봇과의 1:1 chat_id (양수)
```

## 업로드 방법
1. 아래 6개를 repo 루트에 업로드:
   signals.py / filter_stage1.py / evaluate_stage2.py /
   telegram_send.py / main.py / requirements.txt
2. bottleneck.yml 은 `.github/workflows/` 폴더 안에 업로드.
3. main.py 의 fetch_news() 를 기존 gray_man_bot 수집 함수로 연결.
   (형식: [{"title","summary","url","source"}, ...])

## 실행/테스트
- Actions 탭 → bottleneck-idea-bot → Run workflow 로 즉시 실행.
- 메시지 오면 성공. "오늘 S급 없음"도 정상(필터가 빡셈).
- 에러는 Actions 로그에서 확인.

## 튜닝 다이얼 (signals.py)
- SCORE_THRESHOLD = 9   # 올리면 더 빡셈, 내리면 더 많이 통과
- MAX_DAILY = 3         # 하루 최대 전송 수
- 키워드는 각 카테고리 리스트에 추가/삭제

## 비용/품질 다이얼 (evaluate_stage2.py)
- EFFORT = "low"        # 코멘트 깊이를 원하면 "high"/"xhigh"/"max"
- 하루 3건 기준 월 비용 대략 수천 원 수준.

## 메모
- 파생 사슬은 길수록 그럴듯하지만 틀릴 확률도 커진다. 봇 코멘트는 참고이지 신탁이 아님.
- 최종 판단은 사람 몫.
