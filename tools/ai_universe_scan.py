# 유니버스(약 300종목)를 하루 한 번 AI 로 훑어 타겟 후보를 찾는 야간 스캐너 — 무주문·섀도 전용
"""screen_universe.py 가 이미 워밍한 일봉 캐시(bot:backtest:ohlcv:{code}:{days})만 읽는다 — 네이버 추가
호출 0, 콜드미스(미워밍 종목)는 조용히 skip. 종목마다 tools/llm_shadow.judge_from_bars 로 Gemini 에게
판단시키고 core/ai_store.py(개별 종목 관찰과 같은 테이블, config_name=UNIVERSE_CONFIG_NAME 으로 구분)에
기록한다. 콘솔 "AI 테스트" 탭의 "① 유니버스 스크리닝" 단계가 읽는 bot:ai:universe:* 로 별도 발행 —
"② 타겟 종목 관찰"(개별 설정, bot:ai:judgments)과 절대 안 섞인다. 이 단계는 아직 ②로 자동 연결되지
않는다(오너 확인 후 다음 단계에서 연결) — 오늘의 매수 판단만 bot:ai:universe:shortlist 에 모아 둔다.
실행: python tools/ai_universe_scan.py. 크론: screen_universe.py(17:00 KST) 10분 뒤(캐시 워밍 대기).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore
from tools.backtest_worker import DEFAULT_DAYS, UNIVERSE_KEY, _cache_only_fetch
from tools.llm_shadow import judge_from_bars, log_judgment
from kr_research.trading.tracking import HORIZONS, summarize_actions

_KST = timezone(timedelta(hours=9))
K_JUDGMENTS = "bot:ai:universe:judgments"  # String(JSON list) — 콘솔 "① 유니버스 스크리닝" 결과 테이블
K_SUMMARY = "bot:ai:universe:summary"      # String(JSON) — trading.tracking.summarize_actions 출력(action별, sell 부호 반전)
K_SHORTLIST = "bot:ai:universe:shortlist"  # String(JSON {date,codes}) — 오늘 buy 판단만(다음 단계 준비, 미연결)
PUBLISH_LIMIT = 300


def scan_universe(r, store: AiStore, codes: list[str], api_key: str) -> dict:
    """유니버스 전체를 1회 스캔 — 캐시 콜드미스는 skip. 반환 {judged,skipped,shortlist}(테스트/로그용)."""
    fetch = _cache_only_fetch(r)
    judged, skipped, shortlist = 0, 0, []
    for code in codes:
        bars = fetch(code, DEFAULT_DAYS)
        if not bars:
            skipped += 1
            continue
        try:
            record = judge_from_bars(code, bars, api_key,
                                      last_trade_date=store.last_trade_date(UNIVERSE_CONFIG_NAME, code))
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
    return {"judged": judged, "skipped": skipped, "shortlist": shortlist}


def publish_universe_view(r, store: AiStore, shortlist: list[str]) -> None:
    """콘솔 '① 유니버스 스크리닝' 뷰 재발행 — 개별 종목 뷰(publish_ai_view)와 별개 키."""
    rows = store.get_judgments(None, config_name=UNIVERSE_CONFIG_NAME)
    summary = summarize_actions(rows, HORIZONS)
    r.set(K_JUDGMENTS, json.dumps(rows[:PUBLISH_LIMIT], ensure_ascii=False))
    r.set(K_SUMMARY, json.dumps(summary, ensure_ascii=False))
    r.set(K_SHORTLIST, json.dumps(
        {"date": datetime.now(_KST).strftime("%Y%m%d"), "codes": shortlist}, ensure_ascii=False))


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
        result = scan_universe(r, store, codes, api_key)
        publish_universe_view(r, store, result["shortlist"])
    finally:
        store.close()
    print(f"[ai_universe_scan] 유니버스={len(codes)} 판단={result['judged']} "
          f"스킵(캐시미스/중복)={result['skipped']} 매수후보={len(result['shortlist'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
