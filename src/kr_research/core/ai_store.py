# AI 섀도 판단 전용 SQLite 저장소 — core/store.py(매수 전용 signals)와 별개, action(buy/sell/hold) 포함
"""tools/llm_shadow.py 의 판단(run_once 결과)을 영구 저장하고, tools/ai_forward_eval.py 가 D+N 수익률을
채운다. core/store.py 의 signals 테이블은 매수 신호 전용(action 개념 없음)이라 건드리지 않고, 같은
멱등(UNIQUE)·부분 갱신 패턴만 그대로 베껴 별도 테이블/파일로 분리했다."""
import json
import os
import sqlite3
import threading
import time

_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
UNIVERSE_CONFIG_NAME = "universe_daily"  # 예약된 config_name — tools/ai_universe_scan.py 전용.
# 개별 종목 관찰(ai_shadow_scheduler.py/콘솔 AI 테스트 탭)과 유니버스 스캔은 같은 테이블을 쓰되
# 이 이름으로 구분해 두 화면(Redis 발행 키)이 서로 안 섞이게 한다.


class AiStore:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            os.makedirs(_STATE_DIR, exist_ok=True)
            db_path = os.path.join(_STATE_DIR, "ai_shadow.db")
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        self._db.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_judgments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_name TEXT, code TEXT, signaled_at REAL, trade_date TEXT,
                    action TEXT, confidence REAL, reason TEXT, entry_price REAL, snapshot_json TEXT,
                    market_context_analysis TEXT, counter_argument TEXT, model TEXT,
                    ret_d1 REAL, ret_d5 REAL, ret_d20 REAL,
                    bench_d1 REAL, bench_d5 REAL, bench_d20 REAL, evaluated_at REAL,
                    UNIQUE(config_name, code, signaled_at)
                );
                CREATE TABLE IF NOT EXISTS ai_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_name TEXT, code TEXT, status TEXT,              -- 'open' | 'closed'
                    qty INTEGER, avg_price REAL, entry_trade_date TEXT, opened_at REAL,
                    exit_price REAL, exit_trade_date TEXT, closed_at REAL,
                    realized_pnl REAL, realized_return_pct REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_positions_open
                    ON ai_positions(config_name, code) WHERE status='open';
                """
            )
            # CREATE TABLE IF NOT EXISTS 는 이미 만들어진 기존 DB엔 새 컬럼을 안 만든다 — CoT 필드(v2
            # 프롬프트)·model 태깅 도입 시점에 이미 운영 중인 DB를 위한 최소 마이그레이션.
            for col in ("market_context_analysis TEXT", "counter_argument TEXT", "model TEXT"):
                try:
                    self._db.execute(f"ALTER TABLE ai_judgments ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # 이미 있음(신규 DB는 위 CREATE 에 이미 포함, 기존 DB는 방금 추가됨)
            self._db.commit()

    def record_judgment(self, config_name: str, rec: dict) -> None:
        """run_once() 결과(rec: code/trade_date/entry_price/snapshot/action/confidence/reason/
        market_context_analysis/counter_argument/model) 1건 기록.
        멱등: 같은 (config_name, code, signaled_at) 재기록은 무시(스케줄러가 trade_date 로 이미 중복
        호출을 막지만, 저장 레벨에서도 방어)."""
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO ai_judgments"
                "(config_name, code, signaled_at, trade_date, action, confidence, reason, entry_price,"
                " snapshot_json, market_context_analysis, counter_argument, model)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (config_name, rec.get("code"), time.time(), rec.get("trade_date"),
                 rec.get("action"), rec.get("confidence"), rec.get("reason"), rec.get("entry_price"),
                 json.dumps(rec.get("snapshot"), ensure_ascii=False),
                 rec.get("market_context_analysis"), rec.get("counter_argument"), rec.get("model")),
            )
            self._db.commit()

    def get_open_judgments(self, horizons=(1, 5, 20)) -> list[dict]:
        """어느 지평이든 ret_d{n} 또는 bench_d{n} 이 NULL인 판단(평가 대상). ai_forward_eval.py 가 사용."""
        cols = " OR ".join(f"ret_d{int(n)} IS NULL OR bench_d{int(n)} IS NULL" for n in horizons)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM ai_judgments WHERE {cols} ORDER BY signaled_at").fetchall()
        return [dict(r) for r in rows]

    def set_judgment_returns(self, judgment_id: int, rets: dict, benches: dict | None = None) -> None:
        """평가된 지평의 ret_d{n}(+선택 bench_d{n})만 갱신(None 지평은 건너뜀, 기존 값 덮어쓰지 않음)."""
        items = [(f"ret_d{int(n)}", v) for n, v in rets.items() if v is not None]
        items += [(f"bench_d{int(n)}", v) for n, v in (benches or {}).items() if v is not None]
        if not items:
            return
        sets = ", ".join(f"{c}=?" for c, _ in items)
        with self._lock:
            self._db.execute(
                f"UPDATE ai_judgments SET {sets}, evaluated_at=? WHERE id=?",
                (*[v for _, v in items], time.time(), judgment_id))
            self._db.commit()

    def get_judgments(self, limit: int | None = None, config_name: str | None = None) -> list[dict]:
        """판단 이력(최신순). limit=None=전체(집계용), 숫자=콘솔 표용 최근 N.
        config_name 주면 그 설정만(개별 종목 관찰 뷰와 유니버스 스캔 뷰를 안 섞기 위함)."""
        q = "SELECT * FROM ai_judgments"
        params: tuple = ()
        if config_name is not None:
            q += " WHERE config_name=?"
            params = (config_name,)
        q += " ORDER BY signaled_at DESC"
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._db.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def last_trade_date(self, config_name: str, code: str) -> str | None:
        """이 config 의 직전 판단 trade_date — 스케줄러가 동일 일봉 재호출(Gemini 낭비) 여부 판정에 사용."""
        with self._lock:
            row = self._db.execute(
                "SELECT trade_date FROM ai_judgments WHERE config_name=? AND code=?"
                " ORDER BY signaled_at DESC LIMIT 1", (config_name, code)).fetchone()
        return row["trade_date"] if row else None

    # ── 가상 포지션 원장(② 타겟 종목 관찰 전용) — decide_virtual_trade() 의 결정을 기록만 함 ──

    def get_open_position(self, config_name: str, code: str) -> dict | None:
        """이 (config_name,code) 의 열린 가상 포지션(있으면 하나뿐 — idx_ai_positions_open 이 보장)."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM ai_positions WHERE config_name=? AND code=? AND status='open'",
                (config_name, code)).fetchone()
        return dict(row) if row else None

    def open_position(self, config_name: str, code: str, qty: int, price: float, trade_date: str) -> None:
        """가상 매수 체결 기록 — decide_virtual_trade() 가 {"kind":"open"} 을 반환했을 때만 호출."""
        with self._lock:
            self._db.execute(
                "INSERT INTO ai_positions(config_name, code, status, qty, avg_price, entry_trade_date, opened_at)"
                " VALUES (?,?,'open',?,?,?,?)",
                (config_name, code, qty, price, trade_date, time.time()))
            self._db.commit()

    def close_position(self, position_id: int, exit_price: float, trade_date: str,
                       realized_pnl: float, realized_return_pct: float | None) -> None:
        """가상 매도(전량) 체결 기록 — decide_virtual_trade() 가 {"kind":"close"} 을 반환했을 때만 호출."""
        with self._lock:
            self._db.execute(
                "UPDATE ai_positions SET status='closed', exit_price=?, exit_trade_date=?, closed_at=?,"
                " realized_pnl=?, realized_return_pct=? WHERE id=?",
                (exit_price, trade_date, time.time(), realized_pnl, realized_return_pct, position_id))
            self._db.commit()

    def get_positions(self, config_name: str | None = None, limit: int | None = None) -> list[dict]:
        """가상 포지션 이력(열림+청산 모두, 최신순) — 콘솔 표시용. config_name 주면 그 설정만."""
        q = "SELECT * FROM ai_positions"
        params: tuple = ()
        if config_name is not None:
            q += " WHERE config_name=?"
            params = (config_name,)
        q += " ORDER BY opened_at DESC"
        if limit is not None:
            q += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._db.execute(q, params).fetchall()
        return [dict(r) for r in rows]


VIRTUAL_ORDER_KRW = 1_000_000  # 매수 1회당 가상 투입 금액(고정 전역 상수). config별 설정은 차후 확장 여지.


def decide_virtual_trade(open_position: dict | None, action: str, entry_price: float,
                          order_krw: float = VIRTUAL_ORDER_KRW, confidence: float | None = None,
                          min_confidence: float | None = None) -> dict | None:
    """trading/strategy.py 의 self._holding 게이트와 동일 규칙(순수 함수, I/O 없음) — 보유 중 아닐 때만
    buy 로 새 포지션을 열고(반복 매수 무시), 보유 중일 때만 sell 로 전량 청산(부분 청산 없음 — 산 수량
    그대로 판다는 라이브 설계와 동일). hold 는 항상 무동작.
    min_confidence 를 주면(config 별 사이징 실험) confidence 가 그 미만인 buy 는 무동작 처리 —
    청산(sell)은 확신도와 무관하게 항상 그대로 허용(포지션 정리는 언제든 가능해야 함).
    반환: {"kind":"open","qty":int} | {"kind":"close","qty":int,"realized_pnl":float,
    "realized_return_pct":float|None} | None(무동작 — hold, 보유중 반복매수, 미보유 매도, 확신도 미달)."""
    if action == "buy" and open_position is None:
        if entry_price <= 0:
            return None
        if min_confidence is not None and (confidence is None or confidence < min_confidence):
            return None
        qty = max(1, int(order_krw // entry_price))  # 1주가 예산 초과해도 최소 1주(예: 고가주) — 조용히 skip 안 함
        return {"kind": "open", "qty": qty}
    if action == "sell" and open_position is not None:
        qty, avg = open_position["qty"], open_position["avg_price"]
        pnl = (entry_price - avg) * qty
        ret_pct = (entry_price / avg - 1) if avg else None
        return {"kind": "close", "qty": qty, "realized_pnl": pnl, "realized_return_pct": ret_pct}
    return None
