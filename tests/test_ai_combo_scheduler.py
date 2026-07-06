# ③ 콤보 관찰 스케줄러 — due 재사용 배선·dedup 연계·에러 격리·상태기록·발행(프리픽스 벗기기)·교차뷰격리 검증
"""실행: python tests/test_ai_combo_scheduler.py"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core.ai_store import AiStore
from tools import ai_combo_scheduler as combo
from tools import ai_shadow_scheduler as sched


def _rec(action="buy"):
    return {"code": "005930", "trade_date": "20260701", "action": action,
            "confidence": 0.6, "reason": "테스트", "entry_price": 70000,
            "snapshot": {"parent": {"close": 70000}, "child": {"close": 70000}}}


def _test_force_run() -> None:
    """K_FORCE_RUN(콤보 전용 키) — ②와 동일한 강제 실행 메커니즘(2026-07 오너 요청). _due()가 False 여도
    1회 실행되고, 실행 후엔 HDEL 로 원자적으로 소비되어 재실행되지 않아야 한다. _due() 를 명시적으로
    False 로 고정해 "정말 강제가 아니면 절대 안 도는" 상황에서도 강제 플래그만으로 실행됨을 증명한다."""
    r = FakeRedis(decode_responses=True)
    with patch("tools.ai_combo_scheduler._due", return_value=False), \
         tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))
        r.hset(combo.K_CONFIGS, "new_combo", json.dumps(
            {"symbol": "005930", "parent_timeframe": "daily", "child_timeframe": "60m",
             "interval_min": 60, "enabled": True}))
        r.hset(combo.K_FORCE_RUN, "new_combo", "1")

        with patch("tools.ai_combo_scheduler.fetch_bars", return_value=[{"date": "x"}]), \
             patch("tools.ai_combo_scheduler.judge_combo", return_value=_rec()), \
             patch("tools.ai_combo_scheduler.log_judgment"):
            recorded = combo.run_combo_scheduler(r, store, api_key="fake-key")
        assert recorded == 1, "_due()=False 여도 강제 실행 플래그가 있으면 1회 처리돼야 함"
        assert not r.hexists(combo.K_FORCE_RUN, "new_combo"), "실행 후엔 플래그가 소비돼 사라져야 함"

        with patch("tools.ai_combo_scheduler.fetch_bars", return_value=[{"date": "x"}]), \
             patch("tools.ai_combo_scheduler.judge_combo", return_value=_rec()) as mock_judge2, \
             patch("tools.ai_combo_scheduler.log_judgment"):
            recorded2 = combo.run_combo_scheduler(r, store, api_key="fake-key")
        assert recorded2 == 0, "플래그 소비 후 _due()=False 면 재실행되지 않아야 함"
        mock_judge2.assert_not_called()

        store.close()


def main() -> int:
    _test_force_run()

    r = FakeRedis(decode_responses=True)
    # _due() 자체(장 상태 게이트)는 test_ai_shadow_scheduler.py 에서 이미 검증됨 — 여기선 run_combo_scheduler
    # 가 그 함수를 하위(자식) 타임프레임으로 올바르게 배선해서 호출하는지만 확인(장상태·요일 무관하게 고정).
    with patch("tools.ai_combo_scheduler._due", return_value=True), \
         tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))

        # 설정 2개: 활성(due) / 비활성
        r.hset(combo.K_CONFIGS, mapping={
            "combo_on": json.dumps({"symbol": "005930", "parent_timeframe": "daily", "child_timeframe": "60m",
                                     "interval_min": 60, "enabled": True}),
            "combo_off": json.dumps({"symbol": "000660", "parent_timeframe": "daily", "child_timeframe": "60m",
                                      "interval_min": 60, "enabled": False}),
        })

        with patch("tools.ai_combo_scheduler.fetch_bars", return_value=[{"date": "x"}]), \
             patch("tools.ai_combo_scheduler.judge_combo", return_value=_rec()) as mock_judge, \
             patch("tools.ai_combo_scheduler.log_judgment") as mock_log:
            recorded = combo.run_combo_scheduler(r, store, api_key="fake-key")

        assert recorded == 1, "due+enabled 인 combo_on 만 처리돼야 함"
        mock_judge.assert_called_once()
        # judge_combo(code, parent_timeframe, parent_bars, child_timeframe, child_bars, api_key, last_trade_date=...)
        assert mock_judge.call_args[0][1] == "daily", "상위 타임프레임이 그대로 전달돼야 함"
        assert mock_judge.call_args[0][3] == "60m", "하위 타임프레임이 그대로 전달돼야 함"
        assert mock_judge.call_args.kwargs["last_trade_date"] is None, "이력 없으면 last_trade_date=None"
        mock_log.assert_called_once()

        assert r.hexists(combo.K_LAST_RUN, "combo_on"), "처리한 설정은 last_run 갱신"
        assert not r.hexists(combo.K_LAST_RUN, "combo_off"), "비활성 설정은 손대지 않음"
        status = json.loads(r.hget(combo.K_STATUS, "combo_on"))
        assert status["ok"] is True

        # 저장은 COMBO_PREFIX 가 붙은 이름으로 — ②(단일 타임프레임)와 config_name 이 절대 안 겹치게(§ 설계)
        assert len(store.get_judgments(config_name=combo.COMBO_PREFIX + "combo_on")) == 1
        assert len(store.get_judgments(config_name="combo_on")) == 0, "프리픽스 없는 이름으로는 저장 안 됨"

        # 발행 — 콘솔은 내부 네임스페이스를 몰라도 되게 프리픽스를 벗겨서 노출
        published = json.loads(r.get(combo.K_JUDGMENTS))
        assert len(published) == 1 and published[0]["config_name"] == "combo_on", \
            "발행 시 combo: 프리픽스를 벗겨서 콘솔엔 순수 name 만 보여야 함"

        # 가상 포지션 — buy 판단 1건으로 포지션이 열려야 함(1,000,000/70,000 → 14주)
        pos = store.get_open_position(combo.COMBO_PREFIX + "combo_on", "005930")
        assert pos is not None and pos["qty"] == 14, pos
        published_pos = json.loads(r.get(combo.K_POSITIONS))
        assert len(published_pos) == 1 and published_pos[0]["config_name"] == "combo_on"
        assert published_pos[0]["status"] == "open"

        # 같은 config 재실행 — judge_combo 에 직전 trade_date 가 전달됨(dedup 은 judge_combo 내부 책임)
        r.hset(combo.K_LAST_RUN, "combo_on", 0)
        with patch("tools.ai_combo_scheduler.fetch_bars", return_value=[{"date": "x"}]), \
             patch("tools.ai_combo_scheduler.judge_combo", return_value=_rec()) as mock_judge2, \
             patch("tools.ai_combo_scheduler.log_judgment"):
            combo.run_combo_scheduler(r, store, api_key="fake-key")
        assert mock_judge2.call_args.kwargs["last_trade_date"] == "20260701", "직전 판단의 trade_date 를 넘겨줘야 함"

        # sell 판단 — 열린 포지션이 전량 청산되고 실현손익이 계산돼야 함
        r.hset(combo.K_LAST_RUN, "combo_on", 0)
        with patch("tools.ai_combo_scheduler.fetch_bars", return_value=[{"date": "x"}]), \
             patch("tools.ai_combo_scheduler.judge_combo",
                   return_value={**_rec(action="sell"), "trade_date": "20260702", "entry_price": 77000}), \
             patch("tools.ai_combo_scheduler.log_judgment"):
            combo.run_combo_scheduler(r, store, api_key="fake-key")
        assert store.get_open_position(combo.COMBO_PREFIX + "combo_on", "005930") is None
        closed = store.get_positions(config_name=combo.COMBO_PREFIX + "combo_on")
        assert len(closed) == 1 and closed[0]["status"] == "closed"
        assert abs(closed[0]["realized_pnl"] - (77000 - 70000) * 14) < 1e-6, closed[0]

        # judge_combo/fetch_bars 실패 — 격리(다른 설정 영향 없음) + last_run 갱신(재시도 폭주 방지) + status 에러 기록
        r.hset(combo.K_LAST_RUN, "combo_on", 0)
        with patch("tools.ai_combo_scheduler.fetch_bars", side_effect=RuntimeError("429 rate limited")), \
             patch("tools.ai_combo_scheduler.log_judgment"):
            recorded3 = combo.run_combo_scheduler(r, store, api_key="fake-key")
        assert recorded3 == 0
        assert r.hexists(combo.K_LAST_RUN, "combo_on"), "실패해도 last_run 은 갱신돼야 함(폭주 방지)"
        err_status = json.loads(r.hget(combo.K_STATUS, "combo_on"))
        assert err_status["ok"] is False and "429" in err_status["error"]

        # 교차 뷰 격리 — ②(개별 종목 관찰)가 우연히 콤보와 같은 이름("combo_on")으로 판단을 기록해도
        # 서로의 뷰에 안 섞여야 함(COMBO_PREFIX 네임스페이스가 실제로 격리를 보장하는지 검증 — 이번 설계의
        # 핵심 안전장치라 명시적으로 확인).
        store.record_judgment("combo_on", _rec(action="hold"))  # ②가 같은 이름을 쓰는 상황을 가정(프리픽스 없음)
        r.hset(sched.K_CONFIGS, "combo_on",
               json.dumps({"symbol": "005930", "timeframe": "daily", "interval_min": 60, "enabled": True}))
        sched.publish_ai_view(r, store)
        combo.publish_combo_view(r, store)
        ai_view = json.loads(r.get(sched.K_JUDGMENTS))
        combo_view = json.loads(r.get(combo.K_JUDGMENTS))
        assert not any(row["config_name"] == combo.COMBO_PREFIX + "combo_on" for row in ai_view), \
            "②(단일 타임프레임) 뷰엔 콤보 판단(combo: 프리픽스)이 안 섞여야 함"
        assert any(row["config_name"] == "combo_on" and row["action"] == "sell" for row in combo_view), \
            "③ 뷰엔 원래 콤보 판단(마지막 sell)이 정상적으로 나와야 함 — ②가 같은 이름을 써도 안 흔들림"
        assert sum(1 for row in ai_view if row["config_name"] == "combo_on") == 1, \
            "②(프리픽스 없는 combo_on) 자체 판단은 ② 뷰에 정상적으로 남아야 함"

        # 콘솔에서 콤보 설정 삭제 — 판단·포지션이 재발행 시 뷰에서 제외돼야 함(② 삭제 필터와 동일 원리)
        r.hdel(combo.K_CONFIGS, "combo_on")
        combo.publish_combo_view(r, store)
        published_after_delete = json.loads(r.get(combo.K_JUDGMENTS))
        assert not any(row["config_name"] == "combo_on" for row in published_after_delete), \
            "삭제된 콤보 설정의 판단은 뷰에서 빠져야 함"
        assert len(store.get_judgments(config_name=combo.COMBO_PREFIX + "combo_on")) > 0, \
            "SQLite 원본 이력 자체는 삭제하지 않고 보존"

        store.close()

    print("✅ test_ai_combo_scheduler: due 재사용 배선·강제 실행(K_FORCE_RUN)·dedup 연계·에러격리·상태기록·"
          "발행(프리픽스 벗기기)·교차뷰격리·삭제필터·가상포지션(오픈·청산) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
