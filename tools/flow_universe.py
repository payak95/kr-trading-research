# 야간 크론 — 유니버스 종목의 외국인·기관 수급(순매매) 캐시 워밍(스크리닝 강화, §큰손 추적)
"""콘솔 "유니버스 전체 검색"이 `foreign_accumulation`/`institution_accumulation` 셋업을 참조하는 스펙을
쓸 때, 유니버스(수백 종목) 전체에 종목별 실시간 조회를 하면 느리다 — `screen_universe.py`가 이미
확정한 유니버스(`bot:screen:universe`)를 재사용해 각 종목의 최근 20거래일 수급(`tools.naver_investor.
daily_investor_flow`)을 미리 캐시한다(같은 TTL, 같은 방식 — screen_universe.py 의 일봉 캐시 워밍과 동일
아키텍처). 낮엔 워커가 이 캐시만 읽는다(네트워크 재호출 없음). 네이버(무인증·무한도) 전용, KIS 무관.

screen_universe.py **이후**에 돌아야 함(유니버스가 비어 있으면 아무것도 안 함, 기존 캐시 유지).
cron: RUNTIME.md 참고.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import backtest_worker as bw
from tools.naver_investor import daily_investor_flow

FLOW_CACHE_KEY = "bot:screen:flow:{}"  # code → JSON(daily_investor_flow 결과)
WINDOW_DAYS = 20  # AlphaForge flow.foreign_accumulation 관측 기본값(trading/flow.py 와 동일)


def store_cache(r, codes: list[str], days: int = WINDOW_DAYS, ttl: int = bw.UNIVERSE_CACHE_TTL,
                progress_every: int = 50, fetch=daily_investor_flow) -> int:
    """codes 각각의 수급을 네이버로 받아 캐시(성공 건수 반환). 종목별 실패는 skip(전체 보호).
    fetch 주입 가능(테스트용, 기본은 daily_investor_flow). progress_every 마다 진행률 출력
    (장시간 작업 가시화, screen_universe.py 와 동일 패턴)."""
    total = len(codes)
    n = 0
    for i, code in enumerate(codes, 1):
        try:
            rows = fetch(code, days)
        except Exception:
            rows = []
        if rows:
            try:
                r.set(FLOW_CACHE_KEY.format(code), json.dumps(rows, ensure_ascii=False), ex=ttl)
                n += 1
            except Exception:
                pass
        if progress_every and (i % progress_every == 0 or i == total):
            print(f"  [{i}/{total}] 수급 수집 중… (성공 {n})", flush=True)
    return n


def load_flow_cache(r, codes) -> dict:
    """캐시 전용 로더(네트워크 없음) — screen_spec/pipeline_worker 의 유니버스 경로가 사용.
    없거나 손상된 항목은 결과에서 제외(안전 무시)."""
    out = {}
    for code in codes:
        try:
            raw = r.get(FLOW_CACHE_KEY.format(code))
        except Exception:
            continue
        if not raw:
            continue
        try:
            out[code] = json.loads(raw)
        except (ValueError, TypeError):
            continue
    return out


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 수급 캐시 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    codes = sorted(r.smembers(bw.UNIVERSE_KEY))
    if not codes:
        print("유니버스가 비어 있음(screen_universe.py 가 먼저 실행돼야 함) — 수급 캐시 미갱신")
        return 0
    print(f"유니버스 {len(codes)}종목 수급 캐시 시작(네이버, 종목당 ~0.3s)…", flush=True)
    n = store_cache(r, codes)
    print(f"수급 캐시: {len(codes)}종목 중 {n}건 저장(window={WINDOW_DAYS}일)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
