# 백테스트 워커 — 순수 compute(합성 일봉) + 잡 처리(주입 fetch) + 큐 드레인(fakeredis) 검증 (네트워크 없음)
"""실행: python tests/test_backtest_worker.py
콘솔→Redis 큐→워커→결과키 라운드트립을 KIS 없이(주입 fetch_bars·fakeredis) 검증."""
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.backtest_worker import (BENCH_CODE, MAX_DAYS, QUEUE_KEY, RESULT_KEY, _benchmark_return,
                                   backtest_spec, batch_spec, drain, process_job, screen_spec, serve,
                                   sweep_spec)
from kr_research.trading.spec import BASELINE_SPEC

# 추세 스펙(지표만) — 강한 상승=sma_fast>sma_slow=후보, 하락=비후보(셋업 무관·결정적)
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


def _bars(n=80):
    """RSI·SMA 가 움직이도록 진동하는 합성 일봉(>=60 봉, 지표 계산 충분)."""
    out = []
    for i in range(n):
        c = round(100 + 12 * math.sin(i / 3.0), 2)
        out.append({"date": f"d{i:03d}", "open": c, "high": c + 1, "low": c - 1,
                    "close": c, "volume": 1000})
    return out


def main() -> int:
    bars = _bars()
    params = {"rsi_buy": 45, "rsi_sell": 55, "sma_fast": 5, "sma_slow": 20, "qty": 1}

    # ── 순수 compute: 구조·타입·metrics 일관성 ──
    r = backtest_spec(bars, "005930", BASELINE_SPEC, params)
    for k in ("code", "name", "bars", "first", "last", "return_pct", "n_trades",
              "final_equity", "fees", "tax", "metrics", "equity_curve", "trades"):
        assert k in r, f"결과 키 누락: {k}"
    assert len(r["equity_curve"]) == len(bars) and all(isinstance(v, int) for v in r["equity_curve"]), "수익곡선=봉별 자산(정수)"
    assert len(r["trades"]) == r["n_trades"], "거래내역 길이=n_trades"
    for t in r["trades"]:
        assert set(t) == {"date", "side", "qty", "price"} and t["side"] in ("buy", "sell"), f"거래내역 행 형태: {t}"
    assert r["code"] == "005930" and r["bars"] == len(bars)
    assert r["first"] == "d000" and r["last"] == f"d{len(bars) - 1:03d}"
    assert isinstance(r["n_trades"], int) and r["n_trades"] >= 0
    # metrics 는 거래가 있을 때만 채워짐(없으면 빈 dict)
    assert (r["metrics"] != {}) == (r["n_trades"] > 0), "metrics ⇔ n_trades 일관성"
    assert json.dumps(r)  # JSON 직렬화 가능(결과키 저장 가능)

    # ── process_job: 정상(done) ──
    fetch = lambda code, days: bars  # noqa: E731  KIS 대체(합성봉 주입)
    ok = process_job({"job_id": "j1", "spec": BASELINE_SPEC, "code": "005930",
                      "days": 90, "params": params}, fetch)
    assert ok["status"] == "done" and ok["job_id"] == "j1"
    assert ok["result"]["code"] == "005930" and ok["result"]["days"] == 90
    # 벤치마크(코스피 ETF) 대비 알파 — 단일 백테스트에 bench_return_pct/excess_pct 포함(벤치 fetch 성공 시)
    assert "bench_return_pct" in ok["result"] and "excess_pct" in ok["result"], "단일 백테스트 알파 포함"
    assert ok["result"]["excess_pct"] == round(
        ok["result"]["return_pct"] - ok["result"]["bench_return_pct"], 2), "초과수익=전략−벤치"

    # ── process_job: days clamp(상한) ──
    clamped = process_job({"job_id": "j2", "spec": BASELINE_SPEC, "code": "005930",
                           "days": 99999}, fetch)
    assert clamped["status"] == "done" and clamped["result"]["days"] == MAX_DAYS, "days 상한 clamp"

    # ── process_job: 에러 — spec 누락 / 일봉 0건 / 미지의 셋업 ──
    bad_spec = process_job({"job_id": "e1", "code": "005930"}, fetch)
    assert bad_spec["status"] == "error" and "job_id" in bad_spec
    empty = process_job({"job_id": "e2", "spec": BASELINE_SPEC, "code": "005930"},
                        lambda code, days: [])
    assert empty["status"] == "error" and "0건" in empty["error"]
    unknown = process_job({"job_id": "e3", "code": "005930", "spec": {
        "indicators": [], "entry": {"setup": "no_such_setup"}, "exit": {}}}, fetch)
    assert unknown["status"] == "error", "미지의 셋업 key → validate 에러"

    # ── 벤치마크 알파: _benchmark_return 구간 정렬 + process_job 이 BENCH_CODE 로 벤치 fetch ──
    seq = _series([100, 110, 121])  # d000~d002, +21% 매수후보유
    assert round(_benchmark_return(seq, "d000", "d002"), 2) == 21.0, "벤치 매수후보유 수익률(%)"
    assert _benchmark_return(seq, "d001", "d001") is None, "구간 내 2봉 미만 → None"
    # 벤치(069500)는 별 series 로 fetch — 종목과 다른 벤치 수익률이 결과에 반영되는지
    bench_flat = _series([100] * len(bars))  # 벤치 0% → excess == 전략 수익률
    fetch_bench = lambda code, days: bench_flat if code == BENCH_CODE else bars  # noqa: E731
    okb = process_job({"job_id": "j1b", "spec": BASELINE_SPEC, "code": "005930",
                       "days": 90, "params": params}, fetch_bench)
    assert okb["result"]["bench_return_pct"] == 0.0, "평탄 벤치 = 0%"
    assert okb["result"]["excess_pct"] == okb["result"]["return_pct"], "벤치 0% → 초과수익=전략 수익률"
    # 벤치 자신(069500) 백테스트엔 알파 생략(자기 대비 무의미)
    okself = process_job({"job_id": "jbs", "spec": BASELINE_SPEC, "code": BENCH_CODE,
                          "days": 90, "params": params}, fetch_bench)
    assert "excess_pct" not in okself["result"], "벤치 자신 백테스트는 알파 생략"

    # ── screen_spec: 다종목 후보 추출(상승=후보, 하락=비후보, 빈봉 skip) ──
    rising = _series([100 + i for i in range(40)])
    falling = _series([140 - i for i in range(40)])
    sr = screen_spec({"A": rising, "B": falling, "C": []}, _TREND_SPEC, {})
    assert sr["screened"] == 2, "빈 일봉(C) 제외"
    assert [c["code"] for c in sr["candidates"]] == ["A"], f"상승만 후보: {sr['candidates']}"
    assert sr["candidates"][0]["last"] == rising[-1]["close"] and sr["n_candidates"] == 1

    # ── batch_spec: 다종목 백테스트 요약, 수익률 내림차순, 빈봉 skip, 매수후보유(알파용) ──
    br = batch_spec({"A": rising, "B": falling, "C": []}, _TREND_SPEC, params)
    assert br["tested"] == 2 and br["n"] == 2, f"빈봉(C) 제외: {br}"
    rets = [r["return_pct"] for r in br["results"]]
    assert rets == sorted(rets, reverse=True), f"수익률 내림차순: {rets}"
    for row in br["results"]:
        assert set(row) == {"code", "name", "return_pct", "n_trades", "final_equity",
                            "win_rate", "buyhold_return_pct"}, f"배치 행 형태: {row}"
    row_a = next(r for r in br["results"] if r["code"] == "A")
    row_b = next(r for r in br["results"] if r["code"] == "B")
    assert row_a["buyhold_return_pct"] == 39.0, f"매수후보유(rising 100→139): {row_a}"
    assert row_b["buyhold_return_pct"] == -27.86, f"매수후보유(falling 140→101): {row_b}"

    # ── process_job: screen 분기(type) + days + 에러(codes 누락) ──
    fetch2 = lambda code, days: {"AAA": rising, "BBB": falling}.get(code, [])  # noqa: E731
    sj = process_job({"job_id": "s1", "type": "screen", "spec": _TREND_SPEC,
                      "codes": ["AAA", "BBB", "  "], "days": 60}, fetch2)
    assert sj["status"] == "done" and sj["type"] == "screen"
    assert [c["code"] for c in sj["result"]["candidates"]] == ["AAA"] and sj["result"]["days"] == 60
    no_codes = process_job({"job_id": "s2", "type": "screen", "spec": _TREND_SPEC, "codes": []}, fetch2)
    assert no_codes["status"] == "error", "codes 누락 → 에러"

    # ── process_job: batch 분기(type) — 다종목 백테스트 요약 + days + 에러(codes 누락) ──
    bj = process_job({"job_id": "b1", "type": "batch", "spec": _TREND_SPEC,
                      "codes": ["AAA", "BBB"], "days": 60}, fetch2)
    assert bj["status"] == "done" and bj["type"] == "batch"
    assert bj["result"]["n"] == 2 and bj["result"]["days"] == 60
    no_codes_b = process_job({"job_id": "b2", "type": "batch", "spec": _TREND_SPEC, "codes": []}, fetch2)
    assert no_codes_b["status"] == "error", "batch codes 누락 → 에러"

    # ── sweep_spec: 파라미터 그리드 서치(조합 카테시안곱·순위·best·cap) ──
    sweep = sweep_spec(bars, "005930", BASELINE_SPEC, {"rsi_buy": [30, 40, 50], "rsi_sell": [66, 70]},
                       base_params=params, metric="return_pct")
    assert sweep["n_combos"] == 6 and sweep["code"] == "005930", f"3×2=6 조합: {sweep['n_combos']}"
    assert set(sweep["results"][0]["params"]) == {"rsi_buy", "rsi_sell"}, "결과 params=스윕 차원만"
    rets = [r["return_pct"] for r in sweep["results"]]
    assert rets == sorted(rets, reverse=True) and sweep["best"] == sweep["results"][0], "metric 내림차순·best=1위"
    # 정렬 지표 지정(sharpe) — None 은 뒤로
    sw2 = sweep_spec(bars, "005930", BASELINE_SPEC, {"rsi_buy": [30, 40]}, base_params=params, metric="sharpe")
    assert sw2["metric"] == "sharpe"
    # cap: 조합이 상한을 넘으면 잘림
    capped = sweep_spec(bars, "005930", BASELINE_SPEC, {"rsi_buy": [25, 30, 35, 40, 45]}, base_params=params, cap=3)
    assert capped["n_combos"] == 3, "조합 상한 cap"

    # ── process_job: sweep 분기 + 에러(grid 누락) ──
    swj = process_job({"job_id": "w1", "type": "sweep", "spec": BASELINE_SPEC, "code": "005930",
                       "days": 90, "params": params, "grid": {"rsi_buy": [30, 40]}}, fetch)
    assert swj["status"] == "done" and swj["type"] == "sweep" and swj["result"]["n_combos"] == 2
    no_grid = process_job({"job_id": "w2", "type": "sweep", "spec": BASELINE_SPEC, "code": "005930", "grid": {}}, fetch)
    assert no_grid["status"] == "error", "grid 누락 → 에러"

    # ── screen: 한 종목 fetch 실패(레이트리밋 등)는 skip, 잡은 done(전체 보호) ──
    def fetch_err(code, days):
        if code == "BAD":
            raise RuntimeError("초당 거래건수를 초과")
        return {"AAA": rising}.get(code, [])
    sj2 = process_job({"job_id": "s3", "type": "screen", "spec": _TREND_SPEC,
                       "codes": ["AAA", "BAD"], "days": 60}, fetch_err)
    assert sj2["status"] == "done", "한 종목 실패가 전체 잡을 죽이지 않음"
    assert [c["code"] for c in sj2["result"]["candidates"]] == ["AAA"], "성공 종목만 후보"

    import tools.backtest_worker as bw

    assert ok["type"] == "backtest", "기본(type 없음) → backtest 처리(하위호환)"

    # ── _cached_fetch: 첫 호출만 원천 fetch, 같은 (code,days) 재호출은 캐시(KIS 미호출) ──
    from fakeredis import FakeRedis
    frc = FakeRedis(decode_responses=True)
    raw_calls = []
    def _raw(code, days):
        raw_calls.append((code, days))
        return rising
    cf = bw._cached_fetch(frc, _raw, ttl=60)
    assert cf("005930", 90) == rising and len(raw_calls) == 1, "첫 호출=원천 fetch"
    assert cf("005930", 90) == rising and len(raw_calls) == 1, "재호출=캐시(원천 미호출)"
    assert cf("005930", 30) == rising and len(raw_calls) == 2, "다른 days=다른 키=원천 호출"
    assert frc.ttl(bw.OHLCV_CACHE_KEY.format("005930", 90)) > 0, "캐시 TTL 설정"
    # 빈 결과는 캐시 안 함(다음에 다시 시도)
    cf_empty = bw._cached_fetch(frc, lambda c, d: [], ttl=60)
    assert cf_empty("000000", 90) == [] and not frc.exists(bw.OHLCV_CACHE_KEY.format("000000", 90)), "빈봉 미캐시"

    # ── drain: fakeredis 큐 라운드트립(정상2 + 손상1 skip) ──
    fr = FakeRedis(decode_responses=True)
    fr.rpush(QUEUE_KEY, json.dumps({"job_id": "q1", "spec": BASELINE_SPEC, "code": "005930", "params": params}))
    fr.rpush(QUEUE_KEY, json.dumps({"job_id": "q2", "spec": BASELINE_SPEC, "code": "000660", "params": params}))
    fr.rpush(QUEUE_KEY, "{not json")  # 손상 잡 → skip(결과키 없음·미카운트)
    n = drain(fr, fetch)
    assert n == 2, f"정상 2건만 처리(손상 skip), got {n}"
    assert fr.llen(QUEUE_KEY) == 0, "큐 비워짐"
    for jid, code in (("q1", "005930"), ("q2", "000660")):
        raw = fr.get(RESULT_KEY.format(jid))
        assert raw, f"결과키 누락: {jid}"
        out = json.loads(raw)
        assert out["status"] == "done" and out["result"]["code"] == code
        assert fr.ttl(RESULT_KEY.format(jid)) > 0, "결과키 TTL 설정"

    # ── serve(데몬): BLPOP 으로 즉시 처리 — should_continue 로 정확히 2건만 돌고 종료 ──
    fr2 = FakeRedis(decode_responses=True)
    fr2.rpush(QUEUE_KEY, json.dumps({"job_id": "d1", "spec": BASELINE_SPEC, "code": "005930", "params": params}))
    fr2.rpush(QUEUE_KEY, json.dumps({"job_id": "d2", "spec": BASELINE_SPEC, "code": "000660", "params": params}))
    ticks = [0]
    def _cont():  # 루프 상단 점검 — 2회만 True(잡 2건 처리 후 종료, 빈 BLPOP 미도달)
        ticks[0] += 1
        return ticks[0] <= 2
    sn = serve(fr2, fetch, poll_timeout=1, should_continue=_cont)
    assert sn == 2, f"데몬이 잡 2건 처리, got {sn}"
    for jid, code in (("d1", "005930"), ("d2", "000660")):
        out = json.loads(fr2.get(RESULT_KEY.format(jid)))
        assert out["status"] == "done" and out["result"]["code"] == code, f"데몬 결과 누락/불일치: {jid}"
    assert fr2.llen(QUEUE_KEY) == 0, "데몬이 큐 비움"

    # ── 유니버스 스크리닝: _handle_raw 가 유니버스 집합→codes + 캐시 전용 fetch(콜드 미스 skip) ──
    fru = FakeRedis(decode_responses=True)
    fru.sadd(bw.UNIVERSE_KEY, "AAA", "BBB", "CCC")           # 유니버스 3종목
    fru.set(bw.OHLCV_CACHE_KEY.format("AAA", 90), json.dumps(rising))   # 워밍(상승=후보)
    fru.set(bw.OHLCV_CACHE_KEY.format("BBB", 90), json.dumps(falling))  # 워밍(하락=비후보)
    # CCC 는 미워밍 → 캐시 전용 fetch 가 [] → skip(KIS 미접속)
    ujob = json.dumps({"job_id": "u1", "type": "screen", "universe": True, "spec": _TREND_SPEC, "days": 90})
    assert bw._handle_raw(fru, ujob, fetch)
    uout = json.loads(fru.get(RESULT_KEY.format("u1")))
    assert uout["status"] == "done" and uout["result"]["universe"] is True
    assert uout["result"]["screened"] == 2, f"워밍된 2종목만 평가(CCC skip): {uout['result']}"
    assert [c["code"] for c in uout["result"]["candidates"]] == ["AAA"], "상승만 후보"
    cof = bw._cache_only_fetch(fru)
    assert cof("AAA", 90) == rising and cof("ZZZ", 90) == [], "캐시 히트/미스([])"

    # 빈 유니버스 → 친절한 에러
    fre = FakeRedis(decode_responses=True)
    assert bw._handle_raw(fre, json.dumps({"job_id": "u2", "type": "screen", "universe": True,
                                           "spec": _TREND_SPEC, "days": 90}), fetch)
    eout = json.loads(fre.get(RESULT_KEY.format("u2")))
    assert eout["status"] == "error" and "유니버스가 비어" in eout["error"], f"빈 유니버스 에러: {eout}"

    # ── §스크리닝 강화(외국인·기관 수급): screen_spec 에 flow_by_code 직접 주입 ──
    flow_spec = {"name": "flow", "indicators": [], "entry": {"setup": "foreign_accumulation"}, "exit": {"any": []}}
    strong_flow = [{"date": f"2026070{i % 9 + 1}", "close": 100, "volume": 1_000_000,
                    "frgn_ntby_qty": 100_000, "orgn_ntby_qty": 0} for i in range(20)]
    weak_flow = [{"date": f"2026070{i % 9 + 1}", "close": 100, "volume": 1_000_000,
                 "frgn_ntby_qty": 0, "orgn_ntby_qty": 0} for i in range(20)]
    frs = screen_spec({"A": rising, "B": falling}, flow_spec, {}, flow_by_code={"A": strong_flow, "B": weak_flow})
    assert [c["code"] for c in frs["candidates"]] == ["A"], f"A만 수급 활성: {frs}"
    assert screen_spec({"A": rising}, flow_spec, {}, flow_by_code=None)["candidates"] == [], "flow_by_code 없으면 항상 비활성"

    # ── _handle_raw: 유니버스 스크리닝 + flow 셋업 참조 스펙 → flow_universe 캐시(bot:screen:flow:{code}) 사용 ──
    fru2 = FakeRedis(decode_responses=True)
    fru2.sadd(bw.UNIVERSE_KEY, "AAA", "BBB")
    fru2.set(bw.OHLCV_CACHE_KEY.format("AAA", 90), json.dumps(rising))
    fru2.set(bw.OHLCV_CACHE_KEY.format("BBB", 90), json.dumps(rising))  # 차트조건 없음(둘 다 워밍만 필요)
    fru2.set("bot:screen:flow:AAA", json.dumps(strong_flow))
    fru2.set("bot:screen:flow:BBB", json.dumps(weak_flow))
    fjob = json.dumps({"job_id": "f1", "type": "screen", "universe": True, "spec": flow_spec, "days": 90})
    assert bw._handle_raw(fru2, fjob, fetch)
    fout = json.loads(fru2.get(RESULT_KEY.format("f1")))
    assert [c["code"] for c in fout["result"]["candidates"]] == ["AAA"], f"유니버스 경로도 flow 캐시 반영: {fout}"

    # ── _handle_raw: 비유니버스(codes) 스크리닝 + flow 셋업 → naver_investor.daily_investor_flow 라이브 호출 ──
    import tools.naver_investor as ni
    calls = []

    def fake_daily(code, days=20, timeout=15):
        calls.append(code)
        return strong_flow if code == "AAA" else weak_flow
    orig_daily = ni.daily_investor_flow
    ni.daily_investor_flow = fake_daily
    try:
        fru3 = FakeRedis(decode_responses=True)
        cjob = json.dumps({"job_id": "f2", "type": "screen", "codes": ["AAA", "BBB"],
                           "spec": flow_spec, "days": 90})
        assert bw._handle_raw(fru3, cjob, fetch)
    finally:
        ni.daily_investor_flow = orig_daily
    fout2 = json.loads(fru3.get(RESULT_KEY.format("f2")))
    assert [c["code"] for c in fout2["result"]["candidates"]] == ["AAA"], f"codes 경로 라이브 flow 반영: {fout2}"
    assert set(calls) == {"AAA", "BBB"}, f"codes 경로는 요청 종목만 라이브 조회: {calls}"

    # ── flow 셋업 미참조 스펙은 flow 조회 자체를 안 함(불필요한 호출 0) ──
    def _boom(*a, **k):
        raise AssertionError("uses_flow_setups=False 인데 daily_investor_flow 가 호출됨")
    ni.daily_investor_flow = _boom
    try:
        fru4 = FakeRedis(decode_responses=True)
        njob = json.dumps({"job_id": "f3", "type": "screen", "codes": ["AAA"], "spec": _TREND_SPEC, "days": 90})
        assert bw._handle_raw(fru4, njob, fetch)
    finally:
        ni.daily_investor_flow = orig_daily
    nout = json.loads(fru4.get(RESULT_KEY.format("f3")))
    assert nout["status"] == "done", "flow 미참조 스펙은 정상 처리(호출 없이)"

    print("✅ test_backtest_worker: compute·process_job(backtest/screen/batch/sweep/종목격리)·screen_spec(flow 포함)·_cached_fetch·universe·flow_by_code(캐시·라이브·미참조skip)·drain·serve(데몬) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
