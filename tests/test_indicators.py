# 지표 단위 테스트 — SMA 정확값, RSI 경계(상승만/하락만/데이터부족)
"""실행: python tests/test_indicators.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.indicators import (
    atr, atr_pct, bollinger, channel, ema, macd, price, roc, rsi, rvol, sma, stochastic,
)


def main() -> int:
    assert sma([1, 2, 3, 4], 2) == 3.5, "최근 2개 평균"
    assert sma([1, 2, 3, 4], 4) == 2.5
    assert sma([1, 2], 3) is None, "데이터 부족"

    assert rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 14) == 100.0, "상승만→100"
    assert rsi(list(range(20, 0, -1)), 14) == 0.0, "하락만→0"
    assert rsi([1, 2, 3], 14) is None, "데이터 부족→None"

    # EMA — 상수열이면 그 값(시드=값, 갱신해도 불변)
    assert ema([5, 5, 5, 5], 3) == 5.0, "상수열 EMA=상수"
    assert ema([1, 2], 3) is None, "데이터 부족→None"
    # n=3 → k=2/(3+1)=0.5, 시드=(1+2+3)/3=2, 다음값 4: 4·0.5 + 2·0.5 = 3.0
    assert abs(ema([1, 2, 3, 4], 3) - 3.0) < 1e-9, "EMA 갱신식"

    # ROC — n 봉 전 대비 %
    assert abs(roc([100, 110], 1) - 10.0) < 1e-9, "1봉 전 대비 +10%"
    assert abs(roc([100, 90], 1) + 10.0) < 1e-9, "하락 -10%"
    assert roc([100], 1) is None, "데이터 부족→None"

    # 현재가 — 최신 종가(기간 무시)
    assert price([1, 2, 3], 20) == 3, "최신 종가"
    assert price([], 5) is None, "빈 시계열→None"

    # MACD — 다중 출력(line/signal/hist), hist=line-signal, 상승추세면 line>0
    m = macd([100 + i for i in range(60)])
    assert m is not None and set(m) == {"line", "signal", "hist"}, "MACD 3출력"
    assert abs(m["hist"] - (m["line"] - m["signal"])) < 1e-9, "hist=line-signal"
    assert m["line"] > 0, "상승추세 → MACD line>0"
    assert macd(list(range(10))) is None, "데이터 부족→None"
    assert macd([1, 2, 3, 4], fast=5, slow=3) is None, "fast>=slow→None"

    # 볼린저밴드 — 상수열이면 상/중/하단 동일(표준편차 0), 변동 있으면 upper>middle>lower
    b = bollinger([10] * 20, 20, 2)
    assert b["upper"] == b["middle"] == b["lower"] == 10.0, "상수열→밴드폭 0"
    assert bollinger([1, 2], 20) is None, "데이터 부족→None"
    b2 = bollinger([100 + (i % 2) * 10 for i in range(20)], 20, 2)
    assert b2["upper"] > b2["middle"] > b2["lower"], "변동열→상>중>하"

    # 스토캐스틱 — 종가=고가(창 최고)면 %K=100, 데이터 부족 None, 밴드폭 0이면 %K=50
    top = [{"high": 10 + i, "low": i, "close": 10 + i} for i in range(20)]
    st = stochastic(top, 14, 3)
    assert st is not None and set(st) == {"k", "d"}, "스토캐스틱 2출력"
    assert abs(st["k"] - 100.0) < 1e-9, "종가=창 최고 → %K=100"
    assert stochastic(top[:5], 14, 3) is None, "데이터 부족→None"
    flat = [{"high": 5, "low": 5, "close": 5} for _ in range(20)]
    assert stochastic(flat, 14, 3)["k"] == 50.0, "밴드폭 0 → %K=50"

    # 상대거래량 — 최신 거래량 / 직전 period 평균
    vb = [{"high": 1, "low": 1, "close": 1, "volume": 100} for _ in range(20)] + \
         [{"high": 1, "low": 1, "close": 1, "volume": 300}]
    assert abs(rvol(vb, 20) - 3.0) < 1e-9, "300/100 = 3배"
    assert rvol(vb[:5], 20) is None, "데이터 부족→None"

    # 채널 — 직전 period 봉 최고/최저(현재 봉 제외)
    cb = [{"high": i, "low": -i, "close": 0} for i in range(1, 22)]
    ch = channel(cb, 20)
    assert ch["high"] == 20 and ch["low"] == -20, "직전 20봉 고=20·저=-20"
    assert channel(cb[:5], 20) is None, "데이터 부족→None"

    # ATR — 일정 변동폭(고10·저8·전종9 → TR=2)
    ab = [{"high": 10, "low": 8, "close": 9} for _ in range(20)]
    assert atr(ab, 14) == 2.0, "TR 평균=2"
    assert atr(ab[:3], 14) is None, "데이터 부족→None"

    # ATR% — 종목 가격 스케일 무관 정규화(ATR/최근종가×100)
    assert abs(atr_pct(ab, 14) - (2.0 / 9 * 100)) < 1e-9, "ATR/종가×100"
    assert atr_pct(ab[:3], 14) is None, "ATR 계산 불가→None"
    zero_close = [{"high": 1, "low": -1, "close": 0} for _ in range(20)]
    assert atr_pct(zero_close, 14) is None, "종가<=0 → None(방어)"

    print("✅ test_indicators: SMA·RSI·EMA·ROC·현재가·MACD·볼린저·스토캐스틱·상대거래량·채널·ATR·ATR% 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
