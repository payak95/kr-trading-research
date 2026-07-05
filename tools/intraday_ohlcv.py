# Yahoo Finance v8 차트 API로 국내 종목 분봉(5분/30분/1시간) OHLCV 조회 — 연구용(섀도 판단 전용, KIS 분리)
"""tools/naver_ohlcv.py 의 분봉판. 네이버 fchart 분봉(timeframe=minute)은 실측 결과 1분봉·종가/거래량만
주고 시가·고가·저가가 전부 null 이라 못 쓴다(build_snapshot 의 스토캐스틱 계산에 고가/저가 필요). 대신
Yahoo v8 차트 JSON을 requests 로 직접 호출(yfinance/pandas 미사용 — 신규 의존성 없음, VPS Docker 이미지
재빌드 불필요, tools/naver_ohlcv.py 와 같은 경량 스타일).

한국 종목은 시장 접미사(.KS=코스피/.KQ=코스닥)가 필요한데 이 저장소는 종목별 시장 정보를 저장하지 않는다.
`.KS`로 프로브 겸 조회해서 meta.fullExchangeName 이 KOSDAQ 이면 `.KQ`로 재조회하는 결정적 방식으로 해결
(오접미사로 조회해도 Yahoo 의 meta 는 실제 시장을 알려줌, 실측 확인) — 코스피는 1콜, 코스닥은 2콜(첫 조회는
버림), 프로세스 수명 동안 접미사를 캐시해 이후 호출은 항상 1콜.

반환 형태는 tools/naver_ohlcv.daily_ohlcv 와 동일: [{date,open,high,low,close,volume}](시간순). date 는
"YYYYMMDDHHMM"(KST, 12자)로 일봉의 8자 date와 자연히 구분되며, ai_forward_eval.py 가 [:8]로 잘라 캘린더
날짜로 일봉 D+N 평가에 그대로 재사용한다.
"""
import random
import time
from datetime import datetime, timedelta, timezone

import requests

_KST = timezone(timedelta(hours=9))
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_TF = {"5m": ("5m", "5d"), "15m": ("15m", "5d"), "30m": ("30m", "1mo"), "60m": ("60m", "3mo"), "4h": ("4h", "3mo")}
# 지표 워밍업(sma60)에 충분한 봉 확보 — range 는 실측 확인(15m/5d≈121봉, 4h/3mo≈123봉, 전부 60 이상).
# Yahoo v8 interval 유효값 확인됨: 1m,2m,5m,15m,30m,60m,90m,1h,4h,1d,5d,1wk,1mo,3mo("240m" 등은 무효).
_ATTEMPTS = 3  # 일시 오류(429/403/5xx) 재시도 횟수 — tools/llm_shadow.call_gemini 와 동일 사내 패턴
_SUFFIX_CACHE: dict[str, str] = {}


def _yahoo_chart(ticker: str, interval: str, rng: str, timeout: int = 15) -> dict | None:
    """v8 차트 JSON 1회(재시도 포함) → chart.result[0](meta+timestamp+indicators) 반환. 일시 오류는
    지수 백오프로 최대 _ATTEMPTS 회 재시도, 소진·비일시 오류·빈 결과는 None(호출부 skip)."""
    last_exc: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            r = requests.get(_URL.format(ticker=ticker), headers=_HEADERS, timeout=timeout,
                             params={"interval": interval, "range": rng})
            if r.status_code in (429, 403) or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            result = ((r.json().get("chart") or {}).get("result")) or []
            return result[0] if result else None
        except Exception as e:
            last_exc = e
            if attempt < _ATTEMPTS - 1:
                time.sleep(min(1 * (2 ** attempt) + random.uniform(0, 1), 10))
    print(f"[intraday_ohlcv] {ticker} 조회 실패({_ATTEMPTS}회 재시도 소진): {last_exc}")
    return None


def _to_bars(result: dict) -> list[dict]:
    """chart.result[0] → [{date,open,high,low,close,volume}](시간순, close 결측 봉 드롭).
    timestamp 는 UTC epoch(초) — 반드시 명시적으로 KST 변환 후 포맷해야 한다(tz 없이 변환하면 실행 기기의
    로컬 타임존에 결과가 의존하게 되어 VPS(UTC)·로컬 개발기(KST)에서 값이 갈리는 버그가 된다)."""
    ts = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs, lows, closes, volumes = (quote.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
    out = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        dt = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(_KST)
        out.append({
            "date": dt.strftime("%Y%m%d%H%M"),
            "open": round(opens[i]) if i < len(opens) and opens[i] is not None else round(c),
            "high": round(highs[i]) if i < len(highs) and highs[i] is not None else round(c),
            "low": round(lows[i]) if i < len(lows) and lows[i] is not None else round(c),
            "close": round(c),
            "volume": int(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0,
        })
    return out


def resolve_and_fetch(code: str, timeframe: str) -> list[dict]:
    """code(6자리)·timeframe("5m"/"30m"/"60m") → 최근 분봉 OHLCV(시간순). 시장 접미사는 캐시돼 있으면
    그대로, 아니면 .KS 로 조회 겸 프로브(meta.fullExchangeName 이 KOSDAQ 이면 .KQ 로 재조회). 실패는
    [](호출부 skip, naver_ohlcv 와 동일 fail-safe). timeframe 이 알 수 없는 값이면 즉시 []."""
    tf = _TF.get(timeframe)
    if tf is None:
        return []
    interval, rng = tf

    suffix = _SUFFIX_CACHE.get(code)
    if suffix:
        result = _yahoo_chart(f"{code}{suffix}", interval, rng)
        return _to_bars(result) if result else []

    result = _yahoo_chart(f"{code}.KS", interval, rng)
    if result is None:
        return []  # 완전 실패 — 접미사 확정 못 함(캐시 안 함, 다음 호출에서 재시도)
    exch = (result.get("meta") or {}).get("fullExchangeName", "")
    if "KOSDAQ" in exch:
        _SUFFIX_CACHE[code] = ".KQ"
        time.sleep(0.5)  # 프로브 직후 연속 호출 — Yahoo rate limit 완화
        result = _yahoo_chart(f"{code}.KQ", interval, rng)
        if result is None:
            return []
    else:
        _SUFFIX_CACHE[code] = ".KS"
    return _to_bars(result)
