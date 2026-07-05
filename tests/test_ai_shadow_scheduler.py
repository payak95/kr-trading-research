# AI 섀도 스케줄러 — due 판정·중복호출방지 연계·에러 격리·상태기록·발행 검증(fakeredis, 네트워크 없음)
"""실행: python tests/test_ai_shadow_scheduler.py"""
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core.ai_store import AiStore
from tools import ai_shadow_scheduler as sched
from tools.ai_shadow_scheduler import _due


def _rec(action="buy"):
    return {"code": "005930", "trade_date": "20260701", "action": action,
            "confidence": 0.6, "reason": "테스트", "entry_price": 70000,
            "snapshot": {"close": 70000}}


def _test_due_market_gate() -> None:
    """_due() — 주기 경과 + 장 상태 게이트(휴장일·야간에 Naver/Yahoo 호출 자체를 생략해 차단 위험을 줄임)."""
    daily_cfg = {"timeframe": "daily", "interval_min": 60}
    minute_cfg = {"timeframe": "30m", "interval_min": 30}

    with patch("tools.ai_shadow_scheduler.is_trading_day", return_value=True), \
         patch("tools.ai_shadow_scheduler.is_market_open", return_value=True):
        assert not _due(daily_cfg, time.time()), "60분 안 지났으면 daily 도 False"
        assert not _due(minute_cfg, time.time()), "30분 안 지났으면 분봉도 False"
        assert _due(daily_cfg, None), "첫 실행(last_run=None)+거래일이면 True"
        assert _due(minute_cfg, time.time() - 1801), "30분 경과+장중이면 True"

    with patch("tools.ai_shadow_scheduler.is_trading_day", return_value=False):
        assert not _due(daily_cfg, None), "휴장일이면 첫 실행이어도 False(Naver 호출 생략)"
        assert not _due(daily_cfg, time.time() - 3601), "휴장일이면 주기 경과해도 False"

    with patch("tools.ai_shadow_scheduler.is_market_open", return_value=False):
        assert not _due(minute_cfg, time.time() - 1801), "장외(야간·주말·공휴일)면 False(Yahoo 호출 생략)"


def main() -> int:
    _test_due_market_gate()

    r = FakeRedis(decode_responses=True)
    # 아래 회귀 테스트들은 due 판정 자체가 아니라 스케줄러의 다른 동작(dedup·에러 격리·발행 등)을 검증하는
    # 것이라, 실행하는 날의 요일/시간과 무관하게 항상 장이 열린 것으로 고정(그렇지 않으면 주말에 이 테스트를
    # 돌리면 새로 추가한 장 상태 게이트에 걸려 아래 assert 들이 전부 실패하는 flaky 테스트가 됨).
    with patch("tools.ai_shadow_scheduler.is_trading_day", return_value=True), \
         patch("tools.ai_shadow_scheduler.is_market_open", return_value=True), \
         tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))

        # 설정 3개: 활성(due) / 비활성 / 활성이지만 아직 due 아님(방금 돈 것으로 설정)
        r.hset(sched.K_CONFIGS, mapping={
            "on_due": json.dumps({"symbol": "005930", "lookback_days": 120, "interval_min": 5, "enabled": True}),
            "off": json.dumps({"symbol": "000660", "lookback_days": 120, "interval_min": 5, "enabled": False}),
            "on_not_due": json.dumps({"symbol": "035420", "lookback_days": 120, "interval_min": 60, "enabled": True}),
        })
        r.hset(sched.K_LAST_RUN, "on_not_due", 9_999_999_999)  # 아주 먼 미래 → 아직 due 아님

        with patch("tools.ai_shadow_scheduler.run_once", return_value=_rec()) as mock_run, \
             patch("tools.ai_shadow_scheduler.log_judgment") as mock_log:
            recorded = sched.run_scheduler(r, store, api_key="fake-key")

        assert recorded == 1, "due+enabled 인 on_due 만 처리돼야 함"
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["last_trade_date"] is None, "이력 없으면 last_trade_date=None"
        mock_log.assert_called_once()

        assert r.hexists(sched.K_LAST_RUN, "on_due"), "처리한 설정은 last_run 갱신"
        assert not r.hexists(sched.K_LAST_RUN, "off"), "비활성 설정은 손대지 않음"
        status = json.loads(r.hget(sched.K_STATUS, "on_due"))
        assert status["ok"] is True

        assert len(store.get_judgments()) == 1

        # 발행 확인 — bot:ai:judgments/summary/positions 채워짐
        assert r.exists(sched.K_JUDGMENTS) and r.exists(sched.K_SUMMARY) and r.exists(sched.K_POSITIONS)
        published = json.loads(r.get(sched.K_JUDGMENTS))
        assert len(published) == 1 and published[0]["action"] == "buy"

        # 가상 포지션 — buy 판단 1건으로 포지션이 열려야 함(1,000,000/70,000 → 14주)
        pos = store.get_open_position("on_due", "005930")
        assert pos is not None and pos["qty"] == 14 and pos["avg_price"] == 70000, pos
        published_pos = json.loads(r.get(sched.K_POSITIONS))
        assert len(published_pos) == 1 and published_pos[0]["status"] == "open"

        # 같은 config 재실행(같은 last_trade_date) — run_once 에 직전 trade_date 가 전달됨(dedup 은 run_once 내부 책임)
        r.hset(sched.K_LAST_RUN, "on_due", 0)  # 강제로 다시 due 하게
        with patch("tools.ai_shadow_scheduler.run_once", return_value=_rec()) as mock_run2, \
             patch("tools.ai_shadow_scheduler.log_judgment"):
            sched.run_scheduler(r, store, api_key="fake-key")
        assert mock_run2.call_args.kwargs["last_trade_date"] == "20260701", "직전 판단의 trade_date 를 넘겨줘야 함"

        # 이미 보유 중인데 또 buy 판단 — 포지션이 중복 오픈되지 않고 그대로여야 함(반복 매수 무시)
        pos_after_repeat = store.get_open_position("on_due", "005930")
        assert pos_after_repeat["id"] == pos["id"] and pos_after_repeat["qty"] == 14, "반복 매수는 무시(포지션 안 늘어남)"

        # sell 판단 — 열린 포지션이 전량 청산되고 실현손익이 계산돼야 함
        r.hset(sched.K_LAST_RUN, "on_due", 0)
        with patch("tools.ai_shadow_scheduler.run_once",
                   return_value={**_rec(action="sell"), "trade_date": "20260702", "entry_price": 77000}), \
             patch("tools.ai_shadow_scheduler.log_judgment"):
            sched.run_scheduler(r, store, api_key="fake-key")
        assert store.get_open_position("on_due", "005930") is None, "sell 이후엔 열린 포지션이 없어야 함"
        closed = store.get_positions(config_name="on_due")
        assert len(closed) == 1 and closed[0]["status"] == "closed"
        assert abs(closed[0]["realized_pnl"] - (77000 - 70000) * 14) < 1e-6, closed[0]
        published_pos2 = json.loads(r.get(sched.K_POSITIONS))
        assert published_pos2[0]["status"] == "closed", "발행된 포지션 뷰도 청산 상태로 갱신돼야 함"

        # run_once 가 예외 — 격리(다른 설정 영향 없음) + last_run 갱신(재시도 폭주 방지) + status 에러 기록
        r.hset(sched.K_LAST_RUN, "on_due", 0)
        with patch("tools.ai_shadow_scheduler.run_once", side_effect=RuntimeError("429 rate limited")), \
             patch("tools.ai_shadow_scheduler.log_judgment"):
            recorded3 = sched.run_scheduler(r, store, api_key="fake-key")
        assert recorded3 == 0
        assert r.hexists(sched.K_LAST_RUN, "on_due"), "실패해도 last_run 은 갱신돼야 함(폭주 방지)"
        err_status = json.loads(r.hget(sched.K_STATUS, "on_due"))
        assert err_status["ok"] is False and "429" in err_status["error"]

        # 유니버스 스캔(UNIVERSE_CONFIG_NAME) 기록은 개별 종목 뷰(publish_ai_view)에서 제외돼야 함
        # (on_due 의 sell 판단이 위에서 이미 1건 기록돼 있음 — 그건 개별 종목 뷰에 남아야 하는 정상 데이터)
        from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME
        summary_before = json.loads(r.get(sched.K_SUMMARY))
        sell_before = summary_before["by_mode"]["sell"]["signals"]
        store.record_judgment(UNIVERSE_CONFIG_NAME, _rec(action="sell"))
        sched.publish_ai_view(r, store)
        published2 = json.loads(r.get(sched.K_JUDGMENTS))
        assert all(row["config_name"] != UNIVERSE_CONFIG_NAME for row in published2), "유니버스 스캔은 개별 종목 뷰에서 제외"
        summary2 = json.loads(r.get(sched.K_SUMMARY))
        assert summary2["by_mode"]["sell"]["signals"] == sell_before, "유니버스 스캔의 sell 이 개별 종목 집계에 안 섞여야 함"

        # 콘솔에서 설정 삭제(bot:ai_configs 에서 HDEL) — 그 설정의 판단·포지션은 재발행 시 뷰에서 제외돼야
        # 함(오너 리포트: "삭제해도 판단 이력 성과 패널에 계속 나온다"). SQLite 원본은 안 지우고 필터만 거는
        # 방식이라, 삭제 이후에도 store 에서 직접 조회하면 이력은 그대로 남아있어야 한다(감사 목적 보존).
        r.hdel(sched.K_CONFIGS, "on_due")
        sched.publish_ai_view(r, store)
        published3 = json.loads(r.get(sched.K_JUDGMENTS))
        assert all(row["config_name"] != "on_due" for row in published3), "삭제된 설정의 판단은 뷰에서 빠져야 함"
        positions3 = json.loads(r.get(sched.K_POSITIONS))
        assert all(row["config_name"] != "on_due" for row in positions3), "삭제된 설정의 포지션도 뷰에서 빠져야 함"
        assert len(store.get_judgments(config_name="on_due")) > 0, "SQLite 원본 이력 자체는 삭제하지 않고 보존"

        store.close()

    print("✅ test_ai_shadow_scheduler: due 판정·에러 격리·상태기록·발행·유니버스 스캔 제외·삭제된 설정 뷰 필터·"
          "가상 포지션(오픈·반복매수무시·청산+실현손익) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
