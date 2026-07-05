# AI 섀도 고확신 판단 알림 순수 로직 — notable(confidence 임계값 필터) (네트워크 없음)
"""실행: python tests/test_ai_shadow_notify.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.ai_shadow_notify import notable


def main() -> int:
    rows = [
        {"code": "005930", "action": "buy", "confidence": 0.9},   # 고확신 매수 → 대상
        {"code": "000660", "action": "sell", "confidence": 0.75},  # 고확신 매도 → 대상
        {"code": "035420", "action": "buy", "confidence": 0.5},   # 임계값 미달 → 제외
        {"code": "005380", "action": "hold", "confidence": 0.95},  # hold 는 액션 무관 → 제외
        {"code": "003550", "action": "buy", "confidence": None},  # confidence 없음 → 제외
    ]
    hits = notable(rows)
    assert [r["code"] for r in hits] == ["005930", "000660"], hits

    # 임계값 조정
    assert [r["code"] for r in notable(rows, threshold=0.4)] == ["005930", "000660", "035420"]

    print("✅ test_ai_shadow_notify: notable(confidence 임계값·action 필터) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
