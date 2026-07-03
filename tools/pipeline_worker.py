# 스크리닝→백테스트→(튜닝)→검증등록을 하나의 run 으로 자동 연결하는 파이프라인 워커 (파이프라인 자동화)
"""지금까지는 ①스크리닝·②백테스트·③튜닝이 각각 독립 잡이라 사람이 ①결과를 보고 ②에 손으로 다시
입력해야 했다(docs/planning/pipeline-automation-design.md §2). 이 워커는 콘솔이 `bot:pipeline:jobs`
에 적재한 잡 1건으로 ①→②→(③)→④ 를 자동 연결한다(⑤실전은 항상 수동, 범위 밖).

**③튜닝은 선택**(§5 연결고리 완료, v0.12): job 에 `grid`가 없으면 ③을 건너뛰고 ②결과로 바로 ④(검증
신호 등록)로 진행 — 파이프라인의 핵심 목표는 ④ 자동 등록이라 튜닝 없이도 완주해야 함. `grid`가 있으면
기존대로 ③도 실행.

**④검증 자동 등록**(§5 연결고리): ②백테스트를 통과한(top_k) 종목을 `tools/screen_track.pipeline_signals`
로 ①후보의 종가·검색일과 조인해 `bot:screen:track:requests`에 RPUSH — 이후 기존 `screen_track_eval.py`
야간 크론이 그대로 D+N·청산-추적 평가를 이어간다(새 크론 안 만듦). job 에 `strategy`(저장 전략명, 콘솔이
강제)가 없으면 등록은 skip(②③은 정상 완주).

계산 로직은 새로 안 짬 — `tools/backtest_worker.py` 의 순수 함수(`screen_spec`·`batch_spec`·`sweep_spec`
+ fetch 헬퍼)를 그대로 호출하는 얇은 오케스트레이션 레이어일 뿐이다.

**idempotent 재개**(§5): 각 stage 결과를 `bot:pipeline:run:{run_id}:stage{N}` 에 `created_at` 과 함께
저장. 다음 실행 때 이미 저장된 stage 가 있으면 재계산하지 않고 그대로 재사용 — 실패한 stage 부터만
이어서 실행한다(한 stage 실패가 전체 재실행을 강요하지 않음).

**staleness**(§5, v0.7 정정): stage 결과는 `created_at` 이 속한 날의 **다음 거래일 전까지만** 유효
(`core.holidays.next_trading_day`) — "같은 날짜"가 아니라 "다음 거래일 전까지"라 주말·연휴엔 안 낡는다.
어느 stage든 만료되면 그 stage부터 뒤(하위 stage 전부)를 다시 계산 — 하위 stage 가 각자 신선해도
상위 stage 가 새로 계산됐다면 그 결과에 기반한 게 아니므로 함께 무효화한다(`force` 플래그).

실행 모드는 `backtest_worker.py`와 동일 2가지(`--daemon`/cron). 사용법도 동일 패턴.
"""
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core import holidays
from tools import backtest_worker as bw
from tools import flow_universe, screen_track
from tools.naver_investor import daily_investor_flow
from kr_research.trading.spec import uses_flow_setups

PIPELINE_QUEUE = "bot:pipeline:jobs"                     # 콘솔 RPUSH, 워커 LPOP (FIFO)
STAGE_KEY = "bot:pipeline:run:{}:stage{}"                 # run_id, stage(1|2|3|4)
META_KEY = "bot:pipeline:run:{}:meta"                     # run_id — 진행 상태
PIPELINE_TTL = 93600                                      # 초 — 26h(재개 가능 기간, 유니버스 워밍 캐시와 결 맞춤)
DEFAULT_TOP_K = 10                                        # ②→③ 넘길 상위 종목 수 기본값
MAX_TOP_K = 20                                            # 상한(③ 튜닝은 종목마다 grid_search — 비용 보호)
MAX_JOBS_PER_RUN = 3                                      # cron 1틱당 처리 상한(파이프라인 잡은 다단계라 무거움, backtest_worker 보다 낮게)


def _is_fresh(created_at, now=None) -> bool:
    """stage 결과가 아직 유효한지 — created_at 날짜의 다음 거래일이 오기 전까지(§5 v0.7).
    주말 내내 장이 닫혀 있어도 안 낡게, "같은 날짜" 대신 "다음 거래일 전"으로 판정."""
    now = now or dt.datetime.now(holidays.KST)
    created_date = dt.datetime.fromtimestamp(created_at, tz=holidays.KST).date()
    return now.date() < holidays.next_trading_day(created_date)


def _save_stage(r, run_id: str, stage: int, result: dict) -> None:
    try:
        r.set(STAGE_KEY.format(run_id, stage), json.dumps(result, ensure_ascii=False), ex=PIPELINE_TTL)
    except Exception:
        pass  # best-effort — 저장 실패는 다음 실행이 그 stage부터 재계산하는 것으로 귀결


def _load_fresh_stage(r, run_id: str, stage: int) -> dict | None:
    """저장된 stage 결과가 있고 아직 신선하면 반환, 없거나 만료면 None(재계산 필요)."""
    try:
        raw = r.get(STAGE_KEY.format(run_id, stage))
    except Exception:
        return None
    if not raw:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not _is_fresh(result.get("created_at", 0)):
        return None
    return result


def _save_meta(r, run_id: str, meta: dict) -> None:
    try:
        r.set(META_KEY.format(run_id), json.dumps(meta, ensure_ascii=False), ex=PIPELINE_TTL)
    except Exception:
        pass


def _fail(r, run_id: str, stage: int, exc: Exception, now_ts: int) -> dict:
    meta = {"run_id": run_id, "status": "error", "stage": stage, "updated_at": now_ts, "error": str(exc)}
    _save_meta(r, run_id, meta)
    return meta


def _clamp_top_k(v) -> int:
    try:
        k = int(v)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(MAX_TOP_K, k))


def _run_stage1(r, run_id: str, job: dict, spec: dict, days: int, fetch_bars, now_ts: int) -> dict:
    """① 스크리닝 — universe=True 면 유니버스 집합 전체, 아니면 job.codes 대상."""
    if job.get("universe"):
        try:
            codes = sorted(r.smembers(bw.UNIVERSE_KEY))
        except Exception:
            codes = []
        codes = codes[:bw.MAX_UNIVERSE_CODES]
        if not codes:
            raise ValueError("유니버스가 비어 있음 — 야간 유니버스 수집(screen_universe)을 먼저 실행하세요")
    else:
        codes = bw._parse_codes(job)
    bars_by_code = bw._fetch_many(codes, days, fetch_bars)
    flow_by_code = None
    if uses_flow_setups(spec):  # 스펙이 외국인·기관 수급 셋업을 참조할 때만(§스크리닝 강화, 불필요한 호출 방지)
        if job.get("universe"):
            flow_by_code = flow_universe.load_flow_cache(r, codes)  # 캐시 전용(야간 크론이 워밍)
        else:
            flow_by_code = {c: daily_investor_flow(c) for c in codes}  # 라이브(codes 는 MAX_UNIVERSE_CODES/코드입력 상한 안전)
    result = bw.screen_spec(bars_by_code, spec, job.get("params"), flow_by_code)
    result["universe"] = bool(job.get("universe"))
    result["created_at"] = now_ts
    _save_stage(r, run_id, 1, result)
    return result


def _run_stage2(r, run_id: str, spec: dict, params: dict | None, cash: int, stage1: dict,
                days: int, top_k: int, fetch_bars, now_ts: int) -> dict:
    """② 백테스트 — ①후보 전부를 배치 백테스트, 수익률 상위 top_k 만 통과."""
    codes = [c["code"] for c in stage1.get("candidates", [])]
    if not codes:
        result = {"tested": 0, "top_k": top_k, "results": [], "created_at": now_ts}
        _save_stage(r, run_id, 2, result)
        return result
    bars_by_code = bw._fetch_many(codes, days, fetch_bars)
    batch = bw.batch_spec(bars_by_code, spec, params, cash)
    result = {"tested": batch["tested"], "top_k": top_k,
              "results": batch["results"][:top_k], "created_at": now_ts}
    _save_stage(r, run_id, 2, result)
    return result


def _register_validation(r, run_id: str, job: dict, stage1: dict, stage2: dict, now_ts: int) -> dict:
    """④ 검증 신호 자동 등록(§5 연결고리) — ②통과 종목을 ①후보와 조인해 추적요청 큐에 RPUSH.
    job.strategy 없으면 등록 skip(registered=0) — 저장 전략이어야 ④ 청산-추적이 spec 을 찾으므로,
    호출부(콘솔)가 저장 전략명만 넘기도록 강제된 계약. 재RPUSH 중복은 하위 merge_signals(자연키)가 dedup."""
    reqs = screen_track.pipeline_signals(job.get("strategy"), stage1, stage2)
    if reqs:
        r.rpush(screen_track.TRACK_REQUESTS_KEY, *[json.dumps(x, ensure_ascii=False) for x in reqs])
    result = {"registered": len(reqs), "created_at": now_ts}
    _save_stage(r, run_id, 4, result)
    return result


def _run_stage3(r, run_id: str, spec: dict, params: dict | None, grid: dict, metric: str, cash: int,
                stage2: dict, days: int, fetch_bars, now_ts: int) -> dict:
    """③ 파라미터 튜닝 — ②상위 top_k 종목마다 grid_search, 종목별 최적 파라미터. 종목별 실패는 skip(전체 보호)."""
    per_code = []
    for row in stage2.get("results", []):
        code = row["code"]
        try:
            bars = fetch_bars(code, days)
            if not bars:
                continue
            sweep = bw.sweep_spec(bars, code, spec, grid, params, metric, cash=cash)
        except Exception:
            continue
        per_code.append({"code": code, "best": sweep.get("best"), "n_combos": sweep.get("n_combos")})
    result = {"per_code": per_code, "created_at": now_ts}
    _save_stage(r, run_id, 3, result)
    return result


def run_pipeline(r, job: dict, fetch_bars) -> dict:
    """파이프라인 run 1건 실행 — 미완료·만료 stage부터 순차 진행(idempotent, §5). 반환은 meta dict.
    한 stage 실패는 그 stage에서 멈추고 error 로 기록(다음 호출이 이어서 재시도, 드레인 보호)."""
    run_id = str(job.get("run_id", "")).strip()
    now_ts = int(time.time())
    if not run_id:
        return _fail(r, "(missing)", 0, ValueError("run_id 필수"), now_ts)
    spec = job.get("spec")
    if not isinstance(spec, dict):
        return _fail(r, run_id, 0, ValueError("spec(dict) 필수"), now_ts)
    days = bw._clamp_days(job.get("days", bw.DEFAULT_DAYS))
    top_k = _clamp_top_k(job.get("top_k", DEFAULT_TOP_K))
    cash = int(job.get("cash", bw.DEFAULT_CASH))
    params = job.get("params")

    force = False  # 상위 stage 가 이번에 새로 계산되면 True 로 — 하위 stage 는 자기 캐시가 신선해도 강제 재계산
    stage1 = _load_fresh_stage(r, run_id, 1)
    if stage1 is None:
        try:
            stage1 = _run_stage1(r, run_id, job, spec, days, fetch_bars, now_ts)
        except Exception as e:
            return _fail(r, run_id, 1, e, now_ts)
        force = True

    stage2 = None if force else _load_fresh_stage(r, run_id, 2)
    if stage2 is None:
        try:
            stage2 = _run_stage2(r, run_id, spec, params, cash, stage1, days, top_k, fetch_bars, now_ts)
        except Exception as e:
            return _fail(r, run_id, 2, e, now_ts)
        force = True

    grid = {k: v for k, v in (job.get("grid") or {}).items() if isinstance(v, list) and v}
    stage3 = None if force else _load_fresh_stage(r, run_id, 3)
    if stage3 is None and grid:
        try:
            stage3 = _run_stage3(r, run_id, spec, params, grid, job.get("metric", "return_pct"),
                                 cash, stage2, days, fetch_bars, now_ts)
        except Exception as e:
            return _fail(r, run_id, 3, e, now_ts)
        force = True

    stage4 = None if force else _load_fresh_stage(r, run_id, 4)
    if stage4 is None:
        try:
            stage4 = _register_validation(r, run_id, job, stage1, stage2, now_ts)
        except Exception as e:
            return _fail(r, run_id, 4, e, now_ts)

    meta = {"run_id": run_id, "status": "done", "stage": 4, "updated_at": int(time.time())}
    _save_meta(r, run_id, meta)
    return meta


def _handle_raw(r, raw, fetch_bars) -> bool:
    """원시 잡(JSON 문자열) 1건 실행. 손상 잡은 skip(False). universe 잡은 캐시 전용 fetch(낮 KIS/네이버 폭주 방지 —
    유니버스는 야간 크론이 워밍한 캐시에만 의존, backtest_worker._handle_raw 와 동일 결)."""
    try:
        job = json.loads(raw)
    except (ValueError, TypeError):
        return False
    job_fetch = bw._cache_only_fetch(r) if job.get("universe") else fetch_bars
    run_pipeline(r, job, job_fetch)
    return True


def drain(r, fetch_bars, max_jobs: int = MAX_JOBS_PER_RUN) -> int:
    """큐를 최대 max_jobs 건 LPOP 처리. 처리 건수 반환(cron 모드)."""
    n = 0
    for _ in range(max_jobs):
        raw = r.lpop(PIPELINE_QUEUE)
        if raw is None:
            break
        if _handle_raw(r, raw, fetch_bars):
            n += 1
    return n


_STOP = False  # 데몬 종료 플래그(SIGTERM/SIGINT 로 set)


def serve(r, fetch_bars, poll_timeout: int = 5, should_continue=None) -> int:
    """데몬 모드 — BLPOP 으로 잡을 즉시 받아 처리. 처리 건수 반환. 종료 신호까지 무한 루프."""
    should_continue = should_continue or (lambda: not _STOP)
    n = 0
    while should_continue():
        try:
            item = r.blpop(PIPELINE_QUEUE, timeout=poll_timeout)
        except Exception:
            time.sleep(1)
            continue
        if item is None:
            continue
        if _handle_raw(r, item[1], fetch_bars):
            n += 1
    return n


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    daemon = "--daemon" in argv
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 파이프라인 워커는 Redis 큐 전용(폴백 없음)")
        return 1
    import redis  # lazy: REDIS_URL 있을 때만

    r = redis.from_url(redis_url, decode_responses=True)
    fetch = bw._cached_fetch(r, bw.naver_fetch)
    if daemon:
        import signal

        def _on_term(signum, frame):
            global _STOP
            _STOP = True
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
        print("파이프라인 워커 데몬 시작 — BLPOP 대기(즉시 처리)")
        n = serve(r, fetch)
        print(f"파이프라인 워커 데몬 종료 — 누적 {n}건 처리")
        return 0
    n = drain(r, fetch)
    print(f"파이프라인 잡 {n}건 처리")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
