# 가격 시계열 지표 — 전략·백테스트 공용 순수 함수 (부수효과 없음)
"""series 는 시간순 종가 리스트. 데이터가 부족하면 None 반환(전략은 None 이면 판단 보류)."""


def sma(series: list[float], n: int) -> float | None:
    if n <= 0 or len(series) < n:
        return None
    return sum(series[-n:]) / n


def rsi(series: list[float], n: int = 14) -> float | None:
    """단순평균 RSI(베이스라인). 최근 n 개 변화량 기준. 상승만→100, 하락만→0."""
    if len(series) < n + 1:
        return None
    deltas = [series[i] - series[i - 1] for i in range(len(series) - n, len(series))]
    avg_gain = sum(d for d in deltas if d > 0) / n
    avg_loss = sum(-d for d in deltas if d < 0) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def ema(series: list[float], n: int) -> float | None:
    """지수이동평균 — 최근값 가중(계수 2/(n+1)). 앞 n 개 SMA 로 시드 후 순회. 부족하면 None."""
    series_ema = _ema_series(series, n)
    return series_ema[-1] if series_ema else None


def _ema_series(series: list[float], n: int) -> list[float]:
    """EMA 시계열(O(N)) — 인덱스 n-1..끝의 EMA 값. 부족하면 빈 리스트. macd 등 내부용."""
    if n <= 0 or len(series) < n:
        return []
    k = 2.0 / (n + 1)
    e = sum(series[:n]) / n
    out = [e]
    for x in series[n:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def macd(series: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD — EMA(fast)-EMA(slow)=line, line 의 EMA(signal)=signal, hist=line-signal.
    반환 {line, signal, hist}. fast<slow 필요, 데이터 부족(slow+signal-1 미만)이면 None."""
    if not (0 < fast < slow) or signal <= 0:
        return None
    if len(series) < slow + signal - 1:
        return None
    ef = _ema_series(series, fast)          # 인덱스 fast-1..
    es = _ema_series(series, slow)          # 인덱스 slow-1..
    ef = ef[slow - fast:]                    # es 와 동일 시작(slow-1)으로 정렬
    line_series = [a - b for a, b in zip(ef, es)]
    if len(line_series) < signal:
        return None
    sig = _ema_series(line_series, signal)[-1]
    line = line_series[-1]
    return {"line": line, "signal": sig, "hist": line - sig}


def rvol(bars: list[dict], period: int = 20) -> float | None:
    """상대거래량 — 최신 거래량 / 직전 period 봉 평균거래량. 부족·평균0이면 None."""
    period = int(period)
    vols = [float(b.get("volume") or 0) for b in bars]
    if period <= 0 or len(vols) < period + 1:
        return None
    avg = sum(vols[-period - 1:-1]) / period
    return vols[-1] / avg if avg > 0 else None


def channel(bars: list[dict], period: int = 20) -> dict | None:
    """도치안 채널 — 직전 period 봉 최고가/최저가(현재 봉 제외). 종가 > 상단=신고가 돌파.
    부족하면 None. 반환 {high, low}."""
    period = int(period)
    if period <= 0 or len(bars) < period + 1:
        return None
    highs = [float(b.get("high", b["close"])) for b in bars]
    lows = [float(b.get("low", b["close"])) for b in bars]
    return {"high": max(highs[-period - 1:-1]), "low": min(lows[-period - 1:-1])}


def atr(bars: list[dict], period: int = 14) -> float | None:
    """ATR(평균 True Range) — TR=max(고-저, |고-전종|, |저-전종|)의 period 평균. 변동성. 부족하면 None."""
    period = int(period)
    if period <= 0 or len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i].get("high", bars[i]["close"]))
        lo = float(bars[i].get("low", bars[i]["close"]))
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def atr_pct(bars: list[dict], period: int = 14) -> float | None:
    """ATR% = ATR / 최근 종가 × 100 — 원화 단위인 atr()은 종목마다 가격 스케일이 달라 조건 빌더에
    고정 임계값(예: "ATR>500")을 못 만든다(삼성전자 vs 저가주). 정규화하면 종목 무관 임계값이 가능
    (예: "ATR%<2%=저변동성", "ATR%>5%=고변동성"). 부족하거나 최근 종가<=0 이면 None."""
    a = atr(bars, period)
    if a is None or not bars:
        return None
    close = float(bars[-1].get("close") or 0)
    return (a / close) * 100 if close > 0 else None


def stochastic(bars: list[dict], k: int = 14, d: int = 3) -> dict | None:
    """스토캐스틱 %K/%D — %K=100·(종가-최저저)/(최고고-최저저), %D=%K 의 d 봉 SMA.
    입력 bars=[{high,low,close,...}]. 데이터 부족(k+d-1 미만)이면 None. 밴드폭 0 이면 %K=50.
    반환 {k, d}."""
    k, d = int(k), int(d)
    if k <= 0 or d <= 0 or len(bars) < k + d - 1:
        return None
    highs = [float(b.get("high", b["close"])) for b in bars]
    lows = [float(b.get("low", b["close"])) for b in bars]
    closes = [float(b["close"]) for b in bars]
    ks = []
    for end in range(k - 1, len(bars)):
        hh = max(highs[end - k + 1:end + 1])
        ll = min(lows[end - k + 1:end + 1])
        rng = hh - ll
        ks.append(50.0 if rng == 0 else 100.0 * (closes[end] - ll) / rng)
    return {"k": ks[-1], "d": sum(ks[-d:]) / d}


def bollinger(series: list[float], period: int = 20, mult: int = 2) -> dict | None:
    """볼린저밴드 — 중간=SMA(period), 상/하단=중간±mult·표준편차(모집단). 부족하면 None.
    반환 {upper, middle, lower}. mult 는 정수 배수(2·3σ 표준)."""
    period = int(period)
    if period <= 0 or len(series) < period:
        return None
    window = series[-period:]
    mid = sum(window) / period
    sd = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return {"upper": mid + mult * sd, "middle": mid, "lower": mid - mult * sd}


def roc(series: list[float], n: int) -> float | None:
    """모멘텀(Rate of Change) — n 봉 전 대비 변화율(%). 데이터 부족·기준 0 이면 None."""
    if n <= 0 or len(series) < n + 1:
        return None
    prev = series[-n - 1]
    if prev == 0:
        return None
    return (series[-1] / prev - 1) * 100.0


def price(series: list[float], n: int | None = None) -> float | None:
    """현재가 — 최신 종가(기간 무시). 돌파 비교('종가 > 이동평균')용 피연산자. 빈 시계열이면 None."""
    return series[-1] if series else None
