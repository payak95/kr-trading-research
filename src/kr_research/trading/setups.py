# 종목 일봉(OHLCV bars)에서 기술적 셋업을 불린 판정하는 순수 모듈 — 스크리닝 노드(tech.*) 카탈로그의 첫 구현
"""셋업 key = AlphaForge `tech.*` 노드 ID(ma_alignment/golden_cross/box_breakout/bollinger_squeeze/
rsi_filter/volume_surge). telegram-market-bot 차트 패널의 셋업과 **동일 어휘**(어휘 1:1 이식) —
노드 빌더 Phase 1(연구 레이어)의 스크리닝 카탈로그 토대. 순수·주문 무관(라이브 영향 0).

입력 bars = 시간순 [{open,high,low,close,volume,...}] (백테스트·일봉과 동일 형태).
스칼라 지표는 trading.indicators(sma/rsi) 재사용, 시계열(교차·밴드폭)이 필요한 셋업은 로컬 헬퍼로
동일 계산. 반환: {"items": [{key,label,tone,detail}, ...]} — 표시/주입용(정배열은 항상, 나머지는 활성 시).
추후 spec/노드가 entry 조건·후보 거름에 active key 를 사용.
"""
from kr_research.trading.indicators import rsi, sma

# 인식되는 셋업 key(스펙 `{"setup": key}` 검증용). compute_setups 가 내는 key 와 일치.
SETUP_KEYS = frozenset({
    "ma_alignment", "golden_cross", "box_breakout",
    "bollinger_squeeze", "rsi_filter", "volume_surge", "vcp",
})


def _fmt(v):
    """천단위 구분 + 불필요한 소수 0 제거(70000.0→'70,000')."""
    if v is None:
        return "—"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _sma_at(closes, n, end):
    """closes[:end+1] 의 SMA(n) — 인덱스 end 시점 값(부족하면 None). sma() 와 동일 정의."""
    return sma(closes[:end + 1], n)


def _percentile(sorted_vals, pct):
    """오름차순 리스트의 pct 백분위(선형 보간). 빈 리스트면 None."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


def _badge(key, label, tone, detail):
    return {"key": key, "label": label, "tone": tone, "detail": detail}


def ma_alignment(closes):
    """이평 정/역배열 — MA5>MA20>MA60(bull)/MA5<MA20<MA60(bear)/그 외 혼조. 세 MA 중 하나라도 없으면 None."""
    ma5, ma20, ma60 = sma(closes, 5), sma(closes, 20), sma(closes, 60)
    if ma5 is None or ma20 is None or ma60 is None:
        return None
    nums = f"MA5 {_fmt(ma5)} · MA20 {_fmt(ma20)} · MA60 {_fmt(ma60)}"
    if ma5 > ma20 > ma60:
        return _badge("ma_alignment", "정배열", "bull", nums)
    if ma5 < ma20 < ma60:
        return _badge("ma_alignment", "역배열", "bear", nums)
    return _badge("ma_alignment", "이평 혼조", "neutral", nums)


def golden_cross(closes, within=5):
    """MA5/MA20 최근 교차 — 최근 within 봉 이내 상향(골든)/하향(데드)만. 없거나 오래되면 None."""
    n = len(closes)
    last_cross = None  # (idx, kind)
    for i in range(1, n):
        s_prev, s_cur = _sma_at(closes, 5, i - 1), _sma_at(closes, 5, i)
        l_prev, l_cur = _sma_at(closes, 20, i - 1), _sma_at(closes, 20, i)
        if None in (s_prev, s_cur, l_prev, l_cur):
            continue
        prev, cur = s_prev - l_prev, s_cur - l_cur
        if prev <= 0 < cur:
            last_cross = (i, "golden")
        elif prev >= 0 > cur:
            last_cross = (i, "dead")
    if last_cross is None:
        return None
    idx, kind = last_cross
    days_ago = n - 1 - idx
    if days_ago > within:
        return None
    when = "직전 봉" if days_ago == 0 else f"{days_ago}봉 전"
    if kind == "golden":
        return _badge("golden_cross", "골든크로스", "bull", f"{when} MA5가 MA20 상향 돌파")
    return _badge("golden_cross", "데드크로스", "bear", f"{when} MA5가 MA20 하향 이탈")


def box_breakout(closes, highs, lows, period=20):
    """박스권 돌파 — 종가가 직전 period 봉 고점 위(상향)/저점 아래(하향). 그 외 None. highs/lows 없으면 종가 대체."""
    if len(closes) < period + 1:
        return None
    hi = highs or closes
    lo = lows or closes
    box_high = max(hi[-period - 1:-1])
    box_low = min(lo[-period - 1:-1])
    cur = closes[-1]
    if cur > box_high:
        return _badge("box_breakout", "박스 상향 돌파", "bull", f"{period}봉 고점 {_fmt(box_high)} 상향")
    if cur < box_low:
        return _badge("box_breakout", "박스 하향 이탈", "bear", f"{period}봉 저점 {_fmt(box_low)} 하향")
    return None


def bollinger_squeeze(closes, period=20, mult=2.0, lookback=60, pct=25):
    """볼린저 밴드폭 수축 — 현재 밴드폭이 최근 lookback 봉 하위 pct% 이하 + 중앙값 대비 ≤80%(실제 수축). 부족 None."""
    bw = []
    for end in range(period - 1, len(closes)):
        window = closes[end - period + 1:end + 1]
        mean = sum(window) / period
        sd = (sum((x - mean) ** 2 for x in window) / period) ** 0.5
        if mean:
            bw.append((mean + mult * sd - (mean - mult * sd)) / mean)  # 밴드폭/중앙 = 2·mult·sd/mean
    if len(bw) < 10:
        return None
    cur = bw[-1]
    recent = sorted(bw[-lookback:])
    thr = _percentile(recent, pct)
    median = _percentile(recent, 50)
    if thr is not None and cur <= thr and median and cur <= 0.8 * median:
        return _badge("bollinger_squeeze", "볼린저 스퀴즈", "warn",
                      f"밴드폭 {cur * 100:.1f}% (최근 {len(recent)}봉 하위 {pct}% 수축)")
    return None


def rsi_filter(closes, period=14, low=30, high=70):
    """RSI 극단 — RSI≤low(과매도)/RSI≥high(과매수)만 표시(중립 생략). 부족 None."""
    r = rsi(closes, period)
    if r is None:
        return None
    if r <= low:
        return _badge("rsi_filter", "RSI 과매도", "warn", f"RSI {r:.0f} ≤ {low}")
    if r >= high:
        return _badge("rsi_filter", "RSI 과매수", "warn", f"RSI {r:.0f} ≥ {high}")
    return None


def volume_surge(volumes, mult=2.0, period=20):
    """거래량 폭증 — 최신 봉 거래량이 직전 period 봉 평균의 mult 배 이상. 평균 0/부족 None."""
    vols = [float(v) for v in (volumes or []) if v is not None]
    if len(vols) < period + 1:
        return None
    avg = sum(vols[-period - 1:-1]) / period
    if avg <= 0:
        return None
    ratio = vols[-1] / avg
    if ratio >= mult:
        return _badge("volume_surge", "거래량 폭증", "warn", f"최근 {period}봉 평균比 {ratio:.1f}×")
    return None


def vcp(closes, highs=None, lows=None, lookback=60, min_contractions=3, tighten_ratio=0.7, near_high_pct=0.08):
    """VCP(변동성 수축 패턴, Minervini 개념의 단순화 버전) — 최근 lookback 구간을 min_contractions 개
    연속 동일길이 구간으로 나눠, 각 구간의 변동폭((구간 고가-구간 저가)/구간 고가)이 순서대로
    tighten_ratio 이하 비율로 줄어들고(수축 min_contractions-1 회 연속), 현재가가 lookback 전체 고점의
    near_high_pct 이내(돌파 준비 구간)면 활성. 정교한 스윙 고점/저점 탐지 대신 구간 분할 근사(단순·결정적).
    lookback 미만/구간 길이 부족(<2봉)/미형성이면 None."""
    n = len(closes)
    if n < lookback:
        return None
    window = closes[-lookback:]
    hi = [float(v) for v in (highs[-lookback:] if highs else window)]
    lo = [float(v) for v in (lows[-lookback:] if lows else window)]
    seg_len = lookback // min_contractions
    if seg_len < 2:
        return None
    ranges = []
    for i in range(min_contractions):
        start, end = i * seg_len, (i + 1) * seg_len
        seg_hi, seg_lo = max(hi[start:end]), min(lo[start:end])
        if seg_hi <= 0:
            return None
        ranges.append((seg_hi - seg_lo) / seg_hi)
    if any(ranges[i + 1] > ranges[i] * tighten_ratio for i in range(len(ranges) - 1)):
        return None  # 연속 수축이 아님(중간에 확대되거나 정체)
    peak = max(hi)
    cur = window[-1]
    if peak <= 0 or cur < peak * (1 - near_high_pct):
        return None  # 아직 고점 근처 아님(돌파 준비 구간 아님)
    return _badge("vcp", "VCP 패턴", "bull",
                  f"{min_contractions}구간 연속 수축, 고점 대비 {(1 - cur / peak) * 100:.1f}%")


def compute_setups(bars: list[dict]) -> dict:
    """일봉 bars → 기술적 셋업 뱃지 목록. 개별 셋업 실패는 격리(다른 셋업 영향 없음).
    반환: {"items": [{key,label,tone,detail}, ...]} — 표시 가치 있는 셋업만(정배열은 항상)."""
    closes = [float(b["close"]) for b in bars]
    highs = [float(b.get("high", b["close"])) for b in bars]
    lows = [float(b.get("low", b["close"])) for b in bars]
    volumes = [b.get("volume") for b in bars]
    items = []
    for fn, args in ((ma_alignment, (closes,)), (golden_cross, (closes,)),
                     (box_breakout, (closes, highs, lows)), (bollinger_squeeze, (closes,)),
                     (rsi_filter, (closes,)), (volume_surge, (volumes,)), (vcp, (closes, highs, lows))):
        try:
            b = fn(*args)
        except Exception:
            b = None
        if b:
            items.append(b)
    return {"items": items}


def active_keys(bars: list[dict]) -> set:
    """활성 셋업 key 집합(스크리닝/조건용 — UI 라벨 제외). 예: {'ma_alignment','volume_surge'}."""
    return {it["key"] for it in compute_setups(bars)["items"]}
