# 조건검색 전진검증 — 신호 병합·forward 수익 적용·전략별 집계·요청 드레인 검증(fakeredis, 네트워크 없음)
"""실행: python tests/test_screen_track.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from tools.screen_track import (TRACK_REQUESTS_KEY, apply_exit_tracking, apply_returns, merge_signals,
                                newly_validated, open_codes, pipeline_signals, request_to_signal,
                                signal_id, summary_by_strategy)
from tools.screen_track_eval import _drain_requests, _load_strategies

_DATE = "20260601"

# exit 트리를 항상 False 로 둬 손절 판정만 격리 테스트(evaluate_exit 자체 검증은 tests/test_tracking.py)
_STOP_SPEC = {
    "name": "stop_only",
    "indicators": [{"id": "px", "type": "price", "params": {}}],
    "entry": {"gt": ["px", 0]},
    "exit": {"gt": ["px", 10**9]},
}


def _bars(base, step):
    """21봉(D0~D20) — close[i]=base+step*i. 시간순."""
    return [{"date": f"202606{i + 1:02d}", "close": base + step * i} for i in range(21)]


def main() -> int:
    # ── request_to_signal: 유효/무효 ──
    s = request_to_signal({"strategy": "정배열", "code": "005930", "screen_date": _DATE, "entry_price": 100})
    assert s["id"] == signal_id("정배열", "005930", _DATE) and s["ret_d5"] is None and s["entry_price"] == 100.0
    assert s["exited"] is False and s["capped"] is False and s["exit_reason"] is None, "청산-추적 필드 기본값"
    assert request_to_signal({"strategy": "x", "code": "005930", "screen_date": _DATE, "entry_price": 0}) is None, "진입가<=0 무효"
    assert request_to_signal({"strategy": "x", "code": "", "screen_date": _DATE, "entry_price": 1}) is None, "종목 누락 무효"

    # ── merge_signals: 중복 id 제외 + 기존 평가값 보존 + cap ──
    existing = [{**s, "ret_d1": 0.03}]  # 이미 D+1 평가됨
    merged = merge_signals(existing, [
        {"strategy": "정배열", "code": "005930", "screen_date": _DATE, "entry_price": 100},  # 중복 → 무시
        {"strategy": "정배열", "code": "000660", "screen_date": _DATE, "entry_price": 50},   # 신규
    ])
    assert len(merged) == 2 and merged[0]["ret_d1"] == 0.03, "중복 무시·기존 평가 보존"
    assert merge_signals([{"id": str(i)} for i in range(10)], [], cap=5) == [{"id": str(i)} for i in range(5, 10)], "cap 최신만"

    # ── apply_returns: 미평가 지평을 일봉으로 채움(+초과수익), 평가 대상만 ──
    sig = request_to_signal({"strategy": "정배열", "code": "005930", "screen_date": _DATE, "entry_price": 100})
    bars_by_code = {"005930": _bars(100, 1)}      # close: 100,101,...,120 → ret_d1=.01 d5=.05 d20=.20
    bench = _bars(100, 0.5)                        # bench: 100,100.5,... → bench_d5=.025
    n = apply_returns([sig], bars_by_code, bench)
    assert n == 1
    assert abs(sig["ret_d1"] - 0.01) < 1e-9 and abs(sig["ret_d5"] - 0.05) < 1e-9 and abs(sig["ret_d20"] - 0.20) < 1e-9
    assert abs(sig["bench_d5"] - 0.025) < 1e-9, "벤치 D+5"
    # 재실행: 이미 다 찼으니 갱신 0(보존)
    assert apply_returns([sig], bars_by_code, bench) == 0, "이미 평가된 지평 미갱신"
    # open_codes: 다 찬 신호는 제외
    assert open_codes([sig]) == set(), "평가 완료 신호는 open 아님"
    half = request_to_signal({"strategy": "x", "code": "111111", "screen_date": _DATE, "entry_price": 100})
    assert open_codes([half]) == {"111111"}, "미평가 신호 종목은 open"

    # ── summary_by_strategy: 전략별 묶음 + 검증 게이트(N>=30·승률>=.55·평균>0) ──
    win = [{"strategy": "정배열", "ret_d5": 0.03, "bench_d5": 0.0} for _ in range(30)]  # 30건 전부 양수
    summ = summary_by_strategy(win)
    assert set(summ["by_strategy"]) == {"정배열"} and summ["all"]["signals"] == 30
    agg = summ["by_strategy"]["정배열"]
    assert agg["win_d5"] == 1.0 and abs(agg["avg_d5"] - 0.03) < 1e-9 and agg["validated"] is True, f"검증 통과: {agg}"
    few = summary_by_strategy([{"strategy": "약", "ret_d5": 0.03, "bench_d5": 0.0} for _ in range(5)])
    assert few["by_strategy"]["약"]["validated"] is False, "표본<30 → 미검증"

    # ── 안전 불변식(중요): `validated`(D+N 게이트) 값은 core/control_bus.py::get_validated 가 읽어
    #    B2-b 실전 자동 승격을 게이트한다(코드로 확인: control_bus.py get_validated, main.py _load_strategy).
    #    청산-추적(Phase 2) 필드가 신호에 섞여 있어도 D+N 입력이 같으면 `validated` 는 절대 안 바뀌어야 함
    #    (새 게이트는 반드시 다른 키 `validated_realized`로만 산출) — 회귀로 고정. ──
    win_with_exit_fields = [{**request_to_signal({"strategy": "정배열", "code": "005930",
                                                  "screen_date": _DATE, "entry_price": 100}),
                            "ret_d5": 0.03, "bench_d5": 0.0, "exited": True,
                            "realized_return_pct": -99.0}  # 극단값이어도 D+N validated 에 영향 없어야 함
                           for _ in range(30)]
    agg_mixed = summary_by_strategy(win_with_exit_fields)["by_strategy"]["정배열"]
    assert agg_mixed["validated"] is True and agg["validated"] is True, (
        "청산-추적 필드 존재/값이 D+N validated(B2-b 라이브 게이트 소스)를 절대 바꾸면 안 됨")
    assert "validated_realized" in agg_mixed, "새 게이트는 별도 키(validated_realized)로만 산출"

    # ── apply_exit_tracking: 청산-추적(Phase 2) — 전체 OHLCV(저가/고가) + strategies 스냅샷으로 손절 판정 ──
    sig2 = request_to_signal({"strategy": "정배열", "code": "005930", "screen_date": _DATE, "entry_price": 100})
    full_bars = {"005930": [
        {"date": "20260601", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1000},
        {"date": "20260602", "open": 100, "high": 101, "low": 100, "close": 100, "volume": 1000},
        {"date": "20260603", "open": 94, "high": 96, "low": 90, "close": 94, "volume": 1000},  # 손절 트리거
    ]}
    strategies = {"정배열": _STOP_SPEC}
    n2 = apply_exit_tracking([sig2], full_bars, strategies, stop_loss_pct=0.05)
    assert n2 == 1, "평가(호출)한 신호 1건"
    assert sig2["exited"] is True and sig2["exit_reason"] == "stop_loss" and sig2["exit_price"] == 95.0, sig2
    # 이미 청산된 신호는 재평가 안 함(누적 호출 skip)
    assert apply_exit_tracking([sig2], full_bars, strategies, stop_loss_pct=0.05) == 0, "청산 완료 신호 재평가 skip"
    # spec 못 찾으면(전략명 변경·삭제) skip — exited 그대로 False
    sig3 = request_to_signal({"strategy": "없는전략", "code": "005930", "screen_date": _DATE, "entry_price": 100})
    n3 = apply_exit_tracking([sig3], full_bars, strategies, stop_loss_pct=0.05)
    assert n3 == 0 and sig3["exited"] is False, "spec 못 찾으면 안전 skip"

    # ── summary_by_strategy: 실현수익률 집계(청산 완료 건만) + validated_realized(D+N validated 와 별개) ──
    exited_rows = [{"strategy": "정배열", "exited": True, "realized_return_pct": 3.0, "holding_days": 5}
                  for _ in range(30)]
    summ2 = summary_by_strategy(exited_rows)
    agg2 = summ2["by_strategy"]["정배열"]
    assert agg2["n_exited"] == 30 and agg2["win_rate_realized"] == 1.0, agg2
    assert abs(agg2["avg_realized_return_pct"] - 3.0) < 1e-9 and agg2["avg_holding_days"] == 5, agg2
    assert agg2["validated_realized"] is True, agg2
    # 미청산 신호만 있으면 n_exited=0·validated_realized=False(에러 없이)
    open_rows = [{"strategy": "정배열", "exited": False}]
    agg_open = summary_by_strategy(open_rows)["by_strategy"]["정배열"]
    assert agg_open["n_exited"] == 0 and agg_open["validated_realized"] is False, agg_open

    # ── newly_validated: 직전 실행 대비 새로 validated=True 전환된 전략만(알림 전이감지) ──
    assert newly_validated(["정배열"], summ) == [], "이미 prev 에 있으면 신규 아님"
    assert newly_validated([], summ) == ["정배열"], "신규 검증통과"
    assert newly_validated(["정배열"], few) == [], "validated=False 는 제외"
    assert newly_validated(["사라진전략"], summ) == ["정배열"], "prev 의 다른 이름이 summary 에 없어도 에러 없음"

    # ── _drain_requests: 요청 큐 전량 드레인(손상 skip) ──
    r = FakeRedis(decode_responses=True)
    r.rpush(TRACK_REQUESTS_KEY, json.dumps({"strategy": "a", "code": "005930", "screen_date": _DATE, "entry_price": 1}))
    r.rpush(TRACK_REQUESTS_KEY, "{broken")
    r.rpush(TRACK_REQUESTS_KEY, json.dumps({"strategy": "b", "code": "000660", "screen_date": _DATE, "entry_price": 2}))
    reqs = _drain_requests(r)
    assert [q["strategy"] for q in reqs] == ["a", "b"] and r.llen(TRACK_REQUESTS_KEY) == 0, "정상 2건·큐 비움"

    # ── _load_strategies: bot:strategies 스냅샷 → {이름: spec}(파싱 실패 skip) ──
    rs = FakeRedis(decode_responses=True)
    rs.hset("bot:strategies", "정배열", json.dumps(_STOP_SPEC))
    rs.hset("bot:strategies", "깨짐", "{bad json")
    loaded = _load_strategies(rs)
    assert set(loaded) == {"정배열"} and loaded["정배열"] == _STOP_SPEC, "정상만 로드·깨진 항목 skip"
    assert _load_strategies(FakeRedis(decode_responses=True)) == {}, "빈 Hash → 빈 dict"

    # ── pipeline_signals(§5 연결고리): ②통과 종목을 ①후보의 종가·검색일과 조인 ──
    stage1 = {"candidates": [{"code": "AAA", "last": 1000, "date": "20260610"},
                             {"code": "BBB", "last": 2000, "date": "20260610"}]}
    stage2 = {"results": [{"code": "AAA", "return_pct": 5.0}, {"code": "CCC", "return_pct": 1.0}]}
    ps = pipeline_signals("전략1", stage1, stage2)
    assert ps == [{"strategy": "전략1", "code": "AAA", "entry_price": 1000, "screen_date": "20260610"}], (
        "②결과 중 ①후보에 있는 것만(AAA), CCC 는 ①에 없어 skip")
    assert pipeline_signals("", stage1, stage2) == [], "strategy 없으면 등록 skip"
    assert pipeline_signals("전략1", {"candidates": []}, stage2) == [], "①후보 없으면 조인 결과 없음"

    print("✅ test_screen_track: request/merge/apply_returns/apply_exit_tracking/open_codes/summary_by_strategy/pipeline_signals/drain 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
