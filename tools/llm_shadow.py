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
from kr_research.trading.tracking import summarize_actions

_KST = timezone(timedelta(hours=9))
_MODEL = "gemini-flash-lite-latest"  # telegram-market-bot 과 동일(저비용) 모델로 통일 — 대량 스캔·개별 관찰 기본
# 유니버스 사전 필터(is_notable) 통과분만 정밀 판단용(tools/ai_universe_scan.py). 처음엔 gemini-pro-latest
# 를 썼는데 실측 결과 thinking 토큰(1,388개/호출)까지 출력 단가로 청구돼 월 예산(10,000원)을 크게 초과
# (약 67,500원/월 추정) — thinking_budget=0(call_gemini 참고)과 함께 이 모델로 낮춰 예산 안에 맞춤
# (실측 약 3,200원/월, flash-lite보다는 낫고 pro보다 훨씬 저렴).
_MODEL_STAGE2 = "gemini-3-flash-preview"
_PROMPT_VERSION = "v2"  # build_prompt() 문구가 바뀌면 올린다 — 저장 레코드에 심어 전진검증 통계가 프롬프트
                        # 버전 간에 섞이지 않게 구분 가능(과거 레코드엔 소급 불가하니 지금부터 기록)
                        # reflection(되먹임) 유무는 이 버전과 별개 — call 마다 달라지는 가변 데이터라 정적
                        # 버전으로 표현 불가, snapshot["_reflection_injected"] 플래그로 따로 추적한다.
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
# ③ 콤보 관찰(상위 프레임 필터) 임계값 — 종가가 20일 이동평균 위이고(대세 하락 아님) RSI 가 극단적
# 과매도(패닉)가 아니면 허가. AND 조건인 게 중요 — OR 이면 rsi>=20 은 거의 항상 참이라 사실상 항상
# 허가되는 무의미한 게이트가 됨(첫 설계 때 리뷰에서 발견해 AND 로 수정).
_PARENT_RSI_FLOOR = 20
# 불/베어 논쟁(debate) 트리거 구간 — 1차 판단의 확신도가 이 경계 안(애매한 수준)일 때만 2차 Gemini 호출로
# "정반대 입장에서 반박 후 재결정"을 강제한다. 확신 있음(>=HIGH)·사실상 관망(<LOW)은 1콜로 끝내 비용을
# 제한(TradingAgents 식 4~5 에이전트 체인과 달리 경계 구간에서만 1콜 추가). 오너 확인 후 실측 확신도
# 분포를 보고 조정 가능.
_DEBATE_LOW, _DEBATE_HIGH = 0.4, 0.65
_COMBO_PROMPT_VERSION = "combo_v1"  # build_combo_prompt 문구가 바뀌면 올린다(단일 프레임 _PROMPT_VERSION 과
                                    # 별개 네임스페이스 — 스키마 자체가 다름)

# ── LLM 호출 집계·일일 예산 가드(개선 로드맵 §E) ──
# call_gemini 가 과금 시점(generate_content 성공 응답)마다 Redis Hash 에 model별 카운트를 남기고,
# 각 스케줄러(②·③·유니버스 스캔)는 배치 시작 전에 llm_budget_exceeded() 로 오늘 총합이 한도를 넘었는지
# 확인한다 — 자동 핸드오프(§A)로 관찰 종목이 늘어도 폭주 비용을 하드캡. 한도는 "호출 수" 기준(토큰이
# 아니라) — 모델 단가가 고정된 저비용 2종만 쓰므로 호출 수가 곧 비용의 선형 근사(월 예산 10,000원 주석 참고).
K_LLM_CALLS_PREFIX = "ai:llm:calls:"        # + YYYYMMDD(KST) — Hash: model → 호출수. TTL 7일(집계·디버그용).
K_LLM_WARNED_PREFIX = "ai:llm:budget_warned:"  # + YYYYMMDD — 한도 도달 텔레그램 경고 1일 1회 SETNX 마커.
_LLM_DAILY_LIMIT = int(os.environ.get("AI_LLM_DAILY_CALL_LIMIT", "800"))  # 0 이하 = 가드 끔


def _llm_calls_key() -> str:
    return K_LLM_CALLS_PREFIX + datetime.now(_KST).strftime("%Y%m%d")


def record_llm_call(model: str) -> None:
    """Gemini 호출 1건 집계 — REDIS_URL 미설정(로컬 CLI 실험)이면 조용히 no-op. 집계 실패가 판단
    파이프라인을 죽이면 안 되므로 모든 예외를 삼킨다(과금은 이미 발생 — 집계는 관측용일 뿐)."""
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        return
    try:
        import redis
        r = redis.from_url(redis_url, decode_responses=True)
        key = _llm_calls_key()
        pipe = r.pipeline()
        pipe.hincrby(key, model, 1)
        pipe.expire(key, 7 * 86400)
        pipe.execute()
    except Exception:
        pass


def llm_calls_today(r) -> int:
    """오늘(KST) 총 Gemini 호출 수 — 모델별 카운트 합."""
    return sum(int(v) for v in r.hgetall(_llm_calls_key()).values())


def llm_budget_exceeded(r) -> bool:
    """일일 호출 한도(AI_LLM_DAILY_CALL_LIMIT, 기본 800) 초과 여부 + 도달 시 텔레그램 1회 경고.
    스케줄러가 배치 시작 전에 호출 — True 면 이번 배치의 Gemini 호출을 전부 스킵해야 한다."""
    if _LLM_DAILY_LIMIT <= 0:
        return False
    calls = llm_calls_today(r)
    if calls < _LLM_DAILY_LIMIT:
        return False
    warned_key = K_LLM_WARNED_PREFIX + datetime.now(_KST).strftime("%Y%m%d")
    try:
        if r.set(warned_key, "1", nx=True, ex=2 * 86400):
            from kr_research.bot.notify import Notifier
            Notifier(load_config()).send(
                f"⚠️ AI 섀도 LLM 일일 호출 한도 도달 ({calls}/{_LLM_DAILY_LIMIT}) — 오늘 남은 판단은 스킵합니다.")
    except Exception:
        pass  # 경고 실패가 가드 자체를 막지 않게
    return True


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


def _parent_permits(snapshot: dict) -> bool:
    """③ 콤보 관찰의 상위 프레임(필터) 게이트 — 대세가 하락 추세면 하위 프레임에서 아무리 좋은 신호가
    나와도 진입을 원천 차단. close>=sma20(대세 하락 아님) AND rsi14 가 극단적 과매도(<20, 패닉)가 아님 —
    반드시 AND(값이 없으면 판단 불가로 보수적으로 차단, False)."""
    close, sma20, rsi = snapshot.get("close"), snapshot.get("sma20"), snapshot.get("rsi14")
    if close is None or sma20 is None or rsi is None:
        return False
    return close >= sma20 and rsi >= _PARENT_RSI_FLOOR


def build_reflection_note(rows: list[dict], min_n: int = 5, horizon: int = 5) -> str | None:
    """과거 판단 이력(같은 config·종목으로 이미 스코프됨, 호출부 책임) → 다음 프롬프트에 넣을 한국어
    한 단락(없으면 None). hold 는 방향적 베팅이 아니라 제외(action∈{buy,sell}만) — hold 를 섞으면
    "판단이 맞았는가" 집계가 왜곡된다. `summarize_actions`(trading/tracking.py)를 그대로 재사용해
    sell 부호 반전을 얻는다 — 반전 없이 raw 집계하면 매도 위주 이력의 승률이 거꾸로 나온다.
    표본이 min_n 미만이면 None(초기엔 노이즈뿐이라 사실인 양 주입하면 오히려 판단을 왜곡시킴).
    문구는 사실·완충 표현만 담고 "그러니 매수/매도해"류 지시는 넣지 않는다(모델이 직접 재평가하게)."""
    directional = [r for r in rows if r.get("action") in ("buy", "sell")]
    if not directional:
        return None
    agg = summarize_actions(directional, horizons=(horizon,))["all"]
    n_eval = agg["signals"] - agg[f"pending_d{horizon}"]
    win, avg = agg.get(f"win_d{horizon}"), agg.get(f"avg_d{horizon}")
    if n_eval < min_n or win is None or avg is None:
        return None
    return (f"참고(과거 실적, 맹신 말 것): 이 종목 과거 평가 {n_eval}건 중 D+{horizon} 방향적중 "
            f"{win * 100:.0f}%, 평균 {avg * 100:+.1f}%.")


def build_prompt(code: str, snapshot: dict, reflection: str | None = None) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    note = f"{reflection}\n\n" if reflection else ""
    return (
        f"너는 한국 주식 단기 트레이더야. 종목코드 {code}의 최근 지표 스냅샷(JSON, *_history 는 오늘 포함 "
        f"최근 {_HISTORY_LEN}거래일 추이)이야:\n{payload}\n\n"
        f"{note}"
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


def build_combo_prompt(code: str, parent_label: str, parent_snapshot: dict,
                       child_label: str, child_snapshot: dict, reflection: str | None = None) -> str:
    """③ 콤보 관찰 전용 프롬프트 — 상위 프레임(대세 게이트 통과분)과 하위 프레임(필터 없음, 그대로) 스냅샷을
    함께 넘겨, 대세를 거스르지 않는 선에서 하위 프레임의 단기 타이밍을 Gemini 가 스스로 종합 판단하게
    한다. 하위 프레임엔 규칙 기반 사전 필터(is_notable)가 더 이상 없음 — 타이밍 판단 자체를 전적으로
    Gemini 에게 맡기기로 한 오너 결정(2026-07, judge_combo 참고). CoT 스키마는 build_prompt 와 동일."""
    parent_payload = json.dumps(parent_snapshot, ensure_ascii=False, separators=(",", ":"))
    child_payload = json.dumps(child_snapshot, ensure_ascii=False, separators=(",", ":"))
    note = f"{reflection}\n\n" if reflection else ""
    return (
        f"너는 한국 주식 단기 트레이더야. 종목코드 {code}의 상위 프레임({parent_label}, 대세 판단용)과 "
        f"하위 프레임({child_label}, 타이밍 판단용) 지표 스냅샷(JSON, *_history 는 각 프레임 기준 최근 "
        f"{_HISTORY_LEN}봉 추이)이야.\n"
        f"상위 프레임({parent_label}): {parent_payload}\n"
        f"하위 프레임({child_label}): {child_payload}\n\n"
        f"{note}"
        "상위 프레임에서 대세가 매수하기 나쁘지 않은 상태라는 건 이미 확인됐어. 하위 프레임에는 아무 "
        "필터도 걸려있지 않으니, 지금이 실제로 진입할 만한 타이밍인지 하위 프레임 지표를 보고 스스로 "
        "종합적으로 판단해. 다른 지식(뉴스·펀더멘털)은 쓰지 마. 결론을 바로 내지 말고 아래 순서로 먼저 "
        "분석한 뒤 최종 판단해:\n"
        "1) market_context_analysis: 하위 프레임 지표가 지금 진입할 만한 타이밍인지, 아니면 관망해야 "
        "하는 상황인지 한국어 한 문장\n"
        "2) counter_argument: 지금 판단을 내리기 전에 반박할 만한 리스크나 두 프레임 간에 모순되는 "
        "지표가 있다면 한국어 한 문장(없으면 \"없음\")\n"
        "3) 위 분석을 바탕으로 상위 프레임의 대세를 거스르지 않는 선에서 매수/매도/보유 중 하나를 "
        "최종 결정\n\n"
        '아래 JSON 형식으로만 답해(다른 텍스트 금지):\n'
        '{"market_context_analysis":"...","counter_argument":"...","action":"buy|sell|hold",'
        '"confidence":0.0~1.0,"reason":"한국어 한 문장"}'
    )


def build_debate_prompt(code: str, snapshot: dict, first: dict) -> str:
    """불/베어 논쟁(경계 확신도에서만 2차 호출, judge_from_bars/judge_combo 의 _maybe_debate 가 사용) —
    1차 판단을 제시하고 정반대 입장에서 최대한 설득력 있게 반박을 강제한 뒤, 두 관점을 종합해 최종
    action/confidence/reason 을 다시 정하게 한다. TradingAgents 의 강세/약세 리서처 논쟁 구조를
    참고하되, 별도 에이전트 없이 같은 모델에 1콜만 추가하는 압축 버전. hold 는 반대 입장 정의가
    모호해 대상 아님(호출부가 buy/sell 일 때만 부름)."""
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    opposite = "매도하거나 지금은 관망해야" if first["action"] == "buy" else "매수하거나 지금은 관망해야"
    return (
        f"종목코드 {code}의 지표 스냅샷(JSON)이야:\n{payload}\n\n"
        f"방금 1차 판단은 '{first['action']}'(확신도 {first.get('confidence')})이었고, 근거는 "
        f"'{first.get('reason', '')}' 였어. 이 확신도는 애매한 수준이라 재검토가 필요해.\n"
        f"지금부터 정반대 입장 — 지금 {opposite} 하는 이유 — 를 같은 지표로 최대한 설득력 있게 "
        "반박해봐. 그런 다음 원래 판단과 반박을 종합해 최종 action/confidence/reason 을 다시 정해.\n\n"
        '아래 JSON 형식으로만 답해(다른 텍스트 금지):\n'
        '{"debate_argument":"한국어 한두 문장(반박 논거)","action":"buy|sell|hold",'
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
        "debate_argument": data.get("debate_argument"),  # 1차 응답엔 없어 None — 2차(논쟁) 응답에만 채워짐
    }


def call_gemini(prompt: str, api_key: str, model: str = _MODEL) -> dict | None:
    """일시 오류(429/5xx)는 지수 백오프로 최대 _ATTEMPTS 회 재시도, 비일시적 오류는 즉시 raise
    (telegram-market-bot/tools/summarize.py 와 동일 패턴, 모델은 호출부가 고르되 폴백은 없음).
    model: 기본은 저비용 _MODEL, 유니버스 사전 필터 통과분만 _MODEL_STAGE2 로 정밀 판단(ai_universe_scan.py).

    thinking_budget=0 을 항상 명시 — 실측 결과 gemini-3-flash-preview/gemini-3.1-pro-preview 는 응답에
    안 보이는 thinking 토큰을 수백~천 단위로 태우고 이게 출력 단가로 청구돼(월 예산 10,000원 기준 초과의
    핵심 원인이었음). 0으로 끄면 CoT 응답 품질(market_context_analysis 등)은 그대로 나오면서 비용만 빠짐.
    flash-lite 는 원래 thinking 이 없는 모델이라 이 옵션을 줘도 안전(실측 확인, 에러 없음)."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key)  # 변수 보관 필수 — 인라인 호출 시 finalizer 가 httpx 클라이언트를 조기 종료(공유 메모리 known pitfall)
    config = types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0))
    last_exc = None
    for attempt in range(_ATTEMPTS):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            record_llm_call(model)  # 과금 시점(성공 응답) 집계 — 429 등 거절된 재시도는 과금 없음이라 제외
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


def _maybe_debate(code: str, snapshot: dict, judgment: dict, api_key: str, model: str, debate: bool) -> dict:
    """확신도가 경계 구간(_DEBATE_LOW~_DEBATE_HIGH)이고 방향적 액션(buy/sell)일 때만 2차 호출로 반박·
    재결정시킨다(hold 제외 — 반대 입장 정의가 모호 + 최빈 액션이라 비용만 증가). 2차가 실패(파싱 오류 등
    None)하면 이미 비용을 지불한 1차 판단을 그대로 유지(폐기 금지). market_context_analysis·
    counter_argument 는 1차 것을 그대로 두고, action/confidence/reason/debate_argument 만 2차로 덮어쓴다
    — judge_from_bars/judge_combo 공용(콤보는 snapshot 에 {"parent":...,"child":...} 중첩째로 넘김,
    build_debate_prompt 는 내용을 몰라도 되게 그대로 json.dumps 만 함)."""
    if not debate or judgment["action"] not in ("buy", "sell"):
        return judgment
    confidence = judgment.get("confidence")
    if confidence is None or not (_DEBATE_LOW <= confidence < _DEBATE_HIGH):
        return judgment
    try:
        second = call_gemini(build_debate_prompt(code, snapshot, judgment), api_key, model)
    except Exception:
        second = None
    if second is None:
        return judgment
    return {**judgment, "action": second["action"], "confidence": second.get("confidence"),
            "reason": second.get("reason", ""), "debate_argument": second.get("debate_argument")}


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
                     model: str = _MODEL, reflection: str | None = None, debate: bool = False) -> dict | None:
    """이미 조회된 일봉으로 지표 스냅샷→Gemini 판단(네트워크 I/O는 Gemini 호출뿐 — 봉 조회는 호출부 책임).
    run_once(실시간 조회)와 tools/ai_universe_scan.py(캐시된 봉 재사용) 가 공유하는 핵심 로직.
    반환 레코드에 forward_returns 계산에 필요한 entry_price·trade_date 포함. 데이터 부족·파싱 실패면 None.

    last_trade_date 를 주면(호출부가 직전 판단의 trade_date 전달) 최신 봉의 날짜가 그것과 같을 때
    **Gemini 호출 자체를 생략**하고 None 반환 — 일봉은 새 거래일이 오기 전엔 안 바뀌므로, 같은 스냅샷을
    반복해서 물으면 토큰 낭비 + 온도 샘플링 때문에 buy/sell 을 오락가락하는 노이즈만 생김.

    model 은 어느 모델이 판단했는지 레코드에 남겨(전진검증 통계를 모델별로 나눠볼 수 있게) 그대로 저장.
    reflection(build_reflection_note 결과, 호출부가 (config,code) 스코프로 만들어 전달)을 주면 프롬프트에
    실려 Gemini 에게 과거 실적을 참고시킨다 — 레코드 스키마 변경 없이 snapshot["_reflection_injected"]
    플래그로만 유무를 남긴다(A/B 비교용). debate=True 면 확신도가 경계 구간일 때만 _maybe_debate 로 2차
    호출(콘솔 설정별 옵트인, 기본 False — 기존 호출부·테스트는 이 인자를 안 주므로 1콜 그대로 유지)."""
    snapshot = build_snapshot(bars)
    if snapshot is None:
        return None
    trade_date = bars[-1]["date"]
    if last_trade_date is not None and trade_date == last_trade_date:
        return None
    judgment = call_gemini(build_prompt(code, snapshot, reflection), api_key, model)
    if judgment is None:
        return None
    judgment = _maybe_debate(code, snapshot, judgment, api_key, model, debate)
    return {
        "ts": datetime.now(_KST).isoformat(),
        "code": code,
        "trade_date": trade_date,
        "entry_price": snapshot["close"],
        "snapshot": {**snapshot, "_prompt_version": _PROMPT_VERSION,
                     "_reflection_injected": reflection is not None,
                     "_debated": judgment.get("debate_argument") is not None},  # 저장용 태깅(프롬프트엔 이미 포함)
        "model": model,
        **judgment,
    }


def fetch_bars(code: str, timeframe: str = "daily", lookback_days: int = 120) -> list[dict]:
    """일봉(기본, 네이버) 또는 분봉(timeframe="5m"/"15m"/"30m"/"60m"/"4h", tools/intraday_ohlcv.py) 조회 —
    run_once·tools/ai_combo_scheduler.py 공유(양쪽이 상위·하위 프레임을 각각 조회할 때 재사용). 일봉만
    lookback_days 를 쓰고, 분봉은 timeframe 별 고정 range(intraday_ohlcv._TF)라 이 인자를 무시한다."""
    if timeframe == "daily":
        return daily_ohlcv(code, count=lookback_days)
    from tools.intraday_ohlcv import resolve_and_fetch
    return resolve_and_fetch(code, timeframe)


def run_once(code: str, lookback_days: int, api_key: str, last_trade_date: str | None = None,
             timeframe: str = "daily", model: str = _MODEL, reflection: str | None = None,
             debate: bool = False) -> dict | None:
    """일봉(기본, 네이버) 또는 분봉(timeframe="5m"/"15m"/"30m"/"60m"/"4h", tools/intraday_ohlcv.py)을
    실시간 조회한 뒤 judge_from_bars 로 판단 — 개별 종목 설정(ai_shadow_scheduler.py)용. 저장은 하지 않는다
    (호출부 책임). judge_from_bars 는 분봉이든 일봉이든 그대로(레코드의 date 필드가 12자/8자로 자연히
    구분되어 dedup·전진검증이 스키마 변경 없이 동작). reflection·debate 는 judge_from_bars 로 그대로 전달."""
    bars = fetch_bars(code, timeframe, lookback_days)
    return judge_from_bars(code, bars, api_key, last_trade_date, model, reflection, debate)


def judge_combo(code: str, parent_timeframe: str, parent_bars: list[dict], child_timeframe: str,
                child_bars: list[dict], api_key: str, last_trade_date: str | None = None,
                model: str = _MODEL_STAGE2, reflection: str | None = None, debate: bool = False) -> dict | None:
    """③ 콤보 관찰 핵심 로직(tools/ai_combo_scheduler.py 전용) — 상위 프레임으로 대세 허가(_parent_permits)
    를 먼저 확인하고, 통과했을 때만 Gemini 를 호출(build_combo_prompt). 허가 안 되면 Gemini 호출 없이
    None(무료) — 대세를 거스르는 진입은 코드 차원에서 원천 차단하는 구조적 안전장치는 유지.

    하위 프레임엔 규칙 기반 사전 필터(is_notable)를 더 이상 안 건다 — 예전엔 상위·하위 둘 다 파이썬
    게이트를 통과해야 Gemini 를 불렀는데, 오너가 하위 프레임의 단기 타이밍 판단만큼은 규칙이 아니라
    Gemini 의 종합적 판단에 전적으로 맡기기로 결정(2026-07). 상위 게이트만 유지하는 이유는 "대세를
    거스르는 매매는 원천 차단"이라는 이 기능의 핵심 취지가 하위 게이트 제거와는 무관하기 때문.

    entry_price/trade_date 는 하위(자식) 프레임 기준으로 명시적으로 설정한다 — 중첩된 snapshot 딕셔너리엔
    최상위 "close" 가 없어서 judge_from_bars 처럼 자동으로는 못 뽑는다. 이래야 ai_forward_eval.py 가
    코드 변경 없이 그대로 D+N 평가에 재사용 가능(trade_date 로 dedup·forward_returns 계산에 씀).

    last_trade_date 는 하위 프레임의 최신 봉 날짜와 비교 — 하위 게이트는 없어졌어도 dedup 은 그대로
    유지(데이터 안정성 — 같은 봉을 반복해서 물으면 온도 샘플링 때문에 답이 오락가락하는 노이즈만 생김).

    debate=True 면 judge_from_bars 와 동일하게 확신도 경계 구간에서만 _maybe_debate 로 2차 호출(콤보
    설정별 옵트인, 기본 False)."""
    parent_snapshot = build_snapshot(parent_bars)
    if parent_snapshot is None or not _parent_permits(parent_snapshot):
        return None
    child_snapshot = build_snapshot(child_bars)
    if child_snapshot is None:
        return None
    trade_date = child_bars[-1]["date"]
    if last_trade_date is not None and trade_date == last_trade_date:
        return None
    judgment = call_gemini(
        build_combo_prompt(code, parent_timeframe, parent_snapshot, child_timeframe, child_snapshot, reflection),
        api_key, model)
    if judgment is None:
        return None
    combo_snapshot = {"parent": parent_snapshot, "child": child_snapshot}
    judgment = _maybe_debate(code, combo_snapshot, judgment, api_key, model, debate)
    return {
        "ts": datetime.now(_KST).isoformat(),
        "code": code,
        "trade_date": trade_date,
        "entry_price": child_snapshot["close"],
        "snapshot": {**combo_snapshot, "_prompt_version": _COMBO_PROMPT_VERSION,
                     "_reflection_injected": reflection is not None,
                     "_debated": judgment.get("debate_argument") is not None},
        "model": model,
        **judgment,
    }


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

    log_judgment(record, cfg.redis_url, tenant="ai")  # ai_shadow_scheduler.py 와 동일 관례(Config 에 tenant 필드 없음)
    print(f"[llm_shadow] {code} → {record['action']} (확신도 {record['confidence']}) — {record['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
