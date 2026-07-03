# 레짐 스케줄러 테스트 — tone 매핑(Flow vol_action_summary 미러) + regime 매핑 (네트워크 없음)
"""실행: python tests/test_regime_scheduler.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from regime_scheduler import TONE_TO_REGIME, tone_from_items


def main() -> int:
    # 검증 종목 없음 → None(행동 권고 안 함)
    assert tone_from_items([]) is None, "빈 → None"
    assert tone_from_items([{"prob_high": 80}]) is None, "skill_pos 없으면 검증 안 됨 → None"

    # caution: 고변동(up 또는 prob_high>=60) 하나라도 있으면
    assert tone_from_items([{"skill_pos": True, "direction": "up"}]) == "caution", "up → caution"
    assert tone_from_items([{"skill_pos": True, "prob_high": 70}]) == "caution", "prob_high>=60 → caution"

    # calm: 검증 종목 전부 저변동(down 또는 prob_high<=40)
    assert tone_from_items([{"skill_pos": True, "prob_high": 30},
                            {"skill_pos": True, "direction": "down"}]) == "calm", "전부 저변동 → calm"

    # normal: 그 외(섞임)
    assert tone_from_items([{"skill_pos": True, "prob_high": 50}]) == "normal", "중간 → normal"

    # regime 매핑 + 보수 폴백
    assert TONE_TO_REGIME["caution"] == "defensive"
    assert TONE_TO_REGIME["calm"] == "aggressive"
    assert TONE_TO_REGIME["normal"] == "neutral"
    assert TONE_TO_REGIME.get(None, "neutral") == "neutral", "None tone → neutral(보수)"

    print("✅ test_regime_scheduler: tone 미러(caution/calm/normal/None)·regime 매핑·보수폴백 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
