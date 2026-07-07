# 유니버스(약 300종목)를 하루 한 번 AI 로 훑어 타겟 후보를 찾는 야간 스캐너 — 무주문·섀도 전용
"""screen_universe.py 가 이미 워밍한 일봉 캐시(bot:backtest:ohlcv:{code}:{days})만 읽는다 — 네이버 추가
호출 0, 콜드미스(미워밍 종목)는 조용히 skip. 종목마다 tools/llm_shadow.judge_from_bars 로 Gemini 에게
판단시키고 core/ai_store.py(개별 종목 관찰과 같은 테이블, config_name=UNIVERSE_CONFIG_NAME 으로 구분)에
기록한다. 콘솔 "AI 테스트" 탭의 "① 유니버스 스크리닝" 단계가 읽는 bot:ai:universe:* 로 별도 발행 —
"② 타겟 종목 관찰"(개별 설정, bot:ai:judgments)과 절대 안 섞인다. 오늘의 매수 판단은
bot:ai:universe:shortlist 에 모으고, 그중 확신도 상위 AUTO_WATCH_LIMIT 종목은 ② 관찰 설정에 자동
등록한다(①→② 핸드오프, 2026-07-06 오너 승인 — auto_register_watch/expire_auto_watch 참고).
실행: python tools/ai_universe_scan.py. 크론: screen_universe.py(17:00 KST) 10분 뒤(캐시 워밍 대기).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore
from kr_research.core.holidays import is_trading_day
from tools.backtest_worker import DEFAULT_DAYS, UNIVERSE_KEY, _cache_only_fetch
from tools.llm_shadow import (_MODEL_STAGE2, _parent_permits, build_snapshot, is_notable, judge_from_bars,
                              llm_budget_exceeded, log_judgment)
from kr_research.trading.tracking import HORIZONS, summarize_actions, summarize_by_confidence

_KST = timezone(timedelta(hours=9))
K_JUDGMENTS = "bot:ai:universe:judgments"  # String(JSON list) — 콘솔 "① 유니버스 스크리닝" 결과 테이블
K_SUMMARY = "bot:ai:universe:summary"      # String(JSON) — trading.tracking.summarize_actions 출력(action별, sell 부호 반전)
K_SHORTLIST = "bot:ai:universe:shortlist"  # String(JSON {date,codes}) — 오늘 buy 판단만(다음 단계 준비, 미연결)
K_COMBO_CANDIDATES = "bot:ai:universe:combo_candidates"  # String(JSON {date,codes:[{code,close,sma20,rsi14}]})
# — ③ 콤보 관찰의 상위(일봉) 게이트(_parent_permits)를 통과 + 20일선이 60일선 위(중기 추세 구조 확인 —
# 그냥 오늘 우연히 20일선을 넘긴 게 아니라 실제 상승 추세인지 구분)인 종목만. is_notable(위 Gemini 대상
# 필터)과는 독립 조건이라 별도로 집계한다(무료 — Gemini 호출도 AiStore 기록도 없음, 매 스캔마다 재계산).
# 20일선 이격도(종가/20일선) 낮은 순으로 정렬 — "눌림목"(추세는 확인됐지만 아직 많이 안 오른) 우선순위.
# 등록 후 실제 진입 타이밍은 전적으로 하위(1시간봉) Gemini 판단에 맡기므로(judge_combo, 규칙 필터 없음),
# 이 순위는 "어떤 종목을 등록할지"만 돕는다 — 정밀 타점 선정이 아님.
PUBLISH_LIMIT = 300

# ── ①→② 자동 핸드오프(개선 로드맵 §A) — 숏리스트 상위 후보를 ② 타겟 종목 관찰에 자동 등록 ──
# ② 설정 Hash 는 콘솔·ai_shadow_scheduler.py 와 공유(리터럴 일치 필수 — 서로 import 안 하는 별도 저장소).
K_WATCH_CONFIGS = "bot:ai_configs"
K_WATCH_LAST_RUN = "bot:ai:last_run"
K_WATCH_STATUS = "bot:ai:status"
AUTO_WATCH_LIMIT = int(os.environ.get("AI_AUTO_WATCH_LIMIT", "5"))  # 스캔 1회당 자동 등록 상한(0=기능 끔)
AUTO_WATCH_EXPIRE_TDAYS = 5  # 등록 후 이 거래일 수가 지나고 열린 가상 포지션이 없으면 자동 만료
# 자동 등록 표식: cfg["auto_registered"]="YYYYMMDD"(등록일). 콘솔 백엔드(ai_configs_save)는 화이트리스트
# 필드만 재저장하므로 **사용자가 콘솔에서 그 설정을 한 번이라도 수정(토글 포함)하면 이 표식이 벗겨져
# "수동 입양"이 된다** — 입양된 설정은 자동 만료 대상에서 빠짐(의도된 규칙).


def _trading_days_since(reg_date: str, today: datetime) -> int:
    """reg_date(YYYYMMDD, 미포함) 다음날부터 today(포함)까지의 거래일 수. 파싱 불가면 0(만료 안 함)."""
    try:
        d = datetime.strptime(reg_date, "%Y%m%d").replace(tzinfo=_KST)
    except (TypeError, ValueError):
        return 0
    count, cur = 0, d + timedelta(days=1)
    while cur.date() <= today.date():
        if is_trading_day(cur):
            count += 1
        cur += timedelta(days=1)
    return count


def expire_auto_watch(r, store: AiStore, now: datetime | None = None) -> list[str]:
    """자동 등록(auto_registered) 설정 중 등록 후 AUTO_WATCH_EXPIRE_TDAYS 거래일이 지났고 열린 가상
    포지션이 없는 것을 제거(설정·last_run·status 함께 정리). 열린 포지션이 있으면 청산될 때까지 유지
    — 관찰 중간에 설정을 지우면 콘솔 뷰(publish_ai_view 의 활성 설정 필터)에서 그 이력이 사라지기 때문.
    반환=만료된 설정 이름들(로그용)."""
    now = now or datetime.now(_KST)
    expired = []
    for name, cfg_json in r.hgetall(K_WATCH_CONFIGS).items():
        try:
            cfg = json.loads(cfg_json)
        except (TypeError, ValueError):
            continue
        reg = cfg.get("auto_registered")
        if not reg or _trading_days_since(reg, now) < AUTO_WATCH_EXPIRE_TDAYS:
            continue
        if store.get_open_position(name, cfg.get("symbol", "")) is not None:
            continue
        r.hdel(K_WATCH_CONFIGS, name)
        r.hdel(K_WATCH_LAST_RUN, name)
        r.hdel(K_WATCH_STATUS, name)
        expired.append(name)
    return expired


def auto_register_watch(r, buys: list[dict], today: str) -> list[str]:
    """오늘 buy 판단(buys: [{code,confidence}]) 중 확신도 상위 AUTO_WATCH_LIMIT 종목을 ② 관찰 설정에
    자동 등록. 콘솔 UniverseScanPanel 의 수동 "+ 추가"와 동일 기본값(일봉·60분·lookback 120) +
    auto_registered 표식. HSETNX 라 기존 설정(수동·자동 무관)은 절대 덮어쓰지 않음 — 이미 있으면 다음
    후보로 넘어가지 않고 그냥 소진(상한은 '등록 시도'가 아니라 '신규 등록' 기준). 반환=등록된 이름들."""
    if AUTO_WATCH_LIMIT <= 0:
        return []
    added = []
    ranked = sorted(buys, key=lambda b: -(b.get("confidence") or 0.0))
    for b in ranked:
        if len(added) >= AUTO_WATCH_LIMIT:
            break
        name = f"{b['code']}_daily"
        cfg = {"symbol": b["code"], "timeframe": "daily", "lookback_days": 120, "interval_min": 60,
               "enabled": True, "min_confidence": None, "debate": False, "auto_registered": today}
        if r.hsetnx(K_WATCH_CONFIGS, name, json.dumps(cfg, ensure_ascii=False)):
            added.append(name)
    return added


def scan_universe(r, store: AiStore, codes: list[str], api_key: str, allow_llm: bool = True) -> dict:
    """유니버스 전체를 1회 스캔 — 캐시 콜드미스는 skip. 지표만으로 특이점 없는 종목(is_notable=False)은
    Gemini 호출 자체를 생략(파이썬 규칙 기반 사전 필터, API 비용 0)하고, 통과한 소수만 정밀 모델(_MODEL_STAGE2)
    로 판단시킨다 — 300종목 전부를 매번 정밀 모델로 돌리면 비용만 늘고, 대부분은 어차피 hold로 끝나는
    "심심한 날"이라 굳이 비싼 모델로 다시 볼 필요가 없다는 판단.
    allow_llm=False(일일 예산 초과, 로드맵 §E)면 Gemini 경로만 끄고 무료 계산(콤보 후보)은 그대로 수행.
    반환 {judged,skipped,filtered,shortlist,buys,candidates}(테스트/로그용) — buys 는 shortlist 와 같은
    종목에 confidence 를 얹은 것(자동 핸드오프 §A 의 확신도 상위 선별용, 발행 스키마는 shortlist 그대로)."""
    fetch = _cache_only_fetch(r)
    judged, skipped, filtered, shortlist, buys, candidates = 0, 0, 0, [], [], []
    for code in codes:
        bars = fetch(code, DEFAULT_DAYS)
        if not bars:
            skipped += 1
            continue
        snapshot = build_snapshot(bars)
        if snapshot is None:
            skipped += 1
            continue
        if _parent_permits(snapshot):  # ③ 콤보 상위 게이트 — is_notable 과 독립(순서가 그 앞이어야 함:
            sma60 = snapshot.get("sma60")  # 극단적이진 않지만 상승 추세인 종목도 잡아야 함)
            if sma60 is not None and snapshot["sma20"] > sma60:  # 중기 추세 구조 확인(우연한 크로스 배제)
                candidates.append({"code": code, "close": snapshot["close"],
                                    "sma20": snapshot["sma20"], "rsi14": snapshot["rsi14"]})
        if not is_notable(snapshot):
            filtered += 1
            continue
        if not allow_llm:
            skipped += 1
            continue
        try:
            record = judge_from_bars(code, bars, api_key,
                                      last_trade_date=store.last_trade_date(UNIVERSE_CONFIG_NAME, code),
                                      model=_MODEL_STAGE2)
        except Exception as e:
            print(f"[ai_universe_scan] {code} 실패: {e}")
            continue
        if record is None:
            continue
        store.record_judgment(UNIVERSE_CONFIG_NAME, record)
        log_judgment(record, redis_url="", tenant="ai_universe")  # JSONL 감사만
        judged += 1
        if record["action"] == "buy":
            shortlist.append(code)
            buys.append({"code": code, "confidence": record.get("confidence")})
    candidates.sort(key=lambda c: c["close"] / c["sma20"])  # 20일선 이격도 낮은 순(눌림목 우선)
    return {"judged": judged, "skipped": skipped, "filtered": filtered,
            "shortlist": shortlist, "buys": buys, "candidates": candidates}


def publish_universe_view(r, store: AiStore, shortlist: list[str]) -> None:
    """콘솔 '① 유니버스 스크리닝' 뷰 재발행 — 개별 종목 뷰(publish_ai_view)와 별개 키."""
    rows = store.get_judgments(None, config_name=UNIVERSE_CONFIG_NAME)
    summary = summarize_actions(rows, HORIZONS)
    summary["by_confidence"] = summarize_by_confidence(rows, HORIZONS)["by_mode"]
    r.set(K_JUDGMENTS, json.dumps(rows[:PUBLISH_LIMIT], ensure_ascii=False))
    r.set(K_SUMMARY, json.dumps(summary, ensure_ascii=False))
    r.set(K_SHORTLIST, json.dumps(
        {"date": datetime.now(_KST).strftime("%Y%m%d"), "codes": shortlist}, ensure_ascii=False))


def publish_combo_candidates(r, candidates: list[dict]) -> None:
    """③ 콤보 관찰의 상위(일봉) 게이트 통과 후보 재발행 — AiStore 를 안 거치는 순수 계산값(매 스캔 재생성).
    K_SHORTLIST 와 같은 {date,codes} 스키마."""
    r.set(K_COMBO_CANDIDATES, json.dumps(
        {"date": datetime.now(_KST).strftime("%Y%m%d"), "codes": candidates}, ensure_ascii=False))


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[ai_universe_scan] GEMINI_API_KEY 미설정 — 종료")
        return 1
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[ai_universe_scan] REDIS_URL 미설정 — 종료")
        return 1

    import redis
    r = redis.from_url(redis_url, decode_responses=True)
    codes = sorted(r.smembers(UNIVERSE_KEY))
    if not codes:
        print("[ai_universe_scan] 유니버스 비어있음(야간 워밍 전) — 종료")
        return 0

    store = AiStore()
    try:
        allow_llm = not llm_budget_exceeded(r)  # 예산 초과여도 무료 계산(콤보 후보)은 계속(§E)
        if not allow_llm:
            print("[ai_universe_scan] LLM 일일 호출 한도 초과 — Gemini 판단 스킵(무료 계산만 수행)")
        expired = expire_auto_watch(r, store)  # 먼저 만료(§A) — 오늘 등록분과 안 섞이게
        result = scan_universe(r, store, codes, api_key, allow_llm=allow_llm)
        publish_universe_view(r, store, result["shortlist"])
        publish_combo_candidates(r, result["candidates"])
        added = auto_register_watch(r, result["buys"], datetime.now(_KST).strftime("%Y%m%d"))
    finally:
        store.close()
    print(f"[ai_universe_scan] 유니버스={len(codes)} 판단={result['judged']} "
          f"스킵(캐시미스/중복)={result['skipped']} 사전필터제외={result['filtered']} "
          f"매수후보={len(result['shortlist'])} 콤보후보={len(result['candidates'])} "
          f"자동등록={added} 자동만료={expired}")
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("ai_universe_scan", main))
