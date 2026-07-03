# 파이프라인 자동 스케줄링 — build_jobs 순수 로직 + main() 큐 적재 (fakeredis, 네트워크 없음)
"""실행: python tests/test_pipeline_schedule.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from tools.pipeline_schedule import K_STRATEGIES, build_jobs
from tools.pipeline_worker import PIPELINE_QUEUE

_SPEC = {"name": "정배열", "indicators": [], "entry": {}, "exit": {}}


def main() -> int:
    # ── build_jobs: 정상 spec 여러 건 → run_id·strategy·universe=True, days/top_k/cash·grid 없음 ──
    strategies = {"정배열": json.dumps(_SPEC), "모멘텀": json.dumps({**_SPEC, "name": "모멘텀"})}
    jobs = build_jobs(strategies)
    assert len(jobs) == 2
    by_name = {j["strategy"]: j for j in jobs}
    assert by_name["정배열"] == {"run_id": "sched-정배열", "strategy": "정배열", "spec": _SPEC, "universe": True}
    assert "grid" not in by_name["정배열"] and "days" not in by_name["정배열"], "grid·days 생략(run_pipeline 기본값 사용)"

    # ── 파싱 실패 spec 은 skip(개별 실패가 나머지 안 막음) ──
    mixed = {"정배열": json.dumps(_SPEC), "깨짐": "{bad json"}
    jobs2 = build_jobs(mixed)
    assert len(jobs2) == 1 and jobs2[0]["strategy"] == "정배열", "깨진 spec skip"

    # ── 빈 dict → 빈 리스트 ──
    assert build_jobs({}) == []

    # ── main(): 저장 전략 hash → 큐에 정확히 RPUSH(전략 수만큼) ──
    r = FakeRedis(decode_responses=True)
    r.hset(K_STRATEGIES, mapping=strategies)
    os.environ["REDIS_URL"] = "redis://fake"  # main() 의 REDIS_URL 가드 통과용(실제 연결은 아래서 monkeypatch)
    import redis as redis_module
    orig_from_url = redis_module.from_url
    redis_module.from_url = lambda *a, **k: r
    try:
        from tools.pipeline_schedule import main as sched_main
        rc = sched_main()
    finally:
        redis_module.from_url = orig_from_url
        del os.environ["REDIS_URL"]
    assert rc == 0
    queued = [json.loads(x) for x in r.lrange(PIPELINE_QUEUE, 0, -1)]
    assert len(queued) == 2 and {q["strategy"] for q in queued} == {"정배열", "모멘텀"}, queued

    # ── 저장 전략 없음 → 조용히 0 반환, 큐 미적재 ──
    r2 = FakeRedis(decode_responses=True)
    os.environ["REDIS_URL"] = "redis://fake"
    redis_module.from_url = lambda *a, **k: r2
    try:
        rc2 = sched_main()
    finally:
        redis_module.from_url = orig_from_url
        del os.environ["REDIS_URL"]
    assert rc2 == 0 and r2.llen(PIPELINE_QUEUE) == 0

    print("✅ test_pipeline_schedule: build_jobs(정상·파싱실패skip·빈dict)·main(큐적재·저장전략없음) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
