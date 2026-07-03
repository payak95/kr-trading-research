# 콘솔이 적재한 spec 백테스트 잡을 Redis 큐에서 꺼내 네이버 일봉으로 평가·결과 기록 (노드 빌더 Phase 1-a)
"""콘솔(kr-trading-console)은 KIS 크레덴셜이 없어 직접 백테스트를 못 한다(보안 격리, §6).
대신 콘솔이 `bot:backtest:jobs`(Redis List)에 잡(JSON)을 적재하면, 봇/VPS 의 이 워커가
큐를 비우며 각 잡을 `SpecStrategy` 로 평가하고 결과를 `bot:backtest:result:{job_id}`
(TTL 1h)에 기록한다. 콘솔은 결과키를 폴링해 표시한다.

실행 모드 2가지:
- **데몬**(`--daemon`, 권장): `serve()` 가 BLPOP 으로 잡을 **즉시** 받아 처리(지연 거의 0). 전용 컨테이너 상시 실행.
- **cron**(인자 없음): `drain()` 이 큐를 1회 비우고 종료(매 1분 cron). 데몬 미사용 시 폴백 — **둘을 동시에 돌리지 말 것**(중복 처리).

- **연구용 일봉은 네이버(`tools/naver_ohlcv`)** — 무인증·무한도. KIS 초당 한도가 대량 잡(유니버스 수백 종목)·반복
  백테스트의 병목이라 분리(KIS 는 라이브 매매 전용). 캐시(`_cached_fetch`)로 반복 호출도 격감.
- 데이터 채널: **Redis 만**(콘솔→봇 인바운드 없음). 실시간 매매 루프와 분리된 별도 프로세스.
- 입력 불신: spec 검증 + days/params clamp(외부 운영자 입력 방어, PROJECT_GUIDE §0 동일 기조).
- 한 잡 실패가 드레인을 죽이지 않음(잡별 try/except → error 결과 기록).
- 계정 무관(일봉은 테넌트 공통) → 큐/결과 키는 전역(테넌트 네임스페이스 없음).

사용: `python tools/backtest_worker.py`  (REDIS_URL 필요·KIS 불필요). cron: RUNTIME.md 참고.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.params import Params
from kr_research.trading import flow
from kr_research.trading.backtest import run
from kr_research.trading.metrics import summary
from kr_research.trading.spec import SpecStrategy, screen, uses_flow_setups, validate
from kr_research.trading.tuning import grid_search

# 거래비용 근사값 — tools/backtest.py 와 동일 가정(드리프트 시 양쪽 함께 조정).
FEE_RATE = 0.00015   # 편도 위탁수수료
TAX_RATE = 0.0015    # 매도 거래세+농특세
SLIPPAGE = 0.001     # 시장가 불리 체결

QUEUE_KEY = "bot:backtest:jobs"            # 콘솔 RPUSH, 워커 LPOP (FIFO)
RESULT_KEY = "bot:backtest:result:{}"      # 워커 SET(JSON), TTL
RESULT_TTL = 3600                          # 결과 보존 1시간(콘솔 폴링 후 자연 만료)
OHLCV_CACHE_KEY = "bot:backtest:ohlcv:{}:{}"  # code, days — 일봉 캐시(반복 백테스트·벤치마크 재사용)
OHLCV_CACHE_TTL = 3600                      # 초 — 일봉 캐시 보존 1h. KIS 호출 격감(초당 한도 경합↓). 당일 마지막 봉은 그만큼 stale 허용(연구용 허용)
UNIVERSE_CACHE_TTL = 93600                  # 초 — 야간 크론이 워밍한 유니버스 일봉 캐시 보존 26h(다음 야간 실행까지 생존 → 낮 유니버스 검색은 캐시 전용)
MAX_JOBS_PER_RUN = 25                      # cron 1틱당 처리 상한(폭주 방어)
MIN_DAYS, MAX_DAYS = 5, 400                # 평가 일수 clamp
DEFAULT_DAYS = 90
DEFAULT_CASH = 10_000_000
MAX_CODES = 30                             # 스크리닝 1잡당 종목 상한(다종목 호출 방어)
MAX_UNIVERSE_CODES = 500                    # 유니버스 스크리닝 상한(캐시 전용 — 크게 허용)
MAX_SWEEP_COMBOS = 500                      # 파라미터 튜닝 1잡당 조합 상한(캐시·순수계산이라 빠르나 페이로드/시간 보호)
UNIVERSE_KEY = "bot:screen:universe"        # Redis Set — 야간 크론이 시총·거래대금 상위 N 으로 교체하는 유니버스(종목코드)
BENCH_CODE = "069500"                       # 코스피 벤치마크 프록시 = KODEX 200 ETF(일반 종목처럼 일봉 fetch 가능)


def backtest_spec(bars: list[dict], code: str, spec: dict, params: dict | None,
                  cash: int = DEFAULT_CASH) -> dict:
    """일봉(bars)에 spec 을 SpecStrategy 로 백테스트 → JSON 직렬화 가능한 결과 dict.
    순수(주입된 bars 만 사용 — KIS 무관)라 테스트가 합성 일봉으로 검증 가능."""
    validate(spec)
    eff_params = Params.from_dict(params or {}).clamp().to_dict()
    strategy = SpecStrategy(spec, eff_params)
    res = run(strategy, bars, code, cash=cash,
              fee_rate=FEE_RATE, tax_rate=TAX_RATE, slippage=SLIPPAGE)
    out = {
        "code": code,
        "name": spec.get("name", ""),
        "bars": len(bars),
        "first": bars[0].get("date") if bars else None,
        "last": bars[-1].get("date") if bars else None,
        "return_pct": res["return_pct"],
        "n_trades": res["n_trades"],
        "final_equity": res["final_equity"],
        "fees": res.get("fees", 0),
        "tax": res.get("tax", 0),
        "metrics": summary(res["equity_curve"], res["trades"]) if res["n_trades"] else {},
        "equity_curve": [round(v) for v in res["equity_curve"]],  # 봉별 자산(콘솔 수익곡선 그래프용)
        "trades": [{"date": t.get("date"), "side": t.get("side"), "qty": t.get("qty"),
                    "price": round(t.get("price", 0))} for t in res["trades"]],  # 매매 시점(콘솔 거래내역 표용)
    }
    return out


def screen_spec(bars_by_code: dict, spec: dict, params: dict | None, flow_by_code: dict | None = None) -> dict:
    """여러 종목 일봉에 spec.screen(지표+셋업) 적용 → 후보 종목 추출(스크리닝, 무주문).
    셋업(setup) 술어 활성(백테스트도 이제 활성 — screen 은 entry 만 보고 후보만 뽑는 점이 차이).
    flow_by_code(선택, §스크리닝 강화): {code: naver_investor.daily_investor_flow 결과} — 스펙이
    외국인·기관 수급 셋업을 참조할 때만 호출부가 채워 넘김(trading.flow.active_items 로 변환해 screen 의
    extra_active 로 합류). 종목별 예외는 skip(전체 보호)."""
    validate(spec)
    candidates = []
    screened = 0
    for code, bars in bars_by_code.items():
        if not bars:
            continue
        screened += 1
        try:
            extra = flow.active_items(flow_by_code[code]) if flow_by_code and code in flow_by_code else None
            if screen(spec, bars, params or {}, extra_active=extra):
                candidates.append({"code": code, "last": bars[-1].get("close"), "date": bars[-1].get("date")})
        except Exception:
            continue  # 한 종목 실패가 전체 스크리닝을 죽이지 않음
    return {"name": spec.get("name", ""), "screened": screened,
            "n_candidates": len(candidates), "candidates": candidates}


def batch_spec(bars_by_code: dict, spec: dict, params: dict | None,
               cash: int = DEFAULT_CASH) -> dict:
    """여러 종목에 같은 spec 백테스트 → 종목별 요약(수익률·거래수·승률) 수익률 내림차순 표.
    각 행에 buyhold_return_pct(그 종목을 같은 기간 매수후보유했을 때 수익률)도 함께 담아, 콘솔이
    알파(전략 수익률-매수후보유 수익률) 기준으로 성과를 판정할 수 있게 한다.
    배치 페이로드 경량화로 종목별 equity_curve/trades 는 생략. 종목별 예외는 skip(전체 보호)."""
    validate(spec)
    rows = []
    tested = 0
    for code, bars in bars_by_code.items():
        if not bars:
            continue
        tested += 1
        try:
            res = backtest_spec(bars, code, spec, params, cash)
        except Exception:
            continue  # 한 종목 실패가 전체 배치를 죽이지 않음
        first_close = bars[0].get("close")
        buyhold = round((bars[-1].get("close") / first_close - 1) * 100, 2) if first_close else None
        rows.append({"code": code, "name": res["name"], "return_pct": res["return_pct"],
                     "n_trades": res["n_trades"], "final_equity": res["final_equity"],
                     "win_rate": res["metrics"].get("win_rate") if res["metrics"] else None,
                     "buyhold_return_pct": buyhold})
    rows.sort(key=lambda x: x["return_pct"], reverse=True)
    return {"name": spec.get("name", ""), "tested": tested, "n": len(rows), "results": rows}


def sweep_spec(bars: list[dict], code: str, spec: dict, grid: dict, base_params: dict | None = None,
               metric: str = "return_pct", cap: int = MAX_SWEEP_COMBOS, cash: int = DEFAULT_CASH) -> dict:
    """파라미터 그리드 서치 — 얇은 래퍼(순수 로직은 `trading.tuning.grid_search`, 워크포워드와 공유).
    이 워커의 거래비용 가정(FEE_RATE 등)을 주입해 콘솔 파이프라인과 동일 기조 유지.
    (과최적화 주의 — 콘솔이 전진검증과 연계 안내)."""
    return grid_search(bars, code, spec, grid, base_params, metric, cap, cash,
                       fee_rate=FEE_RATE, tax_rate=TAX_RATE, slippage=SLIPPAGE)


def _benchmark_return(bench_bars: list[dict], first_date, last_date) -> float | None:
    """벤치마크(코스피 ETF) 매수후보유 수익률(%) — 백테스트와 같은 [first,last] 날짜 구간으로 정렬.
    구간 내 봉이 2개 미만이면 None(정렬 불가 → 알파 생략)."""
    if not (bench_bars and first_date and last_date):
        return None
    sel = [b for b in bench_bars if first_date <= b.get("date", "") <= last_date]
    if len(sel) < 2:
        return None
    f, l = sel[0].get("close"), sel[-1].get("close")
    return (l / f - 1) * 100 if f else None


def _clamp_days(v) -> int:
    try:
        d = int(v)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, d))


def _parse_codes(job: dict, cap: int = MAX_CODES, empty_msg: str = "codes(list) 필수") -> list[str]:
    """다종목 잡(screen/batch/universe)의 codes 파싱·정리(공백 제거·상한). 비면 empty_msg 로 에러."""
    codes = [str(c).strip() for c in (job.get("codes") or []) if str(c).strip()][:cap]
    if not codes:
        raise ValueError(empty_msg)
    return codes


def _fetch_many(codes: list[str], days: int, fetch_bars) -> dict:
    """종목별 fetch — 한 종목 실패(레이트리밋·상폐 등)는 skip(전체 잡 보호)."""
    out = {}
    for c in codes:
        try:
            out[c] = fetch_bars(c, days)
        except Exception:
            continue
    return out


def process_job(job: dict, fetch_bars) -> dict:
    """잡 1건 처리 → 결과 dict(status=done|error). type=screen|batch|backtest(기본) 분기.
    fetch_bars(code, days)->bars 주입(테스트 분리). 예외는 삼켜 error 결과로(드레인 보호)."""
    job_id = str(job.get("job_id", ""))
    jtype = job.get("type", "backtest")
    base = {"job_id": job_id, "type": jtype, "finished_at": int(time.time())}
    try:
        spec = job.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("spec(dict) 필수")
        days = _clamp_days(job.get("days", DEFAULT_DAYS))
        if jtype == "screen":
            # universe=True 면 codes 는 _handle_raw 가 유니버스 집합에서 채워 넣음(상한·빈집합 메시지 분기)
            if job.get("universe"):
                codes = _parse_codes(job, MAX_UNIVERSE_CODES,
                                     "유니버스가 비어 있음 — 야간 유니버스 수집(screen_universe)을 먼저 실행하세요")
            else:
                codes = _parse_codes(job)
            bars_by_code = _fetch_many(codes, days, fetch_bars)
            result = screen_spec(bars_by_code, spec, job.get("params"), job.get("_flow_by_code"))
            result["universe"] = bool(job.get("universe"))
        elif jtype == "batch":
            bars_by_code = _fetch_many(_parse_codes(job), days, fetch_bars)
            result = batch_spec(bars_by_code, spec, job.get("params"), int(job.get("cash", DEFAULT_CASH)))
        elif jtype == "sweep":
            code = str(job.get("code", "")).strip()
            if not code:
                raise ValueError("code 필수")
            grid = {k: v for k, v in (job.get("grid") or {}).items() if isinstance(v, list) and v}
            if not grid:
                raise ValueError("grid(튜닝 값 목록) 필수")
            bars = fetch_bars(code, days)
            if not bars:
                raise ValueError(f"일봉 0건 — 종목코드/기간 확인({code})")
            result = sweep_spec(bars, code, spec, grid, job.get("params"),
                                job.get("metric", "return_pct"), cash=int(job.get("cash", DEFAULT_CASH)))
        else:
            code = str(job.get("code", "")).strip()
            if not code:
                raise ValueError("code 필수")
            bars = fetch_bars(code, days)
            if not bars:
                raise ValueError(f"일봉 0건 — 종목코드/기간 확인({code})")
            result = backtest_spec(bars, code, spec, job.get("params"), int(job.get("cash", DEFAULT_CASH)))
            # 벤치마크(코스피 ETF) 대비 초과수익(알파) — 같은 기간 매수후보유 대비. 실패/벤치 자신은 생략.
            if code != BENCH_CODE:
                try:
                    bret = _benchmark_return(fetch_bars(BENCH_CODE, days), result["first"], result["last"])
                except Exception:
                    bret = None
                if bret is not None:
                    result["bench_code"] = BENCH_CODE
                    result["bench_return_pct"] = round(bret, 2)
                    result["excess_pct"] = round(result["return_pct"] - bret, 2)
        result["days"] = days
        return {**base, "status": "done", "result": result}
    except Exception as e:
        return {**base, "status": "error", "error": str(e)}


def naver_fetch(code: str, days: int) -> list[dict]:
    """연구용 일봉 소스 — 네이버 fchart(무인증·무한도, KIS 분리). days=최근 거래일(봉) 수.
    KIS 와 달리 1회 호출에 days 봉을 받고 초당 한도가 없어 대량 잡·반복에 적합. 반환 형태는 동일."""
    from tools.naver_ohlcv import daily_ohlcv
    return daily_ohlcv(code, count=days)


def _cached_fetch(r, raw_fetch, ttl: int = OHLCV_CACHE_TTL):
    """일봉 fetch 에 Redis 캐시 레이어 — 같은 (code,days) 반복 호출은 KIS 안 치고 캐시 사용.
    벤치마크(매 백테스트)·동일 종목 반복·여러 전략 비교에서 KIS 초당 한도 경합을 크게 줄임.
    캐시 R/W 실패는 무시(원천 fetch 폴백 — 캐시는 가속용일 뿐 정확성에 무관)."""
    def fetch(code: str, days: int) -> list[dict]:
        key = OHLCV_CACHE_KEY.format(code, days)
        try:
            cached = r.get(key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
        bars = raw_fetch(code, days)
        if bars:
            try:
                r.set(key, json.dumps(bars, ensure_ascii=False), ex=ttl)
            except Exception:
                pass
        return bars
    return fetch


def _cache_only_fetch(r):
    """캐시에 있는 일봉만 반환(KIS 미접속) — 유니버스 스크리닝 전용. 야간 크론이 워밍한 캐시에 의존하며,
    콜드 미스(미워밍 종목)는 [](스크리닝에서 skip)로 두어 낮 시간 KIS 호출 폭주를 원천 차단."""
    def fetch(code: str, days: int) -> list[dict]:
        try:
            cached = r.get(OHLCV_CACHE_KEY.format(code, days))
            if cached:
                return json.loads(cached)
        except Exception:
            pass
        return []
    return fetch


def _handle_raw(r, raw, fetch_bars) -> bool:
    """원시 잡(JSON 문자열) 1건 처리 → 결과를 RESULT_KEY 에 기록. 손상 잡은 skip(False).
    drain(cron)·serve(데몬) 공용. universe 스크리닝은 종목을 유니버스 집합에서 채우고 캐시 전용 fetch 사용."""
    try:
        job = json.loads(raw)
    except (ValueError, TypeError):
        return False  # 손상 잡 skip(결과키 없이 — job_id 모름)
    job_fetch = fetch_bars
    if job.get("type") == "screen" and job.get("universe"):
        try:
            job["codes"] = sorted(r.smembers(UNIVERSE_KEY))  # 유니버스 집합 → codes
        except Exception:
            job["codes"] = []
        job_fetch = _cache_only_fetch(r)  # 콜드 미스 KIS 폭주 방지
    if job.get("type") == "screen" and isinstance(job.get("spec"), dict) and uses_flow_setups(job["spec"]):
        # 스펙이 외국인·기관 수급 셋업을 실제로 참조할 때만(§스크리닝 강화) — 불필요한 네이버 호출 방지.
        # flow_universe 를 지역 import(순환 임포트 회피 — flow_universe.py 가 이 모듈을 bw 로 가져다 씀).
        from tools import flow_universe
        from tools.naver_investor import daily_investor_flow
        if job.get("universe"):
            job["_flow_by_code"] = flow_universe.load_flow_cache(r, job.get("codes") or [])  # 캐시 전용
        else:
            try:
                codes = _parse_codes(job)
            except ValueError:
                codes = []
            job["_flow_by_code"] = {c: daily_investor_flow(c) for c in codes}  # 라이브(MAX_CODES 상한 안전)
    outcome = process_job(job, job_fetch)
    try:
        r.set(RESULT_KEY.format(outcome["job_id"]),
              json.dumps(outcome, ensure_ascii=False), ex=RESULT_TTL)
    except Exception:
        pass  # best-effort — 결과 기록 실패는 콘솔 폴링 타임아웃으로 귀결(매매 무관)
    return True


def drain(r, fetch_bars, max_jobs: int = MAX_JOBS_PER_RUN) -> int:
    """큐를 최대 max_jobs 건 LPOP 처리하고 결과를 RESULT_KEY 에 기록. 처리 건수 반환(cron 모드)."""
    n = 0
    for _ in range(max_jobs):
        raw = r.lpop(QUEUE_KEY)
        if raw is None:
            break
        if _handle_raw(r, raw, fetch_bars):
            n += 1
    return n


_STOP = False  # 데몬 종료 플래그(SIGTERM/SIGINT 로 set)


def serve(r, fetch_bars, poll_timeout: int = 5, should_continue=None) -> int:
    """데몬 모드 — BLPOP 으로 잡을 **즉시** 받아 처리(cron ~60s 지연 제거). 처리 건수 반환.
    유휴 시 BLPOP 블로킹이라 CPU·KIS 호출 0. 종료 신호(should_continue=False)까지 무한 루프.
    Redis 블립·잡 처리 예외는 잡고 계속(데몬은 안 죽는다)."""
    should_continue = should_continue or (lambda: not _STOP)
    n = 0
    while should_continue():
        try:
            item = r.blpop(QUEUE_KEY, timeout=poll_timeout)
        except Exception:
            time.sleep(1)  # Redis 블립 — 잠깐 쉬고 재시도(루프 유지)
            continue
        if item is None:
            continue  # 타임아웃(큐 빔) — 루프 지속(종료신호 점검)
        if _handle_raw(r, item[1], fetch_bars):
            n += 1
    return n


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    daemon = "--daemon" in argv
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL 미설정 — 워커는 Redis 큐 전용(폴백 없음)")
        return 1
    import redis  # lazy: REDIS_URL 있을 때만

    r = redis.from_url(redis_url, decode_responses=True)
    fetch = _cached_fetch(r, naver_fetch)  # 네이버 일봉(무한도) + Redis 캐시로 반복 호출 격감
    if daemon:
        import signal

        def _on_term(signum, frame):
            global _STOP
            _STOP = True
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
        print("백테스트 워커 데몬 시작 — BLPOP 대기(즉시 처리)")
        n = serve(r, fetch)
        print(f"백테스트 워커 데몬 종료 — 누적 {n}건 처리")
        return 0
    n = drain(r, fetch)
    print(f"백테스트 잡 {n}건 처리")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
