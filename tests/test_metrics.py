# 백테스트 지표 순수 로직 — round_trips·MDD·Sharpe·VaR·요약 (네트워크 없음)
"""실행: python tests/test_metrics.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.metrics import daily_returns, max_drawdown, round_trips, summary


def main() -> int:
    # 라운드트립: buy→sell 쌍 순수익률(비용 0 → 가격비)
    trades = [
        {"side": "buy", "qty": 10, "price": 100.0, "fee": 0.0},
        {"side": "sell", "qty": 10, "price": 110.0, "fee": 0.0, "tax": 0.0},   # +10%
        {"side": "buy", "qty": 10, "price": 100.0, "fee": 0.0},
        {"side": "sell", "qty": 10, "price": 90.0, "fee": 0.0, "tax": 0.0},    # -10%
    ]
    rts = round_trips(trades)
    assert len(rts) == 2 and abs(rts[0] - 0.1) < 1e-9 and abs(rts[1] + 0.1) < 1e-9, rts
    # 비용 반영: buy fee 10 → cost 1010, sell fee 5 tax 5 → proceeds 1090 → 1090/1010-1
    rt_fee = round_trips([{"side": "buy", "qty": 10, "price": 100.0, "fee": 10.0},
                          {"side": "sell", "qty": 10, "price": 110.0, "fee": 5.0, "tax": 5.0}])
    assert abs(rt_fee[0] - (1090 / 1010 - 1)) < 1e-9, rt_fee

    # 자산곡선 지표
    curve = [1000.0, 1100.0, 1045.0, 1150.0]
    assert [round(r, 4) for r in daily_returns(curve)] == [0.1, -0.05, round(1150 / 1045 - 1, 4)]
    assert abs(max_drawdown(curve) - 0.05) < 1e-9, "MDD: 1100→1045 = 5%"
    assert max_drawdown([]) == 0.0 and max_drawdown([500.0]) == 0.0, "빈/단일 → 0"

    m = summary(curve, trades)
    assert m["n_round_trips"] == 2 and m["win_rate"] == 0.5, m
    assert abs(m["expectancy"]) < 1e-9 and abs(m["payoff"] - 1.0) < 1e-9, m
    assert abs(m["profit_factor"] - 1.0) < 1e-9 and abs(m["kelly"]) < 1e-9, m
    assert abs(m["mdd"] - 0.05) < 1e-9 and m["sharpe"] is not None and m["sortino"] is not None, m
    # VaR95/CVaR95 (3 일간수익률 [0.1,-0.05,0.1005]) — 5분위 보간·꼬리 평균
    assert abs(m["var95"] - (-0.035)) < 1e-9 and abs(m["cvar95"] - (-0.05)) < 1e-9, m

    # 표본 부족 fail-safe: 거래 없음 → win_rate None, 단일 곡선 → sharpe None
    m0 = summary([1000.0], [])
    assert m0["win_rate"] is None and m0["sharpe"] is None and m0["mdd"] == 0.0, m0

    print("✅ test_metrics: round_trips(비용)·MDD·daily_returns·summary(승률·PF·Kelly·VaR·CVaR)·fail-safe 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
