# 야간(장 마감 후) 크론 — 시총·거래대금 2단 하이브리드로 유니버스 확정 + 일봉 캐시 워밍
"""콘솔 '유니버스 전체 검색'(우량·유동 종목 스크리닝)을 위한 야간 준비 작업(퀀트 표준 2단 필터).
1) **1차 시총**: 네이버 시총 상위 풀(MCAP_POOL, ETF 제외) — 잡주 원천 차단.
2) **일봉 fetch**: 풀 종목 일봉을 네이버로 받음(`backtest_worker.naver_fetch` 재사용, KIS 미접속).
3) **2차 거래대금**: 최근 LIQ_WINDOW 거래일 평균 거래대금(종가×거래량) 상위 UNIVERSE_SIZE 확정.
4) 확정 종목만 일봉 캐시(긴 TTL) → 낮엔 워커가 캐시 전용으로 즉시 스크리닝(KIS 무접속).

장 마감 후 실행이라 라이브 봇과 KIS 한도 경합 자체가 없음(이 크론은 KIS 를 전혀 안 씀). 네이버 미수신 시
유니버스 미갱신(기존 유지). 시총 랭킹·일봉 모두 네이버(무인증·모의 무관, KRX 는 서버 차단됨). cron: RUNTIME.md 참고.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import backtest_worker as bw
from tools.naver_universe import top_marketcap_codes

MCAP_POOL = int(os.environ.get("SCREEN_UNIVERSE_POOL", "450"))      # 1차: 시총 상위 풀(잡주 차단)
UNIVERSE_SIZE = int(os.environ.get("SCREEN_UNIVERSE_SIZE", "300"))  # 2차: 거래대금 상위 최종 N
LIQ_WINDOW = 60                                                     # 평균 거래대금 산정 거래일 수(≈3개월)
META_KEY = "bot:screen:universe:meta"  # 콘솔 표시용 {pool, size, warmed, days, window, updated_at}


def update_universe(r, codes: list[str]) -> int:
    """유니버스 집합을 codes 로 원자적 교체(임시키 SADD → RENAME). 빈 codes 면 0(미갱신·기존 유지)."""
    if not codes:
        return 0
    tmp = bw.UNIVERSE_KEY + ":tmp"
    pipe = r.pipeline()
    pipe.delete(tmp)
    pipe.sadd(tmp, *codes)
    pipe.rename(tmp, bw.UNIVERSE_KEY)
    pipe.execute()
    return len(codes)


def avg_trade_value(bars: list[dict], window: int = LIQ_WINDOW) -> float:
    """최근 window 거래일 평균 거래대금(종가×거래량 근사 — KIS 일봉은 거래대금 미제공). 봉 부족하면 있는 만큼."""
    recent = bars[-window:] if bars else []
    vals = [(b.get("close") or 0) * (b.get("volume") or 0) for b in recent]
    return sum(vals) / len(vals) if vals else 0.0


def select_universe(pool: list[str], fetch, days: int, size: int, window: int = LIQ_WINDOW,
                    progress_every: int = 25):
    """1차 풀(시총 상위 codes) → 일봉 fetch 로 최근 window 평균 거래대금 산정 → 상위 size 확정.
    반환 (final_codes, bars_by_code[final]). 종목별 fetch 실패·빈봉은 제외(전체 보호). 일봉은 1회만 fetch.
    진행률을 progress_every 마다 출력(flush — docker 버퍼링 회피, 장시간 작업 가시화)."""
    total = len(pool)
    scored = []
    bars_by_code = {}
    for i, code in enumerate(pool, 1):
        try:
            bars = fetch(code, days)
        except Exception:
            bars = None
        if bars:
            bars_by_code[code] = bars
            scored.append((code, avg_trade_value(bars, window)))
        if progress_every and (i % progress_every == 0 or i == total):
            print(f"  [{i}/{total}] 일봉·거래대금 수집 중… (유효 {len(scored)})", flush=True)
    scored.sort(key=lambda x: x[1], reverse=True)
    final = [c for c, _ in scored[:size]]
    return final, {c: bars_by_code[c] for c in final}


def store_cache(r, bars_by_code: dict, days: int, ttl: int) -> int:
    """확정 종목의 (이미 받은) 일봉을 OHLCV 캐시에 긴 TTL 로 저장(낮 캐시 전용 스크리닝용). 저장 건수."""
    n = 0
    for code, bars in bars_by_code.items():
        if not bars:
            continue
        try:
            r.set(bw.OHLCV_CACHE_KEY.format(code, days), json.dumps(bars, ensure_ascii=False), ex=ttl)
            n += 1
        except Exception:
            pass
    return n


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 유니버스 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    pool = top_marketcap_codes(count=MCAP_POOL)  # 1차: 시총 상위 풀
    if not pool:
        print("네이버 시총 상위 수신 실패 — 유니버스 미갱신(기존 유지)")
        return 0
    days = bw.DEFAULT_DAYS
    print(f"시총풀 {len(pool)}종목 수신 — 일봉·거래대금 수집 시작(네이버, 종목당 ~0.3s)…", flush=True)
    final, bars_by_code = select_universe([p["code"] for p in pool], bw.naver_fetch, days, UNIVERSE_SIZE)  # 2차: 거래대금
    n_uni = update_universe(r, final)
    n_warm = store_cache(r, bars_by_code, days, bw.UNIVERSE_CACHE_TTL)
    try:
        r.set(META_KEY, json.dumps({"pool": len(pool), "size": n_uni, "warmed": n_warm,
                                    "days": days, "window": LIQ_WINDOW, "updated_at": int(time.time())},
                                   ensure_ascii=False))
    except Exception:
        pass
    print(f"유니버스: 시총풀 {len(pool)} → 거래대금 상위 {n_uni} 확정 · 일봉 캐시 {n_warm}(days={days}, window={LIQ_WINDOW})")
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("screen_universe", main))
