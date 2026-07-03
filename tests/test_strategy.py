# 전략 테스트 — SMA 추세필터 + RSI 평균회귀 (매수/중복억제/매도/추세필터/stale/clamp). 네트워크 없음
"""실행: python tests/test_strategy.py
전제조건(시리즈가 의도한 추세·RSI인지)을 먼저 assert 한 뒤 전략 의도를 검증(자기검증)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.indicators import rsi, sma
from kr_research.trading.strategy import RSI_PERIOD, Strategy

CODE = "005930"

# 상승추세(fast>slow)인데 RSI 중간(~50): rsi_buy 임계로 매수 게이트 검증용 톱니 상승
UPTREND = [100.0 if i % 2 == 0 else 102.0 for i in range(26)]
# 하락추세(fast<slow) + 저RSI: 추세필터가 (과매도라도) 매수 막는지
DOWNTREND = [100.0 - i * 2 for i in range(25)]
# 강한 상승 → 과매수(RSI≥70): 보유 시 익절 매도
OVERBOUGHT = [100.0 + i * 2 for i in range(40)]


def _ctx(closes, params=None, stale=False):
    return {"stale": stale, "closes": list(closes), "params": params or {}}


def _tick(price):
    return {"code": CODE, "price": price}


def main() -> int:
    # --- 전제조건(시리즈가 의도대로인지) ---
    f, s, r = sma(UPTREND, 5), sma(UPTREND, 20), rsi(UPTREND, RSI_PERIOD)
    assert f > s and 45 <= r <= 65, f"UPTREND 전제: 상승추세·중간RSI, got f={f} s={s} r={r}"
    fd, sd, rd = sma(DOWNTREND, 5), sma(DOWNTREND, 20), rsi(DOWNTREND, RSI_PERIOD)
    assert fd < sd and rd <= 30, f"DOWNTREND 전제: 하락추세·저RSI, got f={fd} s={sd} r={rd}"
    fo, so, ro = sma(OVERBOUGHT, 5), sma(OVERBOUGHT, 20), rsi(OVERBOUGHT, RSI_PERIOD)
    assert fo > so and ro >= 70, f"OVERBOUGHT 전제: 상승추세·과매수, got f={fo} s={so} r={ro}"

    # --- 데이터 부족 → 의도 없음 ---
    st = Strategy()
    assert st.on_tick(_tick(102), _ctx([100, 101, 102])) == [], "데이터 부족이면 의도 없음"

    # --- stale → 의도 없음 ---
    assert st.on_tick(_tick(102), _ctx(UPTREND, stale=True)) == [], "stale 이면 의도 없음"

    # --- 매수 게이트: 상승추세 & RSI<=rsi_buy 면 매수 1회 ---
    st = Strategy()
    out = st.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60, "rsi_sell": 70, "qty": 7}))
    assert len(out) == 1 and out[0].side == "buy" and out[0].qty == 7, f"매수 1건 기대: {out}"
    assert out[0].price is None and out[0].code == CODE, "시장가·종목코드"

    # --- in-flight: 체결 확정 전엔 추가/반대 의도 없음(중복·반대주문 방지) ---
    assert st.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60, "rsi_sell": 70})) == [], "체결 확정 전 추가 의도 없음"
    st.resolve_order(CODE, "buy", filled=True)  # 체결 확정 → 보유 상태 확정

    # --- 중복 억제: 보유 중 같은 신호면 추가 매수 없음(매도조건도 아님) ---
    assert st.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60, "rsi_sell": 70})) == [], "보유 중 재매수 없음"

    # --- 과매수 매도: 보유 중 RSI>=rsi_sell 면 매도 ---
    out = st.on_tick(_tick(178), _ctx(OVERBOUGHT, {"rsi_buy": 60, "rsi_sell": 70, "qty": 7}))
    assert len(out) == 1 and out[0].side == "sell" and out[0].qty == 7, f"과매수 매도 기대: {out}"

    # --- 추세필터: 상승추세 아니면(과매도라도) 매수 안 함 ---
    st2 = Strategy()
    assert st2.on_tick(_tick(52), _ctx(DOWNTREND, {"rsi_buy": 60})) == [], "하락추세면 매수 금지"

    # --- 추세이탈 매도: 보유 중 추세 깨지면 매도 ---
    st3 = Strategy()
    assert len(st3.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60}))) == 1, "선행 매수"
    st3.resolve_order(CODE, "buy", filled=True)  # 체결 확정 → 보유
    out = st3.on_tick(_tick(52), _ctx(DOWNTREND, {"rsi_buy": 60, "rsi_sell": 99}))
    assert len(out) == 1 and out[0].side == "sell", f"추세이탈 매도 기대: {out}"

    # --- 드리프트 churn 방지: 매수 거절(filled=False)이면 보유로 오인 안 함 → 빈손 매도 금지 ---
    st6 = Strategy()
    assert len(st6.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60}))) == 1, "매수 의도"
    st6.resolve_order(CODE, "buy", filled=False)  # 게이트/리스크로 거절(체결 안 됨)
    assert st6.on_tick(_tick(52), _ctx(DOWNTREND, {"rsi_buy": 60, "rsi_sell": 99})) == [], "거절 후 빈손 매도 금지"

    # --- 매수 차단: 상승추세지만 RSI>rsi_buy 면 매수 안 함 ---
    st4 = Strategy()
    assert st4.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 40})) == [], "RSI>rsi_buy 면 매수 금지"

    # --- clamp: 비정상 파라미터는 잘려서 적용(qty 99999→100) ---
    st5 = Strategy()
    out = st5.on_tick(_tick(102), _ctx(UPTREND, {"rsi_buy": 60, "qty": 99999}))
    assert len(out) == 1 and out[0].qty == 100, f"qty clamp 100 기대: {out}"

    print("✅ test_strategy: 매수게이트·in-flight·중복억제·과매수/추세이탈 매도·추세필터·거절churn방지·stale·clamp 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
