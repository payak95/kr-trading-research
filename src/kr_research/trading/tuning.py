# 파라미터 그리드서치 — spec 의 params 조합을 순수 백테스트해 순위 매김 (워크포워드 인샘플 최적화에 재사용)
"""tools/backtest_worker.py::sweep_spec 의 순수 로직(Redis·비용상수 무관 부분)을 트레이딩 레이어로 이전.
trading/walkforward.py 가 창(window)마다 인샘플 최적화로 이 함수를 재사용해야 하는데, 원래 위치(tools/)는
Redis 워커 레이어라 trading/(순수 엔진)이 이를 import 하면 레이어가 역전된다(trading→tools 는 금지).
tools/backtest_worker.sweep_spec 은 이제 이 함수에 FEE_RATE 등을 주입하는 얇은 래퍼.

조합 상한(cap) 초과 시: 예전엔 카테시안곱을 전부 만들어 앞에서부터 cap개만 잘랐는데, 이건
itertools.product 의 열거 순서(마지막 파라미터가 가장 빨리 순회)에 **편향**된 부분집합이었다
(앞쪽 파라미터 값은 거의 안 뽑히고 마지막 파라미터만 고르게 도는 조합만 쌓임). 지금은 조합을
전부 만들지 않고 인덱스만으로 i번째 조합을 얻는 mixed-radix 역산(_decode) + 정수 인덱스를
random.sample 로 균등 비복원추출 — 차원·후보 개수와 무관하게 항상 최대 cap번만 평가하면서도
편향이 없다. seed 고정이라 같은 입력엔 같은 결과(재현 가능)."""
import math
import random

from kr_research.trading.backtest import run
from kr_research.trading.metrics import summary
from kr_research.trading.spec import SpecStrategy, validate

SWEEP_METRICS = {"return_pct", "sharpe", "win_rate", "profit_factor", "expectancy", "cagr"}  # 정렬 가능 지표(높을수록 좋음)
DEFAULT_CAP = 500


def _decode(i: int, sizes: list[int]) -> tuple[int, ...]:
    """정수 인덱스를 mixed-radix 로 차원별 인덱스 튜플로 역산 — 카테시안곱을 전부 만들지 않고도
    i번째 조합을 바로 얻는다(sizes = 각 차원 후보 개수)."""
    idx = []
    for s in reversed(sizes):
        i, r = divmod(i, s)
        idx.append(r)
    return tuple(reversed(idx))


def grid_search(bars: list[dict], code: str, spec: dict, grid: dict, base_params: dict | None = None,
                metric: str = "return_pct", cap: int = DEFAULT_CAP, cash: int = 10_000_000, *,
                fee_rate: float = 0.0, tax_rate: float = 0.0, slippage: float = 0.0,
                max_fill_volume_frac: float | None = None, stop_loss_pct: float | None = None,
                take_profit_pct: float | None = None, sizing: dict | None = None,
                seed: int = 0) -> dict:
    """base_params 에 grid({param:[values]}) 조합을 덮어쓰며 한 종목 백테스트, metric 내림차순 순위.
    일봉 1회(주입 bars)·조합마다 순수 백테스트라 빠름. 조합이 cap 이하면 전수, 넘으면 무작위
    비복원추출 cap개(seed 고정 — 재현 가능, 위 모듈 docstring 참고). 반환 best(최적 조합)+상위 결과.
    (과최적화 주의 — 워크포워드/전진검증과 연계 권장.)"""
    validate(spec)
    base = dict(base_params or {})
    names = sorted(grid)
    value_lists = [grid[n] for n in names]
    sizes = [len(v) for v in value_lists]
    total = math.prod(sizes) if sizes else 0
    idxs = range(total) if total <= cap else random.Random(seed).sample(range(total), cap)
    rows = []
    for i in idxs:
        combo_idx = _decode(i, sizes)
        combo = tuple(value_lists[j][combo_idx[j]] for j in range(len(names)))
        params = {**base, **dict(zip(names, combo))}
        strategy = SpecStrategy(spec, params)
        res = run(strategy, bars, code, cash=cash, fee_rate=fee_rate, tax_rate=tax_rate,
                  slippage=slippage, max_fill_volume_frac=max_fill_volume_frac,
                  stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, sizing=sizing)
        m = summary(res["equity_curve"], res["trades"]) if res["n_trades"] else {}
        rows.append({"params": dict(zip(names, combo)), "return_pct": res["return_pct"],
                     "n_trades": res["n_trades"], "win_rate": m.get("win_rate"),
                     "sharpe": m.get("sharpe"), "mdd": m.get("mdd"), "final_equity": res["final_equity"]})
    key = metric if metric in SWEEP_METRICS else "return_pct"
    rows.sort(key=lambda r: (r.get(key) is not None, r.get(key) if r.get(key) is not None else 0.0), reverse=True)
    return {"name": spec.get("name", ""), "code": code, "metric": key, "n_combos": len(rows),
            "best": rows[0] if rows else None, "results": rows[:50]}
