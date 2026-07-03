# 수급 캐시 크론 — store_cache(주입 fetch)·load_flow_cache·main() 검증(fakeredis, 네트워크 없음)
"""실행: python tests/test_flow_universe.py"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from tools import backtest_worker as bw
from tools.flow_universe import FLOW_CACHE_KEY, load_flow_cache, store_cache

_ROWS_A = [{"date": "20260701", "close": 100, "volume": 1000, "orgn_ntby_qty": 10, "frgn_ntby_qty": 20}]


def main() -> int:
    r = FakeRedis(decode_responses=True)

    # ── store_cache: 주입 fetch 로 종목별 수급 캐시(실패 skip, 긴 TTL) ──
    def fetch(code, days):
        if code == "BAD":
            raise RuntimeError("네이버 오류")
        if code == "EMPTY":
            return []
        return _ROWS_A
    codes = ["005930", "BAD", "EMPTY", "000660"]
    n = store_cache(r, codes, fetch=fetch, ttl=bw.UNIVERSE_CACHE_TTL)
    assert n == 2, f"성공 2건(005930·000660), BAD·EMPTY skip: {n}"
    key = FLOW_CACHE_KEY.format("005930")
    assert json.loads(r.get(key)) == _ROWS_A and r.ttl(key) > 0, "저장·TTL 설정"
    assert r.get(FLOW_CACHE_KEY.format("BAD")) is None, "실패 종목은 저장 안 됨"
    assert r.get(FLOW_CACHE_KEY.format("EMPTY")) is None, "빈 결과도 저장 안 됨"

    # ── load_flow_cache: 캐시 전용 로더(네트워크 없음) — 있는 것만, 손상/미존재 skip ──
    loaded = load_flow_cache(r, ["005930", "000660", "004000"])  # 004000 은 캐시 없음
    assert set(loaded) == {"005930", "000660"} and loaded["005930"] == _ROWS_A, loaded
    r.set(FLOW_CACHE_KEY.format("BROKEN"), "{bad json")
    assert "BROKEN" not in load_flow_cache(r, ["BROKEN"]), "손상 JSON skip"

    # ── main(): 유니버스 비어있으면 미갱신(0), REDIS_URL 없으면 에러 코드 ──
    import tools.flow_universe as fu
    r_empty = FakeRedis(decode_responses=True)
    os.environ["REDIS_URL"] = "redis://fake"
    import redis as redis_module
    orig = redis_module.from_url
    redis_module.from_url = lambda *a, **k: r_empty
    try:
        rc = fu.main()
    finally:
        redis_module.from_url = orig
        del os.environ["REDIS_URL"]
    assert rc == 0, "유니버스 비어있어도 정상 종료(0)"

    print("✅ test_flow_universe: store_cache(주입fetch·실패skip·TTL)·load_flow_cache(캐시전용·손상skip)·main(빈유니버스) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
