# 손절/익절 트리거 판정 — 백테스트·전진검증(청산-추적) 공용 순수 함수
"""`trading/backtest.py::run()`이 봉마다 인라인으로 하던 손절/익절 판정을 추출한 것(동작 동일).
백테스트는 이 함수를 봉마다 호출하고, 전진검증(`trading/tracking.py::evaluate_exit`)은 신호마다
매일 새 봉 1개에 대해 같은 함수를 호출 — 두 경로가 같은 기준으로 손절/익절을 판정하도록 강제 통일한다
(파이프라인 자동화 Phase 2, docs/planning/pipeline-automation-design.md §4.2).
"""


def check_stop_take_profit(entry_price: float, low: float, high: float,
                           stop_loss_pct: float | None, take_profit_pct: float | None) -> tuple[float, str] | None:
    """진입가(평단가) 대비 손절/익절 트리거 여부 — 그 봉의 low/high 로 체크(close 전용보다 현실적).
    손절을 익절보다 먼저 검사(둘 다 만족하면 손절 우선). 반환 (trigger_price, reason) 또는 미트리거 시 None."""
    stop_px = entry_price * (1 - stop_loss_pct) if stop_loss_pct else None
    tp_px = entry_price * (1 + take_profit_pct) if take_profit_pct else None
    if stop_px is not None and low <= stop_px:
        return stop_px, "stop_loss"
    if tp_px is not None and high >= tp_px:
        return tp_px, "take_profit"
    return None
