# 헬스 모니터 순수 로직 — assess(신선도·신호수 판정) (네트워크 없음)
"""실행: python tests/test_monitor.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from monitor import assess


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

    print("✅ test_monitor: assess(신선도 경계·ts없음·신호수) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
