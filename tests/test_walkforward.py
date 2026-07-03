# 워크포워드(trading/walkforward.py) 테스트 — 창 분할·인샘플 최적화·아웃오브샘플 이어붙임 (네트워크 없음)
"""실행: python tests/test_walkforward.py"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.spec import BASELINE_SPEC
from kr_research.trading.walkforward import walk_forward


def _bars(n=80):
    """RSI·SMA 가 움직이도록 진동하는 합성 일봉(tests/test_backtest_worker.py 와 동일 패턴)."""
    out = []
    for i in range(n):
        c = round(100 + 12 * math.sin(i / 3.0), 2)
        out.append({"date": f"d{i:03d}", "open": c, "high": c + 1, "low": c - 1,
                    "close": c, "volume": 1000})
    return out


def main() -> int:
    bars = _bars(80)
    params = {"rsi_buy": 45, "rsi_sell": 55, "sma_fast": 5, "sma_slow": 20, "qty": 1}
    grid = {"rsi_buy": [30, 45, 60]}

    wf = walk_forward(bars, "005930", BASELINE_SPEC, grid, train_days=30, test_days=10,
                      base_params=params, cash=10_000_000)
    assert wf["n_windows"] == 5, f"train=30 test=10 bars=80 → 창 5개 기대: {wf['n_windows']}"
    for w in wf["windows"]:
        for k in ("train_start", "train_end", "test_start", "test_end", "params",
                  "out_of_sample_return_pct", "n_trades", "final_equity"):
            assert k in w, f"창 결과 키 누락: {k}"
        assert "rsi_buy" in w["params"] and len(w["params"]) == 1, "params=그리드 차원만"
    # 창은 시간순 비중첩 — 학습 종료 < 검증 시작, 다음 창 학습 시작은 이전 검증 시작과 test_days 만큼 이동
    assert wf["windows"][0]["train_end"] < wf["windows"][0]["test_start"]
    assert wf["windows"][1]["train_start"] == "d010", wf["windows"][1]["train_start"]
    # 자산 이어붙임 — 마지막 창 final_equity == 종합 final_equity
    assert wf["final_equity"] == wf["windows"][-1]["final_equity"], "창 자산 체인 = 종합 최종자산"
    assert wf["total_return_pct"] == (wf["final_equity"] / 10_000_000 - 1) * 100

    # 데이터 부족(창 하나도 못 채움) → n_windows=0, 크래시 없이 cash 그대로
    short = walk_forward(bars[:20], "005930", BASELINE_SPEC, grid, train_days=30, test_days=10, cash=10_000_000)
    assert short["n_windows"] == 0 and short["windows"] == [] and short["final_equity"] == 10_000_000, short

    print("✅ test_walkforward: 창 분할·인샘플 최적화·아웃오브샘플 이어붙임·데이터부족 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
