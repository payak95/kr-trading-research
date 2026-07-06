# 크론 심장박동 — 크론 스크립트가 성공 종료할 때 Redis 에 타임스탬프를 남기고 monitor.py 가 staleness 감시
"""크론 잡이 조용히 죽으면(이미지 깨짐·예외·크론탭 유실) 다음날 "결과가 안 쌓였네"로만 발견되는 문제
방지(개선 로드맵 §C). 각 tools/*.py 는 __main__ 에서 run_with_heartbeat("이름", main) 으로 감싸고,
tools/monitor.py 의 CRON_CHECKS 가 기대 주기 대비 staleness 를 판정해 텔레그램으로 알린다.

원칙:
  - 성공(rc 0/None)만 기록 — 실패한 실행을 심장박동으로 치면 감시가 무의미해진다.
  - 기록 실패는 크론 본연의 일을 절대 방해하지 않는다(모든 예외 삼킴, REDIS_URL 없으면 no-op).
  - "일을 안 한 성공"(장 아님·거래일 아님 조기 종료 등)도 기록한다 — 여기서 감시하는 건
    "크론이 돌고 스크립트가 정상 종료하는가"이지 "실질 작업을 했는가"가 아니다.
"""
import os
import time

K_PREFIX = "cron:heartbeat:"   # + 크론 이름 → epoch 초(성공 종료 시각). monitor.py 와 리터럴 공유.
TTL_S = 14 * 86400             # 크론탭에서 제거된 잡의 키가 영원히 남지 않게(2주 뒤 자연 소멸)


def record(name: str) -> None:
    """성공 심장박동 1회 기록 — REDIS_URL 미설정(로컬 실험)이면 조용히 no-op."""
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        return
    try:
        import redis
        r = redis.from_url(redis_url, decode_responses=True)
        r.set(K_PREFIX + name, time.time(), ex=TTL_S)
    except Exception:
        pass  # 감시용 기록이 크론 본연의 일을 못 막게


def run_with_heartbeat(name: str, main_fn):
    """tools/*.py 의 __main__ 래퍼 — main() 이 성공(0/None) 반환일 때만 record. 반환값은 그대로 전달
    (SystemExit 코드 유지). main() 이 예외로 죽으면 기록 없이 그대로 전파(실패는 심장박동 아님)."""
    rc = main_fn()
    if rc in (0, None):
        record(name)
    return rc
