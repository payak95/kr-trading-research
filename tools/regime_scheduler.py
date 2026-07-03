# 흐름/Flow 변동성 신호 → 전역 레짐(market:regime) 기록 — 경량 스케줄러(cron). 봇은 follow_regime 로 추종.
"""Flow Upstash KV(`market:vol_forecast`)를 읽어 tone→regime 매핑 후 우리 Redis `market:regime` 기록.

fail-safe(control-plane-stage2-design §4-3): Flow stale/missing/unreachable → **덮어쓰지 않음**
(마지막 값 유지, 봇이 자체 max-age 로 neutral 복귀). tone 로직은 Flow outlook.vol_action_summary 미러
(코드 import 금지·격리). cron 으로 주기 실행.
env: FLOW_KV_REST_API_URL · FLOW_KV_REST_API_TOKEN(읽기전용 권장) · REDIS_URL.
"""
import json
import os
import time
from datetime import datetime, timezone

import redis
import requests

STALE_AFTER_S = 30 * 3600  # Flow generated 가 이보다 오래되면 stale → 미갱신(Flow TTL≈30h)
TONE_TO_REGIME = {"caution": "defensive", "calm": "aggressive", "normal": "neutral"}


def tone_from_items(items) -> str | None:
    """Flow `dashboard/data/outlook.vol_action_summary` 미러 — 검증된(skill_pos) 종목 기반 tone.
    검증 종목 없으면 None(행동 권고 안 함). caution/calm/normal."""
    verified = [it for it in (items or []) if isinstance(it, dict) and it.get("skill_pos")]
    if not verified:
        return None

    def _ph(it):
        v = it.get("prob_high")
        return v if isinstance(v, (int, float)) else None

    high = [it for it in verified
            if it.get("direction") == "up" or (_ph(it) is not None and _ph(it) >= 60)]
    low = [it for it in verified
           if it.get("direction") == "down" or (_ph(it) is not None and _ph(it) <= 40)]
    if high:
        return "caution"
    if low and len(low) == len(verified):
        return "calm"
    return "normal"


def _flow_vol_forecast() -> dict | None:
    """Flow Upstash KV REST 로 market:vol_forecast GET. 미설정·실패는 예외."""
    url = os.environ.get("FLOW_KV_REST_API_URL", "").rstrip("/")
    tok = os.environ.get("FLOW_KV_REST_API_TOKEN", "")
    if not (url and tok):
        raise RuntimeError("FLOW_KV_REST_API_URL/TOKEN 미설정")
    r = requests.post(url, headers={"Authorization": f"Bearer {tok}"},
                      json=["GET", "market:vol_forecast"], timeout=10)
    r.raise_for_status()
    res = r.json().get("result")
    return json.loads(res) if res else None


def main() -> int:
    try:
        payload = _flow_vol_forecast()
    except Exception as e:
        print(f"[regime] Flow 읽기 실패 → 미갱신(마지막 값 유지): {e}")
        return 0
    if not payload:
        print("[regime] vol_forecast 없음(만료/미생성) → 미갱신")
        return 0
    gen = payload.get("generated", "")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds()
    except (ValueError, TypeError):
        age = None
    if age is None or age > STALE_AFTER_S:
        print(f"[regime] Flow 데이터 stale(age={age}) → 미갱신")
        return 0

    tone = tone_from_items(payload.get("items"))
    regime = TONE_TO_REGIME.get(tone, "neutral")  # None/불명 → neutral(보수 fail-safe)
    rec = {"regime": regime, "tone": tone, "source_asof": gen,
           "computed_at": time.time(), "fresh": True}
    redis.from_url(os.environ["REDIS_URL"], decode_responses=True).set(
        "market:regime", json.dumps(rec, ensure_ascii=False))
    print(f"[regime] tone={tone} → regime={regime} (asof={gen}) 기록")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
