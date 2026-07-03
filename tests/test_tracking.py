# 전진 검증 순수 로직 — forward_returns(거래일=바)·summarize(프리셋별·게이트) (네트워크 없음)
"""실행: python tests/test_tracking.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.tracking import benchmark_returns, evaluate_exit, forward_returns, summarize, summarize_actions

# exit 트리를 항상 False 로 둬 손절/익절 판정만 순수하게 격리 테스트(spec exit_condition 미간섭)
_NOOP_EXIT_SPEC = {
    "name": "noop_exit",
    "indicators": [{"id": "px", "type": "price", "params": {}}],
    "entry": {"gt": ["px", 0]},
    "exit": {"gt": ["px", 10**9]},  # 항상 False
}
# 하락 크로스로 exit 트리가 발동하는 스펙(exit_condition 경로 테스트용)
_CROSS_SPEC = {
    "name": "cross",
    "indicators": [
        {"id": "sma_fast", "type": "sma", "params": {"period": 5}},
        {"id": "sma_slow", "type": "sma", "params": {"period": 20}},
    ],
    "entry": {"gt": ["sma_fast", "sma_slow"]},
    "exit": {"not": {"gt": ["sma_fast", "sma_slow"]}},
}


def _obars(closes, lows=None, highs=None):
    """{date,open,high,low,close,volume} 시간순 — low/high 미지정 시 close 와 동일."""
    return [{"date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "open": c,
             "high": (highs[i] if highs else c), "low": (lows[i] if lows else c),
             "close": c, "volume": 1000} for i, c in enumerate(closes)]


def main() -> int:
    # 거래일=바: 20260101..20260107 종가 [100,101,102,103,104,105,110]
    bars = [{"date": f"2026010{i}", "close": c}
            for i, c in enumerate([100, 101, 102, 103, 104, 105, 110], start=1)]
    r = forward_returns(bars, "20260101", 100.0, (1, 5, 20))
    assert abs(r[1] - 0.01) < 1e-9, r            # D+1 종가 101 → +1%
    assert abs(r[5] - 0.05) < 1e-9, r            # D+5 종가 105 → +5%
    assert r[20] is None, "D+20 미경과 → None"

    # 중간 거래일 진입(20260103 @102): D+1=103, D+5 미경과
    r2 = forward_returns(bars, "20260103", 102.0, (1, 5))
    assert abs(r2[1] - (103 / 102 - 1)) < 1e-9 and r2[5] is None, r2

    # 방어: entry<=0, 빈 bars, 범위 밖(미래) 거래일
    assert forward_returns(bars, "20260101", 0, (1,))[1] is None, "entry<=0 → None"
    assert forward_returns([], "20260101", 100, (1,))[1] is None, "빈 bars → None"
    assert forward_returns(bars, "20260201", 100, (1,))[1] is None, "미래 거래일 → None"

    # summarize: 프리셋별 집계 + 게이트(N<30 → 미검증)
    sigs = [{"mode": "aggressive", "ret_d1": 0.02, "ret_d5": 0.03},
            {"mode": "aggressive", "ret_d1": -0.01, "ret_d5": None},  # d5 미평가
            {"mode": "neutral", "ret_d1": 0.0, "ret_d5": -0.02}]
    sm = summarize(sigs, (1, 5))
    agg = sm["by_mode"]["aggressive"]
    assert agg["signals"] == 2 and agg["pending_d5"] == 1, agg
    assert abs(agg["avg_d1"] - 0.005) < 1e-9, agg     # (0.02-0.01)/2
    assert agg["win_d1"] == 0.5, agg                  # 1/2 양수
    assert agg["validated"] is False, "N<30 → 미검증"
    assert sm["all"]["signals"] == 3, sm

    # benchmark_returns: base = 거래일 벤치 종가(close[D]→close[D+N])
    bench = [{"date": f"2026010{i}", "close": c}
             for i, c in enumerate([200, 202, 201, 203, 204, 210, 220], start=1)]
    b = benchmark_returns(bench, "20260101", (1, 5))
    assert abs(b[1] - (202 / 200 - 1)) < 1e-9 and abs(b[5] - (210 / 200 - 1)) < 1e-9, b
    assert benchmark_returns([], "20260101", (1,))[1] is None, "빈 벤치 → None"

    # summarize 초과수익: ret - bench. 벤치 없으면 excess None.
    ex = [{"mode": "a", "ret_d5": 0.03, "bench_d5": 0.01},   # 초과 +0.02
          {"mode": "a", "ret_d5": 0.00, "bench_d5": 0.01}]   # 초과 -0.01
    ea = summarize(ex, (5,))["by_mode"]["a"]
    assert abs(ea["avg_excess_d5"] - 0.005) < 1e-9 and ea["win_excess_d5"] == 0.5, ea
    assert summarize([{"mode": "a", "ret_d5": 0.03}], (5,))["by_mode"]["a"]["avg_excess_d5"] is None, "벤치 없으면 excess None"

    # 게이트 통과: D+5 평가 35건·승률 80%·평균>0 → validated (벤치 없으면 절대수익 기준)
    many = [{"mode": "x", "ret_d1": None, "ret_d5": (0.01 if i % 5 else -0.01)} for i in range(35)]
    g = summarize(many, (1, 5))["by_mode"]["x"]
    assert g["win_d5"] >= 0.55 and g["avg_d5"] > 0 and g["validated"] is True, g

    # 게이트: 절대수익 +지만 초과수익 −면 미검증(알파 없음)
    neg_alpha = [{"mode": "y", "ret_d5": 0.01, "bench_d5": 0.03} for _ in range(35)]
    gy = summarize(neg_alpha, (5,))["by_mode"]["y"]
    assert gy["avg_d5"] > 0 and gy["avg_excess_d5"] < 0 and gy["validated"] is False, gy

    # ── evaluate_exit: 손절 — 진입 다음 거래일에 low 가 손절가 아래로 터치 ──
    closes = [100, 100, 94, 100, 100]
    lows = [100, 100, 90, 100, 100]
    highs = [100, 100, 96, 100, 100]
    bars_sl = _obars(closes, lows, highs)
    r_sl = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date=bars_sl[0]["date"],
                         bars=bars_sl, stop_loss_pct=0.05)
    assert r_sl["exited"] is True and r_sl["exit_reason"] == "stop_loss", r_sl
    assert r_sl["exit_price"] == 95.0 and r_sl["holding_days"] == 2, r_sl
    assert r_sl["exit_date"] == bars_sl[2]["date"], r_sl

    # ── evaluate_exit: 익절 — high 가 익절가 위로 터치 ──
    closes2 = [100, 100, 106, 100, 100]
    highs2 = [100, 100, 108, 100, 100]
    bars_tp = _obars(closes2, closes2, highs2)
    r_tp = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date=bars_tp[0]["date"],
                         bars=bars_tp, take_profit_pct=0.05)
    assert r_tp["exited"] is True and r_tp["exit_reason"] == "take_profit" and r_tp["exit_price"] == 105.0, r_tp

    # ── evaluate_exit: 진입 봉 당일은 청산 대상 아님(불변식) — 진입 봉 자체의 low 가 극단이어도 무시 ──
    closes3 = [100, 100, 100, 100]
    lows3 = [10, 100, 100, 100]  # 진입 봉(index0) low=10 이면 즉시 손절 트리거될 값이지만 대상 아님
    bars_inv = _obars(closes3, lows3)
    r_inv = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date=bars_inv[0]["date"],
                          bars=bars_inv, stop_loss_pct=0.05, max_hold_days=3)
    assert r_inv["exited"] is False, "진입 봉 당일 손절가 터치는 무시돼야 함(불변식)"

    # ── evaluate_exit: exit_condition — spec exit 트리(하락 크로스)가 손절/익절보다 먼저 발동 ──
    trend = [100 + i for i in range(25)] + [124 - i for i in range(15)]  # 상승 후 하락 크로스
    bars_cross = _obars(trend)
    entry_date_cross = bars_cross[24]["date"]
    r_cross = evaluate_exit(_CROSS_SPEC, {}, entry_price=124.0, entry_date=entry_date_cross, bars=bars_cross)
    assert r_cross["exited"] is True and r_cross["exit_reason"] == "exit_condition", r_cross
    assert r_cross["holding_days"] > 0 and r_cross["exit_price"] < 124.0, r_cross

    # ── evaluate_exit: 비용모델 — 수수료·세금·슬리피지 반영(손절: 슬리피지 미적용 / exit_condition: 슬리피지 적용) ──
    r_sl_cost = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date=bars_sl[0]["date"],
                              bars=bars_sl, stop_loss_pct=0.05, fee_rate=0.001, tax_rate=0.002, slippage=0.01)
    # entry_cost = 100*1.01*1.001, proceeds = 95*(1-0.001-0.002)(손절은 슬리피지 미적용)
    expect = (95 * (1 - 0.001 - 0.002) / (100 * 1.01 * 1.001) - 1) * 100
    assert abs(r_sl_cost["realized_return_pct"] - expect) < 1e-9, r_sl_cost

    # ── evaluate_exit: 미청산(capped) — max_hold_days 안에 아무 것도 안 뜨면 미청산 ──
    flat = _obars([100] * 6)
    r_cap = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date=flat[0]["date"],
                          bars=flat, max_hold_days=3)
    assert r_cap["exited"] is False and r_cap["capped"] is True and r_cap["holding_days"] == 3, r_cap
    assert r_cap["open_return_pct"] == 0.0, r_cap

    # ── evaluate_exit: entry_date 를 찾을 수 없음(범위 밖) → 안전 기본값 ──
    r_missing = evaluate_exit(_NOOP_EXIT_SPEC, {}, entry_price=100.0, entry_date="20990101", bars=flat)
    assert r_missing == {"exited": False, "holding_days": 0, "open_return_pct": None, "capped": False}, r_missing

    # ── summarize_actions: sell 은 ret/bench 부호 반전(가격 하락="적중"), buy·hold 는 원래 부호 ──
    rows = [
        {"action": "buy",  "ret_d5": 0.03, "bench_d5": 0.01},   # buy: 가격↑ → 그대로 +0.03(적중)
        {"action": "sell", "ret_d5": 0.03, "bench_d5": 0.01},   # sell: 가격↑ → 반전돼 -0.03(오판)
        {"action": "sell", "ret_d5": -0.02, "bench_d5": 0.00},  # sell: 가격↓ → 반전돼 +0.02(적중)
        {"action": "hold", "ret_d5": 0.01, "bench_d5": None},   # hold: 원래 부호 그대로
    ]
    sa = summarize_actions(rows, (5,))["by_mode"]
    assert abs(sa["buy"]["avg_d5"] - 0.03) < 1e-9, sa["buy"]
    assert abs(sa["sell"]["avg_d5"] - (-0.005)) < 1e-9, sa["sell"]        # (-0.03 + 0.02)/2, 부호 반전됨
    assert abs(sa["sell"]["avg_excess_d5"] - 0.0) < 1e-9, sa["sell"]      # ((-0.03)-(-0.01) + (0.02-0))/2
    assert abs(sa["hold"]["avg_d5"] - 0.01) < 1e-9, sa["hold"]
    # 원본 summarize() 는 그대로(반전 없음) — summarize_actions 가 별도 함수임을 확인
    plain = summarize([{**r, "mode": r["action"]} for r in rows], (5,))["by_mode"]
    assert abs(plain["sell"]["avg_d5"] - 0.005) < 1e-9, "summarize() 는 부호 반전 없이 원본 그대로여야 함"

    print("✅ test_tracking: forward_returns·benchmark_returns·초과수익·게이트(알파 조건)·"
          "evaluate_exit(손절·익절·불변식·exit_condition·비용모델·capped)·summarize_actions(sell 부호반전) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
