# 파이프라인 자동 스케줄링(야간 크론) — 저장 전략 전부를 매일 파이프라인 큐에 적재
"""저장 전략(`bot:strategies`, 콘솔 CRUD)을 매일 옵트인 없이 파이프라인(①스크리닝→②백테스트→④검증
신호 자동 등록, §5 연결고리)에 적재한다. `tools/screen_notify.py`(B4-a)와 동일 방침 — 옵트인 UI 없이
저장 전략 전부 대상(노이즈 문제 생기면 후속으로 필터 추가, owner 결정).

무거운 계산은 안 함 — 이미 떠 있는 `kr-pipeline-worker` 데몬(BLPOP)이 잡을 실제로 처리하므로, 이
스크립트는 `bot:pipeline:jobs` 에 잡을 RPUSH 만 하고 즉시 끝난다(콘솔 "파이프라인 실행" 버튼과 같은 큐).
실행: python tools/pipeline_schedule.py 설계: docs/planning/pipeline-automation-design.md §5(v0.13).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pipeline_worker import PIPELINE_QUEUE

K_STRATEGIES = "bot:strategies"  # kr-trading-bot(core/control_bus.py)의 상수와 반드시 일치(전역, 콘솔 CRUD)


def build_jobs(strategies: dict) -> list[dict]:
    """{전략명: spec(JSON 문자열)} → 파이프라인 잡 리스트(순수 함수, 테스트 대상).
    run_id = f"sched-{name}"(전략당 고정 — 매일 같은 run_id 재사용, staleness 규칙(§5 v0.7)이 "다음
    거래일 전까지만 신선"으로 판정해 자동으로 매일 새로 돈다, 날짜를 run_id에 넣을 필요 없음).
    days/top_k/cash 는 생략(run_pipeline 자체 기본값 사용 — bw.DEFAULT_DAYS 등과 항상 동기화).
    grid 는 안 넣음(③튜닝 skip, §5 v0.12 grid-선택화 — 자동 스케줄의 목적은 ④ 자동 등록).
    spec 파싱 실패는 skip(개별 전략 실패가 나머지를 막지 않음)."""
    jobs = []
    for name, spec_json in strategies.items():
        try:
            spec = json.loads(spec_json)
        except (ValueError, TypeError):
            continue
        jobs.append({"run_id": f"sched-{name}", "strategy": name, "spec": spec, "universe": True})
    return jobs


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 파이프라인 스케줄 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    strategies = r.hgetall(K_STRATEGIES)
    if not strategies:
        print("[pipeline_schedule] 저장 전략 없음 — 종료")
        return 0

    jobs = build_jobs(strategies)
    for job in jobs:
        r.rpush(PIPELINE_QUEUE, json.dumps(job, ensure_ascii=False))
    print(f"[pipeline_schedule] 전략 {len(strategies)} · 잡 {len(jobs)}건 → {PIPELINE_QUEUE} 적재"
          "(kr-pipeline-worker 데몬이 처리)")
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("pipeline_schedule", main))
