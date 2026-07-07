# 유니버스 야간 스캐너 — 캐시 재사용(콜드미스 skip)·중복방지·발행 분리 검증(fakeredis, 네트워크 없음)
"""실행: python tests/test_ai_universe_scan.py"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore
from tools import ai_universe_scan as scan
from tools.backtest_worker import DEFAULT_DAYS, OHLCV_CACHE_KEY


def _bars(n=90):
    """단조 상승 — RSI=100(과매수)으로 항상 is_notable=True(사전 필터 통과). 60일선도 계산되게 90봉
    (sma60 은 60봉 미만이면 None 이라 콤보 후보 판정에 필요)."""
    return [{"date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "close": 50000 + i * 10} for i in range(n)]


def _boring_bars(n=90):
    """완만한 우상향 드리프트(RSI~50, 극단 아님이라 is_notable=False) — 그래도 20일선이 60일선 위라
    ③ 콤보 후보의 "진짜 추세" 조건은 만족(수치 검증: close/sma20-1 ≈ +0.04%, 이격도가 가장 작아 정렬 1위)."""
    return [{"date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "close": 50000 + i * 3 + (i % 4) * 20,
              "volume": 100000} for i in range(n)]


def _marginal_cross_bars(n=90):
    """장기 하락(마지막 4봉만 반등) — 오늘 종가가 20일선은 넘겼지만(_parent_permits 는 통과) 60일선은
    여전히 위(중기 추세 자체는 하락 구조, 우연한 크로스). ③ 콤보 후보의 신규 sma20>sma60 확인에서
    제외돼야 함(수치 검증: close=57000 < sma60=62017 방향의 하락 구조 잔존)."""
    closes, price = [], 80000
    for i in range(n):
        price += -300 if i < n - 4 else 700
        closes.append(price)
    return [{"date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}", "close": c} for i, c in enumerate(closes)]


def _rec(code, action):
    return {"code": code, "trade_date": "20260201", "action": action,
            "confidence": 0.6, "reason": "x", "entry_price": 50000, "snapshot": {"close": 50000}}


def main() -> int:
    r = FakeRedis(decode_responses=True)
    with tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))

        # 캐시 워밍된 종목: 특이점 있는 2개("005930"·"000660") + 심심하지만 진짜 추세인 1개("005938",
        # 사전 필터 제외) + 우연히 20일선만 넘긴 1개("017670", 중기 추세는 하락 구조라 콤보 후보 제외) +
        # 콜드미스 1개("035420")
        r.set(OHLCV_CACHE_KEY.format("005930", DEFAULT_DAYS), json.dumps(_bars()))
        r.set(OHLCV_CACHE_KEY.format("000660", DEFAULT_DAYS), json.dumps(_bars()))
        r.set(OHLCV_CACHE_KEY.format("005938", DEFAULT_DAYS), json.dumps(_boring_bars()))
        r.set(OHLCV_CACHE_KEY.format("017670", DEFAULT_DAYS), json.dumps(_marginal_cross_bars()))
        # "035420" 은 캐시 없음(콜드미스)

        def _fake_judge(code, bars, api_key, last_trade_date=None, model=None):
            assert model == scan._MODEL_STAGE2, "사전 필터 통과분은 정밀 모델(_MODEL_STAGE2)로 호출돼야 함"
            return _rec(code, "buy" if code == "005930" else "hold")

        with patch("tools.ai_universe_scan.judge_from_bars", side_effect=_fake_judge), \
             patch("tools.ai_universe_scan.log_judgment"):
            result = scan.scan_universe(
                r, store, ["005930", "000660", "005938", "017670", "035420"], api_key="fake-key")

        assert result["judged"] == 2 and result["skipped"] == 1 and result["filtered"] == 2, result
        assert result["shortlist"] == ["005930"], "buy 판단만 숏리스트에 담김"

        # ③ 콤보 상위 게이트 후보 — is_notable 과 독립: "005938"(심심해서 사전필터엔 걸림)도 진짜 추세(20>60일선)
        # 라 후보에 포함. "017670"은 _parent_permits 는 통과해도 60일선 확인에서 걸러져 제외(우연한 크로스).
        # 20일선 이격도 낮은 순 정렬 — "005938"(≈+0.04%)이 "005930"/"000660"(≈+0.19%)보다 앞에 와야 함.
        assert [c["code"] for c in result["candidates"]] == ["005938", "005930", "000660"], result["candidates"]
        cand_005938 = result["candidates"][0]
        assert cand_005938["close"] > 0 and cand_005938["sma20"] > 0 and cand_005938["rsi14"] > 0

        rows = store.get_judgments(config_name=UNIVERSE_CONFIG_NAME)
        assert len(rows) == 2 and all(row["config_name"] == UNIVERSE_CONFIG_NAME for row in rows)
        assert "005938" not in [row["code"] for row in rows], "특이점 없는 종목은 Gemini 호출 자체가 생략됨"

        # 발행 — bot:ai:universe:* 로만, 개별 종목 뷰(bot:ai:judgments)와 무관
        scan.publish_universe_view(r, store, result["shortlist"])
        scan.publish_combo_candidates(r, result["candidates"])
        assert r.exists(scan.K_JUDGMENTS) and r.exists(scan.K_SUMMARY) and r.exists(scan.K_SHORTLIST)
        assert not r.exists("bot:ai:judgments"), "유니버스 스캔은 개별 종목 뷰 키를 안 건드림"
        shortlist = json.loads(r.get(scan.K_SHORTLIST))
        assert shortlist["codes"] == ["005930"]
        summary = json.loads(r.get(scan.K_SUMMARY))
        assert summary["by_mode"]["buy"]["signals"] == 1 and summary["by_mode"]["hold"]["signals"] == 1

        combo_candidates = json.loads(r.get(scan.K_COMBO_CANDIDATES))
        assert combo_candidates["date"] and len(combo_candidates["codes"]) == 3
        assert [c["code"] for c in combo_candidates["codes"]] == ["005938", "005930", "000660"], \
            "발행값도 이격도 낮은 순 정렬이 유지돼야 함"

        # 동일 trade_date 재스캔 — judge_from_bars 에 직전 trade_date 가 전달됨(중복 방지는 judge_from_bars 책임)
        with patch("tools.ai_universe_scan.judge_from_bars") as mock_judge, \
             patch("tools.ai_universe_scan.log_judgment"):
            mock_judge.return_value = None  # dedup 스킵 시뮬레이션
            scan.scan_universe(r, store, ["005930"], api_key="fake-key")
        assert mock_judge.call_args.kwargs["last_trade_date"] == "20260201"
        assert mock_judge.call_args.kwargs["model"] == scan._MODEL_STAGE2

        store.close()

    # ── ①→② 자동 핸드오프(로드맵 §A) — 자동 등록(상한·확신도순·비덮어쓰기)·자동 만료(입양·보유 예외) ──
    r2 = FakeRedis(decode_responses=True)
    with tempfile.TemporaryDirectory() as d:
        store2 = AiStore(db_path=os.path.join(d, "t2.db"))

        # 수동 설정이 이미 있는 "111111"은 HSETNX 라 덮어쓰지 않음
        manual_cfg = {"symbol": "111111", "timeframe": "daily", "lookback_days": 200, "interval_min": 30,
                       "enabled": False}
        r2.hset(scan.K_WATCH_CONFIGS, "111111_daily", json.dumps(manual_cfg))
        buys = [{"code": "111111", "confidence": 0.9}, {"code": "222222", "confidence": 0.5},
                {"code": "333333", "confidence": 0.8}, {"code": "444444", "confidence": None}]
        with patch.object(scan, "AUTO_WATCH_LIMIT", 2):
            added = scan.auto_register_watch(r2, buys, today="20260706")
        assert added == ["333333_daily", "222222_daily"], \
            f"확신도 내림차순 + 기존 설정 비덮어쓰기(수동 111111 제외) + 상한 2: {added}"
        assert json.loads(r2.hget(scan.K_WATCH_CONFIGS, "111111_daily")) == manual_cfg, "수동 설정 보존"
        auto_cfg = json.loads(r2.hget(scan.K_WATCH_CONFIGS, "333333_daily"))
        assert auto_cfg["auto_registered"] == "20260706" and auto_cfg["enabled"] is True \
            and auto_cfg["timeframe"] == "daily" and auto_cfg["interval_min"] == 60, auto_cfg
        with patch.object(scan, "AUTO_WATCH_LIMIT", 0):
            assert scan.auto_register_watch(r2, buys, today="20260706") == [], "상한 0 = 기능 끔"

        # 만료: 등록 5거래일 경과(20260626 등록 → 2026-07-06 기준 6거래일) + 열린 포지션 없음 → 제거
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 7, 6, 18, 0, tzinfo=timezone(timedelta(hours=9)))
        assert scan._trading_days_since("20260626", now) >= scan.AUTO_WATCH_EXPIRE_TDAYS
        old = {"symbol": "555555", "timeframe": "daily", "lookback_days": 120, "interval_min": 60,
               "enabled": True, "auto_registered": "20260626"}
        r2.hset(scan.K_WATCH_CONFIGS, "555555_daily", json.dumps(old))
        r2.hset(scan.K_WATCH_LAST_RUN, "555555_daily", "123")
        r2.hset(scan.K_WATCH_STATUS, "555555_daily", "{}")
        # 같은 조건이지만 열린 가상 포지션이 있는 "666666"은 청산 전까지 유지
        r2.hset(scan.K_WATCH_CONFIGS, "666666_daily",
                json.dumps({**old, "symbol": "666666"}))
        store2.open_position("666666_daily", "666666", 1, 10000, "20260626")

        expired = scan.expire_auto_watch(r2, store2, now=now)
        assert expired == ["555555_daily"], expired
        assert not r2.hexists(scan.K_WATCH_CONFIGS, "555555_daily") \
            and not r2.hexists(scan.K_WATCH_LAST_RUN, "555555_daily") \
            and not r2.hexists(scan.K_WATCH_STATUS, "555555_daily"), "설정·last_run·status 함께 정리"
        assert r2.hexists(scan.K_WATCH_CONFIGS, "666666_daily"), "열린 포지션 보유 중엔 만료 안 함"
        assert r2.hexists(scan.K_WATCH_CONFIGS, "111111_daily"), "수동(auto_registered 없음)은 만료 대상 아님"
        assert r2.hexists(scan.K_WATCH_CONFIGS, "333333_daily"), "최근(20260706) 등록분은 유지"

        # 예산 초과(allow_llm=False) — Gemini 경로만 꺼지고 무료 계산(콤보 후보)은 계속(§E)
        r2.set(OHLCV_CACHE_KEY.format("005930", DEFAULT_DAYS), json.dumps(_bars()))
        with patch("tools.ai_universe_scan.judge_from_bars") as mock_judge:
            res = scan.scan_universe(r2, store2, ["005930"], api_key="fake-key", allow_llm=False)
        assert mock_judge.call_count == 0 and res["judged"] == 0 and res["buys"] == [], res
        assert {c["code"] for c in res["candidates"]} == {"005930"}, "무료 계산(콤보 후보)은 예산과 무관"

        store2.close()

    print("✅ test_ai_universe_scan: 캐시 재사용(콜드미스 skip)·사전 필터(is_notable)·정밀 모델(_MODEL_STAGE2) 전달·"
          "숏리스트·③ 콤보후보(_parent_permits 독립)·발행 분리·자동 핸드오프(상한·확신도순·비덮어쓰기·"
          "만료+입양/보유 예외)·예산가드(allow_llm=False) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
