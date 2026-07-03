# 조건검색 새 후보 알림 순수 로직 — new_candidates(직전 스냅샷 대비 신규 후보 diff) (네트워크 없음)
"""실행: python tests/test_screen_notify.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.screen_notify import new_candidates


def main() -> int:
    # 변화 없음 → 빈 결과
    assert new_candidates({"a": ["005930"]}, {"a": ["005930"]}) == {}

    # 신규 전략 등장(직전 스냅샷에 없던 전략) → 전량 신규
    assert new_candidates({}, {"b": ["000660"]}) == {"b": ["000660"]}

    # 기존 전략에 종목 추가 → 추가분만
    assert new_candidates({"a": ["005930"]}, {"a": ["005930", "000660"]}) == {"a": ["000660"]}

    # 종목 제거(더 이상 후보 아님)는 알림 대상 아님(diff 는 신규만)
    assert new_candidates({"a": ["005930", "000660"]}, {"a": ["005930"]}) == {}

    # 전략이 current 에서 사라짐(예: 저장 전략 삭제) → 결과에 영향 없음(current 기준 순회)
    assert new_candidates({"a": ["005930"], "b": ["000660"]}, {"a": ["005930"]}) == {}

    print("✅ test_screen_notify: new_candidates(무변화·신규전략·종목추가·종목제거·전략삭제) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
