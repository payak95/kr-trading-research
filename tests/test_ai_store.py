# AI 섀도 저장소(SQLite) 단위 테스트 — 멱등 기록·미평가 조회·지평별 수익 갱신 (임시 DB, 네트워크 없음)
"""실행: python tests/test_ai_store.py"""
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kr_research.core.ai_store import AiStore, decide_virtual_trade


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        s = AiStore(db_path=os.path.join(d, "t.db"))

        rec = {"code": "005930", "trade_date": "20260701", "action": "buy",
               "confidence": 0.7, "reason": "상승 추세", "entry_price": 70000,
               "snapshot": {"close": 70000, "rsi14": 55.0}}

        # 멱등: 같은 (config_name, code, signaled_at) 재기록은 무시
        with patch("kr_research.core.ai_store.time.time", return_value=1000.0):
            s.record_judgment("cfg1", rec)
            s.record_judgment("cfg1", {**rec, "action": "sell"})  # 같은 signaled_at → 무시돼야
        rows = s.get_judgments()
        assert len(rows) == 1 and rows[0]["action"] == "buy", "멱등: 첫 기록 유지"

        # config 가 다르면 별개 레코드(같은 signaled_at 이어도 UNIQUE 키가 다름)
        with patch("kr_research.core.ai_store.time.time", return_value=1000.0):
            s.record_judgment("cfg2", rec)
        assert len(s.get_judgments()) == 2

        # 미평가 조회 → 전부 open
        open_rows = s.get_open_judgments(horizons=(1, 5, 20))
        assert len(open_rows) == 2

        # 지평별 수익 갱신 — None 은 건너뛰고, 채워진 것만 반영
        jid = open_rows[0]["id"]
        s.set_judgment_returns(jid, {1: 0.02, 5: None, 20: None}, {1: 0.01, 5: None, 20: None})
        row = [r for r in s.get_judgments() if r["id"] == jid][0]
        assert row["ret_d1"] == 0.02 and row["bench_d1"] == 0.01
        assert row["ret_d5"] is None and row["evaluated_at"] is not None

        # D+1만 채워졌으니 여전히 open(D+5/D+20 미평가)
        assert jid in [r["id"] for r in s.get_open_judgments(horizons=(1, 5, 20))]
        assert jid not in [r["id"] for r in s.get_open_judgments(horizons=(1,))], "D+1만 보면 평가 완료"

        # config_name 필터 — 개별 종목 뷰와 유니버스 스캔 뷰를 안 섞기 위함
        assert len(s.get_judgments(config_name="cfg1")) == 1
        assert len(s.get_judgments(config_name="cfg2")) == 1
        assert len(s.get_judgments(config_name="없는설정")) == 0

        # last_trade_date — 스케줄러 dedup 판정용
        assert s.last_trade_date("cfg1", "005930") == "20260701"
        assert s.last_trade_date("cfg1", "000660") is None, "다른 종목은 이력 없음"

        # ── decide_virtual_trade: 순수 함수(trading/strategy.py self._holding 게이트와 동일 규칙) ──
        assert decide_virtual_trade(None, "hold", 70000) is None, "hold 는 항상 무동작"
        assert decide_virtual_trade(None, "sell", 70000) is None, "미보유 매도는 무동작(공매도 없음)"

        opened = decide_virtual_trade(None, "buy", 70000, order_krw=1_000_000)
        assert opened == {"kind": "open", "qty": 14}, opened  # floor(1,000,000/70000)=14

        # 이미 보유 중인데 또 buy → 무동작(반복 매수 무시, 보유 유지)
        fake_pos = {"id": 1, "qty": 14, "avg_price": 70000.0}
        assert decide_virtual_trade(fake_pos, "buy", 71000) is None, "보유 중 반복 매수는 무시돼야 함"

        # 보유 중 매도 → 전량 청산 + 실현손익 계산
        closed = decide_virtual_trade(fake_pos, "sell", 77000)
        assert closed["kind"] == "close" and closed["qty"] == 14
        assert abs(closed["realized_pnl"] - (77000 - 70000) * 14) < 1e-6, closed
        assert abs(closed["realized_return_pct"] - (77000 / 70000 - 1)) < 1e-9, closed

        # 고가주(예: SK하이닉스) — 1주 가격이 예산을 초과해도 조용히 skip 하지 않고 최소 1주는 사야 함
        expensive = decide_virtual_trade(None, "buy", 1_500_000, order_krw=1_000_000)
        assert expensive == {"kind": "open", "qty": 1}, expensive

        assert decide_virtual_trade(None, "buy", 0) is None, "진입가<=0 은 무동작(방어)"

        # ── min_confidence 사이징: buy 는 confidence 미달이면 무동작, sell 은 confidence 무관 항상 허용 ──
        assert decide_virtual_trade(None, "buy", 70000, confidence=0.5, min_confidence=0.7) is None, \
            "확신도 미달 매수는 무동작"
        assert decide_virtual_trade(None, "buy", 70000, confidence=None, min_confidence=0.7) is None, \
            "confidence 없음+임계값 설정 시 매수 무동작(방어)"
        gated_open = decide_virtual_trade(None, "buy", 70000, order_krw=1_000_000, confidence=0.8, min_confidence=0.7)
        assert gated_open == {"kind": "open", "qty": 14}, "확신도 충족 매수는 정상 진행"
        gated_close = decide_virtual_trade(fake_pos, "sell", 77000, confidence=0.1, min_confidence=0.7)
        assert gated_close["kind"] == "close", "매도는 확신도 임계값과 무관하게 항상 허용"

        # ── 가상 포지션 원장 라운드트립 ──
        with patch("kr_research.core.ai_store.time.time", return_value=2000.0):
            s.open_position("cfg1", "005930", 14, 70000.0, "20260701")
        pos = s.get_open_position("cfg1", "005930")
        assert pos is not None and pos["qty"] == 14 and pos["status"] == "open"
        assert s.get_open_position("cfg1", "000660") is None, "다른 종목은 열린 포지션 없음"

        # 같은 (config,code) 에 또 open_position 하면 UNIQUE 인덱스(status='open') 위반 → 에러
        try:
            s.open_position("cfg1", "005930", 5, 71000.0, "20260702")
            assert False, "열린 포지션이 이미 있는데 또 열면 부분 유니크 인덱스 위반으로 에러여야 함"
        except Exception:
            pass

        with patch("kr_research.core.ai_store.time.time", return_value=3000.0):
            s.close_position(pos["id"], 77000.0, "20260705", 98000.0, 0.1)
        assert s.get_open_position("cfg1", "005930") is None, "청산 후엔 열린 포지션 없음"
        closed_rows = s.get_positions(config_name="cfg1")
        assert len(closed_rows) == 1 and closed_rows[0]["status"] == "closed"
        assert closed_rows[0]["realized_pnl"] == 98000.0 and closed_rows[0]["exit_price"] == 77000.0

        # 청산 후 같은 종목에 재매수 가능(열린 포지션 없으니 유니크 인덱스에 안 걸림)
        s.open_position("cfg1", "005930", 10, 78000.0, "20260706")
        assert s.get_open_position("cfg1", "005930")["qty"] == 10

        s.close()  # Windows: 임시 디렉터리 삭제 전 연결 닫기(core/store.py 테스트와 동일 관례)

    print("✅ test_ai_store: 멱등 기록·미평가 조회·수익 갱신·config_name 필터·last_trade_date·"
          "decide_virtual_trade(반복매수무시·전량청산·고가주최소1주)·가상포지션원장 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
