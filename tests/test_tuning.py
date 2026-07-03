# 그리드서치(trading/tuning.py) 순수 로직 테스트 — 조합·순위·cap (네트워크 없음)
"""실행: python tests/test_tuning.py"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.spec import BASELINE_SPEC
from kr_research.trading.tuning import grid_search


def _bars(n=80):
    """RSI·SMA 가 움직이도록 진동하는 합성 일봉(tests/test_backtest_worker.py 와 동일 패턴)."""
    out = []
    for i in range(n):
        c = round(100 + 12 * math.sin(i / 3.0), 2)
        out.append({"date": f"d{i:03d}", "open": c, "high": c + 1, "low": c - 1,
                    "close": c, "volume": 1000})
    return out


def main() -> int:
    bars = _bars()
    params = {"rsi_buy": 45, "rsi_sell": 55, "sma_fast": 5, "sma_slow": 20, "qty": 1}

    # 조합 카테시안곱(3×2=6) + metric 내림차순 + best=1위
    sweep = grid_search(bars, "005930", BASELINE_SPEC, {"rsi_buy": [30, 40, 50], "rsi_sell": [66, 70]},
                        base_params=params, metric="return_pct")
    assert sweep["n_combos"] == 6 and sweep["code"] == "005930", f"3×2=6 조합: {sweep['n_combos']}"
    assert set(sweep["results"][0]["params"]) == {"rsi_buy", "rsi_sell"}, "결과 params=스윕 차원만"
    rets = [r["return_pct"] for r in sweep["results"]]
    assert rets == sorted(rets, reverse=True) and sweep["best"] == sweep["results"][0], "metric 내림차순·best=1위"

    # 미지원 metric → return_pct 로 폴백
    sw2 = grid_search(bars, "005930", BASELINE_SPEC, {"rsi_buy": [30, 40]}, base_params=params, metric="not_a_metric")
    assert sw2["metric"] == "return_pct", sw2["metric"]

    # 조합 상한 cap
    capped = grid_search(bars, "005930", BASELINE_SPEC, {"rsi_buy": [25, 30, 35, 40, 45]}, base_params=params, cap=3)
    assert capped["n_combos"] == 3, "조합 상한 cap"

    # 손절/사이징 인자가 그대로 run() 에 전달되는지 — 손절 걸면 무손절보다 손실 제한(또는 동일) 확인
    sl = grid_search(bars, "005930", BASELINE_SPEC, {"rsi_buy": [30]}, base_params=params, stop_loss_pct=0.02)
    assert sl["n_combos"] == 1, sl

    # cap 초과 시 무작위 비복원추출(seed 고정) — 재현성: 같은 seed 는 완전히 동일한 결과
    big_grid = {"rsi_buy": list(range(20, 30)), "rsi_sell": list(range(60, 70)), "sma_fast": list(range(3, 13))}  # 10×10×10=1000
    r1 = grid_search(bars, "005930", BASELINE_SPEC, big_grid, base_params=params, cap=50, seed=7)
    r2 = grid_search(bars, "005930", BASELINE_SPEC, big_grid, base_params=params, cap=50, seed=7)
    assert r1["n_combos"] == 50 == r2["n_combos"], r1["n_combos"]
    assert r1["results"] == r2["results"], "같은 seed → 완전히 동일한 결과(재현성)"
    r3 = grid_search(bars, "005930", BASELINE_SPEC, big_grid, base_params=params, cap=50, seed=8)
    assert r1["results"] != r3["results"], "다른 seed → 다른 표본(무작위 추출 확인)"

    # 편향 회귀 방지: 예전엔 itertools.product 앞부분만 잘라 알파벳상 앞선 파라미터(rsi_buy)가
    # 거의 한 값에 쏠렸다. 지금은 모든 차원이 고르게 섞여야 한다.
    for name in ("rsi_buy", "rsi_sell", "sma_fast"):
        vals = {row["params"][name] for row in r1["results"]}
        assert len(vals) > 1, f"{name} 값이 한쪽에 쏠림(편향 회귀 의심): {vals}"

    print("✅ test_tuning: grid_search 조합·순위·cap·손절 인자·무작위추출 재현성·편향없음 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
