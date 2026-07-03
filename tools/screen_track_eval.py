# 조건검색 전진검증 스케줄러(야간 크론) — 추적요청 드레인→병합→네이버 일봉 평가(D+N + 청산-추적)→전략별 집계 발행
"""콘솔이 `bot:screen:track:requests` 에 적재한 추적요청을 정규 저장소에 병합하고, 미평가 신호의
D+N forward 수익(+코스피 대비 초과수익)을 네이버 일봉으로 평가해 전략별 집계를 `bot:screen:track:summary`
에 발행한다. 무주문·연구 전용. 시세=네이버(무한도, KIS 분리). 실행: python tools/screen_track_eval.py
설계: 라이브 forward_eval 과 동형이되 Redis 저장·전략 묶음·네이버 시세. cron: RUNTIME.md.

**청산-추적(파이프라인 자동화 Phase 2)**: D+N 평가 뒤 이어서 `apply_exit_tracking`으로 그 전략의 실제
청산 규칙(손절·익절·spec exit 트리)이 언제 발동하는지 평가 — 콘솔 `bot:strategies`(전역 저장 전략) 스냅샷을
이름으로 조회해 spec 을 얻는다(이름 변경·삭제 시 안전 skip). 같은 cron·같은 드레인 사이클(새 cron 안 만듦).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.bot.notify import Notifier
from kr_research.core.config import load_config
from tools.backtest_worker import FEE_RATE, SLIPPAGE, TAX_RATE
from tools.naver_ohlcv import daily_ohlcv
from tools.screen_track import (TRACK_REQUESTS_KEY, TRACK_SIGNALS_KEY, TRACK_SUMMARY_KEY,
                                TRACK_VALIDATED_PREV_KEY, apply_exit_tracking, apply_returns,
                                merge_signals, newly_validated, open_codes, open_codes_exit_tracking,
                                summary_by_strategy)

K_STRATEGIES = "bot:strategies"  # kr-trading-bot(core/control_bus.py)의 상수와 반드시 일치(전역, 콘솔 CRUD)
from kr_research.trading.tracking import BENCHMARK_CODE, MAX_HOLD_DAYS

EVAL_COUNT = 60  # 네이버 일봉 조회 봉 수(D+N, 미평가 신호 D+20 + 버퍼 충분)
EXIT_EVAL_COUNT = 200  # 청산-추적용 — 지표 워밍업(콘솔 프리셋 최대 s120)+최대 보유 60거래일+버퍼(네이버 무한도라 비용 부담 없음)


def _slim(code: str) -> list[dict]:
    """code 일봉 → {date,close} 시간순(D+N 평가용, close 만). 실패는 [](해당 종목 skip)."""
    try:
        return [{"date": b["date"], "close": b["close"]} for b in daily_ohlcv(code, count=EVAL_COUNT)]
    except Exception as e:
        print(f"[screen_track_eval] {code} 일봉 실패 → skip: {e}", flush=True)
        return []


def _full(code: str) -> list[dict]:
    """code 일봉 → 원본 그대로(청산-추적용, 저가/고가 필요 — `_slim`은 종가만 남겨 부적합). 실패는 [](skip)."""
    try:
        return daily_ohlcv(code, count=EXIT_EVAL_COUNT)
    except Exception as e:
        print(f"[screen_track_eval] {code} 일봉(전체) 실패 → skip: {e}", flush=True)
        return []


def _load_strategies(r) -> dict:
    """`bot:strategies`(전역 저장 전략, 콘솔 CRUD) 스냅샷 → {이름: spec}. 파싱 실패 항목은 skip(안전 무시)."""
    out = {}
    for name, spec_json in (r.hgetall(K_STRATEGIES) or {}).items():
        try:
            out[name] = json.loads(spec_json)
        except (ValueError, TypeError):
            continue
    return out


def _drain_requests(r) -> list[dict]:
    """요청 큐 전량 LPOP → JSON 파싱(손상 skip). 콘솔 RPUSH 분."""
    reqs = []
    while True:
        raw = r.lpop(TRACK_REQUESTS_KEY)
        if raw is None:
            break
        try:
            reqs.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return reqs


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 전진검증 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    requests = _drain_requests(r)
    try:
        existing = json.loads(r.get(TRACK_SIGNALS_KEY) or "[]")
    except (ValueError, TypeError):
        existing = []
    signals = merge_signals(existing, requests)
    if not signals:
        print("[screen_track_eval] 추적 신호 없음 — 종료")
        return 0

    codes = open_codes(signals)
    bars_by_code = {c: _slim(c) for c in codes}
    bench_bars = _slim(BENCHMARK_CODE)
    updated = apply_returns(signals, bars_by_code, bench_bars)

    strategies = _load_strategies(r)
    exit_codes = open_codes_exit_tracking(signals)
    full_bars_by_code = {c: _full(c) for c in exit_codes}
    exit_updated = apply_exit_tracking(signals, full_bars_by_code, strategies,
                                       fee_rate=FEE_RATE, tax_rate=TAX_RATE, slippage=SLIPPAGE,
                                       max_hold_days=MAX_HOLD_DAYS)

    r.set(TRACK_SIGNALS_KEY, json.dumps(signals, ensure_ascii=False))
    summary = summary_by_strategy(signals)
    r.set(TRACK_SUMMARY_KEY, json.dumps(summary, ensure_ascii=False))

    prev_validated = json.loads(r.get(TRACK_VALIDATED_PREV_KEY) or "[]")
    newly = newly_validated(prev_validated, summary)
    now_validated = sorted(name for name, agg in summary["by_strategy"].items() if agg.get("validated"))
    r.set(TRACK_VALIDATED_PREV_KEY, json.dumps(now_validated, ensure_ascii=False))
    if newly:
        Notifier(load_config()).send("✅ 전진검증 통과 — " + ", ".join(newly) + " (조건검색에서 실전 전환 가능)")

    print(f"[screen_track_eval] 요청 {len(requests)} · 신호 {len(signals)} · 평가갱신 {updated} · "
          f"청산-추적평가 {exit_updated}(전략스냅샷 {len(strategies)}) · "
          f"전략 {len(summary['by_strategy'])} · 신규검증통과 {len(newly)} → 발행", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
