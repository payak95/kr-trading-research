# 외국인·기관 수급(순매수) 셋업 — 스크리닝 강화(큰손 추적, AlphaForge flow.foreign_accumulation 벤치마크)
"""`tools/naver_investor.daily_investor_flow(code)`가 반환하는 종목별 일별 순매매 수량(bars 아님 —
별도 데이터)에서 "최근 window_days 순매수 합 / 같은 기간 거래량 합" 비율(정규화 강도)을 계산해
min_strength 이상이면 활성. AlphaForge 관측 파라미터(WINDOW_DAYS=20·MIN_STRENGTH=0.05)를 기본값으로
그대로 채택. `trading/setups.py`와 동일 스타일(순수·badge 반환) — `active_items`가 낸 badge 를
`trading/spec.py::screen`의 `extra_active`로 넘겨 기존 `{"setup": key}` 조건 DSL에 그대로 합류시킨다.

⚠️ **스크리닝 전용**: 일별 이력이 필요해 데이터가 부족하면(window_days 미만) None — 과거 임의 시점의
누적 이력을 보관하지 않으므로 `②백테스트`(historical replay)에는 반영되지 않는다(그 경로는 이 모듈을
호출 안 함, §스크리닝 강화 계획 참고).
"""
SETUP_KEYS = frozenset({"foreign_accumulation", "institution_accumulation"})


def _badge(key, label, tone, detail):
    return {"key": key, "label": label, "tone": tone, "detail": detail}


def _ratio(flow_rows: list[dict], qty_key: str, window_days: int):
    """최근 window_days 의 (qty_key 합) / (거래량 합). 데이터가 window_days 미만이면 None."""
    if len(flow_rows) < window_days:
        return None
    recent = flow_rows[-window_days:]
    net = sum(r[qty_key] for r in recent if r.get(qty_key) is not None)
    vol = sum(r["volume"] for r in recent if r.get("volume") is not None)
    if not vol:
        return None
    return net / vol


def foreign_accumulation(flow_rows: list[dict], window_days: int = 20, min_strength: float = 0.05):
    """외국인 수급 유입 — 최근 window_days 외국인 순매수/거래량 비율이 min_strength 이상. 부족/미달 None."""
    ratio = _ratio(flow_rows, "frgn_ntby_qty", window_days)
    if ratio is None or ratio < min_strength:
        return None
    return _badge("foreign_accumulation", "외국인 수급 유입", "bull",
                  f"최근 {window_days}일 순매수/거래량 {ratio * 100:.1f}%")


def institution_accumulation(flow_rows: list[dict], window_days: int = 20, min_strength: float = 0.05):
    """기관 수급 유입 — foreign_accumulation 과 동일 정의, 기관계 순매수 기준."""
    ratio = _ratio(flow_rows, "orgn_ntby_qty", window_days)
    if ratio is None or ratio < min_strength:
        return None
    return _badge("institution_accumulation", "기관 수급 유입", "bull",
                  f"최근 {window_days}일 순매수/거래량 {ratio * 100:.1f}%")


def active_items(flow_rows: list[dict] | None, window_days: int = 20, min_strength: float = 0.05) -> list[dict]:
    """flow_rows(없으면 [])에서 활성 수급 셋업 badge 목록 — `trading.setups.compute_setups`의
    `{"items": [...]}` 와 동일 shape, `spec.screen`의 extra_active 로 그대로 병합."""
    if not flow_rows:
        return []
    items = []
    for fn in (foreign_accumulation, institution_accumulation):
        try:
            b = fn(flow_rows, window_days, min_strength)
        except Exception:
            b = None
        if b:
            items.append(b)
    return items
