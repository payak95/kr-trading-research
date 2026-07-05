# 전진 검증 — 진입 신호의 D+N forward 수익·청산-추적 정확 수익률 계산·집계 (순수 로직, 브로커·Redis I/O 없음)
"""백테스트(과거 사후 리플레이, 룩어헤드)와 달리 신호를 낸 *뒤* 실제 미래 종가로 수익을 잰다.
설계: docs/planning/forward-tracking-design.md(D+N 고정, ①단계) ·
docs/planning/pipeline-automation-design.md §4(청산-추적, Phase 2 — `evaluate_exit`). I/O(시세 조회·DB·publish)는
tools/forward_eval.py·tools/screen_track_eval.py 담당, 여기는 단위 테스트 가능한 순수 함수만 둔다.

거래일 = 일봉 시계열의 바 자체(휴장·정지 자동 처리). 신호 거래일 바를 기준으로 N개 뒤 바의 종가로 평가.
"""
from kr_research.trading.exits import check_stop_take_profit
from kr_research.trading.spec import decide
from kr_research.trading import setups

HORIZONS = (1, 5, 20)            # 평가 지평(거래일). 콘솔은 D+1·D+5 표시, D+20 은 장기 참고.
GATE = {"min_n": 30, "win_d5": 0.55}  # 검증 게이트: D+5 평가 N≥30 & 승률≥55% & 평균>0 & (벤치 있으면)초과>0
BENCHMARK_CODE = "069500"        # KODEX 200(KOSPI200 ETF) — 지수 대비 초과수익(알파 vs 베타)용. 일봉은 일반종목처럼 조회.
MAX_HOLD_DAYS = 60               # 청산-추적 최대 보유 거래일 — 이 안에 손절/익절/청산조건이 안 뜨면 미청산(강제청산 없음, 정직 표기)
# Rank IC 는 순위(점수) 전략이 아닌 이진 신호엔 부적용 → 노드형 멀티팩터 도입 시 추가(단계 B 범위 밖).


def forward_returns(bars, trade_date, entry_price, horizons=HORIZONS):
    """일봉 시계열(거래일 캘린더)에서 진입 신호의 D+N forward 수익률.
    bars: 시간순 [{date:'YYYYMMDD', close}], trade_date: 신호 거래일, entry_price: 신호 시점 가격.
    반환 {N: ret 또는 None(아직 N거래일 미경과·데이터 없음)}. entry_price<=0·빈 bars 는 모두 None(방어)."""
    out = {n: None for n in horizons}
    if not bars or entry_price is None or entry_price <= 0:
        return out
    i0 = next((i for i, b in enumerate(bars) if b["date"] >= trade_date), None)
    if i0 is None:
        return out  # 신호 거래일이 시계열 범위 밖(미래) → 평가 불가
    for n in horizons:
        j = i0 + n
        if j < len(bars):
            try:
                out[n] = bars[j]["close"] / entry_price - 1.0
            except (TypeError, ZeroDivisionError):
                out[n] = None
    return out


def benchmark_returns(bench_bars, trade_date, horizons=HORIZONS):
    """벤치마크(지수 ETF) 일봉으로 신호 거래일 D 기준 D+N 지수 수익률(close[D]→close[D+N]).
    forward_returns 와 동일 로직이되 base 가 신호 거래일의 벤치마크 종가(시장 자체 움직임).
    반환 {N: bench_ret 또는 None}. 종목 수익에서 이걸 빼면 초과수익(알파)."""
    if not bench_bars:
        return {n: None for n in horizons}
    i0 = next((i for i, b in enumerate(bench_bars) if b["date"] >= trade_date), None)
    if i0 is None:
        return {n: None for n in horizons}
    return forward_returns(bench_bars, trade_date, bench_bars[i0]["close"], horizons)


def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def _winrate(xs):
    return (sum(1 for v in xs if v > 0) / len(xs)) if xs else None


def _agg(rows, horizons, gate):
    """신호 묶음 1개의 집계: 지평별 평가수·평균·승률 + 초과수익(벤치 대비) + 게이트 판정(validated)."""
    d = {"signals": len(rows)}
    d5_vals, d5_exc = [], []
    for n in horizons:
        vals = [r[f"ret_d{n}"] for r in rows if r.get(f"ret_d{n}") is not None]
        exc = [r[f"ret_d{n}"] - r[f"bench_d{n}"] for r in rows
               if r.get(f"ret_d{n}") is not None and r.get(f"bench_d{n}") is not None]
        d[f"pending_d{n}"] = len(rows) - len(vals)
        d[f"avg_d{n}"], d[f"win_d{n}"] = _mean(vals), _winrate(vals)
        d[f"avg_excess_d{n}"], d[f"win_excess_d{n}"] = _mean(exc), _winrate(exc)
        if n == 5:
            d5_vals, d5_exc = vals, exc
    # 검증: D+5 평가 표본 충분(min_n) + 승률·평균 게이트 통과 + (벤치 있으면) 초과수익도 양수.
    d["validated"] = bool(
        len(d5_vals) >= gate["min_n"]
        and d["win_d5"] is not None and d["win_d5"] >= gate["win_d5"]
        and d["avg_d5"] is not None and d["avg_d5"] > 0
        and (d["avg_excess_d5"] is None or d["avg_excess_d5"] > 0))
    return d


def summarize(signals, horizons=HORIZONS, gate=GATE):
    """신호 리스트 → 프리셋(mode)별 + 전체 집계. signals 원소는 {mode, ret_d1, ret_d5, ...}.
    반환 {"by_mode": {mode: agg}, "all": agg}. 콘솔 검증 패널이 그대로 표시."""
    groups: dict = {}
    for s in signals:
        groups.setdefault(s.get("mode") or "neutral", []).append(s)
    return {"by_mode": {m: _agg(rows, horizons, gate) for m, rows in groups.items()},
            "all": _agg(signals, horizons, gate)}


def _sign_flip_by_action(rows, horizons, mode_of):
    """행마다 action="sell" 이면 ret_d{n}·bench_d{n} 부호를 뒤집고 'mode' 필드를 mode_of(row) 로 교체.
    summarize() 의 avg/win 은 "가격이 올랐는가"를 승리로 보는데, sell 은 반대로 가격이 내려가야 판단이
    맞은 것이라 부호 반전 없이 그대로 쓰면 avg_d5>0 이 오히려 "판단이 틀렸다"는 뜻이 된다. 부호를 미리
    뒤집으면 avg/win/초과수익이 buy·hold 와 마찬가지로 "판단이 맞았는가"로 통일되게 해석된다(buy·hold 는
    원래 부호 그대로 — hold 는 뚜렷한 방향이 없다는 판단이라 반전 대상 아님). ret_d{n}·bench_d{n} 을
    함께 뒤집어야 초과수익(ret-bench)도 올바르게 부호 반전된다(각각 따로 뒤집으면 계산이 깨짐)."""
    signed = []
    for row in rows:
        flip = -1 if (row.get("action") or "hold") == "sell" else 1
        r = {**row, "mode": mode_of(row)}
        for n in horizons:
            for key in (f"ret_d{n}", f"bench_d{n}"):
                if r.get(key) is not None:
                    r[key] = r[key] * flip
        signed.append(r)
    return signed


def summarize_actions(rows, horizons=HORIZONS, gate=GATE):
    """buy/sell/hold 액션 판단(row 에 'action' 필드, 예: AI 섀도 판단) 전용 집계 — summarize(mode:=action)
    를 그대로 재사용하되 sell 행의 부호를 먼저 뒤집는다(_sign_flip_by_action). _agg 자체(순수 집계 수식)는
    손대지 않는다 — buy 전용 forward-tracking(screen_track 등, mode 가 액션이 아닌 프리셋/톤을 의미) 에
    영향 없음."""
    signed = _sign_flip_by_action(rows, horizons, lambda row: row.get("action") or "hold")
    return summarize(signed, horizons, gate)


CONFIDENCE_BUCKETS = (0.3, 0.6)  # (low<0.3, mid 0.3~0.6, high>=0.6) — 경계는 임의 3분할, 필요시 조정


def confidence_bucket(confidence) -> str:
    """confidence(0~1 또는 None) → "low"|"mid"|"high"|"unknown" 라벨."""
    if confidence is None:
        return "unknown"
    lo, hi = CONFIDENCE_BUCKETS
    if confidence < lo:
        return "low"
    if confidence < hi:
        return "mid"
    return "high"


def summarize_by_confidence(rows, horizons=HORIZONS, gate=GATE):
    """AI 판단(row 에 'action'+'confidence' 필드)을 신뢰도 구간별로 집계 — "확신도가 높을수록 실제로
    더 잘 맞았는가"(캘리브레이션)를 보기 위함. summarize_actions 와 동일하게 sell 부호만 먼저 뒤집고
    (판단이 맞았는지가 기준), 그룹 키만 action 대신 confidence 버킷을 쓴다."""
    signed = _sign_flip_by_action(rows, horizons, lambda row: confidence_bucket(row.get("confidence")))
    return summarize(signed, horizons, gate)


def _realized_return_pct(entry_price: float, exit_price: float, reason: str,
                         fee_rate: float, tax_rate: float, slippage: float) -> float:
    """backtest.run 과 동일 비용 모델(진입=매수 슬리피지+수수료, 청산=수수료+거래세).
    손절/익절은 그 봉 저가/고가 트리거 가격이 곧 체결가(backtest.run 의 강제청산도 슬리피지 없음) —
    exit_condition(전략 매도 신호)만 backtest.run 의 전략매도 경로처럼 종가에 슬리피지 적용."""
    entry_cost = entry_price * (1 + slippage) * (1 + fee_rate)
    exit_px = exit_price * (1 - slippage) if reason == "exit_condition" else exit_price
    proceeds = exit_px * (1 - fee_rate - tax_rate)
    return (proceeds / entry_cost - 1) * 100


def evaluate_exit(spec: dict, params: dict, entry_price: float, entry_date: str, bars: list[dict], *,
                  stop_loss_pct: float | None = None, take_profit_pct: float | None = None,
                  fee_rate: float = 0.0, tax_rate: float = 0.0, slippage: float = 0.0,
                  max_hold_days: int = MAX_HOLD_DAYS) -> dict:
    """진입 신호 이후 그 전략의 실제 청산 규칙(손절·익절·spec exit 트리)이 언제 처음 발동하는지 매일 새
    봉을 따라가며 판정 — 고정 D+N 대신 "그 전략이 실제로 언제 팔았을지"를 반영한 정확한 실현 수익률.

    bars: entry_date **이전**(지표 워밍업용)~오늘까지 시간순 전체 OHLCV(entry_date 포함). 매일 그 시점까지의
    슬라이스만 사용해 룩어헤드 없음. `trading/exits.py::check_stop_take_profit`(백테스트와 공용) → 없으면
    `trading/spec.py::decide(holding=True)`(exit 트리) 순서로 평가 — **진입 봉 당일은 청산 대상 아님**
    (entry_date 다음 거래일부터 평가, backtest.run 과 동일 불변식, docs/planning/pipeline-automation-design.md
    §4.2 v0.6).

    반환: 청산되면 {exited:True, exit_date, exit_price, exit_reason(stop_loss|take_profit|exit_condition),
    holding_days, realized_return_pct}. max_hold_days 안에 못 찾으면(강제 청산 없음, 정직 표기)
    {exited:False, holding_days, open_return_pct, capped}(capped=True 면 더 평가해도 결론 안 남)."""
    i0 = next((i for i, b in enumerate(bars) if b["date"] >= entry_date), None)
    if i0 is None or not entry_price or entry_price <= 0:
        return {"exited": False, "holding_days": 0, "open_return_pct": None, "capped": False}
    last_i = min(i0 + max_hold_days, len(bars) - 1)
    for j in range(i0 + 1, last_i + 1):
        b = bars[j]
        sub = bars[: j + 1]
        trig = check_stop_take_profit(entry_price, b.get("low", b["close"]), b.get("high", b["close"]),
                                      stop_loss_pct, take_profit_pct)
        if trig is not None:
            exit_price, reason = trig
        else:
            closes = [x["close"] for x in sub]
            active = setups.compute_setups(sub)["items"]
            if decide(spec, closes, params, holding=True, active=active, bars=sub) != "sell":
                continue
            exit_price, reason = b["close"], "exit_condition"
        return {"exited": True, "exit_date": b.get("date"), "exit_price": exit_price, "exit_reason": reason,
                "holding_days": j - i0,
                "realized_return_pct": _realized_return_pct(entry_price, exit_price, reason,
                                                             fee_rate, tax_rate, slippage)}
    holding_days = last_i - i0
    open_return_pct = (bars[last_i]["close"] / entry_price - 1) * 100 if last_i > i0 else None
    return {"exited": False, "holding_days": holding_days, "open_return_pct": open_return_pct,
            "capped": holding_days >= max_hold_days}
