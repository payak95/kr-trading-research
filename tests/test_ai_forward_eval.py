# AI 섀도 판단 전진검증 — forward_returns/benchmark_returns 연계·부분 갱신·발행 검증(네트워크는 mock)
"""실행: python tests/test_ai_forward_eval.py"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core.ai_store import AiStore
from tools import ai_forward_eval as ev
from tools import ai_shadow_scheduler as sched


def _series(start_price: float, start_date: str, n: int = 40):
    """단조 상승 합성 일봉(간단히 날짜를 YYYYMMDD 순증가 문자열로) — forward_returns 는 date 문자열 비교(>=)만 하므로 충분."""
    base = int(start_date)
    return [{"date": str(base + i), "close": start_price * (1 + 0.01 * i)} for i in range(n)]


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))
        with patch("kr_research.core.ai_store.time.time", return_value=1000.0):
            store.record_judgment("cfg1", {"code": "005930", "trade_date": "20260601", "action": "buy",
                                            "confidence": 0.7, "reason": "x", "entry_price": 100.0,
                                            "snapshot": {"close": 100.0}})

        bars = _series(100.0, "20260601")  # 종목 시세: entry_price=100 과 동일 시작가

        def _fake_slim(code, count):
            return bars  # 종목·벤치마크 둘 다 같은 합성 시계열(단순화 — 값 자체보다 배선 검증이 목적)

        with patch("tools.ai_forward_eval.daily_ohlcv", side_effect=_fake_slim):
            evaluated = ev.run_eval(store)

        assert evaluated == 1
        row = store.get_judgments()[0]
        assert row["ret_d1"] is not None and row["ret_d5"] is not None, "충분한 미래 봉이 있으면 D+1/D+5 채워짐"
        assert row["bench_d1"] is not None, "벤치마크 초과수익도 채워짐"

        # 이미 채운 지평은 재실행해도 덮어쓰지 않음(재호출 시 evaluated=0, 새로 채울 게 없음)
        with patch("tools.ai_forward_eval.daily_ohlcv", side_effect=_fake_slim):
            evaluated2 = ev.run_eval(store)
        assert evaluated2 == 0, "이미 다 채워진 판단은 재평가 대상에서 빠짐"

        # publish_ai_view 배선 확인(스케줄러와 공용 함수) — bot:ai:judgments/summary 채워짐
        r = FakeRedis(decode_responses=True)
        sched.publish_ai_view(r, store)
        assert r.exists(sched.K_JUDGMENTS) and r.exists(sched.K_SUMMARY)

        # 분봉 판단(trade_date 12자 YYYYMMDDHHMM) — [:8]로 캘린더 날짜만 취해 일봉 D+N 평가에 재사용돼야 함
        with patch("kr_research.core.ai_store.time.time", return_value=2000.0):
            store.record_judgment("cfg_5m", {"code": "005930", "trade_date": "202606011005", "action": "buy",
                                              "confidence": 0.6, "reason": "y", "entry_price": 100.0,
                                              "snapshot": {"close": 100.0}})
        with patch("tools.ai_forward_eval.daily_ohlcv", side_effect=_fake_slim):
            evaluated3 = ev.run_eval(store)
        assert evaluated3 == 1, "분봉 판단도 [:8] 슬라이스로 평가돼야 함"
        rows_by_code = [row for row in store.get_judgments() if row["config_name"] == "cfg_5m"]
        assert rows_by_code[0]["ret_d1"] is not None, "12자 trade_date 도 8자로 잘려 일봉 시계열과 매칭돼야 함"

        store.close()

    print("✅ test_ai_forward_eval: run_eval(부분 갱신·재평가 스킵)·publish_ai_view 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
