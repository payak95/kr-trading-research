# 프로젝트 공유 기억 (Cross-machine memory) — kr-trading-research

> 이 저장소는 2026-07-04 에 `kr-trading-bot`에서 분리 신설됐다. Owner 가 "kr-trading-bot-toss 가 뒤처지는
> 걸 막으려면 KIS/토스 운영은 별도로 두고, 전략운영·AI 테스트는 공용 프로젝트로 분리하는 게 낫지 않냐"고
> 제안한 것을 계기로, 이미 Redis 키 레벨에서 존재하던 경계(연구 관련 키는 전부 전역, 실행 관련 키만
> 테넌트 스코프)를 저장소 구조에도 반영했다. 자세한 배경은 kr-trading-bot 의
> `docs/planning/toss-broker-migration.md`를 참고(이 분리 결정이 그 문서에 기록돼 있음).

## 결정
- **브로커 자격증명 없음** — KIS·토스 어느 쪽 클라이언트도 import 하지 않는다. 시세는 네이버(무인증)만.
- **`main.py` 없음** — 상시 실행 프로세스가 아니라 전부 cron/데몬 워커(`tools/*.py`)로 기동.
- **패키지명 `kr_research`**(설치 시 `trading`/`core` 가 아님) — kr-trading-bot·kr-trading-bot-toss 가 이미
  자체 `trading/`·`core/` 디렉터리(executor·positions·kis_client 등 브로커 운영 코드)를 갖고 있어서, pip
  설치 시 이름이 겹치면 import 순서에 따라 어느 쪽이 로드되는지 갈리는 혼란스러운 버그가 날 수 있었다 —
  그래서 의도적으로 다른 이름을 골랐다.
- **버전 규칙(2026-07-06): 태그 = pyproject `version`** — 소비자(kr-trading-bot·kr-trading-bot-toss)는
  git 태그 핀(`@vX.Y.Z`)으로 설치하므로, 태그를 만드는 커밋에서 pyproject 버전을 같이 범프한다.
  v0.1.1 까지는 pyproject 가 0.1.0 인 채 태그만 올라가 불일치했음(0.1.2 부터 일치). 릴리스 절차:
  pyproject 범프 커밋 → push → 그 커밋에 `vX.Y.Z` 태그 → 소비자 requirements 핀 갱신.
- **`core/config.py`/`bot/notify.py`는 kr-trading-bot 것의 축소 복제판** — KIS OAuth 필드(app_key·account_8·
  hts_id 등)가 전혀 없는 자체 버전을 새로 만들었다(REDIS_URL·TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID·
  GEMINI_API_KEY 만). `core/control_bus.py`의 `K_STRATEGIES`(="bot:strategies") 상수도 전체 파일을 끌어오는
  대신 각 도구에 리터럴로 복제했다 — kr-trading-bot 의 `core/control_bus.py`와 **반드시 같은 값**을 유지
  해야 한다(바뀌면 양쪽 다 고칠 것).

## 마이그레이션 메커니즘(가장 비직관적이었던 부분)
kr-trading-bot 의 `main.py`(라이브 매매 루프)와 수동 CLI 도구 3개(`tools/backtest.py`·`screen.py`·
`forward_eval.py`, 전부 KIS 시세가 필요해서 kr-trading-bot 에 남음)가 `trading/*.py` 전체를 이미 쓰고
있어서, 처음 계획했던 "연구 전용 파일만 이관"이 불가능했다(옮기면 라이브 루프가 깨짐). 그래서
**`main.py`를 포함한 kr-trading-bot 쪽 import 문 전체를 `kr_research.trading.X`/`kr_research.core.X` 를
가리키도록 고치는 방식**으로 갔다 — 로직은 한 줄도 안 바뀌고 import 경로만 바뀌었으므로, 전체 테스트
스위트 무회귀로 검증했다.

## 배포
독립 VPS 컨테이너(`/opt/kr-trading-research`, git clone)로 크론·데몬 워커를 돌린다. 자세한 절차는
`docs/ops/RUNTIME.md`.
