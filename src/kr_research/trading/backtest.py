# 백테스트 하니스 — 과거 일봉을 라이브와 동일한 strategy.on_tick(tick, ctx)로 재생해 손익 평가
"""전략 평가 전용 시뮬레이터(라이브의 risk/executor 와 분리). 각 일봉을 틱처럼 전략에 넣고,
의도(Intent)를 그 봉 종가 기준으로 체결한 셈 친다. 거래비용(수수료·거래세·슬리피지)과
부분체결(거래량 한도)을 선택적으로 반영한다 — **기본값 0/off 이면 순수 시뮬(결정적)**.
실제 수수료/세금 근사값은 호출부(tools/backtest.py)가 주입한다(엔진에 세율을 박지 않음).
ctx 모양은 market_data.context 와 맞춰 전략 코드 호환.

손절/익절(stop_loss_pct·take_profit_pct)과 포지션 사이징(sizing)은 전략(spec)과 무관한
**백테스트 현실성 오버레이**(수수료·슬리피지와 같은 자리) — 기본 None 이면 기존 동작 그대로.

손절/익절 트리거 판정은 `trading/exits.py::check_stop_take_profit`(전진검증 청산-추적과 공용, Phase 2).
"""
from kr_research.trading.exits import check_stop_take_profit


def _sized_qty(intent_qty: int, cash: float, exec_px: float, fee_rate: float,
               stop_loss_pct: float | None, sizing: dict | None) -> int:
    """사이징 오버레이 적용 — sizing=None 이면 전략이 낸 수량 그대로(하위호환).
    pct_cash: 현금의 value 비율만큼 매수. risk: 손절폭(stop_loss_pct) 대비 손실이 현금의
    risk_pct 가 되도록 역산(stop_loss_pct 없으면 리스크 거리 계산 불가 → 전략 수량 폴백)."""
    if not sizing:
        return intent_qty
    unit = exec_px * (1 + fee_rate)
    mode = sizing.get("mode")
    if mode == "pct_cash":
        budget = cash * sizing.get("value", 1.0)
        return int(budget // unit) if unit > 0 else 0
    if mode == "risk" and stop_loss_pct:
        risk_budget = cash * sizing.get("risk_pct", 0.01)
        per_share_risk = exec_px * stop_loss_pct
        return int(risk_budget // per_share_risk) if per_share_risk > 0 else 0
    return intent_qty


def run(strategy, bars: list[dict], code: str, cash: int = 10_000_000, *,
        fee_rate: float = 0.0, tax_rate: float = 0.0, slippage: float = 0.0,
        max_fill_volume_frac: float | None = None, stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None, sizing: dict | None = None) -> dict:
    """bars: 시간순 [{date, open, high, low, close, volume}]. 반환: 손익·거래·비용 내역.

    fee_rate: 편도 위탁수수료율(매수·매도 양쪽). tax_rate: 매도 시 거래세+농특세율.
    slippage: 시장가 불리 체결 비율(매수 +, 매도 -). max_fill_volume_frac: 그 봉 거래량의
    이 비율까지만 체결 가능(부분체결; volume 있을 때만). None 이면 거래량 무제한.
    stop_loss_pct/take_profit_pct: 진입가(평단가) 대비 이 비율만큼 하락/상승하면 그 봉에서
    강제 전량청산(그 봉의 low/high 로 트리거 — close 전용보다 현실적). sizing: 매수 수량
    오버라이드({"mode":"pct_cash","value":..} 또는 {"mode":"risk","risk_pct":..}).
    """
    init = cash
    closes: list[float] = []
    hist: list[dict] = []     # 그 시점까지의 OHLCV — SpecStrategy 가 ctx["bars"] 로 읽어 차트신호(setup) 계산(워크포워드)
    curve: list[float] = []   # 매 봉 종료 시 자산(cash+pos*close) — 지표(Sharpe·MDD 등)용
    pos = 0
    entry_price: float | None = None  # 보유 포지션 평단가(가중평균) — 손절/익절 트리거 기준
    trades: list[dict] = []
    total_fees = 0.0
    total_tax = 0.0

    for b in bars:
        px = b["close"]
        closes.append(px)
        hist.append(b)
        forced_exit = False

        if pos > 0 and entry_price is not None and (stop_loss_pct or take_profit_pct):
            low, high = b.get("low", px), b.get("high", px)
            triggered = check_stop_take_profit(entry_price, low, high, stop_loss_pct, take_profit_pct)
            if triggered is not None:
                trigger_px, reason = triggered
                qty = pos
                gross = qty * trigger_px
                fee = gross * fee_rate
                tax = gross * tax_rate
                cash += gross - fee - tax
                pos = 0
                total_fees += fee
                total_tax += tax
                trades.append({"date": b.get("date"), "side": "sell", "qty": qty,
                               "price": trigger_px, "fee": fee, "tax": tax, "reason": reason})
                strategy.resolve_order(code, "sell", filled=True)
                entry_price = None
                forced_exit = True

        ctx = {"stale": False, "price": px, "closes": list(closes), "date": b.get("date"), "bars": hist}
        vol_cap = None
        if max_fill_volume_frac is not None and b.get("volume"):
            vol_cap = int(b["volume"] * max_fill_volume_frac)

        for it in ([] if forced_exit else (strategy.on_tick({"code": code, "price": px}, ctx) or [])):
            if it.side == "buy":
                exec_px = px * (1 + slippage)
                unit = exec_px * (1 + fee_rate)  # 1주당 수수료 포함 소요 현금
                qty = _sized_qty(it.qty, cash, exec_px, fee_rate, stop_loss_pct, sizing)
                if vol_cap is not None:
                    qty = min(qty, vol_cap)
                qty = min(qty, int(cash // unit) if unit > 0 else qty)  # 현금 한도(부분체결)
                if qty <= 0:
                    strategy.resolve_order(code, "buy", filled=False)  # 미체결 → in-flight 해제
                    continue
                gross = qty * exec_px
                fee = gross * fee_rate
                cash -= gross + fee
                prev_pos = pos
                pos += qty
                entry_price = exec_px if prev_pos == 0 or entry_price is None else \
                    (entry_price * prev_pos + exec_px * qty) / pos
                total_fees += fee
                trades.append({"date": b.get("date"), "side": "buy", "qty": qty,
                               "price": exec_px, "fee": fee})
                strategy.resolve_order(code, "buy", filled=True)
            elif it.side == "sell":
                exec_px = px * (1 - slippage)
                qty = min(it.qty, pos)
                if vol_cap is not None:
                    qty = min(qty, vol_cap)
                if qty <= 0:
                    strategy.resolve_order(code, "sell", filled=False)  # 미체결 → in-flight 해제
                    continue
                gross = qty * exec_px
                fee = gross * fee_rate
                tax = gross * tax_rate
                cash += gross - fee - tax
                pos -= qty
                if pos <= 0:
                    entry_price = None
                total_fees += fee
                total_tax += tax
                trades.append({"date": b.get("date"), "side": "sell", "qty": qty,
                               "price": exec_px, "fee": fee, "tax": tax})
                strategy.resolve_order(code, "sell", filled=True)

        curve.append(cash + pos * px)  # 봉 종료 자산(시간순 — 지표 입력)

    last = closes[-1] if closes else 0
    equity = cash + pos * last
    return {"final_equity": equity, "cash": cash, "position": pos,
            "return_pct": (equity / init - 1) * 100 if init else 0.0,
            "n_trades": len(trades), "trades": trades, "equity_curve": curve,
            "fees": total_fees, "tax": total_tax}
