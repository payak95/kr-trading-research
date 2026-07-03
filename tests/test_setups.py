# 기술적 셋업 판정 단위 테스트 — 정/역배열·골든/데드크로스·박스돌파·볼린저스퀴즈·RSI필터·거래량폭증 (네트워크 없음)
"""실행: python tests/test_setups.py
telegram-market-bot 차트 셋업과 동일 어휘를 kr-trading-bot 데이터(bars)로 이식한 모듈 검증."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.trading.setups import (
    active_keys, bollinger_squeeze, box_breakout, compute_setups,
    golden_cross, ma_alignment, rsi_filter, vcp, volume_surge)


def _bars(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    highs = highs or closes
    lows = lows or closes
    volumes = volumes or [1000] * n
    return [{"open": closes[i], "high": highs[i], "low": lows[i],
             "close": closes[i], "volume": volumes[i]} for i in range(n)]


def main() -> int:
    up = [float(x) for x in range(1, 81)]            # 단조 상승
    down = [float(x) for x in range(80, 0, -1)]      # 단조 하락

    # 1. 정/역배열
    assert ma_alignment(up)["tone"] == "bull" and ma_alignment(up)["label"] == "정배열"
    assert ma_alignment(down)["tone"] == "bear"
    assert ma_alignment([1, 2, 3]) is None

    # 2. 골든/데드 크로스 — 최근 within 봉 이내만
    flat_up = [100.0] * 25 + [101, 103, 106, 110, 115]
    assert golden_cross(flat_up)["tone"] == "bull", golden_cross(flat_up)
    flat_down = [100.0] * 25 + [99, 97, 94, 90, 85]
    assert golden_cross(flat_down)["tone"] == "bear", golden_cross(flat_down)
    assert golden_cross(up) is None                  # 줄곧 상승 → 교차 한참 전 → None

    # 3. 박스 돌파/이탈
    flat = [100.0] * 25
    assert box_breakout(flat + [105.0], flat + [105.0], flat + [105.0])["tone"] == "bull"
    assert box_breakout(flat + [95.0], flat + [95.0], flat + [95.0])["tone"] == "bear"
    assert box_breakout(flat + [100.0], None, None) is None       # 박스 안
    assert box_breakout([1, 2, 3], None, None) is None            # 부족

    # 4. 볼린저 스퀴즈
    squeeze = [100 + (10 if i % 2 else -10) for i in range(40)] + [100 + 0.1 * (i % 2) for i in range(20)]
    assert bollinger_squeeze(squeeze)["tone"] == "warn", bollinger_squeeze(squeeze)
    noisy = [100 + (10 if i % 2 else -10) for i in range(60)]
    assert bollinger_squeeze(noisy) is None                       # 지속 변동 → 수축 아님

    # 5. RSI 필터(과매도/과매수)
    assert rsi_filter(down)["label"] == "RSI 과매도", rsi_filter(down)
    assert rsi_filter(up)["label"] == "RSI 과매수", rsi_filter(up)
    mixed = [100.0]
    for i in range(40):
        mixed.append(mixed[-1] + (1.2 if i % 2 else -1.0))        # 등락 반복 → 중립
    assert rsi_filter(mixed) is None, rsi_filter(mixed)

    # 6. 거래량 폭증
    assert volume_surge([1000.0] * 30 + [5000.0])["tone"] == "warn"
    assert volume_surge([1000.0] * 31) is None
    assert volume_surge([0.0] * 31) is None                       # 평균 0 방어

    # 7. VCP 패턴(변동성 수축, 구간 분할 근사) — 3구간(20봉씩) 연속 수축 + 고점 근접
    contracting = ([100.0 if i % 2 == 0 else 140.0 for i in range(20)]     # 구간1: 범위 40/140=28.6%
                  + [115.0 if i % 2 == 0 else 135.0 for i in range(20)]    # 구간2: 범위 20/135=14.8%(수축)
                  + [128.0 if i % 2 == 0 else 136.0 for i in range(20)])   # 구간3: 범위 8/136=5.9%(더 수축), 종가=136≈고점140
    v = vcp(contracting)
    assert v is not None and v["key"] == "vcp" and v["tone"] == "bull", v
    # 반대(확대) — 구간이 좁음→넓음(볼륨 확대, VCP 아님) → None
    expanding = ([128.0 if i % 2 == 0 else 136.0 for i in range(20)]
                + [115.0 if i % 2 == 0 else 135.0 for i in range(20)]
                + [100.0 if i % 2 == 0 else 140.0 for i in range(20)])
    assert vcp(expanding) is None, "수축 아님(확대) → None"
    assert vcp(contracting[:59]) is None, "lookback(60) 미만 → None"
    # 고점에서 너무 멀어지면(근접 조건 미달) None — 구간 자체는 수축이어도 종가가 저점 쪽이면 탈락
    far_from_high = contracting[:-1] + [128.0]  # 마지막 종가를 구간3 저점으로 교체(고점 140 대비 8.6% 이탈)
    assert vcp(far_from_high) is None, "고점 근접 미달 → None"

    # 8. compute_setups / active_keys 통합(bars) — 상승추세 + 막판 거래량 폭증
    bars = _bars(up, volumes=[1000] * 79 + [6000])
    out = compute_setups(bars)
    assert isinstance(out.get("items"), list) and out["items"], out
    keys = active_keys(bars)
    assert "ma_alignment" in keys and "volume_surge" in keys, keys
    assert compute_setups(_bars([1.0, 2.0, 3.0])) == {"items": []}   # 부족 → 빈 목록 안전

    print("✅ test_setups: ma_alignment·golden_cross·box_breakout·bollinger_squeeze·rsi_filter·volume_surge·vcp "
          "+ compute_setups/active_keys(bars) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
