# AI 섀도 판단 중 확신도 높은 매수/매도만 텔레그램 알림(신규만, de-noise) — 무주문·연구 전용
"""AI 섀도 판단(core/ai_store.py, ① 유니버스 스크리닝+② 타겟 종목 관찰 공용 테이블)을 훑어 confidence 가
CONFIDENCE_THRESHOLD 이상인 buy/sell 판단만, 직전 실행 이후 새로 기록된 것만 통지한다(id 기준 — 재확인
방지, tools/screen_notify.py·tools/monitor.py 와 동일 de-noise 패턴). 실행: python tools/ai_shadow_notify.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.bot.notify import Notifier
from kr_research.core.ai_store import AiStore
from kr_research.core.config import load_config

CONFIDENCE_THRESHOLD = 0.7
LAST_ID_KEY = "bot:ai:notify:last_id"  # String(int) — 마지막으로 검토한 judgment id(신뢰도 무관, 전체 기준)
MAX_SHOWN = 15
ACTION_LABEL = {"buy": "매수", "sell": "매도"}


def notable(rows: list[dict], threshold: float = CONFIDENCE_THRESHOLD) -> list[dict]:
    """action 이 buy/sell 이고 confidence>=threshold 인 판단만(순수 함수, 테스트 대상). hold 는 대상 아님."""
    return [r for r in rows if r.get("action") in ("buy", "sell") and (r.get("confidence") or 0) >= threshold]


def _format(rows: list[dict]) -> str:
    lines = ["🤖 AI 섀도 고확신 판단"]
    for r in rows[:MAX_SHOWN]:
        lines.append(f"· {r['code']} {ACTION_LABEL.get(r['action'], r['action'])} "
                      f"(확신도 {r['confidence']:.2f}) — {r.get('reason', '')}")
    if len(rows) > MAX_SHOWN:
        lines.append(f"…{len(rows) - MAX_SHOWN}건 더")
    return "\n".join(lines)


def main() -> int:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — AI 섀도 알림 크론은 Redis 필요")
        return 1
    import redis

    r = redis.from_url(redis_url, decode_responses=True)
    store = AiStore()
    rows = store.get_judgments(None)
    last_id = int(r.get(LAST_ID_KEY) or 0)
    new_rows = [row for row in rows if row["id"] > last_id]
    hits = notable(new_rows)

    if hits:
        Notifier(load_config()).send(_format(hits))
    if rows:
        r.set(LAST_ID_KEY, max(row["id"] for row in rows))
    store.close()
    print(f"[ai_shadow_notify] 신규 판단 {len(new_rows)} · 고확신 {len(hits)} → "
          f"알림 {'발송' if hits else '없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
