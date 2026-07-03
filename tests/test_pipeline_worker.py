# 파이프라인 워커 — ①스크리닝→②백테스트→(③튜닝)→④검증등록 체이닝 + idempotent 재개·staleness 검증 (합성 일봉·fakeredis, 네트워크 없음)
"""실행: python tests/test_pipeline_worker.py
콘솔이 채울 잡을 흉내 낸 합성 잡으로 run_pipeline 을 직접 호출(주입 fetch)해 4단계 체이닝·재개·만료
규칙을 검증하고, 큐 라운드트립(_handle_raw/drain)도 fakeredis 로 확인."""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core import holidays
from tools import backtest_worker as bw
from tools.pipeline_worker import MAX_TOP_K, META_KEY, PIPELINE_QUEUE, STAGE_KEY, _is_fresh, drain, run_pipeline
from tools.screen_track import TRACK_REQUESTS_KEY

# 방향성 뚜렷한 선형 램프 — sma_fast(5)>sma_slow(20) 크로스가 슬로프와 무관하게 비슷한 시점에 발생하지만
# 총 상승폭이 크게 달라 배치 백테스트 수익률 순위가 항상 AAA>BBB 로 결정적(그리드/튜닝 값은 사용 안 함).
_TREND_SPEC = {
    "name": "trend",
    "indicators": [
        {"id": "sma_fast", "type": "sma", "params": {"period": 5}},
        {"id": "sma_slow", "type": "sma", "params": {"period": 20}},
    ],
    "entry": {"gt": ["sma_fast", "sma_slow"]},
    "exit": {"not": {"gt": ["sma_fast", "sma_slow"]}},
}


def _series(closes):
    return [{"date": f"d{i:03d}", "open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def main() -> int:
    rising = _series([100 + i * 3 for i in range(40)])     # AAA — 가파른 상승(후보, 배치 1위)
    rising2 = _series([50 + i * 0.3 for i in range(40)])   # BBB — 완만한 상승(후보, 배치 2위)
    falling = _series([140 - i for i in range(40)])        # CCC — 하락(비후보)

    def fetch(code, days):
        return {"AAA": rising, "BBB": rising2, "CCC": falling}.get(code, [])

    # ── _is_fresh: "같은 날짜"가 아니라 "다음 거래일 전까지" — 금요일 완료는 주말 내내 신선, 월요일 되는 순간 만료 ──
    fri_ts = dt.datetime(2026, 6, 19, 23, 50, tzinfo=holidays.KST).timestamp()
    assert _is_fresh(fri_ts, now=dt.datetime(2026, 6, 19, 23, 59, tzinfo=holidays.KST)), "생성 당일은 신선"
    assert _is_fresh(fri_ts, now=dt.datetime(2026, 6, 20, 2, 0, tzinfo=holidays.KST)), "토요일 새벽에도 신선(주말 안 낡음)"
    assert _is_fresh(fri_ts, now=dt.datetime(2026, 6, 21, 23, 59, tzinfo=holidays.KST)), "일요일 밤까지 신선"
    assert not _is_fresh(fri_ts, now=dt.datetime(2026, 6, 22, 0, 0, tzinfo=holidays.KST)), "월요일(다음 거래일) 되는 순간 만료"

    # ── 정상 4단계 완주(유니버스, ③튜닝 포함) ──
    r = FakeRedis(decode_responses=True)
    r.sadd(bw.UNIVERSE_KEY, "AAA", "BBB", "CCC")
    job = {"run_id": "run1", "spec": _TREND_SPEC, "strategy": "trend", "universe": True, "days": 40,
           "top_k": 1, "grid": {"x": [1, 2]}, "cash": 10_000_000}

    meta = run_pipeline(r, job, fetch)
    assert meta["status"] == "done" and meta["stage"] == 4, meta

    stage1 = json.loads(r.get(STAGE_KEY.format("run1", 1)))
    assert stage1["universe"] is True and stage1["screened"] == 3 and "created_at" in stage1
    assert sorted(c["code"] for c in stage1["candidates"]) == ["AAA", "BBB"], f"상승 2종목만 후보: {stage1}"

    stage2 = json.loads(r.get(STAGE_KEY.format("run1", 2)))
    assert stage2["tested"] == 2 and stage2["top_k"] == 1 and "created_at" in stage2
    assert len(stage2["results"]) == 1 and stage2["results"][0]["code"] == "AAA", f"top_k=1 절단, 1위=AAA: {stage2}"

    stage3 = json.loads(r.get(STAGE_KEY.format("run1", 3)))
    assert len(stage3["per_code"]) == 1 and stage3["per_code"][0]["code"] == "AAA"
    assert stage3["per_code"][0]["best"] is not None and stage3["per_code"][0]["n_combos"] == 2, stage3

    # ── ④ 검증 신호 자동 등록(§5 연결고리) — ②통과(AAA) 를 ①후보의 종가·검색일과 조인해 큐에 RPUSH ──
    stage4 = json.loads(r.get(STAGE_KEY.format("run1", 4)))
    assert stage4["registered"] == 1 and "created_at" in stage4, stage4
    queued = [json.loads(x) for x in r.lrange(TRACK_REQUESTS_KEY, 0, -1)]
    assert queued == [{"strategy": "trend", "code": "AAA", "entry_price": rising[-1]["close"],
                       "screen_date": rising[-1]["date"]}], queued

    # ── idempotent 재개: 전부 신선하면 재실행해도 fetch 0회·재등록 0건(4단계 전부 재사용) ──
    calls: list[str] = []
    meta2 = run_pipeline(r, job, lambda code, days: (calls.append(code), fetch(code, days))[1])
    assert meta2["status"] == "done" and calls == [], f"신선한 stage 전부 재사용, fetch 없어야: {calls}"
    assert r.llen(TRACK_REQUESTS_KEY) == 1, "stage4 도 신선하면 재등록 안 함(큐 그대로)"

    # ── 부분 재개: stage3 만 지우면 stage1·2 는 fetch 없이 재사용, stage3 만 재계산(AAA 1건) —
    #    단 stage3 가 새로 계산됐으므로 하위 stage4 도 함께 강제 재등록(중복 RPUSH, 하위 dedup 은 별도 관심사) ──
    r.delete(STAGE_KEY.format("run1", 3))
    calls2: list[str] = []
    meta3 = run_pipeline(r, job, lambda code, days: (calls2.append(code), fetch(code, days))[1])
    assert meta3["status"] == "done" and calls2 == ["AAA"], f"stage3만 재계산(AAA): {calls2}"
    assert r.llen(TRACK_REQUESTS_KEY) == 2, "stage3 재계산 → stage4 도 강제 재등록"

    # ── staleness(v0.7): stage1 을 오래된 created_at 으로 덮으면 stage1 재계산 + 하위(2·3·4) 도 강제 재계산
    #    (하위는 자기 timestamp 가 아직 신선해도, 상위가 새로 계산된 이상 그 결과에 기반하지 않아 함께 무효화) ──
    stale = json.loads(r.get(STAGE_KEY.format("run1", 1)))
    # XKRX 캘린더 범위(2006-07-03~) 안에서 확실히 과거인 날짜 — 1970(epoch 0)은 캘린더 밖이라 에러남
    stale["created_at"] = dt.datetime(2020, 1, 1, tzinfo=holidays.KST).timestamp()
    r.set(STAGE_KEY.format("run1", 1), json.dumps(stale, ensure_ascii=False))
    calls3: list[str] = []
    meta4 = run_pipeline(r, job, lambda code, days: (calls3.append(code), fetch(code, days))[1])
    assert meta4["status"] == "done"
    assert calls3 == ["AAA", "BBB", "CCC", "AAA", "BBB", "AAA"], (
        f"stage1(유니버스 3종목)→stage2(후보 2종목)→stage3(top1) 전부 재계산: {calls3}")
    assert r.llen(TRACK_REQUESTS_KEY) == 3, "stage1 재계산 캐스케이드 → stage4 도 강제 재등록"

    # ── ③튜닝 선택화(§5, 2026-07-02 결정): grid 없으면 stage3 는 건너뛰고 stage4 로 바로 진행 ──
    no_grid_job = {"run_id": "run_no_grid", "spec": _TREND_SPEC, "strategy": "trend", "universe": True,
                   "days": 40, "top_k": 1, "cash": 10_000_000}
    meta5 = run_pipeline(r, no_grid_job, fetch)
    assert meta5["status"] == "done" and meta5["stage"] == 4, meta5
    assert r.get(STAGE_KEY.format("run_no_grid", 3)) is None, "grid 없으면 stage3 저장 안 됨(건너뜀)"
    stage4_ng = json.loads(r.get(STAGE_KEY.format("run_no_grid", 4)))
    assert stage4_ng["registered"] == 1, "grid 없어도 ②결과 기준 ④ 등록은 정상 진행"

    # ── strategy 없으면 ④ 등록 skip(②③은 정상 완주) ──
    no_strategy_job = {k: v for k, v in no_grid_job.items() if k != "strategy"}
    no_strategy_job["run_id"] = "run_no_strategy"
    meta6 = run_pipeline(r, no_strategy_job, fetch)
    assert meta6["status"] == "done" and meta6["stage"] == 4, meta6
    stage4_noreg = json.loads(r.get(STAGE_KEY.format("run_no_strategy", 4)))
    assert stage4_noreg["registered"] == 0, "strategy 없으면 등록 0건(skip)"

    # ── 입력 검증: run_id/spec 누락 ──
    e1 = run_pipeline(r, {"spec": _TREND_SPEC}, fetch)
    assert e1["status"] == "error" and "run_id" in e1["error"]
    e2 = run_pipeline(r, {"run_id": "run_no_spec"}, fetch)
    assert e2["status"] == "error" and "spec" in e2["error"]

    # ── codes(비유니버스) 경로 ──
    job_codes = {"run_id": "run_codes", "spec": _TREND_SPEC, "codes": ["AAA", "CCC"], "days": 40,
                "top_k": 5, "grid": {"x": [1]}}
    meta7 = run_pipeline(r, job_codes, fetch)
    assert meta7["status"] == "done"
    s1 = json.loads(r.get(STAGE_KEY.format("run_codes", 1)))
    assert s1["universe"] is False and [c["code"] for c in s1["candidates"]] == ["AAA"], s1

    # ── top_k 상한 clamp ──
    job_topk = {**job, "run_id": "run_topk", "top_k": 999}
    run_pipeline(r, job_topk, fetch)
    s2 = json.loads(r.get(STAGE_KEY.format("run_topk", 2)))
    assert s2["top_k"] == MAX_TOP_K, s2

    # ── 빈 유니버스 → 친절한 에러(stage1) ──
    r_empty = FakeRedis(decode_responses=True)
    e3 = run_pipeline(r_empty, {"run_id": "run_empty_uni", "spec": _TREND_SPEC, "universe": True,
                               "days": 40, "grid": {"x": [1]}}, fetch)
    assert e3["status"] == "error" and e3["stage"] == 1 and "유니버스가 비어" in e3["error"], e3

    # ── 큐 라운드트립: RPUSH → drain → 손상 잡 skip, 유니버스 잡은 캐시 전용 fetch(콜드 미스 skip)로 done,
    #    codes 잡은 주입 fetch 로 정상 후보 산출 ──
    rq = FakeRedis(decode_responses=True)
    rq.sadd(bw.UNIVERSE_KEY, "AAA", "BBB", "CCC")
    rq.rpush(PIPELINE_QUEUE, json.dumps({"run_id": "q1", "spec": _TREND_SPEC, "universe": True,
                                         "days": 40, "top_k": 1, "grid": {"x": [1]}}))
    rq.rpush(PIPELINE_QUEUE, json.dumps({"run_id": "q2", "spec": _TREND_SPEC, "codes": ["AAA", "CCC"],
                                         "days": 40, "top_k": 1, "grid": {"x": [1]}}))
    rq.rpush(PIPELINE_QUEUE, "{not json")  # 손상 잡 → skip
    n = drain(rq, fetch)
    assert n == 2, f"정상 2건만 처리(손상 skip): {n}"
    assert rq.llen(PIPELINE_QUEUE) == 0, "큐 비워짐"
    meta_q1 = json.loads(rq.get(META_KEY.format("q1")))
    assert meta_q1["status"] == "done", "universe 잡은 캐시 전용 fetch(미워밍이라 후보 0)라도 정상 완주"
    meta_q2 = json.loads(rq.get(META_KEY.format("q2")))
    assert meta_q2["status"] == "done"
    sq2 = json.loads(rq.get(STAGE_KEY.format("q2", 1)))
    assert [c["code"] for c in sq2["candidates"]] == ["AAA"], "codes 잡은 주입 fetch 로 정상 스크리닝"

    # ── §스크리닝 강화: stage1 이 flow 셋업 참조 스펙이면 유니버스 flow_universe 캐시를 반영 ──
    flow_spec = {"run_id": "unused", "name": "flow",
                "indicators": [{"id": "sma_fast", "type": "sma", "params": {"period": 5}},
                               {"id": "sma_slow", "type": "sma", "params": {"period": 20}}],
                "entry": {"all": [{"gt": ["sma_fast", "sma_slow"]}, {"setup": "foreign_accumulation"}]},
                "exit": {"not": {"gt": ["sma_fast", "sma_slow"]}}}
    strong_flow = [{"date": f"2026070{i % 9 + 1}", "close": 100, "volume": 1_000_000,
                    "frgn_ntby_qty": 100_000, "orgn_ntby_qty": 0} for i in range(20)]
    weak_flow = [{"date": f"2026070{i % 9 + 1}", "close": 100, "volume": 1_000_000,
                 "frgn_ntby_qty": 0, "orgn_ntby_qty": 0} for i in range(20)]
    rf = FakeRedis(decode_responses=True)
    rf.sadd(bw.UNIVERSE_KEY, "AAA", "BBB")
    rf.set("bot:screen:flow:AAA", json.dumps(strong_flow))
    rf.set("bot:screen:flow:BBB", json.dumps(weak_flow))

    def flow_fetch(code, days):
        return {"AAA": rising, "BBB": rising2}.get(code, [])  # 둘 다 상승(차트조건 둘 다 통과)
    flow_job = {"run_id": "flow_run", "spec": flow_spec, "strategy": "trend", "universe": True,
               "days": 40, "top_k": 5}
    flow_meta = run_pipeline(rf, flow_job, flow_fetch)
    assert flow_meta["status"] == "done", flow_meta
    fstage1 = json.loads(rf.get(STAGE_KEY.format("flow_run", 1)))
    assert [c["code"] for c in fstage1["candidates"]] == ["AAA"], (
        f"차트조건은 둘 다 통과·flow 캐시는 AAA만 활성 → AAA만 후보: {fstage1}")

    print("✅ test_pipeline_worker: 4단계 체이닝(④자동등록)·idempotent 재개·부분 재개·staleness 캐스케이드·grid 선택화·flow 셋업 연결·큐 라운드트립 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
