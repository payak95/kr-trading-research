# 유니버스 크론 — 집합 교체(원자적)·2단 선별(시총풀→거래대금)·캐시 저장 검증(fakeredis, 네트워크 없음)
"""실행: python tests/test_screen_universe.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from tools import backtest_worker as bw
from tools.screen_universe import avg_trade_value, select_universe, store_cache, update_universe


def _bars(close, vol, n=70):
    return [{"date": f"d{i:03d}", "open": close, "high": close, "low": close, "close": close, "volume": vol}
            for i in range(n)]


def main() -> int:
    r = FakeRedis(decode_responses=True)

    # ── update_universe: 집합 교체(기존 다른 종목을 새 codes 로 대체) ──
    r.sadd(bw.UNIVERSE_KEY, "OLD1", "OLD2")
    n = update_universe(r, ["005930", "000660", "035420"])
    assert n == 3 and r.smembers(bw.UNIVERSE_KEY) == {"005930", "000660", "035420"}, "기존 OLD* 대체"
    assert not r.exists(bw.UNIVERSE_KEY + ":tmp"), "임시키 정리됨(rename)"
    assert update_universe(r, []) == 0 and r.scard(bw.UNIVERSE_KEY) == 3, "빈 codes 면 기존 유지"

    # ── avg_trade_value: 최근 window 평균 거래대금(종가×거래량) ──
    assert avg_trade_value(_bars(100, 10), window=60) == 1000.0, "100×10"
    assert avg_trade_value([], window=60) == 0.0 and avg_trade_value(_bars(50, 0)) == 0.0

    # ── select_universe: 시총풀 → 거래대금 상위 size 확정(일봉 1회 fetch, 실패·빈봉 제외) ──
    # 거래대금: HIGH(200×100=20000) > MID(100×50=5000) > LOW(100×5=500). BAD 예외, EMPTY 빈봉.
    series = {"HIGH": _bars(200, 100), "MID": _bars(100, 50), "LOW": _bars(100, 5)}
    calls = []
    def fetch(code, days):
        calls.append(code)
        if code == "BAD":
            raise RuntimeError("초당 거래건수 초과")
        if code == "EMPTY":
            return []
        return series.get(code, _bars(100, 1))
    pool = ["LOW", "HIGH", "MID", "BAD", "EMPTY"]
    final, bars_by = select_universe(pool, fetch, days=90, size=2)
    assert final == ["HIGH", "MID"], f"거래대금 상위2(BAD·EMPTY 제외): {final}"
    assert set(bars_by) == {"HIGH", "MID"}, "확정 종목 일봉만 반환"
    assert calls == pool, "풀 전체 1회씩만 fetch(중복 없음)"

    # ── store_cache: 확정 종목 일봉을 긴 TTL 로 저장 → 낮 캐시 전용 fetch 가 읽음 ──
    n_warm = store_cache(r, bars_by, days=90, ttl=bw.UNIVERSE_CACHE_TTL)
    assert n_warm == 2
    key = bw.OHLCV_CACHE_KEY.format("HIGH", 90)
    assert json.loads(r.get(key)) == series["HIGH"] and r.ttl(key) > bw.OHLCV_CACHE_TTL, "긴 TTL 저장"
    cof = bw._cache_only_fetch(r)
    assert cof("HIGH", 90) == series["HIGH"] and cof("LOW", 90) == [], "확정만 캐시 / 탈락은 미스"

    print("✅ test_screen_universe: update_universe·avg_trade_value·select_universe(시총→거래대금)·store_cache·캐시연결 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
