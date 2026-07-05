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
_MODEL = "gemini-flash-lite-latest"  # telegram-market-bot 과 동일(저비용) 모델로 통일
_PROMPT_VERSION = "v1"  # build_prompt() 문구가 바뀌면 올린다 — 저장 레코드에 심어 전진검증 통계가 프롬프트
                        # 버전 간에 섞이지 않게 구분 가능(과거 레코드엔 소급 불가하니 지금부터 기록)
_STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "shadow_judgments.jsonl")
_MAX_REDIS_RECORDS = 199  # LTRIM 0 199 — 최근 200건 보관(다른 status:logs 는 50, 판단 리뷰용이라 더 길게)
_ATTEMPTS = 3  # call_gemini 일시 오류(429/5xx) 재시도 횟수 — telegram-market-bot/tools/summarize.py 와 동일 패턴


def build_snapshot(bars: list[dict]) -> dict | None:
    """최근 일봉 → 지표 스냅샷(dict, JSON 직렬화 가능한 값만). 데이터 부족하면 None."""
    if len(bars) < 30:
        return None
    closes = [float(b["close"]) for b in bars]
    macd = ind.macd(closes)
    boll = ind.bollinger(closes)
    stoch = ind.stochastic(bars)
    return {
        "close": closes[-1],
        "sma5": ind.sma(closes, 5),
        "sma20": ind.sma(closes, 20),
        "sma60": ind.sma(closes, 60),
        "rsi14": ind.rsi(closes),
        "macd_hist": macd["hist"] if macd else None,
        "bollinger_pct": ((closes[-1] - boll["lower"]) / (boll["upper"] - boll["lower"])
                           if boll and boll["upper"] != boll["lower"] else None),
        "rvol20": ind.rvol(bars),
        "stoch_k": stoch["k"] if stoch else None,
        "roc5": ind.roc(closes, 5),
    }


def build_prompt(code: str, snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        f"너는 한국 주식 단기 트레이더야. 종목코드 {code}의 최근 지표 스냅샷(JSON)이야:\n{payload}\n\n"
        "이 지표만 근거로 매수/매도/보유 중 하나를 판단해. 다른 지식(뉴스·펀더멘털)은 쓰지 마. "
        '아래 JSON 형식으로만 답해(다른 텍스트 금지):\n'
        '{"action":"buy|sell|hold","confidence":0.0~1.0,"reason":"한국어 한 문장"}'
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
    return {"action": data["action"], "confidence": data.get("confidence"), "reason": data.get("reason", "")}


def call_gemini(prompt: str, api_key: str) -> dict | None:
    """일시 오류(429/5xx)는 지수 백오프로 최대 _ATTEMPTS 회 재시도, 비일시적 오류는 즉시 raise
    (telegram-market-bot/tools/summarize.py 와 동일 패턴, 모델은 하나뿐이라 폴백 없이 단순화)."""
    from google import genai
    client = genai.Client(api_key=api_key)  # 변수 보관 필수 — 인라인 호출 시 finalizer 가 httpx 클라이언트를 조기 종료(공유 메모리 known pitfall)
    last_exc = None
    for attempt in range(_ATTEMPTS):
        try:
            resp = client.models.generate_content(model=_MODEL, contents=prompt)
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


def judge_from_bars(code: str, bars: list[dict], api_key: str, last_trade_date: str | None = None) -> dict | None:
    """이미 조회된 일봉으로 지표 스냅샷→Gemini 판단(네트워크 I/O는 Gemini 호출뿐 — 봉 조회는 호출부 책임).
    run_once(실시간 조회)와 tools/ai_universe_scan.py(캐시된 봉 재사용) 가 공유하는 핵심 로직.
    반환 레코드에 forward_returns 계산에 필요한 entry_price·trade_date 포함. 데이터 부족·파싱 실패면 None.

    last_trade_date 를 주면(호출부가 직전 판단의 trade_date 전달) 최신 봉의 날짜가 그것과 같을 때
    **Gemini 호출 자체를 생략**하고 None 반환 — 일봉은 새 거래일이 오기 전엔 안 바뀌므로, 같은 스냅샷을
    반복해서 물으면 토큰 낭비 + 온도 샘플링 때문에 buy/sell 을 오락가락하는 노이즈만 생김."""
    snapshot = build_snapshot(bars)
    if snapshot is None:
        return None
    trade_date = bars[-1]["date"]
    if last_trade_date is not None and trade_date == last_trade_date:
        return None
    judgment = call_gemini(build_prompt(code, snapshot), api_key)
    if judgment is None:
        return None
    return {
        "ts": datetime.now(_KST).isoformat(),
        "code": code,
        "trade_date": trade_date,
        "entry_price": snapshot["close"],
        "snapshot": {**snapshot, "_prompt_version": _PROMPT_VERSION},  # 저장용에만 태깅(Gemini 프롬프트엔 안 보냄)
        **judgment,
    }


def run_once(code: str, lookback_days: int, api_key: str, last_trade_date: str | None = None,
             timeframe: str = "daily") -> dict | None:
    """일봉(기본, 네이버) 또는 분봉(timeframe="5m"/"30m"/"60m", tools/intraday_ohlcv.py)을 실시간
    조회한 뒤 judge_from_bars 로 판단 — 개별 종목 설정(ai_shadow_scheduler.py)용. 저장은 하지 않는다
    (호출부 책임). judge_from_bars 는 분봉이든 일봉이든 그대로(레코드의 date 필드가 12자/8자로 자연히
    구분되어 dedup·전진검증이 스키마 변경 없이 동작)."""
    if timeframe == "daily":
        bars = daily_ohlcv(code, count=lookback_days)
    else:
        from tools.intraday_ohlcv import resolve_and_fetch
        bars = resolve_and_fetch(code, timeframe)
    return judge_from_bars(code, bars, api_key, last_trade_date)


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
