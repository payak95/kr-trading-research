# 런타임 — 크론·데몬 워커 (2026-07-04, kr-trading-bot 에서 이관)

> 이 저장소엔 상시 실행 프로세스(`main.py`)가 없다 — 전부 cron 또는 데몬 워커(`tools/*.py`)로 기동하며,
> **브로커 자격증명이 전혀 없다**(KIS·토스 어느 쪽 클라이언트도 import 안 함). 시세는 네이버(무인증)만 쓴다.
> 배포 절차는 kr-trading-bot 의 [deploy-vps.md](../../../kr-trading-bot/workflows/deploy-vps.md) 와 동일
> 패턴(`git clone`+`docker build`)을 이 저장소용으로 그대로 따른다 — `/opt/kr-trading-research`, 이미지명
> `kr-trading-research`.

## VPS 크론
모두 `docker run --rm --env-file /opt/kr-trading-research/.env --network kr-net -v /opt/kr-trading-research:/app
kr-trading-research python tools/<script>` 형태(UTC):

| 시각(UTC) | 스크립트 | 용도 |
|---|---|---|
| `*/30 * * * *` | `regime_scheduler.py` | Flow→`market:regime`(전역, kis·토스 둘 다 추종) |
| `0 */2 * * *` | `monitor.py` | 2시간마다 `bot:{tenant}:status:current` 다운 감지(전이 알림) |
| `15 7 * * *` | `monitor.py --heartbeat` | 일일 점검 요약 |
| `30 8 * * 1-5` | `pipeline_schedule.py` | 저장 전략 전부 파이프라인 큐 적재(유니버스 워밍 30분 뒤) |
| `*/5 * * * *` | `ai_shadow_scheduler.py` | AI 섀도 판단 ②타겟 종목 관찰(콘솔 "AI 테스트" 탭, due 한 것만·무주문) |
| `2-59/5 * * * *` | `ai_shadow_notify.py` | AI 섀도 판단 중 확신도≥0.7 buy/sell 신규분만 텔레그램 알림(scheduler 2분 뒤, de-noise) |
| `10 7 * * *` | `ai_forward_eval.py` | AI 섀도 판단(①②) 전진검증(장 마감 후, 네이버 일봉) |
| `10 8 * * 1-5` | `ai_universe_scan.py` | AI 섀도 판단 ①유니버스 스크리닝(유니버스 워밍 10분 뒤, 캐시만 읽음) |
| `0 8 * * 1-5` | `screen_universe.py` | 시총풀→거래대금 상위 유니버스 확정 + 일봉 워밍 |
| `10 8 * * 1-5` | `flow_universe.py` | 유니버스 종목 외국인·기관 수급 캐시 워밍 |
| `0 8 * * 1-5` | `screen_track_eval.py` | 추적 신호 D+N forward 수익 평가·전략별 집계 발행 + 신규검증통과 알림 |
| `10 8 * * 1-5` | `screen_notify.py` | 저장 전략 전부를 유니버스와 대조 → 신규 후보 텔레그램 알림 |

> ⚠️ 백테스트 워커·파이프라인 워커는 cron 이 아니라 **데몬 컨테이너**(아래 참고).

## 백테스트 워커 (데몬)
콘솔이 `bot:backtest:jobs` 에 적재한 spec 잡을 BLPOP 으로 즉시 처리.
```bash
docker run -d --name kr-backtest-worker --restart unless-stopped --network kr-net \
  --env-file /opt/kr-trading-research/.env -v /opt/kr-trading-research:/app \
  kr-trading-research python tools/backtest_worker.py --daemon
```
코드 업데이트: `cd /opt/kr-trading-research && git pull && docker restart kr-backtest-worker`(재빌드 불필요 — repo 마운트).

## 파이프라인 워커 (데몬)
콘솔이 `bot:pipeline:jobs` 에 적재한 잡을 처리(①스크리닝→②백테스트→(③튜닝)→④검증 신호 자동 등록).
```bash
docker run -d --name kr-pipeline-worker --restart unless-stopped --network kr-net \
  --env-file /opt/kr-trading-research/.env -v /opt/kr-trading-research:/app \
  kr-trading-research python tools/pipeline_worker.py --daemon
```
코드 업데이트: `cd /opt/kr-trading-research && git pull && docker restart kr-pipeline-worker`(재빌드 불필요).

## kr-trading-bot/kr-trading-bot-toss 에 남은 것(이 저장소로 안 옮김)
- `tools/forward_eval.py`(KIS 매수 신호 전진검증, 실 KIS 시세 필요) — 각 운영 저장소에 그대로.
- `main.py`·`trading/{executor,positions,risk}.py`·`core/{kis_client,kis_ws,toss_client,config,control_bus,
  store}.py` — 브로커 운영 코드, 이 저장소로 옮길 대상 아님(§Non-goals, kr-trading-bot 의
  `docs/planning/toss-broker-migration.md` 참고).
- `tools/backtest.py`·`tools/screen.py`(수동 CLI, 실 KIS 시세 필요) — kr-trading-bot 에 남되, 내부적으로는
  이 저장소를 pip 의존성(`kr_research.trading.*`)으로 가져다 씀.
