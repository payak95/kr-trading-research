# 백테스트 하니스 테스트 — 결정적 테스트 전략으로 체결·손익 계산 검증 (네트워크 없음)
"""실행: python tests/test_backtest.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.backtest import run
from kr_research.trading.strategy import Intent, Strategy


class BuyThenSell(Strategy):
    """2번째 봉에 1주 매수, 4번째 봉에 1주 매도 (결정적)."""
    def on_tick(self, tick, ctx):
        n = len(ctx["closes"])
        if n == 2:
            return [Intent("buy", tick["code"], 1)]
        if n == 4:
            return [Intent("sell", tick["code"], 1)]
        return []


class BuyBig(Strategy):
    """첫 봉에 qty 주 매수 시도(부분체결 검증용)."""
    def __init__(self, qty):
        super().__init__()
        self._q = qty
    def on_tick(self, tick, ctx):
        return [Intent("buy", tick["code"], self._q)] if len(ctx["closes"]) == 1 else []


def main() -> int:
    bars = [{"date": f"d{i}", "close": c} for i, c in enumerate([100, 110, 120, 130, 140])]
    res = run(BuyThenSell(), bars, "005930", cash=10_000_000)
    # 110 에 매수, 130 에 매도 → 현금 10,000,000 -110 +130 = 10,000,020, 포지션 0
    assert res["n_trades"] == 2, res
    assert res["position"] == 0
    assert res["final_equity"] == 10_000_020, res["final_equity"]

    # 빈 전략(기본 Strategy)은 거래 0
    res0 = run(Strategy(), bars, "005930")
    assert res0["n_trades"] == 0 and res0["final_equity"] == 10_000_000

    # 비용 모델: 수수료(편도 0.1%)+거래세(매도 0.2%) → 손익 차감
    # 매수@110 fee 0.11, 매도@130 fee 0.13+tax 0.26 → 순증 +19.50
    rc = run(BuyThenSell(), bars, "005930", cash=10_000_000,
             fee_rate=0.001, tax_rate=0.002)
    assert rc["position"] == 0
    assert abs(rc["final_equity"] - 10_000_019.5) < 1e-6, rc["final_equity"]
    assert abs(rc["fees"] - 0.24) < 1e-9 and abs(rc["tax"] - 0.26) < 1e-9, rc
    assert rc["final_equity"] < res["final_equity"], "비용 반영 시 무비용보다 손익 낮음"

    # 슬리피지: 매수 더 비싸게·매도 더 싸게 체결
    rs = run(BuyThenSell(), bars, "005930", slippage=0.01)
    assert rs["trades"][0]["price"] == 110 * 1.01 and rs["trades"][1]["price"] == 130 * 0.99, rs["trades"]

    # 부분체결: 거래량 1000 * 5% = 50주 한도 → 100주 의도라도 50주만 체결
    volbars = [{"date": f"d{i}", "close": 100, "volume": 1000} for i in range(3)]
    rp = run(BuyBig(100), volbars, "005930", max_fill_volume_frac=0.05)
    assert rp["position"] == 50 and rp["trades"][0]["qty"] == 50, rp

    # 손절: 110에 매수(BuyThenSell n==2) → 4번째 봉 low 가 손절가(110*0.95=104.5) 아래로 하락 →
    # 그 봉에서 강제청산(전략의 n==4 매도 신호는 skip). 5번째 봉은 손절 이후라 전략도 무관.
    sl_bars = [{"date": "d0", "close": 100, "low": 100, "high": 100},
               {"date": "d1", "close": 110, "low": 110, "high": 110},
               {"date": "d2", "close": 120, "low": 118, "high": 120},
               {"date": "d3", "close": 90, "low": 85, "high": 92},
               {"date": "d4", "close": 140, "low": 140, "high": 140}]
    rsl = run(BuyThenSell(), sl_bars, "005930", cash=10_000_000, stop_loss_pct=0.05)
    assert rsl["n_trades"] == 2 and rsl["position"] == 0, rsl
    assert rsl["trades"][1]["reason"] == "stop_loss" and abs(rsl["trades"][1]["price"] - 104.5) < 1e-9, rsl["trades"]
    assert abs(rsl["final_equity"] - 9_999_994.5) < 1e-6, rsl["final_equity"]

    # 익절: 110에 매수 → 3번째 봉 high 가 익절가(110*1.05=115.5) 위로 상승 → 그 봉에서 강제청산
    tp_bars = [{"date": "d0", "close": 100, "low": 100, "high": 100},
               {"date": "d1", "close": 110, "low": 110, "high": 110},
               {"date": "d2", "close": 112, "low": 110, "high": 116},
               {"date": "d3", "close": 90, "low": 85, "high": 92},
               {"date": "d4", "close": 140, "low": 140, "high": 140}]
    rtp = run(BuyThenSell(), tp_bars, "005930", cash=10_000_000, take_profit_pct=0.05)
    assert rtp["trades"][1]["reason"] == "take_profit" and abs(rtp["trades"][1]["price"] - 115.5) < 1e-9, rtp["trades"]

    # 사이징(pct_cash): 100주 매수 시도라도 현금의 50%만 사용 → 50주(cash=10,000, px=100)
    sizebars = [{"date": f"d{i}", "close": 100} for i in range(3)]
    rsz = run(BuyBig(100), sizebars, "005930", cash=10_000, sizing={"mode": "pct_cash", "value": 0.5})
    assert rsz["position"] == 50, rsz

    # 사이징(risk): 손절폭 대비 리스크 예산으로 역산 — risk_budget=100,000*0.02=2000, per_share_risk=100*0.05=5 → 400주
    rrisk = run(BuyBig(100), sizebars, "005930", cash=100_000, stop_loss_pct=0.05,
               sizing={"mode": "risk", "risk_pct": 0.02})
    assert rrisk["position"] == 400, rrisk

    # 사이징(risk)인데 stop_loss_pct 없음 → 리스크 거리 계산 불가, 전략 수량(100)으로 폴백
    rfallback = run(BuyBig(100), sizebars, "005930", cash=10_000_000, sizing={"mode": "risk", "risk_pct": 0.02})
    assert rfallback["position"] == 100, rfallback

    print("✅ test_backtest: 체결·손익·빈전략·비용·슬리피지·부분체결·손절·익절·사이징 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
