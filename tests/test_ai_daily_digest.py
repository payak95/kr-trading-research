# 일일 다이제스트 — 이전 거래일·적중 판정(sell 반전)·스코프 분류·본문 조립 검증 (순수 함수, 네트워크 없음)
"""실행: python tests/test_ai_daily_digest.py"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.ai_daily_digest import _hit, _scope, build_digest, prev_trading_day

_KST = timezone(timedelta(hours=9))


def main() -> int:
    # prev_trading_day — 2026-07-06(월)의 직전 거래일은 07-03(금), 주말은 건너뜀
    mon = datetime(2026, 7, 6, 16, 20, tzinfo=_KST)
    assert prev_trading_day(mon) == "20260703", prev_trading_day(mon)

    # _hit — buy 는 상승=적중, sell 은 하락=적중(부호 반전), hold·미평가는 제외(None)
    assert _hit("buy", 0.02) is True and _hit("buy", -0.01) is False
    assert _hit("sell", -0.02) is True and _hit("sell", 0.01) is False
    assert _hit("hold", 0.05) is None and _hit("buy", None) is None

    # _scope — 유니버스/콤보 프리픽스/그 외(② 관찰) 분류
    assert _scope("universe_daily") == "universe"
    assert _scope("combo:005930_daily_60m") == "combo"
    assert _scope("005930_daily") == "watch" and _scope("") == "watch"

    # build_digest — 스코프별 라인·오늘/어제 구분·게이트 표본 수·청산 손익 합산
    rows = [
        # ② 오늘: 매수1 보유1 / 어제: 매수 적중 1, 매도 실패 1(상승했는데 sell), hold 는 적중 집계 제외
        {"config_name": "005930_daily", "trade_date": "20260706", "action": "buy", "ret_d1": None},
        {"config_name": "005930_daily", "trade_date": "20260706", "action": "hold", "ret_d1": None},
        {"config_name": "000660_daily", "trade_date": "20260703", "action": "buy", "ret_d1": 0.02, "ret_d5": 0.01},
        {"config_name": "000660_daily", "trade_date": "20260703", "action": "sell", "ret_d1": 0.01, "ret_d5": -0.02},
        {"config_name": "000660_daily", "trade_date": "20260703", "action": "hold", "ret_d1": 0.09, "ret_d5": 0.01},
        # ③ 오늘 1건 / ① 오늘 1건(② 집계에 안 섞여야 함)
        {"config_name": "combo:005930_daily_60m", "trade_date": "20260706", "action": "buy", "ret_d1": None},
        {"config_name": "universe_daily", "trade_date": "20260706", "action": "buy", "ret_d1": None},
    ]
    positions = [
        {"status": "open", "realized_pnl": None, "exit_trade_date": None},
        {"status": "closed", "realized_pnl": 31500.0, "exit_trade_date": "20260706"},
        {"status": "closed", "realized_pnl": -10000.0, "exit_trade_date": "20260706"},
        {"status": "closed", "realized_pnl": 99999.0, "exit_trade_date": "20260703"},  # 오늘 아님 — 제외
    ]
    shortlist = {"date": "20260706", "codes": ["005930", "035420"]}
    text = build_digest(rows, positions, shortlist, llm_calls=42, now=mon)

    assert "① 스캔: 판단 1건 · 매수후보 2" in text, text
    assert "② 관찰: 오늘 2건(매수1/보유1/매도0) · 어제 D+1 적중 1/2 · 게이트 D+5 3/30" in text, text
    assert "③ 콤보: 오늘 1건(매수1/보유0/매도0)" in text and "게이트 D+5 0/30" in text, text
    assert "가상 포지션: 보유 1 · 오늘 청산 2건 +21,500원" in text, text
    assert "Gemini 호출 42회" in text, text
    assert text.splitlines()[0].startswith("📊 AI 섀도 일일 요약 (2026-07-06 월"), text

    # 숏리스트 날짜가 오늘이 아니면(전일 발행분) 0 으로 — 어제 후보를 오늘 것처럼 보이게 하지 않음
    stale = build_digest(rows, positions, {"date": "20260703", "codes": ["1", "2", "3"]}, 0, mon)
    assert "매수후보 0" in stale, stale

    print("✅ test_ai_daily_digest: prev_trading_day(주말 스킵)·_hit(sell 반전·hold 제외)·_scope 분류·"
          "본문 조립(스코프 격리·오늘/어제·게이트·청산합산·stale 숏리스트) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
