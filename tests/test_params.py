# params 테스트 — mode 프리셋·효과 파라미터(effective) 머지·clamp·None 상속 (네트워크 없음)
"""실행: python tests/test_params.py
2A: 전략 효과 파라미터 = 프리셋(mode) ← 명시 오버라이드(non-None) 머지, 항상 clamp."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.core.params import MODE_PRESETS, REGIME_MAX_AGE, Params, resolve_mode


def main() -> int:
    # 1) neutral 프리셋(1분봉 튜닝값)
    eff = Params(mode="neutral").effective()
    assert eff == {"qty": 1, "sma_fast": 5, "sma_slow": 20, "rsi_buy": 34.0, "rsi_sell": 70.0}, eff

    # 2) mode 가 매매 성향을 바꾼다(프리셋 적용)
    assert Params(mode="aggressive").effective()["rsi_buy"] == 38.0, "공격=얕은 눌림 매수"
    assert Params(mode="aggressive").effective()["qty"] == 2, "공격=수량↑"
    d = Params(mode="defensive").effective()
    assert d["rsi_buy"] == 30.0 and d["rsi_sell"] == 66.0, f"방어=선택적·일찍 매도: {d}"

    # 3) 명시 오버라이드(non-None)는 프리셋을 이긴다, 미지정(None)은 프리셋 상속
    o = Params(mode="aggressive", rsi_buy=33).effective()
    assert o["rsi_buy"] == 33.0 and o["qty"] == 2, f"오버라이드 rsi_buy + 프리셋 qty: {o}"

    # 4) 무효 mode → neutral 프리셋
    assert Params(mode="weird").effective()["rsi_buy"] == 34.0, "무효 mode → neutral"

    # 5) effective 는 항상 clamp(오버라이드·프리셋 공통 최종 방어)
    c = Params(mode="neutral", qty=99999, rsi_buy=999).effective()
    assert c["qty"] == 100 and c["rsi_buy"] == 99.0, f"effective clamp: {c}"

    # 6) clamp 는 None 을 유지(미설정=프리셋 상속), enabled·mode 는 보정
    p = Params(mode="x").clamp()
    assert p.qty is None and p.rsi_buy is None, "미설정 전략필드는 None 유지"
    assert p.mode == "neutral" and p.enabled is False, "mode·enabled 보정"

    # 7) from_dict 부분 입력: mode 만 줘도 프리셋 적용(n8n 이 mode 만 써도 됨)
    eff2 = Params.from_dict({"mode": "aggressive"}).clamp().effective()
    assert eff2 == MODE_PRESETS["aggressive"], f"mode 만 → 프리셋 그대로: {eff2}"

    # 8) resolve_mode: 레짐 추종 opt-in + fail-safe
    now = 1_000_000.0
    fresh = {"regime": "aggressive", "computed_at": now - 100}      # 신선
    stale = {"regime": "aggressive", "computed_at": now - REGIME_MAX_AGE - 1}  # 너무 오래됨
    assert resolve_mode({"mode": "defensive", "follow_regime": False}, fresh, now) == "defensive", "추종 OFF → 수동 mode"
    assert resolve_mode({"mode": "defensive", "follow_regime": True}, fresh, now) == "aggressive", "추종 ON+신선 → 레짐"
    assert resolve_mode({"mode": "defensive", "follow_regime": True}, stale, now) == "neutral", "추종 ON+stale → neutral"
    assert resolve_mode({"mode": "defensive", "follow_regime": True}, None, now) == "neutral", "추종 ON+레짐없음 → neutral"
    assert resolve_mode({"mode": "x", "follow_regime": False}, None, now) == "neutral", "무효 수동 mode → neutral"
    assert resolve_mode({"mode": "aggressive", "follow_regime": True}, {"regime": "weird", "computed_at": now}, now) == "neutral", "레짐값 불명 → neutral"

    # 9) strategy_name(B2-a): strip + 빈 문자열→None + 60자 cap, 미설정은 None 유지
    assert Params(strategy_name="  내 전략  ").clamp().strategy_name == "내 전략", "strip"
    assert Params(strategy_name="   ").clamp().strategy_name is None, "빈 문자열→None"
    assert Params().clamp().strategy_name is None, "기본값 None(베이스라인)"
    long_name = "가" * 80
    assert len(Params(strategy_name=long_name).clamp().strategy_name) == 60, "60자 cap"
    assert Params.from_dict({"strategy_name": "x"}).clamp().to_dict()["strategy_name"] == "x", "from_dict/to_dict 왕복"

    # 10) 리스크 필드 clamp 경계값(하한 미만→하한, 상한 초과→상한, None 유지)
    r = Params(max_order_krw=1, max_position_krw=9_999_999_999, daily_loss_limit_krw=500_000,
               max_open_positions=0).clamp()
    assert r.max_order_krw == 10_000, "1회주문 하한 클램프"
    assert r.max_position_krw == 1_000_000_000, "종목당보유 상한 클램프"
    assert r.daily_loss_limit_krw == 500_000, "범위 내는 그대로"
    assert r.max_open_positions == 1, "동시보유수 하한 클램프"
    assert Params().clamp().max_order_krw is None, "미설정 리스크필드는 None 유지(.env 기본값 상속)"

    # 11) effective_risk: 콘솔 오버라이드(non-None) ← env 기본값, 결과는 clamp 됨
    env = {"max_order_krw": 1_000_000, "max_position_krw": 3_000_000,
           "daily_loss_limit_krw": 500_000, "max_open_positions": 5}
    assert Params().effective_risk(env) == env, "오버라이드 없으면 env 그대로"
    over = Params(max_order_krw=5_000_000).effective_risk(env)
    assert over["max_order_krw"] == 5_000_000 and over["max_position_krw"] == 3_000_000, f"부분 오버라이드: {over}"
    huge = Params(max_order_krw=99_999_999_999).effective_risk(env)
    assert huge["max_order_krw"] == 1_000_000_000, "effective_risk 도 상한 클램프 적용"

    print("✅ test_params: 프리셋·effective 머지·오버라이드·clamp·None 상속·resolve_mode(추종/fail-safe)·strategy_name·리스크한도(clamp/effective_risk) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
