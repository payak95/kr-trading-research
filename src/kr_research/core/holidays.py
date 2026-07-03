# KR 거래일·장 운영시간 게이트 — 휴장일/장외면 매매 안 함 (fail-safe)
"""거래일은 exchange_calendars XKRX(정확, 공휴일 반영). 미설치 시 평일만 폴백(휴장일 미반영).
정규장 09:00~15:30 KST. 시간 판단이 불확실하면 매매하지 않는 쪽으로.
"""
import datetime as dt
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_OPEN = dt.time(9, 0)
_CLOSE = dt.time(15, 30)
_cal = None  # None=미시도, False=사용불가, 그 외=캘린더


def _calendar():
    global _cal
    if _cal is None:
        try:
            import exchange_calendars as xc
            _cal = xc.get_calendar("XKRX")
        except Exception:
            _cal = False
    return _cal or None


def is_trading_day(d: dt.datetime | dt.date | None = None) -> bool:
    now = d or dt.datetime.now(KST)
    date = now.date() if isinstance(now, dt.datetime) else now
    cal = _calendar()
    if cal is None:
        return date.weekday() < 5  # 폴백: 평일만(휴장일 미반영)
    return bool(cal.is_session(date.strftime("%Y-%m-%d")))


def is_market_open(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(KST)
    if not is_trading_day(now):
        return False
    return _OPEN <= now.time() <= _CLOSE


def next_trading_day(d: dt.datetime | dt.date | None = None, max_days: int = 14) -> dt.date:
    """d(포함 안 함) 다음의 첫 거래일. 파이프라인 stage 재시도 만료(§5, 다음 거래일 전까지 유효) 등에 사용.
    max_days 안에 못 찾으면(캘린더 이상) ValueError — 무한루프 방어."""
    now = d or dt.datetime.now(KST)
    date = now.date() if isinstance(now, dt.datetime) else now
    for i in range(1, max_days + 1):
        cand = date + dt.timedelta(days=i)
        if is_trading_day(cand):
            return cand
    raise ValueError(f"{max_days}일 안에 거래일을 못 찾음(캘린더 확인 필요): {date}")
