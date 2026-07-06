# 크론 심장박동 — 성공 종료만 기록·실패 미기록·no-op(REDIS_URL 없음)·TTL 검증 (fakeredis, 네트워크 없음)
"""실행: python tests/test_heartbeat.py"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fakeredis import FakeRedis

from kr_research.core import heartbeat as hb


def main() -> int:
    fr = FakeRedis(decode_responses=True)

    with patch.dict(os.environ, {"REDIS_URL": "redis://fake"}), \
         patch("redis.from_url", return_value=fr):
        # 성공(0) → 기록 + TTL
        assert hb.run_with_heartbeat("job_a", lambda: 0) == 0
        assert fr.exists(hb.K_PREFIX + "job_a") and fr.ttl(hb.K_PREFIX + "job_a") > 0

        # 성공(None 반환 main) → 기록
        assert hb.run_with_heartbeat("job_none", lambda: None) is None
        assert fr.exists(hb.K_PREFIX + "job_none")

        # 실패(rc 1) → 기록 안 함(실패를 심장박동으로 치면 감시 무의미)
        assert hb.run_with_heartbeat("job_fail", lambda: 1) == 1
        assert not fr.exists(hb.K_PREFIX + "job_fail")

        # main 이 예외로 죽으면 기록 없이 그대로 전파
        try:
            hb.run_with_heartbeat("job_boom", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            raise AssertionError("예외가 전파돼야 함")
        except RuntimeError:
            pass
        assert not fr.exists(hb.K_PREFIX + "job_boom")

    # REDIS_URL 없으면 조용히 no-op(로컬 실험) — 예외도 기록도 없음
    os.environ.pop("REDIS_URL", None)
    assert hb.run_with_heartbeat("job_local", lambda: 0) == 0
    assert not fr.exists(hb.K_PREFIX + "job_local")

    # Redis 접속 실패도 크론 본연의 일을 못 막음(예외 삼킴)
    with patch.dict(os.environ, {"REDIS_URL": "redis://fake"}), \
         patch("redis.from_url", side_effect=ConnectionError("down")):
        assert hb.run_with_heartbeat("job_redis_down", lambda: 0) == 0

    print("✅ test_heartbeat: 성공만 기록(0/None)·실패/예외 미기록·TTL·REDIS_URL 없음 no-op·접속실패 무해 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
