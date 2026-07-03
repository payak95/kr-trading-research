# kr-trading-research

전략·지표·백테스트·스크리닝·AI 섀도 판단 — **브로커 무관 연구 레이어**. [kr-trading-bot](../kr-trading-bot)(KIS)과
[kr-trading-bot-toss](../kr-trading-bot-toss)(토스)가 이 저장소를 pip 의존성으로 설치해 전략 판단 로직을
공유한다. **브로커 자격증명이 전혀 없다** — 시세는 네이버(무인증) API만 쓰고, 신호·판단·집계는 전역(테넌트
무관) Redis 키(`bot:screen:*`·`bot:backtest:*`·`bot:pipeline:*`·`bot:ai:*`·`market:regime`·`bot:strategies`)로
발행해 KIS·토스 양쪽 운영 봇과 [kr-trading-console](../kr-trading-console)이 공통으로 읽는다.

## 구조
```
src/kr_research/
  core/       ai_store.py·holidays.py·params.py·config.py(이 저장소 전용, KIS/토스 필드 없음)
  bot/        notify.py(텔레그램 알림, 개인 봇 토큰)
  trading/    indicators·strategy·spec·setups·exits·tracking·backtest·tuning·walkforward·metrics·market_data·flow
tools/        cron/데몬 진입점 스크립트(패키지 밖, 저장소 체크아웃에서 직접 실행)
tests/        위 전부에 대응하는 단위 테스트
```

## 로컬 개발
```
pip install -e .          # kr_research 패키지를 편집 가능 모드로 설치(이 저장소 자체 도구·테스트 실행용)
python tests/test_X.py    # 개별 테스트 실행(pytest 아님, 각 파일이 자체 main())
python tools/X.py         # 개별 도구 수동 실행
```

## 배포
독립 VPS 컨테이너로 배포되는 cron/데몬 워커 전용 — 상시 실행 프로세스(`main.py`)는 없다. `kr-trading-bot`/
`kr-trading-bot-toss`는 `requirements.txt`에 `kr-trading-research @ git+https://github.com/payak95/
kr-trading-research.git@<태그>`로 고정 버전 의존한다. 자세한 배포 절차는 `docs/ops/RUNTIME.md` 참고.
