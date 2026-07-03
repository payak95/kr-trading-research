# 워크포워드 검증 — 학습창에서 그리드서치로 파라미터를 고르고, 그 파라미터를 다음(미학습) 검증창에 적용해 롤링 평가
"""그리드서치(trading.tuning.grid_search) 단독은 전체 구간을 인샘플로 최적화하므로 과최적화 위험이 있다
(docs/ops/redis-control-bus.md "과최적화 주의" 경고). 워크포워드는 [학습(train_days)][검증(test_days)]
블록을 test_days 만큼 비중첩으로 밀며 반복해, 각 창은 "그 시점까지의 데이터로 고른 파라미터를 그 다음
미래(test) 구간에 그대로 썼다면"을 시뮬레이션한다. 아웃오브샘플(test) 결과만 이어붙여 종합 성과를 낸다
— 실전에서 재추정 없이 그 파라미터를 계속 썼을 때의 근사치.

전진검증(forward-tracking-design.md)과는 다른 개념이다: 그쪽은 라이브/페이퍼 신호의 실제 미래 수익을
측정하고, 이건 과거 데이터 안에서 인샘플/아웃오브샘플을 시간순으로 분리해 백테스트 자체의 과최적화를
가늠한다.
"""
from kr_research.trading.backtest import run
from kr_research.trading.metrics import summary
from kr_research.trading.spec import SpecStrategy, validate
from kr_research.trading.tuning import DEFAULT_CAP, grid_search


def walk_forward(bars: list[dict], code: str, spec: dict, grid: dict, train_days: int, test_days: int, *,
                  base_params: dict | None = None, metric: str = "return_pct", cash: int = 10_000_000,
                  fee_rate: float = 0.0, tax_rate: float = 0.0, slippage: float = 0.0,
                  max_fill_volume_frac: float | None = None, stop_loss_pct: float | None = None,
                  take_profit_pct: float | None = None, sizing: dict | None = None,
                  cap: int = DEFAULT_CAP) -> dict:
    """bars 를 [train_days 학습 + test_days 검증] 창으로 비중첩 롤링. 창마다:
    1) train 구간에서 grid_search 로 최적 파라미터 선정(인샘플)
    2) 그 파라미터로 test 구간을 백테스트(아웃오브샘플 — 학습에 쓰이지 않은 미래 데이터)
    자산은 창 사이에 이어짐(직전 창의 최종 자산 = 다음 창의 시작 현금, 복리 근사).
    데이터가 창 하나도 못 채우면(bars 부족) n_windows=0 으로 빈 결과 반환(크래시 없음)."""
    validate(spec)
    windows = []
    curve: list[float] = []
    trades: list[dict] = []
    running_cash = cash
    start = 0
    while start + train_days + test_days <= len(bars):
        train_bars = bars[start:start + train_days]
        test_bars = bars[start + train_days:start + train_days + test_days]
        sweep = grid_search(train_bars, code, spec, grid, base_params, metric, cap, running_cash,
                            fee_rate=fee_rate, tax_rate=tax_rate, slippage=slippage,
                            max_fill_volume_frac=max_fill_volume_frac,
                            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, sizing=sizing)
        if sweep["best"] is None:
            start += test_days
            continue  # 그리드 조합 0건 — 이 창은 skip(다음 창으로)
        merged = {**(base_params or {}), **sweep["best"]["params"]}
        strategy = SpecStrategy(spec, merged)
        res = run(strategy, test_bars, code, cash=running_cash, fee_rate=fee_rate, tax_rate=tax_rate,
                  slippage=slippage, max_fill_volume_frac=max_fill_volume_frac,
                  stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, sizing=sizing)
        windows.append({
            "train_start": train_bars[0].get("date"), "train_end": train_bars[-1].get("date"),
            "test_start": test_bars[0].get("date"), "test_end": test_bars[-1].get("date"),
            "params": sweep["best"]["params"], "in_sample_metric": sweep["best"].get(sweep["metric"]),
            "out_of_sample_return_pct": res["return_pct"], "n_trades": res["n_trades"],
            "final_equity": res["final_equity"],
        })
        curve.extend(res["equity_curve"])
        trades.extend(res["trades"])
        running_cash = res["final_equity"]  # 자산 이어붙임(복리 근사)
        start += test_days
    total_return = (running_cash / cash - 1) * 100 if cash else 0.0
    return {
        "name": spec.get("name", ""), "code": code, "train_days": train_days, "test_days": test_days,
        "n_windows": len(windows), "windows": windows,
        "final_equity": running_cash, "total_return_pct": total_return,
        "metrics": summary(curve, trades) if trades else {},
    }
