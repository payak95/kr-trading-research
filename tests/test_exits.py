# 손절/익절 트리거 판정 공용 함수 테스트 (백테스트·전진검증 공용, 네트워크 없음)
"""실행: python tests/test_exits.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.exits import check_stop_take_profit


def main() -> int:
    # 손절: low 가 손절가(진입가*0.95) 이하로 터치
    r = check_stop_take_profit(100.0, low=94.0, high=101.0, stop_loss_pct=0.05, take_profit_pct=None)
    assert r == (95.0, "stop_loss"), r

    # 익절: high 가 익절가(진입가*1.05) 이상으로 터치
    r2 = check_stop_take_profit(100.0, low=99.0, high=106.0, stop_loss_pct=None, take_profit_pct=0.05)
    assert r2 == (105.0, "take_profit"), r2

    # 둘 다 설정됐고 둘 다 그 봉에서 만족 → 손절 우선(백테스트 관례)
    r3 = check_stop_take_profit(100.0, low=90.0, high=110.0, stop_loss_pct=0.05, take_profit_pct=0.05)
    assert r3 == (95.0, "stop_loss"), r3

    # 미트리거(범위 안)
    assert check_stop_take_profit(100.0, low=96.0, high=104.0, stop_loss_pct=0.05, take_profit_pct=0.05) is None

    # 둘 다 None(미설정) → 항상 None
    assert check_stop_take_profit(100.0, low=0.0, high=1000.0, stop_loss_pct=None, take_profit_pct=None) is None

    # 경계값: low==stop_px, high==tp_px 는 트리거(<=, >=)
    assert check_stop_take_profit(100.0, low=95.0, high=100.0, stop_loss_pct=0.05, take_profit_pct=None) == (95.0, "stop_loss")
    assert check_stop_take_profit(100.0, low=100.0, high=105.0, stop_loss_pct=None, take_profit_pct=0.05) == (105.0, "take_profit")

    print("✅ test_exits: check_stop_take_profit(손절·익절·우선순위·경계·미설정) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
