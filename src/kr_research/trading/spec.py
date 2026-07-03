# 전략 스펙 — 전략을 코드가 아닌 선언형 데이터(지표 + entry/exit 불린 트리)로 표현·해석 (노드 빌더 Phase 0)
"""목표: 전략을 '스펙(JSON 직렬화 가능 dict)'으로 정의하고 해석기가 신호를 낸다. 노드형 전략 빌더의
첫 돌 — 사용자가 코드 없이 전략을 조립하는 토대. AlphaForge 의 '타입 있는 노드' 구조를 미러링.
설계: docs/planning/strategy-spec.md · 레퍼런스: docs/planning/alphaforge-benchmark.md §7.

스펙 구조:
  indicators: [{id, type(sma|rsi|ema|roc|price), params:{period: int 또는 {"param":이름}}}]
  entry/exit: 불린 트리 — {"all":[...]}/{"any":[...]}/{"not":expr} 또는 비교 {op:[좌,우]}
              op ∈ gt/lt/gte/lte/eq, 피연산자 = 지표id(str)/{"param":이름}/리터럴 숫자
  params: 임계값·기간(rsi_buy/rsi_sell/sma_fast/sma_slow/qty) — 프리셋이 이 값만 바꿔 같은 스펙 재사용.

해석기는 closes 로 지표를 계산하고(부족하면 보류), 보유 여부에 따라 entry/exit 트리를 평가해
"buy"/"sell"/None 을 낸다. SpecStrategy 는 기존 Strategy 와 동일 인터페이스(드롭인) — 봇이 스펙으로 구동 가능.
"""
from kr_research.core.params import Params
from kr_research.trading import flow, setups
from kr_research.trading.indicators import atr, bollinger, channel, ema, macd, price, roc, rsi, rvol, sma, stochastic
from kr_research.trading.strategy import Intent

# 지표 메타 — fn + 파라미터 순서 + 다중 출력 이름(None=스칼라, dict=id.출력 키) + 입력(needs).
# needs="closes"(기본)=종가만 · "bars"=OHLCV 필요(백테스트/스크리닝/라이브 모두 bars 공급 시 활성 —
# 라이브는 main 의 관심종목 일봉 리프레시(B3) 전이거나 실패 중이면 bars 없어 보류).
_IND = {
    "sma":   {"fn": sma,   "params": ["period"], "outputs": None},
    "ema":   {"fn": ema,   "params": ["period"], "outputs": None},
    "rsi":   {"fn": rsi,   "params": ["period"], "outputs": None},
    "roc":   {"fn": roc,   "params": ["period"], "outputs": None},
    "price": {"fn": price, "params": [],         "outputs": None},
    "macd":  {"fn": macd,  "params": ["fast", "slow", "signal"], "outputs": ["line", "signal", "hist"]},
    "bb":    {"fn": bollinger, "params": ["period", "mult"], "outputs": ["upper", "middle", "lower"]},
    "stoch": {"fn": stochastic, "params": ["k", "d"], "outputs": ["k", "d"], "needs": "bars"},
    "rvol":    {"fn": rvol,    "params": ["period"], "outputs": None, "needs": "bars"},
    "channel": {"fn": channel, "params": ["period"], "outputs": ["high", "low"], "needs": "bars"},
    "atr":     {"fn": atr,     "params": ["period"], "outputs": None, "needs": "bars"},
}
_OPS = {
    "gt": lambda a, b: a > b, "lt": lambda a, b: a < b,
    "gte": lambda a, b: a >= b, "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}

# 현재 RSI 평균회귀+SMA 추세필터(strategy.py)를 스펙으로 인코딩. 프리셋은 params 만 교체.
# 매수(보유X): sma_fast>sma_slow AND rsi<=rsi_buy / 매도(보유O): rsi>=rsi_sell OR not(sma_fast>sma_slow)
BASELINE_SPEC: dict = {
    "version": 1,
    "name": "rsi-sma-baseline",
    "indicators": [
        {"id": "sma_fast", "type": "sma", "params": {"period": {"param": "sma_fast"}}},
        {"id": "sma_slow", "type": "sma", "params": {"period": {"param": "sma_slow"}}},
        {"id": "rsi", "type": "rsi", "params": {"period": 14}},  # RSI 창 14 고정(strategy.RSI_PERIOD)
    ],
    "entry": {"all": [
        {"gt": ["sma_fast", "sma_slow"]},
        {"lte": ["rsi", {"param": "rsi_buy"}]},
    ]},
    "exit": {"any": [
        {"gte": ["rsi", {"param": "rsi_sell"}]},
        {"not": {"gt": ["sma_fast", "sma_slow"]}},
    ]},
}


def _operand(o, vals, params):
    """피연산자 해석: 지표id(str)→계산값 · {"param":이름}→params 값 · 그 외→리터럴."""
    if isinstance(o, str):
        return vals.get(o)
    if isinstance(o, dict) and "param" in o:
        return params.get(o["param"])
    return o


def eval_expr(node, vals, params, active=None) -> bool:
    """불린 트리 평가(순수). all/any/not + 비교 op + 셋업 술어. 알 수 없는 노드는 예외.
    active = 활성 셋업 항목 리스트([{key,tone,...}], bars 있는 경로가 주입 — 백테스트/스크리닝/
    라이브(B3, bars 확보 시) 공통). 셋업 술어:
      `{"setup": key}` → 해당 key 활성 / `{"setup": key, "tone": t}` → key+tone 일치(방향 구분).
    active 미제공(bars 없음)이면 셋업 술어는 항상 False(보류, fail-safe)."""
    if "all" in node:
        return all(eval_expr(c, vals, params, active) for c in node["all"])
    if "any" in node:
        return any(eval_expr(c, vals, params, active) for c in node["any"])
    if "not" in node:
        return not eval_expr(node["not"], vals, params, active)
    if "setup" in node:
        want_tone = node.get("tone")
        return any(it.get("key") == node["setup"] and (want_tone is None or it.get("tone") == want_tone)
                   for it in (active or []))
    for op, fn in _OPS.items():
        if op in node:
            a, b = (_operand(x, vals, params) for x in node[op])
            if a is None or b is None:
                return False  # 미해석 피연산자(사라진 지표 참조·누락 파라미터) → 크래시 대신 조건 미충족
            return fn(a, b)
    raise ValueError(f"알 수 없는 expr 노드: {node}")


def uses_flow_setups(spec: dict) -> bool:
    """스펙 entry/exit 가 수급(외국인·기관, `trading.flow.SETUP_KEYS`) 셋업을 하나라도 참조하는지.
    호출부(screen_spec)가 이 값이 True 일 때만 네이버 수급 조회를 하도록(불필요한 호출 방지)."""
    used = _setup_keys_in(spec["entry"]) | _setup_keys_in(spec["exit"])
    return bool(used & flow.SETUP_KEYS)


def _setup_keys_in(node) -> set:
    """불린 트리가 참조하는 셋업 key 수집(검증용)."""
    keys = set()
    if not isinstance(node, dict):
        return keys
    if "setup" in node:
        keys.add(node["setup"])
    for k in ("all", "any"):
        for c in node.get(k, []):
            keys |= _setup_keys_in(c)
    if "not" in node:
        keys |= _setup_keys_in(node["not"])
    return keys


def _valid_operand_keys(spec: dict) -> set:
    """지표가 낼 수 있는 유효 피연산자 키 — 스칼라=id, 다중출력=id.출력(예: m.line, bb.upper)."""
    keys = set()
    for ind in spec["indicators"]:
        meta = _IND.get(ind.get("type"))
        if not meta:
            continue  # 미지원 타입은 validate 앞부분에서 이미 거부
        outs = meta["outputs"]
        if outs:
            keys |= {f"{ind.get('id')}.{o}" for o in outs}
        else:
            keys.add(ind.get("id"))
    return keys


def _operand_strs_in(node) -> set:
    """불린 트리의 비교 op 문자열 피연산자(지표 참조) 수집 — 리터럴/{"param"}/setup 은 제외(검증용)."""
    refs = set()
    if not isinstance(node, dict):
        return refs
    for k in ("all", "any"):
        for c in node.get(k, []):
            refs |= _operand_strs_in(c)
    if "not" in node:
        refs |= _operand_strs_in(node["not"])
    if "setup" in node:
        return refs
    for op in _OPS:
        if op in node:
            refs |= {x for x in node[op] if isinstance(x, str)}
    return refs


def validate(spec: dict) -> None:
    """경량 스키마 검증 — 누락 키·미지원 지표/연산·미지의 셋업 key 는 예외(외부 입력 방어)."""
    for k in ("indicators", "entry", "exit"):
        if k not in spec:
            raise ValueError(f"스펙에 '{k}' 누락")
    for ind in spec["indicators"]:
        t = ind.get("type")
        if t not in _IND:
            raise ValueError(f"미지원 지표: {t}")
        if "id" not in ind:
            raise ValueError("지표에 id 누락")
        missing = [p for p in _IND[t]["params"] if p not in (ind.get("params") or {})]
        if missing:
            raise ValueError(f"지표 '{ind.get('id')}' 파라미터 누락: {missing}")
    used = _setup_keys_in(spec["entry"]) | _setup_keys_in(spec["exit"])
    unknown = used - setups.SETUP_KEYS - flow.SETUP_KEYS
    if unknown:
        raise ValueError(f"미지의 셋업 key: {sorted(unknown)}")
    valid = _valid_operand_keys(spec)
    refs = _operand_strs_in(spec["entry"]) | _operand_strs_in(spec["exit"])
    dangling = refs - valid
    if dangling:
        raise ValueError(f"조건이 참조하는 지표를 찾을 수 없음: {sorted(dangling)}")


def _indicator_vals(spec: dict, closes, params: dict, bars=None):
    """스펙 지표를 계산 → {id: 값}(스칼라) 또는 {id.출력: 값}(다중 출력).
    needs="bars" 지표는 OHLCV(bars) 로 계산 — bars 없으면 보류(라이브는 리프레시 전/실패 중일 때만
    해당, B3). 파라미터/데이터 부족이면 None."""
    vals = {}
    for ind in spec["indicators"]:
        meta = _IND[ind["type"]]
        args = []
        for pname in meta["params"]:
            pv = _operand(ind["params"].get(pname), {}, params)
            if pv is None:
                return None  # 파라미터 부족 → 보류
            args.append(int(pv))
        src = bars if meta.get("needs") == "bars" else closes
        if src is None:
            return None  # OHLCV 지표인데 bars 없음 → 보류
        out = meta["fn"](src, *args)
        if out is None:
            return None  # 데이터 부족 → 보류
        if meta["outputs"]:
            for k in meta["outputs"]:
                vals[f"{ind['id']}.{k}"] = out[k]
        else:
            vals[ind["id"]] = out
    return vals


def decide(spec: dict, closes, params: dict, holding: bool, active=None, bars=None):
    """스펙+종가로 매매 의도 결정. 지표 부족(None)이면 보류(None). 반환 'buy'|'sell'|None.
    보유X면 entry, 보유O면 exit 트리 평가(strategy.py 와 동일 의미). active=활성 셋업·bars=OHLCV(선택)."""
    vals = _indicator_vals(spec, closes, params, bars)
    if vals is None:
        return None  # 데이터 부족 → 판단 보류
    tree = spec["exit"] if holding else spec["entry"]
    if eval_expr(tree, vals, params, active):
        return "sell" if holding else "buy"
    return None


def screen(spec: dict, bars: list[dict], params: dict | None = None, extra_active=None) -> bool:
    """스펙 entry 를 한 종목 일봉(bars)에 평가 → 후보 여부(스크리닝, 무주문·연구용).
    지표(스칼라) + 셋업(불린, `trading.setups.active_keys`) 조건을 함께 평가한다.
    셋업은 OHLCV 가 필요 — 라이브 decide(SpecStrategy.on_tick)도 bars 가 있으면 똑같이 쓴다(B3).
    extra_active(선택): bars 로 못 구하는 외부 셋업(예 `trading.flow.active_items`, 외국인·기관 수급)을
    이번 평가에만 추가로 합류시킴 — 스크리닝 전용 호출부(screen_spec)만 씀, 백테스트 경로는 안 넘겨
    그 조건이 항상 False 로 남는다(§스크리닝 강화 계획, 오늘 스냅샷이라 과거 재현 불가라 의도된 제약).
    데이터 부족=False."""
    eff = Params.from_dict(params or {}).clamp().effective()
    closes = [float(b["close"]) for b in bars]
    vals = _indicator_vals(spec, closes, eff, bars)   # bars → OHLCV 지표 활성
    if vals is None:
        return False
    active = setups.compute_setups(bars)["items"]   # [{key,tone,...}] — 방향(tone) 구분 가능
    if extra_active:
        active = active + extra_active
    return eval_expr(spec["entry"], vals, eff, active)


class SpecStrategy:
    """스펙 구동 전략 — 기존 Strategy 와 동일 인터페이스(on_tick/resolve_order/sync_holdings) 드롭인.
    보유/in-flight 관리는 Strategy 와 동일(체결/ reconcile 로만 갱신, 낙관적 set 금지)."""

    def __init__(self, spec: dict = BASELINE_SPEC, params: dict | None = None):
        validate(spec)
        self._spec = spec
        self._defaults = Params.from_dict(params or {}).clamp().to_dict()
        self._holding: dict[str, bool] = {}
        self._inflight: dict[str, bool] = {}
        # 스펙이 차트신호(setup)를 참조하면 백테스트/스크리닝 경로에서 봉별로 계산(1회 판정으로 불필요한 계산 회피).
        self._uses_setups = bool(_setup_keys_in(spec["entry"]) | _setup_keys_in(spec["exit"]))

    def resolve_order(self, code: str, side: str, filled: bool) -> None:
        if filled:
            self._holding[code] = (side == "buy")
        self._inflight[code] = False

    def sync_holdings(self, held_codes) -> None:
        self._holding = {code: True for code in set(held_codes)}
        self._inflight.clear()

    def on_tick(self, tick: dict, ctx: dict) -> list[Intent]:
        code = tick.get("code")
        if code is None or ctx.get("stale"):
            return []
        if self._inflight.get(code, False):
            return []
        eff = Params.from_dict({**self._defaults, **(ctx.get("params") or {})}).clamp().effective()
        # ctx["bars"](OHLCV)가 있으면 차트신호·OHLCV 지표 활성 — 백테스트/스크리닝은 항상 공급, 라이브는
        # main 의 관심종목 일봉 주기 리프레시(B3)가 채움(리프레시 전/실패 중이면 None → 그 조건만 보류).
        bars = ctx.get("bars")
        active = setups.compute_setups(bars)["items"] if (self._uses_setups and bars) else None
        action = decide(self._spec, ctx.get("closes") or [], eff, self._holding.get(code, False), active, bars)
        if action in ("buy", "sell"):
            self._inflight[code] = True
            return [Intent(side=action, code=code, qty=eff["qty"], price=None)]
        return []
