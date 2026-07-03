# 전략 스펙 — 해석기(eval_expr·decide)·검증 + SpecStrategy ≡ Strategy 동치 증명 (네트워크 없음)
"""실행: python tests/test_spec.py
핵심: BASELINE_SPEC 으로 구동한 SpecStrategy 가 하드코딩 strategy.py 와 모든 프리셋·매 틱에서
동일한 의도를 내는지 증명(스펙이 현재 전략을 정확히 인코딩함)."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.backtest import run
from kr_research.trading.spec import BASELINE_SPEC, SpecStrategy, decide, eval_expr, screen, uses_flow_setups, validate
from kr_research.trading.strategy import Strategy


def _intents(lst):
    return [(it.side, it.code, it.qty) for it in lst]


def main() -> int:
    # eval_expr: all/any/not + 비교
    vals = {"a": 10.0, "b": 5.0, "r": 30.0}
    params = {"rsi_buy": 34.0}
    assert eval_expr({"gt": ["a", "b"]}, vals, params) is True
    assert eval_expr({"all": [{"gt": ["a", "b"]}, {"lte": ["r", {"param": "rsi_buy"}]}]}, vals, params) is True
    assert eval_expr({"any": [{"lt": ["a", "b"]}, {"gte": ["r", 50]}]}, vals, params) is False
    assert eval_expr({"not": {"gt": ["a", "b"]}}, vals, params) is False

    # decide: 보유X→entry, 보유O→exit. 데이터 부족이면 None
    assert decide(BASELINE_SPEC, [1, 2, 3], {"sma_fast": 5, "sma_slow": 20, "rsi_buy": 34, "rsi_sell": 70}, False) is None, "데이터 부족 → 보류"

    # validate 방어
    try:
        validate({"entry": {}, "exit": {}})  # indicators 누락
        raise AssertionError("indicators 누락인데 통과")
    except ValueError:
        pass

    # ── 셋업 술어(eval_expr) + 스크리닝(screen) ──
    act = [{"key": "ma_alignment", "tone": "bull"}, {"key": "golden_cross", "tone": "bull"}]
    assert eval_expr({"setup": "golden_cross"}, {}, {}, act) is True               # key 일치
    assert eval_expr({"setup": "golden_cross", "tone": "bull"}, {}, {}, act) is True  # key+tone 일치
    assert eval_expr({"setup": "golden_cross", "tone": "bear"}, {}, {}, act) is False  # tone 불일치
    assert eval_expr({"setup": "volume_surge"}, {}, {}, act) is False               # 미활성
    assert eval_expr({"setup": "ma_alignment"}, {}, {}, None) is False              # active 없음(라이브)→False
    # validate: 미지의 셋업 key 거부
    try:
        validate({"indicators": [], "entry": {"setup": "bogus"}, "exit": {}})
        raise AssertionError("미지의 셋업 key 인데 통과")
    except ValueError:
        pass

    # ── 견고성: 미해석 피연산자(사라진 지표·다중출력 오참조)는 크래시 대신 False ──
    assert eval_expr({"gt": ["ghost", 100]}, {"rsi": 45.0}, {}) is False, "없는 지표 참조 → False(크래시 없음)"
    assert eval_expr({"gt": ["m", "m.signal"]}, {"m.line": 1.0, "m.signal": 0.0}, {}) is False, "다중출력을 스칼라 m 으로 오참조 → False"
    assert eval_expr({"lte": ["rsi", {"param": "missing"}]}, {"rsi": 45.0}, {}) is False, "누락 파라미터 → False"
    # decide: dangling 참조 spec 이라도 크래시 없이 보류(None)
    dangling = {"version": 1, "name": "dangling",
                "indicators": [{"id": "rsi", "type": "rsi", "params": {"period": 3}}],
                "entry": {"all": [{"gt": ["price", 100]}]}, "exit": {"any": []}}
    assert decide(dangling, [100.0, 101.0, 102.0, 103.0], {}, holding=False) is None, "없는 price 참조 → 크래시 없이 보류"
    # validate: 조건이 없는 지표 id 참조 → 거부
    try:
        validate({"indicators": [{"id": "a", "type": "sma", "params": {"period": 5}}],
                  "entry": {"gt": ["nonexistent", 10]}, "exit": {}})
        raise AssertionError("없는 지표 참조인데 통과")
    except ValueError:
        pass
    # validate: 다중출력 지표를 스칼라 id 로 참조 → 거부(m.line 이어야 함)
    try:
        validate({"indicators": [{"id": "m", "type": "macd", "params": {"fast": 5, "slow": 10, "signal": 4}}],
                  "entry": {"gt": ["m", 0]}, "exit": {}})
        raise AssertionError("MACD 를 스칼라 m 으로 참조했는데 통과")
    except ValueError:
        pass
    # 회귀: 정상 스펙(BASELINE_SPEC)은 새 검사에도 통과
    validate(BASELINE_SPEC)

    # screen: 정배열(bull) 조건을 bars 에 평가 — 상승추세=후보, 하락추세(역배열)=제외(tone 구분)
    scr = {"version": 1, "name": "t",
           "indicators": [{"id": "rsi", "type": "rsi", "params": {"period": 14}}],
           "entry": {"all": [{"setup": "ma_alignment", "tone": "bull"}, {"lt": ["rsi", 101]}]},
           "exit": {"any": [{"not": {"setup": "ma_alignment", "tone": "bull"}}]}}

    def _bars(cl):
        return [{"open": c, "high": c, "low": c, "close": c, "volume": 1000} for c in cl]
    up = [100.0 + 0.5 * i for i in range(80)]        # 정배열(bull)
    down = [140.0 - 0.5 * i for i in range(80)]      # 역배열(bear)
    assert screen(scr, _bars(up)) is True, "정배열 상승추세 → 후보"
    assert screen(scr, _bars(down)) is False, "역배열 → 제외(tone 구분)"
    assert screen(scr, _bars([1.0, 2.0, 3.0])) is False, "데이터 부족 → False"

    # ── screen extra_active(§스크리닝 강화, 외국인·기관 수급) — bars 로 못 구하는 셋업을 이번 평가에만 합류 ──
    flow_spec = {"version": 1, "name": "flow",
                "indicators": [{"id": "rsi", "type": "rsi", "params": {"period": 14}}],
                "entry": {"all": [{"setup": "foreign_accumulation"}, {"lt": ["rsi", 101]}]},
                "exit": {"any": []}}
    validate(flow_spec)  # foreign_accumulation 은 미지의 셋업 아님(setups|flow 합집합 검증) — 예외 없어야 통과
    assert screen(flow_spec, _bars(up)) is False, "extra_active 없으면 flow 셋업은 항상 비활성"
    fa_badge = [{"key": "foreign_accumulation", "label": "외국인 수급 유입", "tone": "bull", "detail": ""}]
    assert screen(flow_spec, _bars(up), extra_active=fa_badge) is True, "extra_active 로 flow 셋업 활성화"
    assert uses_flow_setups(flow_spec) is True, "entry 가 foreign_accumulation 참조"
    assert uses_flow_setups(scr) is False, "정배열 스펙은 flow 셋업 미참조"

    # ── 동치 증명: SpecStrategy ≡ Strategy (매 틱 동일 의도) ──
    closes = [100 + 0.4 * i + 16 * math.sin(i / 3.0) for i in range(120)]  # 상승추세+큰 진동

    def run_equiv(params, require_signals):
        """같은 종가 시계열을 두 전략에 흘려 매 틱 의도가 동일한지 검증. 체결도 동일하게 반영."""
        old, new = Strategy(), SpecStrategy(BASELINE_SPEC)
        saw_buy = saw_sell = False
        for i in range(len(closes)):
            window = closes[: i + 1]
            ctx = {"stale": False, "price": window[-1], "closes": window, "params": params}
            tick = {"code": "X", "price": window[-1]}
            io, ino = old.on_tick(tick, ctx), new.on_tick(tick, ctx)
            assert _intents(io) == _intents(ino), f"{params} step {i}: {_intents(io)} != {_intents(ino)}"
            for it in io:
                old.resolve_order("X", it.side, filled=True)
                saw_buy |= it.side == "buy"
                saw_sell |= it.side == "sell"
            for it in ino:
                new.resolve_order("X", it.side, filled=True)
        if require_signals:
            assert saw_buy and saw_sell, f"{params}: 매수·매도 둘 다 발생(동치 비교 비공허)"

    # 프리셋 3개 — 동치만(실전 프리셋은 보수적이라 신호가 드물 수 있음)
    for preset in ("defensive", "neutral", "aggressive"):
        run_equiv({"mode": preset}, require_signals=False)
    # churny 파라미터 — 매수·매도 빈발로 entry·exit 양쪽 분기 모두 동치 확인(비공허)
    churn = {"mode": "neutral", "rsi_buy": 55.0, "rsi_sell": 58.0, "sma_fast": 5, "sma_slow": 20, "qty": 1}
    run_equiv(churn, require_signals=True)

    # ── 백테스트 배선 동치: SpecStrategy 가 backtest.run 으로도 Strategy 와 동일 결과 (tools/backtest --spec 경로) ──
    bars = [{"date": f"d{i}", "open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]
    r_old = run(Strategy(churn), bars, "X", cash=10_000_000)
    r_new = run(SpecStrategy(BASELINE_SPEC, churn), bars, "X", cash=10_000_000)
    assert r_old["trades"] == r_new["trades"], "백테스트 거래 내역 불일치"
    assert r_old["final_equity"] == r_new["final_equity"], "백테스트 최종자산 불일치"
    assert r_new["n_trades"] > 0, "백테스트 거래 0 — 동치 비교 공허"

    # ── 차트신호(setup)가 백테스트 경로에서 활성 — 라이브(bars 없음)에선 무효(실매매 불변) ──
    vs_spec = {"version": 1, "name": "vs", "indicators": [],
               "entry": {"all": [{"setup": "volume_surge"}]}, "exit": {"any": []}}
    vs_bars = [{"date": f"d{i}", "open": 1000, "high": 1000, "low": 1000, "close": 1000, "volume": 100}
               for i in range(22)]
    vs_bars.append({"date": "spike", "open": 1000, "high": 1000, "low": 1000, "close": 1000, "volume": 1000})
    vs_bars += [{"date": f"e{i}", "open": 1000, "high": 1000, "low": 1000, "close": 1000, "volume": 100}
                for i in range(3)]
    r_vs = run(SpecStrategy(vs_spec, {"qty": 1}), vs_bars, "VS", cash=10_000_000)
    assert r_vs["n_trades"] >= 1, "거래량 급증 차트신호가 백테스트에서 매수를 냈어야 함"
    # 라이브 흉내(ctx 에 bars 없음) → 셋업 무효 → 매수 없음
    live = SpecStrategy(vs_spec, {"qty": 1})
    assert live.on_tick({"code": "VS", "price": 1000},
                        {"stale": False, "closes": [1000.0] * 25}) == [], "bars 없으면 셋업 무효(라이브 불변)"

    # ── 새 지표(ema/roc/price) 스펙이 검증·백테스트에서 동작 ──
    px_spec = {"version": 1, "name": "breakout",
               "indicators": [{"id": "p", "type": "price", "params": {"period": 1}},
                              {"id": "s", "type": "sma", "params": {"period": 5}}],
               "entry": {"all": [{"gt": ["p", "s"]}]}, "exit": {"any": [{"lt": ["p", "s"]}]}}
    validate(px_spec)  # 새 지표 타입 통과
    assert run(SpecStrategy(px_spec, {"qty": 1}), bars, "PX", cash=10_000_000)["n_trades"] > 0, "가격 돌파(price>sma) 거래 발생"
    ema_spec = {"version": 1, "name": "emacross",
                "indicators": [{"id": "f", "type": "ema", "params": {"period": 5}},
                               {"id": "g", "type": "ema", "params": {"period": 20}}],
                "entry": {"all": [{"gt": ["f", "g"]}]}, "exit": {"any": [{"lt": ["f", "g"]}]}}
    validate(ema_spec)
    assert run(SpecStrategy(ema_spec, {"qty": 1}), bars, "EMA", cash=10_000_000)["n_trades"] > 0, "EMA 크로스 거래 발생"

    # ── 다중 출력 지표(MACD): id.출력 피연산자로 백테스트 동작 + 파라미터 검증 ──
    macd_spec = {"version": 1, "name": "macd",
                 "indicators": [{"id": "m", "type": "macd", "params": {"fast": 5, "slow": 10, "signal": 4}}],
                 "entry": {"all": [{"gt": ["m.line", "m.signal"]}]},
                 "exit": {"any": [{"lt": ["m.line", "m.signal"]}]}}
    validate(macd_spec)
    assert run(SpecStrategy(macd_spec, {"qty": 1}), bars, "MACD", cash=10_000_000)["n_trades"] > 0, "MACD 크로스 거래 발생"
    try:
        validate({"indicators": [{"id": "m", "type": "macd", "params": {"fast": 5}}], "entry": {}, "exit": {}})
        raise AssertionError("MACD 파라미터 누락인데 통과")
    except ValueError:
        pass

    # ── 볼린저밴드(다중 출력): 하단 이탈 매수 / 중간 회귀 매도 백테스트 ──
    bb_spec = {"version": 1, "name": "bb",
               "indicators": [{"id": "px", "type": "price", "params": {}},
                              {"id": "bb", "type": "bb", "params": {"period": 10, "mult": 2}}],
               "entry": {"all": [{"lt": ["px", "bb.lower"]}]},
               "exit": {"any": [{"gt": ["px", "bb.middle"]}]}}
    validate(bb_spec)
    assert run(SpecStrategy(bb_spec, {"qty": 1}), bars, "BB", cash=10_000_000)["n_trades"] > 0, "볼린저 하단 이탈 거래 발생"

    # ── OHLCV 지표(스토캐스틱): 백테스트 활성 + 라이브 bars 없으면 보류·있으면(B3) 활성 ──
    stoch_spec = {"version": 1, "name": "stoch",
                  "indicators": [{"id": "st", "type": "stoch", "params": {"k": 14, "d": 3}}],
                  "entry": {"all": [{"lt": ["st.k", 30]}]},
                  "exit": {"any": [{"gt": ["st.k", 70]}]}}
    validate(stoch_spec)
    assert run(SpecStrategy(stoch_spec, {"qty": 1}), bars, "ST", cash=10_000_000)["n_trades"] > 0, "스토캐스틱 백테스트 거래 발생"
    # 라이브 흉내: bars 없으면 OHLCV 지표 보류(매수 없음, 기존 불변)
    assert SpecStrategy(stoch_spec, {"qty": 1}).on_tick(
        {"code": "ST", "price": 100}, {"stale": False, "closes": [100.0] * 40}) == [], "bars 없으면 OHLCV 지표 보류"
    # 라이브 흉내: bars 있으면(B3, main 의 관심종목 일봉 리프레시가 채움) 실전에서도 OHLCV 지표가 활성화된다.
    # 단조 하락 구간(과매도) → 매 시점 마지막 종가=구간 최저가라 %K=0 확정 → entry(%K<30) 반드시 성립.
    down_closes = [100.0 - i for i in range(20)]
    down_bars = [{"date": f"x{i}", "open": c, "high": c, "low": c, "close": c, "volume": 1000}
                 for i, c in enumerate(down_closes)]
    live = SpecStrategy(stoch_spec, {"qty": 1}).on_tick(
        {"code": "ST", "price": down_closes[-1]}, {"stale": False, "closes": down_closes, "bars": down_bars})
    assert live and live[0].side == "buy", f"bars 공급(B3) → OHLCV 지표 활성·매수 신호: {live}"

    # ── 채널(다중 출력, OHLCV): 신고가 돌파 백테스트 ──
    ch_spec = {"version": 1, "name": "ch",
               "indicators": [{"id": "px", "type": "price", "params": {}},
                              {"id": "ch", "type": "channel", "params": {"period": 10}}],
               "entry": {"all": [{"gt": ["px", "ch.high"]}]},
               "exit": {"any": [{"lt": ["px", "ch.low"]}]}}
    validate(ch_spec)
    assert run(SpecStrategy(ch_spec, {"qty": 1}), bars, "CH", cash=10_000_000)["n_trades"] > 0, "채널 신고가 돌파 거래 발생"

    print("✅ test_spec: eval_expr·decide·validate + SpecStrategy≡Strategy(매 틱·backtest.run 동일) + 셋업 백테스트 활성 + 새 지표(ema/roc/price/macd/bb/stoch) + 라이브 bars 활성화(B3) + screen extra_active·uses_flow_setups 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
