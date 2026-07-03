# 런타임 매매 파라미터 — n8n/콘솔 제어 평면이 조정, 봇이 읽음
"""전략·실행·리스크 한도를 코드 재배포 없이 조정하기 위한 파라미터. 외부(n8n/콘솔) 입력은
신뢰하지 않고 clamp() 로 sane 범위로 자른다. **RiskGuard 하드 머니 한도(1회 주문·종목당 보유·
일일손실·동시보유수)도 콘솔에서 오버라이드 가능(의도적 설계, 2026-07-02)** — 단 clamp() 의
안전 상한선(예: 1회 주문 10억원)을 넘어설 수 없어 RiskGuard 는 여전히 최후 방어선 역할.
None=해당 필드 미설정 → `.env` 기본값(`Config.risk`) 유지.

enabled 기본 False = 자동매매 OFF(안전). n8n 이 명시적으로 켜야 매매.
mode 는 레짐 라벨. 전략 필드(qty·sma·rsi)는 **None=해당 mode 프리셋 상속**, 값이 있으면 오버라이드.
효과 파라미터는 effective(): 프리셋(mode) ← 명시 오버라이드 머지(2A, control-plane-stage2-design §10).
리스크 필드는 effective_risk(): `.env` 기본값 ← 명시 오버라이드 머지(프리셋 없음, mode 무관).
"""
from dataclasses import asdict, dataclass

MODES = {"defensive", "neutral", "aggressive"}

# 레짐 프리셋(전략 효과 파라미터의 기본). 1분봉 백테스트(tools/backtest.py)로 튜닝 — 중간 거래빈도
# 목표(rsi 40↑ 는 비용에 churn). 방어=선택적·일찍매도, 공격=적극적·늦게매도. 모든 값 clamp 범위 내.
MODE_PRESETS: dict[str, dict] = {
    "defensive":  {"qty": 1, "sma_fast": 5, "sma_slow": 20, "rsi_buy": 30.0, "rsi_sell": 66.0},
    "neutral":    {"qty": 1, "sma_fast": 5, "sma_slow": 20, "rsi_buy": 34.0, "rsi_sell": 70.0},
    "aggressive": {"qty": 2, "sma_fast": 5, "sma_slow": 20, "rsi_buy": 38.0, "rsi_sell": 74.0},
}
_STRAT_FIELDS = ("qty", "sma_fast", "sma_slow", "rsi_buy", "rsi_sell")
_RISK_FIELDS = ("max_order_krw", "max_position_krw", "daily_loss_limit_krw", "max_open_positions")
_RISK_RANGES = {  # 콘솔 오버라이드 안전 상한선(실수 방지용 — 실사용 범위보다 훨씬 넉넉)
    "max_order_krw": (10_000, 1_000_000_000),
    "max_position_krw": (10_000, 1_000_000_000),
    "daily_loss_limit_krw": (10_000, 1_000_000_000),
    "max_open_positions": (1, 50),
}
REGIME_MAX_AGE = 86400.0  # s, 전역 레짐 신선도 상한 — 초과 시 neutral 복귀(fail-safe, 설계 §4-3)


@dataclass
class Params:
    enabled: bool = False              # 자동매매 on/off (기본 off)
    mode: str = "neutral"              # 레짐 라벨(수동). follow_regime 이면 전역 레짐이 우선
    follow_regime: bool = False        # True 면 신선한 market:regime 을 mode 로 추종(아니면 neutral)
    qty: int | None = None             # None = mode 프리셋 사용(오버라이드 없음)
    sma_fast: int | None = None
    sma_slow: int | None = None
    rsi_buy: float | None = None
    rsi_sell: float | None = None
    strategy_name: str | None = None   # None=베이스라인 전략. 값=bot:strategies(콘솔 B1) 저장 스펙 이름
                                        # — 존재/라이브 적합성 검사는 Redis I/O 필요해 main.py 로드 시점에
    max_order_krw: int | None = None       # None = .env RISK_MAX_ORDER_KRW 사용
    max_position_krw: int | None = None    # None = .env RISK_MAX_POSITION_KRW 사용
    daily_loss_limit_krw: int | None = None  # None = .env RISK_DAILY_LOSS_LIMIT_KRW 사용
    max_open_positions: int | None = None  # None = .env RISK_MAX_OPEN_POSITIONS 사용

    def clamp(self) -> "Params":
        self.enabled = bool(self.enabled)
        self.follow_regime = bool(self.follow_regime)
        self.mode = self.mode if self.mode in MODES else "neutral"
        if self.qty is not None:
            self.qty = max(1, min(int(self.qty), 100))
        if self.sma_fast is not None:
            self.sma_fast = max(1, min(int(self.sma_fast), 200))
        if self.sma_slow is not None:
            self.sma_slow = max(2, min(int(self.sma_slow), 400))
        if self.rsi_buy is not None:
            self.rsi_buy = min(max(float(self.rsi_buy), 1.0), 99.0)
        if self.rsi_sell is not None:
            self.rsi_sell = min(max(float(self.rsi_sell), 1.0), 99.0)
        if self.strategy_name is not None:
            self.strategy_name = self.strategy_name.strip()[:60] or None
        for f in _RISK_FIELDS:
            v = getattr(self, f)
            if v is not None:
                lo, hi = _RISK_RANGES[f]
                setattr(self, f, max(lo, min(int(v), hi)))
        return self

    def effective(self) -> dict:
        """전략이 쓰는 효과 파라미터(qty·sma·rsi) = mode 프리셋 ← 명시 오버라이드(non-None) 머지.
        결과는 clamp 범위로 보정. mode 가 무효면 neutral 프리셋."""
        preset = MODE_PRESETS.get(self.mode if self.mode in MODES else "neutral")
        merged = {f: (getattr(self, f) if getattr(self, f) is not None else preset[f])
                  for f in _STRAT_FIELDS}
        p = Params(mode=self.mode, **merged).clamp()   # 머지값 최종 clamp(오버라이드·프리셋 공통 방어)
        return {f: getattr(p, f) for f in _STRAT_FIELDS}

    def effective_risk(self, env_defaults: dict) -> dict:
        """RiskGuard 가 쓸 최종 리스크 한도 = 콘솔 오버라이드(non-None) ← env_defaults(Config.risk).
        프리셋 없음(mode 무관) — effective() 의 리스크 필드 버전. 결과는 clamp 범위로 보정."""
        merged = {f: (getattr(self, f) if getattr(self, f) is not None else env_defaults[f])
                  for f in _RISK_FIELDS}
        p = Params(**merged).clamp()
        return {f: getattr(p, f) for f in _RISK_FIELDS}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        fields = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**fields)


def resolve_mode(params: dict, regime: dict | None, now: float) -> str:
    """효과 mode 결정(레짐 추종 + fail-safe, 설계 §4-3).
    follow_regime=False → params.mode(수동). True → 신선한(market:regime.computed_at 이
    now-REGIME_MAX_AGE 이후) 전역 레짐을 mode 로, 레짐 없음·stale·불명 → neutral(보수)."""
    manual = params.get("mode")
    manual = manual if manual in MODES else "neutral"
    if not params.get("follow_regime"):
        return manual
    if regime:
        try:
            fresh = (now - float(regime.get("computed_at", 0))) < REGIME_MAX_AGE
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            r = regime.get("regime")
            return r if r in MODES else "neutral"
    return "neutral"  # 추종이지만 신선한 레짐 없음 → 보수 fail-safe
