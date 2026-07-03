# AI 섀도 판단 스케줄러 — bot:ai_configs 를 읽어 주기 도래(due)한 설정만 Gemini 호출·기록(cron */5분)
"""콘솔이 CRUD 하는 bot:ai_configs(Hash: name→{symbol,timeframe,lookback_days,interval_min,enabled})를
읽어, 활성화됐고 interval_min 이 지난 설정만 tools/llm_shadow.run_once 로 판단시킨다. 동일 거래일(trade_date)
재호출은 run_once 자체가 스킵(Gemini 비용·환각 방지). 배치 실패는 설정별로 격리(하나가 죽어도 나머지 진행)
하고 bot:ai:status 에 성공/에러를 남겨 콘솔이 뱃지로 보여줄 수 있게 한다. 무주문·연구 전용 — 실제 주문은
전혀 안 나가지만, 판단마다 core/ai_store.py::decide_virtual_trade 로 가상(무자본) 포지션을 갱신해 "이
판단을 실제로 따라갔다면 어떻게 됐을지"를 회계 시뮬레이션으로 관찰할 수 있게 한다(bot:ai:positions).
실행: python tools/ai_shadow_scheduler.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore, decide_virtual_trade
from tools.llm_shadow import log_judgment, run_once
from kr_research.trading.tracking import HORIZONS, summarize_actions

K_CONFIGS = "bot:ai_configs"     # Hash(콘솔 CRUD): name → {symbol,timeframe,lookback_days,interval_min,enabled}
K_LAST_RUN = "bot:ai:last_run"   # Hash: name → epoch(마지막 시도 시각, 성공/실패 무관 — 재시도 폭주 방지)
K_STATUS = "bot:ai:status"       # Hash: name → {ts,ok,error?}(콘솔 에러 뱃지)
K_JUDGMENTS = "bot:ai:judgments"  # String(JSON list) — 콘솔 결과 테이블
K_SUMMARY = "bot:ai:summary"      # String(JSON) — trading.tracking.summarize_actions 출력(sell 부호 반전)
K_POSITIONS = "bot:ai:positions"  # String(JSON list) — 가상 포지션 원장(core/ai_store.py::decide_virtual_trade)
PUBLISH_LIMIT = 200


def _due(cfg: dict, last_run: float | None) -> bool:
    interval_min = max(int(cfg.get("interval_min") or 60), 1)
    return last_run is None or time.time() >= last_run + interval_min * 60


def publish_ai_view(r, store: AiStore) -> None:
    """콘솔 '개별 종목 관찰' 뷰(bot:ai:judgments/summary/positions) 재발행 — 스케줄러 배치 뒤·전진검증
    (ai_forward_eval.py) 뒤 공용. 유니버스 스캔(UNIVERSE_CONFIG_NAME, tools/ai_universe_scan.py 가
    별도 발행)은 제외 — 두 화면이 섞이지 않게. LIMIT 을 걸기 전에 먼저 걸러야 한다(안 그러면 유니버스
    스캔 300건이 최신순 상위를 차지해 개별 종목 판단이 밀려날 수 있음)."""
    rows = [row for row in store.get_judgments(None) if row["config_name"] != UNIVERSE_CONFIG_NAME]
    summary = summarize_actions(rows, HORIZONS)
    r.set(K_JUDGMENTS, json.dumps(rows[:PUBLISH_LIMIT], ensure_ascii=False))
    r.set(K_SUMMARY, json.dumps(summary, ensure_ascii=False))
    positions = [row for row in store.get_positions() if row["config_name"] != UNIVERSE_CONFIG_NAME]
    r.set(K_POSITIONS, json.dumps(positions[:PUBLISH_LIMIT], ensure_ascii=False))


def run_scheduler(r, store: AiStore, api_key: str) -> int:
    """1회 배치. due+enabled 설정만 처리, 나머지는 건너뜀. 반환=이번 배치에서 새로 기록된 판단 건수."""
    configs = r.hgetall(K_CONFIGS)
    last_runs = r.hgetall(K_LAST_RUN)
    recorded = 0
    for name, cfg_json in configs.items():
        try:
            cfg = json.loads(cfg_json)
        except (TypeError, ValueError):
            continue
        if not cfg.get("enabled"):
            continue
        last_run = float(last_runs[name]) if name in last_runs else None
        if not _due(cfg, last_run):
            continue
        code = cfg.get("symbol", "")
        try:
            record = run_once(code, lookback_days=int(cfg.get("lookback_days") or 120), api_key=api_key,
                               last_trade_date=store.last_trade_date(name, code),
                               timeframe=cfg.get("timeframe") or "daily")
            if record is not None:
                store.record_judgment(name, record)
                log_judgment(record, redis_url="", tenant="ai")  # JSONL 감사만(Redis 발행은 publish_ai_view 가 별도로)
                # 가상 포지션 시뮬레이터(섀도 — 실제 주문 없음) — trading/strategy.py 의 보유 게이트와 동일 규칙.
                open_pos = store.get_open_position(name, code)
                trade = decide_virtual_trade(open_pos, record["action"], record["entry_price"])
                if trade and trade["kind"] == "open":
                    store.open_position(name, code, trade["qty"], record["entry_price"], record["trade_date"])
                elif trade and trade["kind"] == "close":
                    store.close_position(open_pos["id"], record["entry_price"], record["trade_date"],
                                         trade["realized_pnl"], trade["realized_return_pct"])
                recorded += 1
            r.hset(K_STATUS, name, json.dumps({"ts": time.time(), "ok": True}, ensure_ascii=False))
        except Exception as e:
            print(f"[ai_shadow_scheduler] {name}({code}) 실패: {e}")
            r.hset(K_STATUS, name, json.dumps({"ts": time.time(), "ok": False, "error": str(e)[:200]}, ensure_ascii=False))
        finally:
            # 성공/실패/스킵 무관하게 last_run 갱신 — 실패해도 매 5분 재시도로 폭주하지 않고 interval_min 만큼 물러남.
            r.hset(K_LAST_RUN, name, time.time())
    if configs:
        publish_ai_view(r, store)
    return recorded


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[ai_shadow_scheduler] GEMINI_API_KEY 미설정 — 종료")
        return 1
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[ai_shadow_scheduler] REDIS_URL 미설정 — bot:ai_configs 를 읽을 수 없음")
        return 1

    import redis
    r = redis.from_url(redis_url, decode_responses=True)
    store = AiStore()
    try:
        recorded = run_scheduler(r, store, api_key)
    finally:
        store.close()
    print(f"[ai_shadow_scheduler] 배치 완료 — 신규 판단 {recorded}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
