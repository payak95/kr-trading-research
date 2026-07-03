# 네이버 일봉 파서 검증(네트워크 없음)
"""실행: python tests/test_naver_ohlcv.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.naver_ohlcv import parse_items

_XML = """<?xml version="1.0" encoding="EUC-KR" ?>
<protocol><chartdata symbol="005930" count="3" timeframe="day">
  <item data="20260617|332000|348000|331500|346500|18134051" />
  <item data="20260618|345000|363000|344500|362500|3276445" />
  <item data="20260619|360000|365000|355000|358000|5000000" />
</chartdata></protocol>"""


def main() -> int:
    bars = parse_items(_XML)
    assert len(bars) == 3, f"3봉: {len(bars)}"
    assert bars[0] == {"date": "20260617", "open": 332000, "high": 348000,
                       "low": 331500, "close": 346500, "volume": 18134051}, bars[0]
    assert [b["date"] for b in bars] == ["20260617", "20260618", "20260619"], "시간순"
    assert all(isinstance(b[k], int) for b in bars for k in ("open", "high", "low", "close", "volume")), "정수"
    assert parse_items("garbage") == [], "비정상 입력 → 빈 리스트"
    print("✅ test_naver_ohlcv: parse_items(OHLCV·시간순·정수) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
