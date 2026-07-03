# 외국인·기관 수급 셋업(trading/flow.py) 검증 — 합성 flow_rows(네트워크 없음)
"""실행: python tests/test_flow.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.flow import active_items, foreign_accumulation, institution_accumulation


def _rows(n, frgn, orgn, vol=1_000_000):
    """n일치 합성 flow_rows — 날짜는 형식만 맞추고(테스트엔 안 씀) qty/volume 만 의미."""
    return [{"date": f"2026070{i % 9 + 1}", "close": 100, "volume": vol,
            "frgn_ntby_qty": frgn, "orgn_ntby_qty": orgn} for i in range(n)]


def main() -> int:
    # ── 외국인 순매수/거래량 20일 평균 = 100,000/1,000,000 = 10% ≥ 5%(기본 min_strength) → 활성 ──
    strong = _rows(20, frgn=100_000, orgn=0)
    b = foreign_accumulation(strong)
    assert b is not None and b["key"] == "foreign_accumulation" and b["tone"] == "bull", b
    assert institution_accumulation(strong) is None, "기관 순매수 0이면 기관 셋업은 비활성"

    # ── 임계 미달(1%) → None ──
    weak = _rows(20, frgn=10_000, orgn=0)
    assert foreign_accumulation(weak) is None, "1% < 5% 미달"

    # ── 데이터 부족(window_days=20 미만) → None(fail-safe) ──
    short = _rows(10, frgn=100_000, orgn=0)
    assert foreign_accumulation(short) is None, "10일치뿐이면 20일 윈도 부족"

    # ── window_days 파라미터 조정 시 그 윈도만 사용 ──
    assert foreign_accumulation(short, window_days=10) is not None, "window_days=10 이면 10일치로 충분"

    # ── active_items: 둘 다 활성이면 둘 다 포함, None/빈 리스트는 안전 처리 ──
    both = _rows(20, frgn=100_000, orgn=100_000)
    items = active_items(both)
    assert {i["key"] for i in items} == {"foreign_accumulation", "institution_accumulation"}, items
    assert active_items(None) == [] and active_items([]) == [], "빈 입력 → 빈 리스트"

    print("✅ test_flow: foreign/institution_accumulation(임계·윈도·부족데이터)·active_items 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
