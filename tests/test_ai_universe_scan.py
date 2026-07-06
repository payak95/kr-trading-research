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


def _bars(n=40):
    """단조 상승 — RSI=100(과매수)으로 항상 is_notable=True(사전 필터 통과)."""
    return [{"date": f"202601{(i % 28) + 1:02d}", "close": 50000 + i * 10} for i in range(n)]


def _boring_bars(n=40):
    """완만한 지그재그 — RSI~55·rvol~1.0·볼린저 중간대라 is_notable=False(사전 필터에 걸러짐)."""
    return [{"date": f"202601{(i % 28) + 1:02d}", "close": 50000 + (i % 4) * 20,
              "volume": 100000} for i in range(n)]


def _rec(code, action):
    return {"code": code, "trade_date": "20260201", "action": action,
            "confidence": 0.6, "reason": "x", "entry_price": 50000, "snapshot": {"close": 50000}}


def main() -> int:
    r = FakeRedis(decode_responses=True)
    with tempfile.TemporaryDirectory() as d:
        store = AiStore(db_path=os.path.join(d, "t.db"))

        # 캐시 워밍된 종목: 특이점 있는 2개("005930"·"000660") + 심심한 1개("005938", 사전 필터 제외) +
        # 콜드미스 1개("035420")
        r.set(OHLCV_CACHE_KEY.format("005930", DEFAULT_DAYS), json.dumps(_bars()))
        r.set(OHLCV_CACHE_KEY.format("000660", DEFAULT_DAYS), json.dumps(_bars()))
        r.set(OHLCV_CACHE_KEY.format("005938", DEFAULT_DAYS), json.dumps(_boring_bars()))
        # "035420" 은 캐시 없음(콜드미스)

        def _fake_judge(code, bars, api_key, last_trade_date=None, model=None):
            assert model == scan._MODEL_STAGE2, "사전 필터 통과분은 정밀 모델(_MODEL_STAGE2)로 호출돼야 함"
            return _rec(code, "buy" if code == "005930" else "hold")

        with patch("tools.ai_universe_scan.judge_from_bars", side_effect=_fake_judge), \
             patch("tools.ai_universe_scan.log_judgment"):
            result = scan.scan_universe(r, store, ["005930", "000660", "005938", "035420"], api_key="fake-key")

        assert result["judged"] == 2 and result["skipped"] == 1 and result["filtered"] == 1, result
        assert result["shortlist"] == ["005930"], "buy 판단만 숏리스트에 담김"

        # ③ 콤보 상위 게이트 후보 — is_notable 과 독립: "005938"(심심해서 사전필터엔 걸림)도 상승 추세라
        # 후보엔 포함돼야 함(수치로 검증됨: close=50060>=sma20=50030, rsi14=55>=20). 콜드미스("035420")만 제외.
        assert {c["code"] for c in result["candidates"]} == {"005930", "000660", "005938"}, result["candidates"]
        cand_005938 = next(c for c in result["candidates"] if c["code"] == "005938")
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
        assert {c["code"] for c in combo_candidates["codes"]} == {"005930", "000660", "005938"}

        # 동일 trade_date 재스캔 — judge_from_bars 에 직전 trade_date 가 전달됨(중복 방지는 judge_from_bars 책임)
        with patch("tools.ai_universe_scan.judge_from_bars") as mock_judge, \
             patch("tools.ai_universe_scan.log_judgment"):
            mock_judge.return_value = None  # dedup 스킵 시뮬레이션
            scan.scan_universe(r, store, ["005930"], api_key="fake-key")
        assert mock_judge.call_args.kwargs["last_trade_date"] == "20260201"
        assert mock_judge.call_args.kwargs["model"] == scan._MODEL_STAGE2

        store.close()

    print("✅ test_ai_universe_scan: 캐시 재사용(콜드미스 skip)·사전 필터(is_notable)·정밀 모델(_MODEL_STAGE2) 전달·"
          "숏리스트·③ 콤보후보(_parent_permits 독립)·발행 분리 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
