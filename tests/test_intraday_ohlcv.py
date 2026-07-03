# Yahoo v8 분봉 조회 — 시장 접미사 결정(.KS/.KQ)·캐시·재시도·타임존 변환 검증(네트워크는 mock)
"""실행: python tests/test_intraday_ohlcv.py"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import intraday_ohlcv as io


def _result(exch: str, ts: list, closes: list, opens=None, highs=None, lows=None, volumes=None) -> dict:
    n = len(ts)
    return {
        "meta": {"fullExchangeName": exch},
        "timestamp": ts,
        "indicators": {"quote": [{
            "open": opens or closes, "high": highs or closes, "low": lows or closes,
            "close": closes, "volume": volumes or [1000] * n,
        }]},
    }


def _resp(status_code: int, body: dict | None = None) -> MagicMock:
    r = MagicMock(status_code=status_code)
    r.json.return_value = {"chart": {"result": [body] if body else []}}
    return r


def main() -> int:
    io._SUFFIX_CACHE.clear()

    # 1) 코스피 — .KS 조회 결과가 KOSDAQ 이 아니면 그대로 사용(1콜)
    ts0 = int(datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())  # UTC 00:00 = KST 09:00
    with patch("tools.intraday_ohlcv.requests.get",
               return_value=_resp(200, _result("KSE", [ts0], [70000]))) as mock_get:
        bars = io.resolve_and_fetch("005930", "5m")
    assert mock_get.call_count == 1, "코스피는 1콜로 끝나야 함"
    assert len(bars) == 1 and bars[0]["close"] == 70000
    assert bars[0]["date"] == "202607010900", f"UTC→KST 명시 변환 필요: {bars[0]['date']}"
    assert io._SUFFIX_CACHE["005930"] == ".KS"

    # 2) 코스닥 — 첫(.KS) 조회는 meta 로만 판별하고 버림, .KQ 로 재조회(2콜)
    io._SUFFIX_CACHE.clear()
    ts1 = int(datetime(2026, 7, 1, 0, 30, 0, tzinfo=timezone.utc).timestamp())
    with patch("tools.intraday_ohlcv.requests.get",
               side_effect=[_resp(200, _result("KOSDAQ", [ts1], [None])),  # .KS 프로브 — 무효 데이터
                            _resp(200, _result("KOSDAQ", [ts1], [124400]))],  # .KQ 재조회 — 진짜 데이터
               ) as mock_get, patch("tools.intraday_ohlcv.time.sleep"):
        bars = io.resolve_and_fetch("247540", "30m")
    assert mock_get.call_count == 2, "코스닥은 .KS 프로브 + .KQ 재조회로 2콜"
    urls = [c.args[0] for c in mock_get.call_args_list]
    assert urls[0].endswith("247540.KS") and urls[1].endswith("247540.KQ"), urls
    assert io._SUFFIX_CACHE["247540"] == ".KQ"
    assert len(bars) == 1 and bars[0]["close"] == 124400

    # 3) 캐시 재사용 — 같은 코드 재호출은 1콜(접미사 재프로브 없음)
    with patch("tools.intraday_ohlcv.requests.get",
               return_value=_resp(200, _result("KOSDAQ", [ts1], [125000]))) as mock_get2:
        bars2 = io.resolve_and_fetch("247540", "60m")
    assert mock_get2.call_count == 1, "캐시된 접미사는 재프로브 없이 1콜"
    assert bars2[0]["close"] == 125000

    # 4) close 결측 봉은 드롭
    io._SUFFIX_CACHE.clear()
    with patch("tools.intraday_ohlcv.requests.get",
               return_value=_resp(200, _result("KSE", [ts0, ts1], [70000, None]))):
        bars3 = io.resolve_and_fetch("005930", "5m")
    assert len(bars3) == 1, "close 가 null 인 봉은 드롭돼야 함"

    # 5) 알 수 없는 timeframe → 네트워크 호출 없이 즉시 []
    with patch("tools.intraday_ohlcv.requests.get") as mock_get3:
        assert io.resolve_and_fetch("005930", "1h") == []
    mock_get3.assert_not_called()

    # 6) 일시 오류(429) 재시도 후 성공
    io._SUFFIX_CACHE.clear()
    with patch("tools.intraday_ohlcv.requests.get",
               side_effect=[_resp(429), _resp(200, _result("KSE", [ts0], [70000]))]) as mock_get4, \
         patch("tools.intraday_ohlcv.time.sleep"):
        bars4 = io.resolve_and_fetch("005930", "5m")
    assert mock_get4.call_count == 2, "429 이후 재시도로 성공해야 함"
    assert len(bars4) == 1

    # 7) 재시도 소진(완전 실패) → [](접미사 캐시 안 함)
    io._SUFFIX_CACHE.clear()
    with patch("tools.intraday_ohlcv.requests.get", return_value=_resp(500)), \
         patch("tools.intraday_ohlcv.time.sleep"):
        assert io.resolve_and_fetch("005930", "5m") == []
    assert "005930" not in io._SUFFIX_CACHE, "완전 실패 시 접미사를 확정 못 하므로 캐시하면 안 됨"

    print("✅ test_intraday_ohlcv: 접미사 프로브·캐시·null 드롭·재시도·UTC→KST 변환 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
