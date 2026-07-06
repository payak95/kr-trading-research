# ③ 콤보 관찰 스케줄러 — 상위 프레임(필터)+하위 프레임(타점) 이중 게이트 통과분만 Gemini 호출·기록(cron */5분)
"""콘솔이 CRUD 하는 bot:ai_combo_configs(Hash: name→{symbol,parent_timeframe,child_timeframe,interval_min,
enabled,min_confidence})를 읽어, 활성화됐고 하위(자식) 타임프레임 기준 due 인 설정만
tools/llm_shadow.judge_combo 로 판단시킨다. ai_shadow_scheduler.py(② 개별 종목 관찰)와 구조는 거의
동일하지만 완전히 별개 스크립트로 분리했다 — 이미 검증된 ②의 동작을 이번 작업으로 건드리지 않기 위함
(코드 중복은 감수, 대신 due 판정 함수(_due)는 그대로 import 재사용해 장 상태 게이트 로직만은 한 곳에
남긴다 — 이 파일의 due 판정이 바뀌면 ai_shadow_scheduler.py 도 같이 확인할 것).

저장소(AiStore)의 config_name 은 항상 "combo:"+name 프리픽스를 붙여 기록한다 — 콘솔의 PUT 엔드포인트가
임의의 이름을 받다 보니, 밑줄 개수 등 네이밍 관례만으로 ②의 단일 타임프레임 설정과 우연히 겹치는 걸
막을 방법이 없어서(예: 콘솔에서 실수로 ②와 같은 이름으로 콤보를 등록해도 안전). 발행할 때만 이 프리픽스를
벗겨 콘솔엔 순수 name 만 노출한다.

무주문·연구 전용 — 실제 주문은 전혀 안 나가지만, 판단마다 core/ai_store.py::decide_virtual_trade 로
가상(무자본) 포지션을 갱신한다(bot:ai:combo:positions).
실행: python tools/ai_combo_scheduler.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore, decide_virtual_trade
from tools.ai_shadow_scheduler import _due
from tools.llm_shadow import build_reflection_note, fetch_bars, judge_combo, llm_budget_exceeded, log_judgment
from kr_research.trading.tracking import HORIZONS, summarize_actions, summarize_by_confidence

K_CONFIGS = "bot:ai_combo_configs"      # Hash(콘솔 CRUD): name → {symbol,parent_timeframe,child_timeframe,
                                         # interval_min,enabled,min_confidence}
K_LAST_RUN = "bot:ai:combo:last_run"    # Hash: name → epoch(마지막 시도 시각, 성공/실패 무관)
K_STATUS = "bot:ai:combo:status"        # Hash: name → {ts,ok,error?}(콘솔 ③ 전용 에러 뱃지 — ②(bot:ai:status)
                                         # 와 별개 키. 같이 쓰면 /api/ai/status 가 Hash 전체를 그대로 서빙하는
                                         # 구조라 ③ 전용 뱃지를 못 만들어서 처음부터 분리)
K_JUDGMENTS = "bot:ai:combo:judgments"  # String(JSON list) — 콘솔 ③ 결과 테이블
K_SUMMARY = "bot:ai:combo:summary"      # String(JSON) — trading.tracking.summarize_actions 출력
K_POSITIONS = "bot:ai:combo:positions"  # String(JSON list) — 가상 포지션 원장
K_FORCE_RUN = "bot:ai:combo:force_run"  # Hash: name → "1" — ai_shadow_scheduler.K_FORCE_RUN 과 동일 목적
                                         # (콘솔이 신규 콤보 설정 등록 직후 세팅, 리터럴은 콘솔 redis_bus.py
                                         # 의 K_AI_COMBO_FORCE_RUN 과 일치해야 함), HDEL 로 원자적 확인+소비.
PUBLISH_LIMIT = 200
COMBO_PREFIX = "combo:"  # AiStore config_name 네임스페이스 — ②/유니버스 config_name 과 절대 안 겹치게(§ 위 docstring)


def publish_combo_view(r, store: AiStore) -> None:
    """콘솔 '③ 콤보 관찰' 뷰(bot:ai:combo:judgments/summary/positions) 재발행 — ai_shadow_scheduler.py 의
    publish_ai_view() 와 같은 "활성 설정만 남기기" 필터 패턴(삭제된 콤보 설정의 과거 판단·포지션은 재발행
    시 뷰에서 자동 제외, SQLite 원본은 감사 목적으로 보존). 발행 시 COMBO_PREFIX 를 벗겨 콘솔엔 순수
    name 만 노출(내부 네임스페이스 구현 세부사항을 콘솔에 안 새게)."""
    active_prefixed = {COMBO_PREFIX + name for name in r.hgetall(K_CONFIGS).keys()}
    rows = [
        {**row, "config_name": row["config_name"][len(COMBO_PREFIX):]}
        for row in store.get_judgments(None)
        if row["config_name"] != UNIVERSE_CONFIG_NAME and row["config_name"] in active_prefixed
    ]
    summary = summarize_actions(rows, HORIZONS)
    summary["by_confidence"] = summarize_by_confidence(rows, HORIZONS)["by_mode"]
    r.set(K_JUDGMENTS, json.dumps(rows[:PUBLISH_LIMIT], ensure_ascii=False))
    r.set(K_SUMMARY, json.dumps(summary, ensure_ascii=False))
    positions = [
        {**row, "config_name": row["config_name"][len(COMBO_PREFIX):]}
        for row in store.get_positions()
        if row["config_name"] in active_prefixed
    ]
    r.set(K_POSITIONS, json.dumps(positions[:PUBLISH_LIMIT], ensure_ascii=False))


def run_combo_scheduler(r, store: AiStore, api_key: str) -> int:
    """1회 배치. (due 이거나 K_FORCE_RUN 으로 강제된)+enabled 콤보 설정만 처리, 나머지는 건너뜀. due 판정은
    하위(자식) 타임프레임 기준으로 ai_shadow_scheduler._due() 를 그대로 재사용(장 상태 게이트 로직 중복
    방지 — 하위 프레임에 새 봉이 생겨야 재판단 의미가 있으므로). 반환=이번 배치에서 새로 기록된 판단 건수."""
    if llm_budget_exceeded(r):  # 일일 호출 한도(로드맵 §E) — 초과 시 이번 배치 통째로 스킵(내일 자동 재개)
        print("[ai_combo_scheduler] LLM 일일 호출 한도 초과 — 배치 스킵")
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
        child_tf = cfg.get("child_timeframe") or "60m"
        forced = bool(r.hdel(K_FORCE_RUN, name))  # 확인+소비를 원자적으로(중복 강제실행 방지)
        if not _due({"timeframe": child_tf, "interval_min": cfg.get("interval_min")}, last_run) and not forced:
            continue
        code = cfg.get("symbol", "")
        store_name = COMBO_PREFIX + name
        try:
            parent_tf = cfg.get("parent_timeframe") or "daily"
            parent_bars = fetch_bars(code, parent_tf)
            child_bars = fetch_bars(code, child_tf)
            # (콤보 설정, code) 로 스코프한 과거 이력만 되먹임 — ②(단일 프레임)와 다른 의사결정 맥락.
            history = [row for row in store.get_judgments(config_name=store_name) if row["code"] == code]
            record = judge_combo(code, parent_tf, parent_bars, child_tf, child_bars, api_key,
                                 last_trade_date=store.last_trade_date(store_name, code),
                                 reflection=build_reflection_note(history),
                                 debate=bool(cfg.get("debate")))
            if record is not None:
                store.record_judgment(store_name, record)
                log_judgment(record, redis_url="", tenant="ai_combo")  # JSONL 감사만(Redis 발행은 publish_combo_view 가 별도로)
                # 가상 포지션 시뮬레이터(섀도 — 실제 주문 없음) — ai_shadow_scheduler.py 와 동일 규칙.
                open_pos = store.get_open_position(store_name, code)
                trade = decide_virtual_trade(open_pos, record["action"], record["entry_price"],
                                              confidence=record.get("confidence"),
                                              min_confidence=cfg.get("min_confidence"))
                if trade and trade["kind"] == "open":
                    store.open_position(store_name, code, trade["qty"], record["entry_price"], record["trade_date"])
                elif trade and trade["kind"] == "close":
                    store.close_position(open_pos["id"], record["entry_price"], record["trade_date"],
                                         trade["realized_pnl"], trade["realized_return_pct"])
                recorded += 1
            r.hset(K_STATUS, name, json.dumps({"ts": time.time(), "ok": True}, ensure_ascii=False))
        except Exception as e:
            print(f"[ai_combo_scheduler] {name}({code}) 실패: {e}")
            r.hset(K_STATUS, name, json.dumps({"ts": time.time(), "ok": False, "error": str(e)[:200]}, ensure_ascii=False))
        finally:
            # 성공/실패/스킵 무관하게 last_run 갱신 — 실패해도 매 5분 재시도로 폭주하지 않고 interval_min 만큼 물러남.
            r.hset(K_LAST_RUN, name, time.time())
    if configs:
        publish_combo_view(r, store)
    return recorded


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[ai_combo_scheduler] GEMINI_API_KEY 미설정 — 종료")
        return 1
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[ai_combo_scheduler] REDIS_URL 미설정 — bot:ai_combo_configs 를 읽을 수 없음")
        return 1

    import redis
    r = redis.from_url(redis_url, decode_responses=True)
    store = AiStore()
    try:
        recorded = run_combo_scheduler(r, store, api_key)
    finally:
        store.close()
    print(f"[ai_combo_scheduler] 배치 완료 — 신규 판단 {recorded}건")
    return 0


if __name__ == "__main__":
    from kr_research.core.heartbeat import run_with_heartbeat  # 크론 심장박동(로드맵 §C) — 성공 종료만 기록
    raise SystemExit(run_with_heartbeat("ai_combo_scheduler", main))
