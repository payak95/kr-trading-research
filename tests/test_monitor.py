# 헬스 모니터 순수 로직 — assess(신선도·신호수 판정)·assess_crons(크론 심장박동 staleness) (네트워크 없음)
"""실행: python tests/test_monitor.py"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from monitor import assess, assess_crons

_KST = timezone(timedelta(hours=9))


def main() -> int:
    now = 1000.0
    statuses = {
        "kis": {"ts": now - 5},          # 방금 → 정상
        "toss": {"ts": now - 600},       # 10분 전 → 끊김(>300s)
        "x": {},                         # ts 없음 → 끊김
    }
    summaries = {
        "kis": {"all": {"signals": 12}},
        "toss": {"all": {"signals": 3}},
        # x: 요약 없음 → 0
    }
    rep = assess(now, statuses, summaries, stale_s=300)

    assert rep["kis"]["alive"] is True and rep["kis"]["signals"] == 12, rep["kis"]
    assert rep["toss"]["alive"] is False and rep["toss"]["signals"] == 3, rep["toss"]
    assert rep["x"]["alive"] is False and rep["x"]["age"] is None and rep["x"]["signals"] == 0, rep["x"]

    # 경계: 정확히 임계값이면 살아있음(<=)
    assert assess(now, {"a": {"ts": now - 300}}, {})["a"]["alive"] is True, "경계 == stale_s → alive"
    assert assess(now, {"a": {"ts": now - 301}}, {})["a"]["alive"] is False, "임계 초과 → down"

    # ── assess_crons(로드맵 §C) — 주기형(max_age)·일1회형(daily_after)·거래일 전용·기록 없음 ──
    now_dt = datetime(2026, 7, 7, 18, 30, tzinfo=_KST)  # 화요일(거래일) 저녁 — 모든 daily_after 지난 시각
    ts = now_dt.timestamp()
    checks = {
        "fast_ok":      {"max_age": 1800},
        "fast_stale":   {"max_age": 1800},
        "fast_missing": {"max_age": 1800},
        "daily_ok":     {"daily_after": "16:40"},
        "daily_old":    {"daily_after": "16:40"},                       # 어제 성공이 마지막
        "daily_miss":   {"daily_after": "16:40"},                       # 기록 자체가 없음
        "tday_only":    {"daily_after": "17:30", "trading_day": True},
        "not_yet":      {"daily_after": "23:00"},                       # 아직 기준 시각 전 → 검사 안 함
    }
    hbs = {
        "fast_ok": ts - 600, "fast_stale": ts - 3600, "fast_missing": None,
        "daily_ok": ts - 3600,                       # 오늘 낮 성공
        "daily_old": ts - 86400,                     # 어제 같은 시각
        "daily_miss": None,
        "tday_only": None,
        "not_yet": None,
    }
    stale = assess_crons(now_dt, hbs, checks, trading_day=True)
    assert set(stale) == {"fast_stale", "fast_missing", "daily_old", "daily_miss", "tday_only"}, stale
    assert stale["fast_missing"] == "기록 없음" and "60분" in stale["fast_stale"], stale
    assert "2026-07-06" in stale["daily_old"] and stale["daily_miss"] == "오늘 기록 없음", stale

    # 휴장일(trading_day=False)엔 거래일 전용(tday_only)만 검사 제외 — 매일 도는 daily_* 는 계속 검사
    stale_holiday = assess_crons(now_dt, hbs, checks, trading_day=False)
    assert "tday_only" not in stale_holiday and "daily_miss" in stale_holiday, stale_holiday

    # 기준 시각 직전이면 일1회형은 아직 판정하지 않음(새벽·오전 오탐 방지)
    morning = datetime(2026, 7, 7, 9, 0, tzinfo=_KST)
    stale_morning = assess_crons(morning, hbs, {"daily_miss": {"daily_after": "16:40"}}, trading_day=True)
    assert stale_morning == {}, stale_morning

    print("✅ test_monitor: assess(신선도 경계·ts없음·신호수)·assess_crons(주기형/일1회형/거래일전용/"
          "기준시각 전 미판정/기록없음) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
