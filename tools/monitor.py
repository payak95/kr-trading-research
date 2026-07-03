# 봇 헬스 모니터 — Redis 상태 신선도·신호 누적 점검 → 텔레그램(문제 전이 + 일일 하트비트). cron, KIS 컨텍스트.
"""모든 bot:*:status:current 의 신선도(ts age)와 bot:*:tracking:summary 의 신호 수를 점검.
무인 관찰 중 봇 다운·cron 실패를 조용히 놓치지 않게 한다. 상태 끊김/복구는 전이에서만 알림
(monitor:state 로 de-noise → 도배 방지), --heartbeat 면 정상이어도 요약 1회.
텔레그램 토큰 보유한 KIS 봇 컨텍스트에서 실행. 실행: python tools/monitor.py [--heartbeat]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.config import load_config
from kr_research.bot.notify import Notifier

STALE_S = 300            # status:current 가 이보다 오래되면 봇 다운 판정(봇은 매 루프 publish → 정상이면 수초)
K_STATE = "monitor:state"
_LABEL = {"kis": "한투", "toss": "토스"}


def assess(now, statuses, summaries, stale_s=STALE_S):
    """테넌트별 생존(신선도)·신호수 판정. statuses:{tenant:status|None}, summaries:{tenant:summary|None}.
    반환 {tenant: {alive, age, signals}}. 순수 함수(테스트 대상)."""
    out = {}
    for t, st in statuses.items():
        ts = (st or {}).get("ts")
        age = (now - ts) if isinstance(ts, (int, float)) else None
        alive = age is not None and age <= stale_s
        signals = ((summaries.get(t) or {}).get("all") or {}).get("signals", 0)
        out[t] = {"alive": alive, "age": age, "signals": signals}
    return out


def _label(t):
    return _LABEL.get(t, t)


def _json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def main() -> int:
    heartbeat = "--heartbeat" in sys.argv
    cfg = load_config()
    notify = Notifier(cfg)
    if not cfg.redis_url:
        print("[monitor] REDIS_URL 없음 → 점검 불가")
        return 0
    import redis
    r = redis.from_url(cfg.redis_url, decode_responses=True)

    tenants = sorted({k.split(":")[1] for k in r.keys("bot:*:status:current") if k.count(":") >= 3})
    statuses = {t: _json(r.get(f"bot:{t}:status:current")) for t in tenants}
    summaries = {t: _json(r.get(f"bot:{t}:tracking:summary")) for t in tenants}
    rep = assess(time.time(), statuses, summaries)

    down = sorted(t for t, v in rep.items() if not v["alive"])
    prev = set((_json(r.get(K_STATE)) or {}).get("down", []))
    newly = [t for t in down if t not in prev]
    recovered = sorted(prev - set(down))
    r.set(K_STATE, json.dumps({"down": down}))

    if newly:
        notify.send("⚠️ 봇 상태 끊김 — " + ", ".join(_label(t) for t in newly)
                    + " (status 5분+ 미갱신). 컨테이너/콘솔 확인 필요.")
    if recovered:
        notify.send("✅ 봇 복구 — " + ", ".join(_label(t) for t in recovered))
    if heartbeat:
        lines = [f"{_label(t)}: {'정상' if v['alive'] else '끊김'} · 신호 {v['signals']}건"
                 for t, v in sorted(rep.items())]
        notify.send("📋 일일 점검\n" + ("\n".join(lines) if lines else "등록 봇 없음"))

    print(f"[monitor] tenants={tenants} down={down} newly={newly} recovered={recovered} heartbeat={heartbeat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
