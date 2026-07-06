# 조건검색 새 후보 알림(야간 크론) — 저장 전략 전부를 유니버스와 대조해 신규 후보를 텔레그램으로
"""저장 전략(`bot:strategies`, 콘솔 CRUD)을 매일 유니버스 전체(`bot:screen:universe`, 야간 크론이
워밍한 일봉 캐시 재사용 — KIS 추가 호출 없음)와 대조해 스크리닝하고, 직전 실행 대비 새로 나타난
종목만 통지한다(전이만 알림 → 도배 방지, `tools/monitor.py`와 동일 de-noise 패턴).
무주문·연구 전용. 실행: python tools/screen_notify.py 설계: docs/planning/BACKLOG.md B4.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.bot.notify import Notifier
from kr_research.core.config import load_config
from tools.backtest_worker import DEFAULT_DAYS, UNIVERSE_KEY, _cache_only_fetch, screen_spec

K_STRATEGIES = "bot:strategies"  # kr-trading-bot(core/control_bus.py)의 상수와 반드시 일치(전역, 콘솔 CRUD)
SEEN_KEY = "bot:screen:notify:seen"  # String(JSON) — {전략명: [code,...]} 직전 실행 후보 스냅샷
MAX_CODES_SHOWN = 15  # 메시지 1건에 보여줄 종목 수 상한(초과분은 "…N개 더")


def new_candidates(prev: dict, current: dict) -> dict[str, list[str]]:
    """전략별 신규 후보(직전 스냅샷에 없던 종목)만 — 비어있으면 결과에서 제외. 순수 함수(테스트 대상)."""
    out = {}
    for name, codes in current.items():
        diff = sorted(set(codes) - set(prev.get(name, [])))
        if diff:
            out[name] = diff
    return out


def _format(diff: dict[str, list[str]]) -> str:
    lines = ["🔎 조건검색 새 후보"]
    for name, codes in diff.items():
        shown = codes[:MAX_CODES_SHOWN]
        tail = f" …{len(codes) - MAX_CODES_SHOWN}개 더" if len(codes) > MAX_CODES_SHOWN else ""
        lines.append(f"· {name}: {', '.join(shown)}{tail} ({len(codes)}건)")
    return "\n".join(lines)


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 조건검색 알림 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    strategies = r.hgetall(K_STRATEGIES)
    if not strategies:
        print("[screen_notify] 저장 전략 없음 — 종료")
        return 0
    codes = sorted(r.smembers(UNIVERSE_KEY))
    if not codes:
        print("[screen_notify] 유니버스 비어있음(야간 워밍 전) — 종료")
        return 0

    fetch = _cache_only_fetch(r)
    bars_by_code = {c: fetch(c, DEFAULT_DAYS) for c in codes}

    current: dict[str, list[str]] = {}
    for name, spec_json in strategies.items():
        try:
            spec = json.loads(spec_json)
            result = screen_spec(bars_by_code, spec, params=None)
        except Exception as e:
            print(f"[screen_notify] '{name}' 스크리닝 실패 → skip: {e}")
            continue
        current[name] = [c["code"] for c in result["candidates"]]

    prev = json.loads(r.get(SEEN_KEY) or "{}")
    diff = new_candidates(prev, current)
    r.set(SEEN_KEY, json.dumps(current, ensure_ascii=False))

    if diff:
        Notifier(load_config()).send(_format(diff))
    print(f"[screen_notify] 전략 {len(strategies)} · 유니버스 {len(codes)} · 신규후보 전략 {len(diff)} → 발행")
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("screen_notify", main))
