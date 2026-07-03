# 조건검색 후보의 전진검증 — 신호 병합·forward 수익/청산-추적 적용·전략별 집계 (순수 로직, I/O 없음)
"""콘솔 조건검색으로 찾은 후보를 '추적 시작'하면, 그 (전략·종목·검색일·진입가)를 신호로 남기고
이후 실제 미래 종가로 D+N 수익을 재 전략이 진짜 통하는지 무주문 검증한다(연구 레이어).
라이브 전진검증(`trading/tracking.py`)의 순수함수를 재활용하되, 묶음 기준은 mode 가 아니라 **전략**.
I/O(요청 드레인·네이버 일봉·publish)는 tools/screen_track_eval.py 담당, 여기는 단위 테스트 가능한 순수만.

**청산-추적(파이프라인 자동화 Phase 2)**: 기존 D+N(`ret_d{n}`)은 고정 시점 가격만 보는데, 이건 "언제
팔지"를 전략이 아니라 평가 시점이 임의로 정하는 것 — `apply_exit_tracking`이 그 전략의 실제 청산 규칙
(손절·익절·spec exit 트리, `trading.tracking.evaluate_exit`)이 언제 발동하는지 매일 새 봉을 따라가며
판정해 정확한 실현 수익률을 신호에 추가로 채운다. 기존 D+N 필드는 그대로 유지(애더티브, 콘솔 UI 무변경).
"""
from kr_research.trading.tracking import (GATE, HORIZONS, MAX_HOLD_DAYS, _agg, benchmark_returns, evaluate_exit,
                              forward_returns)

MAX_SIGNALS = 5000  # 저장 신호 상한(오래된 것부터 버림 — 무한 성장 방지)

# Redis 키(전역 — 연구 레이어). 콘솔이 requests RPUSH, 봇 eval 이 signals/summary 소유.
TRACK_REQUESTS_KEY = "bot:screen:track:requests"  # List — 콘솔 추적요청 {strategy,code,entry_price,screen_date}
TRACK_SIGNALS_KEY = "bot:screen:track:signals"    # String(JSON) — 봇 소유 정규 신호 저장소(평가값 포함)
TRACK_SUMMARY_KEY = "bot:screen:track:summary"    # String(JSON) — 봇 발행 {by_strategy,all}, 콘솔 표시용
TRACK_VALIDATED_PREV_KEY = "bot:screen:track:validated_prev"  # String(JSON list) — 직전 실행 validated=True 전략명(알림 전이감지용, 봇 내부 상태)


def signal_id(strategy: str, code: str, screen_date: str) -> str:
    """신호 자연키 — 같은 전략·종목·검색일은 1건(중복 추적 방지)."""
    return f"{strategy}|{code}|{screen_date}"


def request_to_signal(req: dict) -> dict | None:
    """콘솔 추적요청 1건 → 신호 dict(미평가). 필수 필드 누락·진입가<=0 이면 None(skip)."""
    strategy = str(req.get("strategy") or "").strip()
    code = str(req.get("code") or "").strip()
    date = str(req.get("screen_date") or "").strip()
    try:
        entry = float(req.get("entry_price"))
    except (TypeError, ValueError):
        return None
    if not (strategy and code and date) or entry <= 0:
        return None
    sig = {"id": signal_id(strategy, code, date), "strategy": strategy, "code": code,
           "screen_date": date, "entry_price": entry}
    for n in HORIZONS:
        sig[f"ret_d{n}"] = None
        sig[f"bench_d{n}"] = None
    # 청산-추적(Phase 2) 필드 — evaluate_exit 이 채움, 그 전까지 미평가 기본값
    sig.update({"exited": False, "exit_date": None, "exit_price": None, "exit_reason": None,
               "holding_days": None, "realized_return_pct": None, "open_return_pct": None, "capped": False})
    return sig


def merge_signals(existing: list[dict], requests: list[dict], cap: int = MAX_SIGNALS) -> list[dict]:
    """기존 신호 + 새 요청(중복 id 제외) → 최신 cap 건. 기존의 평가값은 보존."""
    seen = {s["id"] for s in existing}
    out = list(existing)
    for req in requests:
        sig = request_to_signal(req)
        if sig and sig["id"] not in seen:
            seen.add(sig["id"])
            out.append(sig)
    return out[-cap:]


def apply_returns(signals: list[dict], bars_by_code: dict, bench_bars: list[dict],
                  horizons=HORIZONS) -> int:
    """미평가 지평을 네이버 일봉으로 채운다(이미 값 있는 지평은 보존). 갱신된 신호 수 반환.
    bars_by_code[code]: [{date,close}] 시간순. bench_bars: 벤치마크 동형. in-place 갱신."""
    updated = 0
    for s in signals:
        bars = bars_by_code.get(s["code"])
        if not bars:
            continue
        rets = forward_returns(bars, s["screen_date"], s["entry_price"], horizons)
        brets = benchmark_returns(bench_bars, s["screen_date"], horizons)
        changed = False
        for n in horizons:
            if s.get(f"ret_d{n}") is None and rets[n] is not None:
                s[f"ret_d{n}"] = rets[n]
                changed = True
            if s.get(f"bench_d{n}") is None and brets[n] is not None:
                s[f"bench_d{n}"] = brets[n]
                changed = True
        if changed:
            updated += 1
    return updated


def apply_exit_tracking(signals: list[dict], bars_by_code: dict, strategies: dict, *,
                        stop_loss_pct: float | None = None, take_profit_pct: float | None = None,
                        fee_rate: float = 0.0, tax_rate: float = 0.0, slippage: float = 0.0,
                        max_hold_days: int = MAX_HOLD_DAYS) -> int:
    """청산 규칙(손절·익절·spec exit 트리) 반영 정확한 실현 수익률 평가 — 아직 청산·capped 아닌 신호만 대상
    (`trading.tracking.evaluate_exit` 재사용). strategies: {전략명: spec}(`bot:strategies` 스냅샷) — 이름
    변경·삭제로 못 찾으면 skip(안전 무시). bars_by_code[code]: 전체 OHLCV(저가/고가 포함) 필요 —
    D+N 용 `_slim()` 결과(종가만)로는 평가 불가. in-place 갱신. 평가(호출)한 신호 수 반환."""
    updated = 0
    for s in signals:
        if s.get("exited") or s.get("capped"):
            continue
        spec = strategies.get(s.get("strategy"))
        bars = bars_by_code.get(s.get("code"))
        if not spec or not bars:
            continue
        res = evaluate_exit(spec, {}, s.get("entry_price"), s.get("screen_date"), bars,
                            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                            fee_rate=fee_rate, tax_rate=tax_rate, slippage=slippage,
                            max_hold_days=max_hold_days)
        s.update(res)
        updated += 1
    return updated


def _agg_realized(rows: list[dict], gate=GATE) -> dict:
    """청산 완료(exited=True) 신호만 집계 — 평균 실현수익률·승률·평균 보유일 + 게이트(§forward-tracking-design
    §6 임계치 재사용). D+N 집계(`_agg`)와 별도 키로 병행(기존 `validated` 무변경, `validated_realized` 신설)."""
    exited = [r for r in rows if r.get("exited")]
    rets = [r["realized_return_pct"] for r in exited if r.get("realized_return_pct") is not None]
    holds = [r["holding_days"] for r in exited if r.get("holding_days") is not None]
    wins = [v for v in rets if v > 0]
    avg = (sum(rets) / len(rets)) if rets else None
    win_rate = (len(wins) / len(rets)) if rets else None
    avg_hold = (sum(holds) / len(holds)) if holds else None
    validated = bool(len(rets) >= gate["min_n"] and win_rate is not None and win_rate >= gate["win_d5"]
                     and avg is not None and avg > 0)
    return {"n_exited": len(exited), "avg_realized_return_pct": avg, "win_rate_realized": win_rate,
            "avg_holding_days": avg_hold, "validated_realized": validated}


def summary_by_strategy(signals: list[dict], horizons=HORIZONS, gate=GATE) -> dict:
    """전략별 + 전체 집계(D+N 평가수·평균·승률·초과수익·검증여부 + 청산-추적 실현수익률 집계). 콘솔 검증
    패널이 표시. 반환 {"by_strategy": {name: agg}, "all": agg}."""
    groups: dict = {}
    for s in signals:
        groups.setdefault(s.get("strategy") or "?", []).append(s)
    return {"by_strategy": {name: {**_agg(rows, horizons, gate), **_agg_realized(rows, gate)}
                            for name, rows in groups.items()},
            "all": {**_agg(signals, horizons, gate), **_agg_realized(signals, gate)}}


def open_codes(signals: list[dict], horizons=HORIZONS) -> set:
    """아직 미평가 지평이 남은 신호들의 종목코드(평가 대상만 일봉 조회 — 호출 절약)."""
    return {s["code"] for s in signals
            if any(s.get(f"ret_d{n}") is None for n in horizons)}


def open_codes_exit_tracking(signals: list[dict]) -> set:
    """아직 청산·capped 안 된 신호들의 종목코드(청산-추적 전체 OHLCV 조회 대상만 — 호출 절약).
    D+N 의 `open_codes`와 별개 기준(D+N 은 D+20 안에 끝나지만 청산-추적은 max_hold_days 까지 이어짐)."""
    return {s["code"] for s in signals if not s.get("exited") and not s.get("capped")}


def pipeline_signals(strategy: str, stage1: dict, stage2: dict) -> list[dict]:
    """파이프라인 자동화 §5 마지막 연결고리 — ②백테스트 통과 종목을 ①스크리닝 후보의 종가(entry_price)·
    검색일(screen_date)과 조인해 추적요청 리스트로(스키마는 `request_to_signal` 입력과 동일).
    strategy 없으면 [](자동 등록 skip — 저장 전략 아니면 ④ 청산-추적이 spec 을 못 찾아 평가 불가하므로
    호출부가 저장 전략명만 넘기도록 강제). ①에 없는 종목(last/date 누락)은 skip."""
    strategy = str(strategy or "").strip()
    if not strategy:
        return []
    candidates = {c["code"]: c for c in stage1.get("candidates", []) if c.get("code")}
    out = []
    for row in stage2.get("results", []):
        code = row.get("code")
        cand = candidates.get(code)
        if not cand or cand.get("last") is None or not cand.get("date"):
            continue
        out.append({"strategy": strategy, "code": code, "entry_price": cand["last"],
                    "screen_date": cand["date"]})
    return out


def newly_validated(prev: list[str], summary: dict) -> list[str]:
    """직전 실행 대비 새로 validated=True 로 전환된 전략명(알림용) — 정렬 리스트. 순수 함수(테스트 대상)."""
    now = {name for name, agg in summary.get("by_strategy", {}).items() if agg.get("validated")}
    return sorted(now - set(prev))
