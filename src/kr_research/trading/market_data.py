# 실시간 피드 보관 + stale 판정 — 틱/폴링을 고정 시간격자(bar)로 샘플링해 전략 입력으로 제공
"""WebSocket 콜백/시세 폴링이 주는 정규화된 틱({code,time,price})을 받아 최근값 + 종가 히스토리를 유지한다.
**bar_seconds 고정 시간격자로 closes 를 샘플링**한다 — 한투(WS 고빈도 틱)와 토스(N초 폴링)가 틱 빈도와
무관하게 같은 시간기준 SMA/RSI 를 쓰도록(브로커 간 신호 비교 공정성). 한 bar 안의 추가 틱은 _last(현재가)만
갱신하고, bar 경계를 넘을 때 그 시점 가격 1개를 closes 에 적재한다(시간 균일 시계열).
연결 전/끊김이면 stale=True → strategy/risk 가 매매를 멈추도록(fail-safe).
context() 의 모양은 backtest 하니스가 만드는 ctx 와 동일(closes 포함) → 전략 코드 호환.

관심종목 일봉(OHLCV)은 여기서 직접 안 받는다 — main 의 주기 리프레시(B3, KIS daily_ohlcv)가
set_bars() 로 주입하는 캐시일 뿐(네트워크 호출은 이 클래스 책임 밖, "정규화된 데이터를 받아 보관"
역할 유지). context()["bars"] 로 노출 — 있으면 SpecStrategy 가 OHLCV 지표·차트신호를 활성화한다.
"""
import time
from collections import deque

BAR_SECONDS = 60.0  # closes 샘플 간격(초)=1분봉. 양 봇 동일해야 비교 공정 · 토스 POLL_INTERVAL ≤ 이 값(빈 bar 방지).
# 1분봉 선택: 전략(RSI+SMA)이 분/초 단위에선 비용에 churn(백테스트 확인) — 1분이 균형(활동 有·churn 완화).


class MarketData:
    def __init__(self, history: int = 300, bar_seconds: float = BAR_SECONDS):
        self._last: dict = {}        # code -> {time, price, recv}
        self._closes: dict = {}      # code -> deque[price] (bar 격자 샘플)
        self._bar: dict = {}         # code -> 마지막 적재 bar 인덱스(floor(now/bar_seconds))
        self._bars: dict = {}        # code -> 일봉 OHLCV 리스트(B3, main 의 주기 리프레시가 주입)
        self._history = history
        self._bar_seconds = bar_seconds
        self.stale: bool = True      # 첫 틱 전·끊김이면 True

    def on_raw_tick(self, tick: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        tick["recv"] = now
        code = tick["code"]
        self._last[code] = tick
        self.stale = False
        bar = int(now // self._bar_seconds)
        if self._bar.get(code) != bar:   # 새 bar 경계(또는 첫 틱) → 시간격자 샘플 1개 적재
            self._closes.setdefault(code, deque(maxlen=self._history)).append(tick["price"])
            self._bar[code] = bar
        return tick

    def mark_stale(self) -> None:
        self.stale = True

    def set_bars(self, code: str, bars: list[dict]) -> None:
        """관심종목 일봉(OHLCV) 캐시 갱신(B3) — main 의 주기 리프레시가 호출."""
        self._bars[code] = bars

    def context(self, code: str) -> dict:
        last = self._last.get(code)
        return {"stale": self.stale, "last": last,
                "price": last["price"] if last else None,
                "closes": list(self._closes.get(code, [])),
                "bars": self._bars.get(code)}
