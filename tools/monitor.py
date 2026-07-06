# 봇 헬스 모니터 — Redis 상태 신선도·신호 누적·크론 심장박동 점검 → 텔레그램(문제 전이 + 일일 하트비트)
"""모든 bot:*:status:current 의 신선도(ts age)와 bot:*:tracking:summary 의 신호 수, 그리고 각 크론의
심장박동(cron:heartbeat:*, kr_research.core.heartbeat — 로드맵 §C)을 점검한다.
무인 관찰 중 봇 다운·cron 실패를 조용히 놓치지 않게 한다. 상태 끊김/복구는 전이에서만 알림
(monitor:state 로 de-noise → 도배 방지), --heartbeat 면 정상이어도 요약 1회.
텔레그램 토큰 보유한 KIS 봇 컨텍스트에서 실행. 실행: python tools/monitor.py [--heartbeat]
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.config import load_config
from kr_research.core.heartbeat import K_PREFIX as HB_PREFIX
from kr_research.core.holidays import is_trading_day
from kr_research.bot.notify import Notifier

STALE_S = 300            # status:current 가 이보다 오래되면 봇 다운 판정(봇은 매 루프 publish → 정상이면 수초)
K_STATE = "monitor:state"
_LABEL = {"kis": "한투", "toss": "토스"}
_KST = timezone(timedelta(hours=9))

# 크론 심장박동 기대치(로드맵 §C) — 크론탭(docs/ops/RUNTIME.md)과 함께 관리: 크론을 추가/제거하면 여기도.
# 두 형태: max_age(상시 주기형, 초) / daily_after(일1회형 — 이 KST 시각을 지나서도 오늘 기록이 없으면
# 이상. 크론 예정 시각 + 여유 30~40분). trading_day=True 면 거래일에만 검사(주말·휴장일 미실행이 정상).
# 데몬 워커(backtest/pipeline)는 크론이 아니라 여기 대상 아님 — --restart unless-stopped + 콘솔 잡 지연으로 발견.
CRON_CHECKS = {
    "ai_shadow_scheduler": {"max_age": 30 * 60},        # */5분
    "ai_combo_scheduler":  {"max_age": 30 * 60},        # */5분
    "ai_shadow_notify":    {"max_age": 30 * 60},        # */5분
    "regime_scheduler":    {"max_age": 2 * 3600 + 600}, # */30분
    "ai_forward_eval":     {"daily_after": "16:40"},                        # 07:10 UTC 매일
    "backup":              {"daily_after": "21:10"},                        # 11:30 UTC 매일(호스트 backup.sh)
    "ai_daily_digest":     {"daily_after": "16:50", "trading_day": True},   # 07:20 UTC 평일
    "screen_universe":     {"daily_after": "17:30", "trading_day": True},   # 08:00 UTC 평일
    "screen_track_eval":   {"daily_after": "17:30", "trading_day": True},   # 08:00 UTC 평일
    "ai_universe_scan":    {"daily_after": "17:40", "trading_day": True},   # 08:10 UTC 평일
    "flow_universe":       {"daily_after": "17:40", "trading_day": True},   # 08:10 UTC 평일
    "screen_notify":       {"daily_after": "17:40", "trading_day": True},   # 08:10 UTC 평일
    "pipeline_schedule":   {"daily_after": "18:00", "trading_day": True},   # 08:30 UTC 평일
}


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


def assess_crons(now: datetime, heartbeats: dict, checks: dict = CRON_CHECKS,
                 trading_day: bool | None = None) -> dict:
    """크론별 심장박동 staleness 판정 — 이상인 것만 {이름: 사유} 로 반환. 순수 함수(테스트 대상).
    now: KST aware datetime. heartbeats: {이름: epoch초|None}. trading_day 미지정 시 캘린더로 판정."""
    if trading_day is None:
        trading_day = is_trading_day(now)
    stale = {}
    for name, chk in checks.items():
        hb = heartbeats.get(name)
        if "max_age" in chk:
            if hb is None:
                stale[name] = "기록 없음"
            elif now.timestamp() - float(hb) > chk["max_age"]:
                stale[name] = f"{int((now.timestamp() - float(hb)) / 60)}분 전이 마지막"
            continue
        # daily_after 형 — 기준 시각 전이거나 (거래일 전용인데) 휴장일이면 검사 자체를 안 함
        if chk.get("trading_day") and not trading_day:
            continue
        hh, mm = chk["daily_after"].split(":")
        if (now.hour, now.minute) < (int(hh), int(mm)):
            continue
        hb_date = datetime.fromtimestamp(float(hb), tz=_KST).date() if hb is not None else None
        if hb_date != now.date():
            stale[name] = "오늘 기록 없음" if hb is None or hb_date is None else f"마지막 성공 {hb_date}"
    return stale


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
    state = _json(r.get(K_STATE)) or {}
    prev = set(state.get("down", []))
    newly = [t for t in down if t not in prev]
    recovered = sorted(prev - set(down))

    # 크론 심장박동(로드맵 §C) — cron:heartbeat:* 를 기대 주기와 대조, 봇 다운과 동일한 전이 de-noise.
    hbs = {name: (lambda raw: float(raw) if raw else None)(r.get(HB_PREFIX + name)) for name in CRON_CHECKS}
    cron_stale = assess_crons(datetime.now(_KST), hbs)
    prev_cron = set(state.get("cron_stale", []))
    cron_newly = sorted(n for n in cron_stale if n not in prev_cron)
    cron_recovered = sorted(prev_cron - set(cron_stale))
    r.set(K_STATE, json.dumps({"down": down, "cron_stale": sorted(cron_stale)}))

    if newly:
        notify.send("⚠️ 봇 상태 끊김 — " + ", ".join(_label(t) for t in newly)
                    + " (status 5분+ 미갱신). 컨테이너/콘솔 확인 필요.")
    if recovered:
        notify.send("✅ 봇 복구 — " + ", ".join(_label(t) for t in recovered))
    if cron_newly:
        notify.send("⚠️ 크론 심장박동 이상 — "
                    + " · ".join(f"{n}({cron_stale[n]})" for n in cron_newly)
                    + "\nVPS 크론탭/로그(/var/log/kr-*.log) 확인 필요.")
    if cron_recovered:
        notify.send("✅ 크론 복구 — " + ", ".join(cron_recovered))
    if heartbeat:
        lines = [f"{_label(t)}: {'정상' if v['alive'] else '끊김'} · 신호 {v['signals']}건"
                 for t, v in sorted(rep.items())]
        lines.append(f"크론 심장박동: 정상 {len(CRON_CHECKS) - len(cron_stale)}/{len(CRON_CHECKS)}"
                     + (" — 이상: " + ", ".join(sorted(cron_stale)) if cron_stale else ""))
        notify.send("📋 일일 점검\n" + "\n".join(lines))

    print(f"[monitor] tenants={tenants} down={down} newly={newly} recovered={recovered} "
          f"cron_stale={sorted(cron_stale)} heartbeat={heartbeat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
