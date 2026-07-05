# 지표만 보고 Gemini 가 매수/매도/보유를 판단하는 섀도(관찰) 프로토타입 — 주문 전혀 안 함
"""라이브/페이퍼 매매 루프와 완전히 분리된 독립 스크립트. 최근 일봉→지표 스냅샷을 만들어 Gemini 에
JSON 으로 넘기고, 돌아온 판단(action·confidence·reason)을 그대로 기록만 한다. 실제 주문 경로(core/
kis_client 등)는 전혀 import 하지 않음 — 판단 품질을 먼저 눈으로 검증한 뒤에야 실제 매매 연동을 논의.
실행: python tools/llm_shadow.py [종목코드](기본 005930)
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.config import load_config
from tools.naver_ohlcv import daily_ohlcv
from kr_research.trading import indicators as ind

_KST = timezone(timedelta(hours=9))
_MODEL = "gemini-flash-lite-latest"  # telegram-market-bot 과 동일(저비용) 모델로 통일 — 대량 스캔·개별 관찰 기본
_MODEL_PRO = "gemini-pro-latest"  # 유니버스 사전 필터(is_notable) 통과분만 정밀 판단용(tools/ai_universe_scan.py)
_PROMPT_VERSION = "v2"  # build_prompt() 문구가 바뀌면 올린다 — 저장 레코드에 심어 전진검증 통계가 프롬프트
                        # 버전 간에 섞이지 않게 구분 가능(과거 레코드엔 소급 불가하니 지금부터 기록)
_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "shadow_judgments.jsonl")
_MAX_REDIS_RECORDS = 199  # LTRIM 0 199 — 최근 200건 보관(다른 status:logs 는 50, 판단 리뷰용이라 더 길게)
_ATTEMPTS = 3  # call_gemini 일시 오류(429/5xx) 재시도 횟수 — telegram-market-bot/tools/summarize.py 와 동일 패턴
_HISTORY_LEN = 4  # 지표 히스토리 길이(오늘 포함 최근 4거래일) — 단일 시점 숫자만으론 방향성(상승/하락 중)을
                  # 못 봐서 오판이 잦다는 피드백 반영. 최소 소요 봉수(bollinger period 20+3)는 build_snapshot
                  # 의 30봉 하한 안에 이미 들어옴.
# 유니버스 사전 필터(is_notable) 임계값 — 과매도/과매수·거래량 급증·밴드 이탈 중 하나라도 해당하면 특이점.
_NOTABLE_RSI_LOW, _NOTABLE_RSI_HIGH = 30, 70
_NOTABLE_RVOL_MIN = 2.0
_NOTABLE_BOLLINGER_LOW, _NOTABLE_BOLLINGER_HIGH = 0.05, 0.95


def _bollinger_pct(closes: list[float]) -> float | None:
    boll = ind.bollinger(closes)
    return ((closes[-1] - boll["lower"]) / (boll["upper"] - boll["lower"])
            if boll and boll["upper"] != boll["lower"] else None)


def _series_history(data: list, fn, n: int = _HISTORY_LEN) -> list:
    """data 를 뒤에서부터 한 칸씩 잘라 fn 을 반복 호출 — [n-1일 전, ..., 어제, 오늘] 순서로 히스토리 반환.
    데이터가 부족한 시점은 fn 이 알아서 None 을 낸다(각 지표 함수의 기존 하한 그대로)."""
    return [fn(data[: len(data) - i] if i else data) for i in range(n - 1, -1, -1)]


def build_snapshot(bars: list[dict]) -> dict | None:
    """최근 일봉 → 지표 스냅샷(dict, JSON 직렬화 가능한 값만). 데이터 부족하면 None."""
    if len(bars) < 30:
        return None
    closes = [float(b["close"]) for b in bars]
    macd = ind.macd(closes)
    stoch = ind.stochastic(bars)
    return {
        "close": closes[-1],
        "sma5": ind.sma(closes, 5),
        "sma20": ind.sma(closes, 20),
        "sma60": ind.sma(closes, 60),
        "rsi14": ind.rsi(closes),
        "macd_hist": macd["hist"] if macd else None,
        "bollinger_pct": _bollinger_pct(closes),
        "rvol20": ind.rvol(bars),
        "stoch_k": stoch["k"] if stoch else None,
        "roc5": ind.roc(closes, 5),
        # 단일 시점 숫자만으론 방향성(상승 중/하락 중)을 못 봐서 오판이 잦다는 피드백 반영 — 최근 며칠 추이.
        "rsi14_history": _series_history(closes, ind.rsi),
        "rvol20_history": _series_history(bars, ind.rvol),
        "bollinger_pct_history": _series_history(closes, _bollinger_pct),
    }


def is_notable(snapshot: dict) -> bool:
    """유니버스 사전 필터(Stage1, tools/ai_universe_scan.py 전용) — 과매도/과매수·거래량 급증·볼린저 밴드
    이탈 근접 중 하나라도 해당하면 True. API 호출 없는 순수 파이썬 규칙이라 무료·즉시(Gemini 를 또 불러
    필터링하면 비용·지연·파싱 실패 지점만 늘고 이득이 없음 — 이미 계산된 지표로 충분)."""
    rsi = snapshot.get("rsi14")
    if rsi is not None and (rsi <= _NOTABLE_RSI_LOW or rsi >= _NOTABLE_RSI_HIGH):
        return True
    rvol = snapshot.get("rvol20")
    if rvol is not None and rvol >= _NOTABLE_RVOL_MIN:
        return True
    bpct = snapshot.get("bollinger_pct")
    if bpct is not None and (bpct <= _NOTABLE_BOLLINGER_LOW or bpct >= _NOTABLE_BOLLINGER_HIGH):
        return True
    return False


def build_prompt(code: str, snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        f"너는 한국 주식 단기 트레이더야. 종목코드 {code}의 최근 지표 스냅샷(JSON, *_history 는 오늘 포함 "
        f"최근 {_HISTORY_LEN}거래일 추이)이야:\n{payload}\n\n"
        "이 지표만 근거로 판단해. 다른 지식(뉴스·펀더멘털)은 쓰지 마. 결론을 바로 내지 말고 아래 순서로 "
        "먼저 분석한 뒤 최종 판단해:\n"
        "1) market_context_analysis: 지표들의 추이가 상승/하락 중 어느 쪽에 더 힘을 싣는지 한국어 한 문장\n"
        "2) counter_argument: 지금 판단을 내리기 전에 반박할 만한 리스크나 서로 모순되는 지표가 있다면 "
        "한국어 한 문장(없으면 \"없음\")\n"
        "3) 위 분석을 바탕으로 매수/매도/보유 중 하나를 최종 결정\n\n"
        '아래 JSON 형식으로만 답해(다른 텍스트 금지):\n'
        '{"market_context_analysis":"...","counter_argument":"...","action":"buy|sell|hold",'
        '"confidence":0.0~1.0,"reason":"한국어 한 문장"}'
    )


def parse_judgment(text: str) -> dict | None:
    """Gemini 응답에서 JSON 판단 추출 — 마크다운 코드펜스(```json ... ```)가 섞여도 파싱."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if data.get("action") not in ("buy", "sell", "hold"):
        return None
    return {
        "action": data["action"], "confidence": data.get("confidence"), "reason": data.get("reason", ""),
        "market_context_analysis": data.get("market_context_analysis", ""),
        "counter_argument": data.get("counter_argument", ""),
    }


def call_gemini(prompt: str, api_key: str, model: str = _MODEL) -> dict | None:
    """일시 오류(429/5xx)는 지수 백오프로 최대 _ATTEMPTS 회 재시도, 비일시적 오류는 즉시 raise
    (telegram-market-bot/tools/summarize.py 와 동일 패턴, 모델은 호출부가 고르되 폴백은 없음).
    model: 기본은 저비용 _MODEL, 유니버스 사전 필터 통과분만 _MODEL_PRO 로 정밀 판단(ai_universe_scan.py)."""
    from google import genai
    client = genai.Client(api_key=api_key)  # 변수 보관 필수 — 인라인 호출 시 finalizer 가 httpx 클라이언트를 조기 종료(공유 메모리 known pitfall)
    last_exc = None
    for attempt in range(_ATTEMPTS):
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            return parse_judgment((resp.text or "").strip())
        except Exception as exc:
            last_exc = exc
            transient = "429" in str(exc) or "503" in str(exc) or "500" in str(exc)
            if not transient:
                raise
            if attempt < _ATTEMPTS - 1:
                wait = min(3 * (2 ** attempt) + random.uniform(0, 2), 30)
                print(f"[llm_shadow] Gemini 일시 오류({type(exc).__name__}) — {wait:.1f}초 후 재시도 ({attempt + 1}/{_ATTEMPTS})")
                time.sleep(wait)
    raise last_exc


def log_judgment(record: dict, redis_url: str, tenant: str) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if not redis_url:
        return
    try:
        import redis
        r = redis.from_url(redis_url, decode_responses=True)
        key = f"bot:{tenant}:shadow:judgments"
        r.lpush(key, json.dumps(record, ensure_ascii=False))
        r.ltrim(key, 0, _MAX_REDIS_RECORDS)
    except Exception as e:
        print(f"[llm_shadow] Redis 기록 실패(파일 기록은 완료): {e}")


def judge_from_bars(code: str, bars: list[dict], api_key: str, last_trade_date: str | None = None,
                     model: str = _MODEL) -> dict | None:
    """이미 조회된 일봉으로 지표 스냅샷→Gemini 판단(네트워크 I/O는 Gemini 호출뿐 — 봉 조회는 호출부 책임).
    run_once(실시간 조회)와 tools/ai_universe_scan.py(캐시된 봉 재사용) 가 공유하는 핵심 로직.
    반환 레코드에 forward_returns 계산에 필요한 entry_price·trade_date 포함. 데이터 부족·파싱 실패면 None.

    last_trade_date 를 주면(호출부가 직전 판단의 trade_date 전달) 최신 봉의 날짜가 그것과 같을 때
    **Gemini 호출 자체를 생략**하고 None 반환 — 일봉은 새 거래일이 오기 전엔 안 바뀌므로, 같은 스냅샷을
    반복해서 물으면 토큰 낭비 + 온도 샘플링 때문에 buy/sell 을 오락가락하는 노이즈만 생김.

    model 은 어느 모델이 판단했는지 레코드에 남겨(전진검증 통계를 모델별로 나눠볼 수 있게) 그대로 저장."""
    snapshot = build_snapshot(bars)
    if snapshot is None:
        return None
    trade_date = bars[-1]["date"]
    if last_trade_date is not None and trade_date == last_trade_date:
        return None
    judgment = call_gemini(build_prompt(code, snapshot), api_key, model)
    if judgment is None:
        return None
    return {
        "ts": datetime.now(_KST).isoformat(),
        "code": code,
        "trade_date": trade_date,
        "entry_price": snapshot["close"],
        "snapshot": {**snapshot, "_prompt_version": _PROMPT_VERSION},  # 저장용에만 태깅(Gemini 프롬프트엔 안 보냄)
        "model": model,
        **judgment,
    }


def run_once(code: str, lookback_days: int, api_key: str, last_trade_date: str | None = None,
             timeframe: str = "daily", model: str = _MODEL) -> dict | None:
    """일봉(기본, 네이버) 또는 분봉(timeframe="5m"/"30m"/"60m", tools/intraday_ohlcv.py)을 실시간
    조회한 뒤 judge_from_bars 로 판단 — 개별 종목 설정(ai_shadow_scheduler.py)용. 저장은 하지 않는다
    (호출부 책임). judge_from_bars 는 분봉이든 일봉이든 그대로(레코드의 date 필드가 12자/8자로 자연히
    구분되어 dedup·전진검증이 스키마 변경 없이 동작)."""
    if timeframe == "daily":
        bars = daily_ohlcv(code, count=lookback_days)
    else:
        from tools.intraday_ohlcv import resolve_and_fetch
        bars = resolve_and_fetch(code, timeframe)
    return judge_from_bars(code, bars, api_key, last_trade_date, model)


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[llm_shadow] GEMINI_API_KEY 미설정 — .env 확인")
        return 1

    cfg = load_config()
    record = run_once(code, lookback_days=120, api_key=api_key)
    if record is None:
        print(f"[llm_shadow] {code} 일봉 부족 또는 Gemini 응답 파싱 실패 — skip")
        return 1

    log_judgment(record, cfg.redis_url, cfg.tenant)
    print(f"[llm_shadow] {code} → {record['action']} (확신도 {record['confidence']}) — {record['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
