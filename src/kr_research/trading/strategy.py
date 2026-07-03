# 매매 전략 — SMA 추세필터 + RSI 평균회귀 베이스라인 (롱 전용, 종목당 1포지션)
"""전략은 순수 함수에 가깝게: (시장 상태) → 의도. 부수효과(주문)는 executor 책임.
실거래 전 반드시 백테스트/페이퍼로 검증.

규칙(틱마다, closes 충분할 때):
  uptrend = SMA_fast > SMA_slow
  매수(보유X): uptrend AND RSI <= rsi_buy    # 상승추세의 과매도 눌림목
  매도(보유O): RSI >= rsi_sell OR not uptrend # 과매수 익절 또는 추세 이탈

파라미터는 ctx['params'](라이브 실시간 조정) > 생성자 기본값 순으로 적용하고, 항상 clamp(외부 입력 불신).
효과 파라미터(qty·sma·rsi)는 Params.effective() = **mode 프리셋 ← 명시 오버라이드 머지**(2A).
RSI 창은 14 고정(파라미터 아님; 대시보드는 임계값만 조정).

보유 상태(self._holding)는 **체결 확정**(resolve_order: 라이브 on_fill / 백테스트 체결)과
reconcile(sync_holdings)로만 갱신한다 — 주문 emit 만으로 보유로 치지 않는다(낙관적 set 금지).
주문을 내면 self._inflight 로 표시해 체결/거절이 확정되기 전까지 추가·반대 주문을 막는다
(중복 주문·빈손 매도 churn 방지). in-flight 는 체결 확정 또는 다음 reconcile 에서 해제된다.
매도는 확정 보유 중일 때만 낸다(빈손 매도 방지).

의도 예: {"side": "buy"|"sell", "code": "005930", "qty": 10, "price": None}  # price=None=시장가
"""
from dataclasses import dataclass

from kr_research.core.params import Params
from kr_research.trading.indicators import rsi, sma

RSI_PERIOD = 14  # RSI 계산 창(파라미터 아님)


@dataclass
class Intent:
    side: str          # "buy" | "sell"
    code: str
    qty: int
    price: int | None = None  # None = 시장가


class Strategy:
    def __init__(self, params: dict | None = None):
        # 생성자 기본값(백테스트용). 라이브는 ctx['params'] 가 매 틱 덮어씀.
        self._defaults = Params.from_dict(params or {}).clamp().to_dict()
        self._holding: dict[str, bool] = {}   # code -> 확정 보유(체결/reconcile 로만 갱신)
        self._inflight: dict[str, bool] = {}  # code -> 주문 emit 후 체결/거절 확정 전까지 True

    def resolve_order(self, code: str, side: str, filled: bool) -> None:
        """직전 주문의 결과 확정. 라이브 on_fill(체결) / 백테스트 체결 시 호출.
        체결되면 보유 상태를 체결 기준으로 갱신(매수=보유, 매도=청산). 결과와 무관하게
        in-flight 를 해제한다 — 거절(filled=False)이면 보유는 그대로 두어 빈손 매도를 막는다."""
        if filled:
            self._holding[code] = (side == "buy")
        self._inflight[code] = False

    def sync_holdings(self, held_codes) -> None:
        """reconcile 결과(실제 보유 종목)로 보유 플래그를 브로커 진실로 강제 동기화 — 드리프트 보정.
        동시에 in-flight 를 모두 해제한다: reconcile 이 진실을 확정했으므로 직전 의도 결과가 정해졌다
        (블로킹된 매수처럼 체결통보가 안 오는 주문의 in-flight 도 여기서 풀린다)."""
        held = set(held_codes)
        self._holding = {code: True for code in held}
        self._inflight.clear()

    def on_tick(self, tick: dict, ctx: dict) -> list[Intent]:
        """실시간 틱마다 호출. stale·데이터부족·in-flight 면 의도 없음(fail-safe)."""
        code = tick.get("code")
        if code is None or ctx.get("stale"):
            return []
        if self._inflight.get(code, False):
            return []  # 직전 주문의 체결/거절이 확정되기 전 — 중복·반대 주문 방지
        # 효과 파라미터 = mode 프리셋 ← 명시 오버라이드 머지(2A). qty·sma·rsi 는 eff 사용.
        eff = Params.from_dict({**self._defaults, **(ctx.get("params") or {})}).clamp().effective()
        closes = ctx.get("closes") or []
        f = sma(closes, eff["sma_fast"])
        s = sma(closes, eff["sma_slow"])
        r = rsi(closes, RSI_PERIOD)
        if f is None or s is None or r is None:
            return []  # 데이터 부족 → 판단 보류

        uptrend = f > s
        if not self._holding.get(code, False):
            if uptrend and r <= eff["rsi_buy"]:
                self._inflight[code] = True
                return [Intent(side="buy", code=code, qty=eff["qty"], price=None)]
        else:
            if r >= eff["rsi_sell"] or not uptrend:
                self._inflight[code] = True
                return [Intent(side="sell", code=code, qty=eff["qty"], price=None)]
        return []
