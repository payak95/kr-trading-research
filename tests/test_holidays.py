# 거래일/장시간 게이트 테스트 — XKRX 휴장 판정 + 정규장 시간 경계 (고정 날짜, 결정적)
"""실행: python tests/test_holidays.py"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.core.holidays import KST, is_market_open, is_trading_day, next_trading_day


def main() -> int:
    # 거래일: 목(세션) / 토(주말) / 현충일(공휴일)
    assert is_trading_day(dt.date(2026, 6, 18)), "2026-06-18 목요일은 거래일"
    assert not is_trading_day(dt.date(2026, 6, 20)), "토요일 휴장"
    assert not is_trading_day(dt.date(2026, 6, 6)), "현충일 휴장"

    # next_trading_day: 금요일 → 다음 거래일은 월요일(주말 스킵)
    assert next_trading_day(dt.date(2026, 6, 19)) == dt.date(2026, 6, 22), "금요일 다음 거래일=월요일"
    # 평일 한가운데 — 다음날이 거래일이면 바로 다음날
    assert next_trading_day(dt.date(2026, 6, 17)) == dt.date(2026, 6, 18), "수요일 다음 거래일=목요일"
    # 평일 연휴(추석 목·금)가 주말과 이어지는 경우 — 수요일 다음은 목·금(추석)·토·일을 모두 건너뛰고 월요일
    assert next_trading_day(dt.date(2026, 9, 23)) == dt.date(2026, 9, 28), "추석 연휴+주말 연속 스킵 → 다음 월요일"
    # datetime 입력도 date 로 정규화돼 동작
    assert next_trading_day(dt.datetime(2026, 6, 19, 23, 50, tzinfo=KST)) == dt.date(2026, 6, 22), "datetime 입력도 date 기준"

    # 장시간: 거래일 10:00 열림 / 08:00·16:00 닫힘 / 휴장일 10:00 닫힘
    def at(y, mo, d, h, mi):
        return dt.datetime(y, mo, d, h, mi, tzinfo=KST)

    assert is_market_open(at(2026, 6, 18, 10, 0)), "거래일 10:00 개장"
    assert not is_market_open(at(2026, 6, 18, 8, 0)), "08:00 개장 전"
    assert not is_market_open(at(2026, 6, 18, 16, 0)), "16:00 마감 후"
    assert not is_market_open(at(2026, 6, 20, 10, 0)), "휴장일은 시간 무관 닫힘"
    assert is_market_open(at(2026, 6, 18, 15, 30)), "15:30 경계 포함"

    print("✅ test_holidays: 거래일·장시간 게이트 통과 (XKRX 기반)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
