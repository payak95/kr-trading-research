# market_data 테스트 — 틱 빈도와 무관하게 고정 시간격자(bar)로 closes 적재 (시간 주입, 네트워크 없음)
"""실행: python tests/test_market_data.py
한 bar 안 여러 틱 → close 1개, bar 경계 넘으면 적재, 그리고 핵심: 고빈도(WS)·저빈도(폴링) 피드가
같은 시간격자면 같은 closes 시계열을 만든다(브로커 간 신호 비교 공정성)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.market_data import MarketData

CODE = "005930"
BAR = 5.0


def feed(md, samples):
    for now, price in samples:
        md.on_raw_tick({"code": CODE, "price": price}, now=now)


def main() -> int:
    # 1) 한 bar 안 여러 틱 → close 1개(첫 틱 가격), price 는 최신
    md = MarketData(bar_seconds=BAR)
    feed(md, [(1000.0, 10), (1001.0, 11), (1004.9, 12)])  # 모두 bar 200
    ctx = md.context(CODE)
    assert ctx["closes"] == [10], f"한 bar=1 close: {ctx['closes']}"
    assert ctx["price"] == 12 and ctx["stale"] is False, "price=최신틱, stale 해제"

    # 2) bar 경계 넘으면 적재
    feed(md, [(1005.0, 20), (1012.0, 30)])  # bar 201, 202
    assert md.context(CODE)["closes"] == [10, 20, 30], "새 bar 마다 적재"

    # 3) 핵심: 고빈도(0.5s) vs 저빈도(3s) 피드가 같은 시간격자면 같은 closes
    def px(t):
        return int(500 + (t // BAR))   # bar 마다 +1 (같은 시장 가정)
    hi = MarketData(bar_seconds=BAR)
    lo = MarketData(bar_seconds=BAR)
    feed(hi, [(1000.0 + i * 0.5, px(1000.0 + i * 0.5)) for i in range(61)])  # 0.5s 간격 0~30s
    feed(lo, [(1000.0 + i * 3.0, px(1000.0 + i * 3.0)) for i in range(11)])  # 3s 간격 0~30s
    chi, clo = hi.context(CODE)["closes"], lo.context(CODE)["closes"]
    assert chi == clo and len(chi) == 7, f"같은 격자 → 같은 closes: hi={chi} lo={clo}"

    # 4) stale 토글
    md.mark_stale()
    assert md.context(CODE)["stale"] is True, "mark_stale"

    # 5) 일봉(bars) 캐시(B3) — 미설정 시 None, set_bars 후 context 에 반영
    assert md.context(CODE)["bars"] is None, "bars 미설정 → None"
    bars = [{"date": "d1", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100}]
    md.set_bars(CODE, bars)
    assert md.context(CODE)["bars"] == bars, "set_bars 후 context 반영"
    assert md.context("000660")["bars"] is None, "다른 종목은 독립(영향 없음)"

    print("✅ test_market_data: bar 시간격자 샘플링(한 bar=1close·경계적재·고저빈도 동일·stale)·일봉캐시(B3) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
