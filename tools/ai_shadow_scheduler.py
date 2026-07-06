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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore, decide_virtual_trade
from kr_research.core.holidays import KST, is_market_open, is_trading_day
from tools.llm_shadow import build_reflection_note, llm_budget_exceeded, log_judgment, run_once
from kr_research.trading.tracking import HORIZONS, summarize_actions, summarize_by_confidence

K_CONFIGS = "bot:ai_configs"     # Hash(콘솔 CRUD): name → {symbol,timeframe,lookback_days,interval_min,enabled}
K_LAST_RUN = "bot:ai:last_run"   # Hash: name → epoch(마지막 시도 시각, 성공/실패 무관 — 재시도 폭주 방지)
K_STATUS = "bot:ai:status"       # Hash: name → {ts,ok,error?}(콘솔 에러 뱃지)
K_JUDGMENTS = "bot:ai:judgments"  # String(JSON list) — 콘솔 결과 테이블
K_SUMMARY = "bot:ai:summary"      # String(JSON) — trading.tracking.summarize_actions 출력(sell 부호 반전)
K_POSITIONS = "bot:ai:positions"  # String(JSON list) — 가상 포지션 원장(core/ai_store.py::decide_virtual_trade)
K_FORCE_RUN = "bot:ai:force_run"  # Hash: name → "1"(콘솔이 신규 설정 등록 직후 세팅) — 콘솔 redis_bus.py 의
                                   # K_AI_FORCE_RUN 과 리터럴이 같아야 함(양쪽이 서로 import 안 하는 별도
                                   # 저장소라 문자열로만 연결). 있으면 이번 tick 에서 _due() 게이트(경과시간·
                                   # 분 정렬·장상태) 전부 무시하고 즉시 1회 실행 — 신규 등록 직후 "설정이
                                   # 실제로 동작하는지" 바로 확인시켜주기 위함(2026-07 오너 요청). HDEL 로
                                   # 원자적 확인+소비(중복 실행 방지, 그 다음 tick 부터는 정상 게이트 적용).
PUBLISH_LIMIT = 200


_ALIGNED_MINUTES = {30: (15, 45), 60: (15,)}  # 정각(hh:00) 실행 회피 — 정각은 트래픽이 몰려 그 시각 캔들
# 데이터가 아직 안 나와 있을 수 있음(오너 요청, 2026-07). 30분 주기는 hh:15/hh:45, 60분 주기는 hh:15 에만
# 실행. cron 이 5분 단위로 도니 이 분들은 항상 정확히 한 번의 tick 과 일치한다(반올림·윈도 불필요).


def _due(cfg: dict, last_run: float | None) -> bool:
    """interval_min 경과 + (30/60분 주기는) 정렬된 분(_ALIGNED_MINUTES) + 장 상태 게이트. daily 는 거래일이면
    되고(마감 후 종가 반영을 언제든 잡아야 하니 정규장 시간까지 좁히지 않음), 분봉(5m/15m/30m/60m/4h)은
    정규장(09:00~15:30 KST) 안에서만 — 장 밖에는 새 봉이 안 생겨 호출해봐야 헛수고인데, cron 이 요일 제한
    없이 24시간 돌아서 그대로 두면 주말·야간에도 Naver/Yahoo 를 계속 두드리게 된다(무료 API 라도 지속 호출
    시 차단 위험).

    interval_min 이 30·60 이면 경과 시간만으론 부족하고 현재 KST 분이 _ALIGNED_MINUTES 에 속해야도 통과 —
    타임프레임 무관하게 일괄 적용(daily 설정에 interval_min=60 을 줘도 동일하게 hh:15 에만 실행). 처음
    등록 직후(last_run=None)라도 정렬 안 된 분이면 다음 정렬 시각까지 대기.

    tools/ai_combo_scheduler.py(③ 콤보 관찰)도 이 함수를 그대로 import 재사용한다(하위/자식 타임프레임을
    cfg["timeframe"] 자리에 넘겨서) — 이 로직을 바꾸면 그쪽 due 판정도 같이 확인할 것."""
    interval_min = max(int(cfg.get("interval_min") or 60), 1)
    if not (last_run is None or time.time() >= last_run + interval_min * 60):
        return False
    aligned = _ALIGNED_MINUTES.get(interval_min)
    if aligned is not None and datetime.now(KST).minute not in aligned:
        return False
    timeframe = cfg.get("timeframe") or "daily"
    return is_trading_day() if timeframe == "daily" else is_market_open()


def publish_ai_view(r, store: AiStore) -> None:
    """콘솔 '개별 종목 관찰' 뷰(bot:ai:judgments/summary/positions) 재발행 — 스케줄러 배치 뒤·전진검증
    (ai_forward_eval.py) 뒤 공용. 유니버스 스캔(UNIVERSE_CONFIG_NAME, tools/ai_universe_scan.py 가
    별도 발행)은 제외 — 두 화면이 섞이지 않게. LIMIT 을 걸기 전에 먼저 걸러야 한다(안 그러면 유니버스
    스캔 300건이 최신순 상위를 차지해 개별 종목 판단이 밀려날 수 있음).

    콘솔에서 삭제된 설정(bot:ai_configs 에 더 이상 없는 config_name)의 과거 판단·포지션도 같이 제외한다 —
    콘솔은 Redis 만 쓰고 이 SQLite(source of truth)엔 접근할 수 없어 삭제 자체는 못 하니, 재발행할 때마다
    "지금 살아있는 설정"으로 뷰만 다시 걸러낸다(원본 이력은 SQLite 에 그대로 남아 감사 목적은 유지 —
    비활성화(enabled=false)만 된 설정은 살아있는 걸로 치므로 안 걸러짐, 완전 삭제된 것만 제외)."""
    active = set(r.hgetall(K_CONFIGS).keys())
    rows = [row for row in store.get_judgments(None)
            if row["config_name"] != UNIVERSE_CONFIG_NAME and row["config_name"] in active]
    summary = summarize_actions(rows, HORIZONS)
    summary["by_confidence"] = summarize_by_confidence(rows, HORIZONS)["by_mode"]
    r.set(K_JUDGMENTS, json.dumps(rows[:PUBLISH_LIMIT], ensure_ascii=False))
    r.set(K_SUMMARY, json.dumps(summary, ensure_ascii=False))
    positions = [row for row in store.get_positions()
                 if row["config_name"] != UNIVERSE_CONFIG_NAME and row["config_name"] in active]
    r.set(K_POSITIONS, json.dumps(positions[:PUBLISH_LIMIT], ensure_ascii=False))


def run_scheduler(r, store: AiStore, api_key: str) -> int:
    """1회 배치. (due 이거나 K_FORCE_RUN 으로 강제된)+enabled 설정만 처리, 나머지는 건너뜀. 반환=이번
    배치에서 새로 기록된 판단 건수."""
    if llm_budget_exceeded(r):  # 일일 호출 한도(로드맵 §E) — 초과 시 이번 배치 통째로 스킵(내일 자동 재개)
        print("[ai_shadow_scheduler] LLM 일일 호출 한도 초과 — 배치 스킵")
        return 0
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
        forced = bool(r.hdel(K_FORCE_RUN, name))  # 확인+소비를 원자적으로(중복 강제실행 방지)
        if not _due(cfg, last_run) and not forced:
            continue
        code = cfg.get("symbol", "")
        try:
            # (config, code) 로 스코프한 과거 이력만 되먹임 — 콤보(③)는 다른 의사결정 맥락이라 안 섞음.
            history = [row for row in store.get_judgments(config_name=name) if row["code"] == code]
            record = run_once(code, lookback_days=int(cfg.get("lookback_days") or 120), api_key=api_key,
                               last_trade_date=store.last_trade_date(name, code),
                               timeframe=cfg.get("timeframe") or "daily",
                               reflection=build_reflection_note(history),
                               debate=bool(cfg.get("debate")))
            if record is not None:
                store.record_judgment(name, record)
                log_judgment(record, redis_url="", tenant="ai")  # JSONL 감사만(Redis 발행은 publish_ai_view 가 별도로)
                # 가상 포지션 시뮬레이터(섀도 — 실제 주문 없음) — trading/strategy.py 의 보유 게이트와 동일 규칙.
                open_pos = store.get_open_position(name, code)
                trade = decide_virtual_trade(open_pos, record["action"], record["entry_price"],
                                              confidence=record.get("confidence"),
                                              min_confidence=cfg.get("min_confidence"))
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
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("ai_shadow_scheduler", main))
