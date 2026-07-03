# 백테스트 성과·리스크 지표 — 자산곡선/라운드트립에서 Sharpe·Sortino·MDD·VaR·Kelly 등 산출 (순수)
"""trading/backtest.run() 결과(equity_curve·trades)에서 AlphaForge식 지표를 계산.
모두 순수 함수(네트워크·상태 없음, 단위 테스트). 연율화 기본 252 거래일.
우리 백테스트는 일봉을 시간순 재생(전략은 과거 종가만 사용) → point-in-time(룩어헤드 없음).
"""
import math
import statistics as st

TRADING_DAYS = 252


def round_trips(trades):
    """buy→sell 쌍의 라운드트립 순수익률(비용·세금 반영). 종목당 1포지션(매수 후 매도) 가정."""
    out, buy = [], None
    for t in trades:
        if t["side"] == "buy":
            buy = t
        elif t["side"] == "sell" and buy:
            cost = buy["qty"] * buy["price"] + buy.get("fee", 0.0)
            proceeds = t["qty"] * t["price"] - t.get("fee", 0.0) - t.get("tax", 0.0)
            out.append(proceeds / cost - 1.0 if cost else 0.0)
            buy = None
    return out


def daily_returns(curve):
    """자산곡선 → 일간 수익률 리스트."""
    return [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1]]


def max_drawdown(curve):
    """최대 낙폭(양수 비율). 빈/단일 곡선이면 0."""
    peak, mdd = None, 0.0
    for e in curve:
        peak = e if peak is None else max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def _sharpe(rets, periods):
    if len(rets) < 2:
        return None
    sd = st.pstdev(rets)
    return (st.mean(rets) / sd) * math.sqrt(periods) if sd else None


def _sortino(rets, periods):
    if len(rets) < 2:
        return None
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))  # 하방 편차
    return (st.mean(rets) / dd) * math.sqrt(periods) if dd else None


def _cagr(curve, periods):
    if len(curve) < 2 or curve[0] <= 0:
        return None
    return (curve[-1] / curve[0]) ** (periods / (len(curve) - 1)) - 1.0


def _percentile(xs, q):
    """q 분위수(0..1, 선형보간). 빈 리스트면 None."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summary(curve, trades, periods=TRADING_DAYS):
    """자산곡선+체결에서 성과·리스크 지표 dict. 표본 부족하면 해당 항목 None."""
    rets = daily_returns(curve)
    rts = round_trips(trades)
    wins = [r for r in rts if r > 0]
    losses = [r for r in rts if r < 0]
    avg_win = st.mean(wins) if wins else None
    avg_loss = st.mean(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if (avg_win and avg_loss) else None
    win_rate = (len(wins) / len(rts)) if rts else None
    kelly = (win_rate - (1 - win_rate) / payoff) if (win_rate is not None and payoff) else None
    gross_loss = abs(sum(losses))
    var95 = _percentile(rets, 0.05)
    tail = [r for r in rets if var95 is not None and r <= var95]
    cagr, mdd = _cagr(curve, periods), max_drawdown(curve)
    return {
        "n_round_trips": len(rts),
        "win_rate": win_rate,
        "expectancy": st.mean(rts) if rts else None,
        "payoff": payoff,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss else None,
        "kelly": kelly,
        "sharpe": _sharpe(rets, periods),
        "sortino": _sortino(rets, periods),
        "cagr": cagr,
        "mdd": mdd,
        "calmar": (cagr / mdd) if (cagr is not None and mdd) else None,
        "var95": var95,
        "cvar95": (st.mean(tail) if tail else None),
    }
