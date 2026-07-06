# AI 섀도 판단의 D+N forward 수익·초과수익을 일봉으로 평가 → Redis publish (cron 일1회)
"""core/ai_store.py 의 미평가 판단을 네이버 일봉(무인증)으로 평가한다. tools/forward_eval.py(매수 신호
전용, KIS 브로커 시세 필요)와 같은 수학(trading/tracking.py)을 쓰지만, 저장소가 다르고(ai_store) 브로커
자격증명이 필요 없다(섀도 판단은 라이브 계좌와 무관 — 네이버로 충분, KIS 라이브 경로 분리 원칙 유지).
실행: python tools/ai_forward_eval.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import AiStore
from tools.ai_shadow_scheduler import publish_ai_view
from tools.ai_combo_scheduler import publish_combo_view
from tools.naver_ohlcv import daily_ohlcv
from kr_research.trading.tracking import BENCHMARK_CODE, HORIZONS, benchmark_returns, forward_returns


def _slim(code: str, days: int) -> list[dict]:
    """일봉 조회 → {date,close} 슬림 리스트. 실패 시 빈 리스트(해당 종목 평가 skip, naver_ohlcv 와 동일 fail-safe)."""
    try:
        return [{"date": b["date"], "close": b["close"]} for b in daily_ohlcv(code, count=days)]
    except Exception as e:
        print(f"[ai_forward_eval] {code} 일봉 조회 실패 → skip: {e}")
        return []


def run_eval(store: AiStore) -> int:
    """미평가 판단을 전부 평가·저장(발행은 안 함, main()이 처리) — 테스트 가능하도록 분리. 반환=평가 건수."""
    open_rows = store.get_open_judgments(HORIZONS)
    by_code: dict[str, list[dict]] = {}
    for row in open_rows:
        by_code.setdefault(row["code"], []).append(row)

    # 벤치마크(KODEX200) 일봉 1회 — D+20 까지 보려면 넉넉히(120영업일 ≈ 6개월)
    bench = _slim(BENCHMARK_CODE, 200) if open_rows else []

    evaluated = 0
    for code, rows in by_code.items():
        slim = _slim(code, 200)
        if not slim:
            continue
        for row in rows:
            # trade_date 는 일봉=8자(YYYYMMDD), 분봉=12자(YYYYMMDDHHMM) — [:8]로 캘린더 날짜만 취해 일봉
            # D+N 평가에 그대로 재사용(일봉은 no-op). 분봉 진입가 대비 이후 "일봉" 종가 D+N 근사치다.
            rets = forward_returns(slim, row["trade_date"][:8], row["entry_price"], HORIZONS)
            brets = benchmark_returns(bench, row["trade_date"][:8], HORIZONS)
            new_r = {n: rets[n] for n in HORIZONS if row.get(f"ret_d{n}") is None and rets[n] is not None}
            new_b = {n: brets[n] for n in HORIZONS if row.get(f"bench_d{n}") is None and brets[n] is not None}
            if new_r or new_b:
                store.set_judgment_returns(row["id"], new_r, new_b)
                evaluated += 1
    return evaluated


def main() -> int:
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[ai_forward_eval] REDIS_URL 미설정 — publish 불가, 종료")
        return 1

    store = AiStore()
    open_before = len(store.get_open_judgments(HORIZONS))
    evaluated = run_eval(store)

    import redis
    r = redis.from_url(redis_url, decode_responses=True)
    publish_ai_view(r, store)
    publish_combo_view(r, store)  # ③ 콤보 관찰 판단도 config_name 무관하게 run_eval() 이 함께 평가하므로,
                                  # 평가 직후 즉시 반영되도록 같이 재발행(안 하면 다음 콤보 스케줄러 tick 까지 지연)
    print(f"[ai_forward_eval] open={open_before} 평가={evaluated} → publish 완료")
    store.close()
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("ai_forward_eval", main))
