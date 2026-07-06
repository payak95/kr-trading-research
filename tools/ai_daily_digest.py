# AI 섀도 판단 일일 다이제스트 — 장 마감 후 텔레그램 1통으로 오늘 판단·D+1 적중·가상손익·게이트·LLM 호출 요약
"""'어제 AI 가 얼마나 맞았는지'를 콘솔에 안 들어가고도 알 수 있게(로드맵 §B). ai_forward_eval.py(07:10 UTC,
D+N 평가)가 어제 판단의 ret_d1 을 채운 직후에 돌도록 크론 07:20 UTC(=16:20 KST, 평일)로 예약한다.
거래일이 아니면 no-op. 집계(build_digest 등 순수 함수)와 I/O(main)를 분리해 단위 테스트 가능하게 한다.
적중은 방향이 있는 buy(상승=적중)·sell(하락=적중)만 센다 — hold 는 방향 판단이 아니라 제외.
실행: python tools/ai_daily_digest.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kr_research.core.ai_store import UNIVERSE_CONFIG_NAME, AiStore
from kr_research.core.holidays import is_trading_day
from tools.ai_combo_scheduler import COMBO_PREFIX
from tools.ai_universe_scan import K_SHORTLIST
from tools.llm_shadow import llm_calls_today
from kr_research.trading.tracking import GATE

_KST = timezone(timedelta(hours=9))
_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")


def prev_trading_day(d: datetime, max_days: int = 14) -> str:
    """d(미포함) 직전 거래일의 YYYYMMDD. max_days 안에 못 찾으면 ValueError(캘린더 이상 방어)."""
    cur = d - timedelta(days=1)
    for _ in range(max_days):
        if is_trading_day(cur):
            return cur.strftime("%Y%m%d")
        cur -= timedelta(days=1)
    raise ValueError(f"{max_days}일 안에 거래일을 못 찾음(캘린더 확인 필요): {d.date()}")


def _hit(action: str, ret_d1) -> bool | None:
    """D+1 적중 여부 — buy 는 상승, sell 은 하락이 적중. hold·미평가는 None(집계 제외)."""
    if ret_d1 is None or action not in ("buy", "sell"):
        return None
    return ret_d1 > 0 if action == "buy" else ret_d1 < 0


def _scope(config_name: str) -> str:
    """판단 레코드의 소속 화면 — ①유니버스 / ③콤보 / ②개별 관찰."""
    if config_name == UNIVERSE_CONFIG_NAME:
        return "universe"
    if (config_name or "").startswith(COMBO_PREFIX):
        return "combo"
    return "watch"


def _scope_line(label: str, rows: list[dict], today: str, yesterday: str) -> str:
    """②/③ 공용 한 줄 요약 — 오늘 판단 수(액션 분해)·어제 D+1 적중·게이트(D+5 평가 표본) 진행률."""
    todays = [r for r in rows if r.get("trade_date") == today]
    acts = {a: sum(1 for r in todays if r.get("action") == a) for a in ("buy", "sell", "hold")}
    hits = [_hit(r.get("action"), r.get("ret_d1")) for r in rows if r.get("trade_date") == yesterday]
    hits = [h for h in hits if h is not None]
    evaluated_d5 = sum(1 for r in rows if r.get("ret_d5") is not None)
    parts = [f"오늘 {len(todays)}건(매수{acts['buy']}/보유{acts['hold']}/매도{acts['sell']})"]
    if hits:
        parts.append(f"어제 D+1 적중 {sum(hits)}/{len(hits)}")
    parts.append(f"게이트 D+5 {min(evaluated_d5, GATE['min_n'])}/{GATE['min_n']}"
                 + (" ✅표본충족" if evaluated_d5 >= GATE["min_n"] else ""))
    return f"{label}: " + " · ".join(parts)


def build_digest(rows: list[dict], positions: list[dict], shortlist: dict, llm_calls: int,
                 now: datetime) -> str:
    """하루 요약 문자열(텔레그램 본문) — 입력은 전부 이미 조회된 값(순수 함수, I/O 없음)."""
    today = now.strftime("%Y%m%d")
    yesterday = prev_trading_day(now)
    by = {"universe": [], "combo": [], "watch": []}
    for r in rows:
        by[_scope(r.get("config_name") or "")].append(r)

    lines = [f"📊 AI 섀도 일일 요약 ({now.strftime('%Y-%m-%d')} {_WEEKDAY_KO[now.weekday()]})"]
    uni_today = [r for r in by["universe"] if r.get("trade_date") == today]
    shortlist_n = len(shortlist.get("codes") or []) if shortlist.get("date") == today else 0
    lines.append(f"① 스캔: 판단 {len(uni_today)}건 · 매수후보 {shortlist_n}")
    lines.append(_scope_line("② 관찰", by["watch"], today, yesterday))
    lines.append(_scope_line("③ 콤보", by["combo"], today, yesterday))

    open_n = sum(1 for p in positions if p.get("status") == "open")
    closed_today = [p for p in positions if p.get("status") == "closed" and p.get("exit_trade_date") == today]
    pnl = sum(p.get("realized_pnl") or 0 for p in closed_today)
    pos_line = f"가상 포지션: 보유 {open_n}"
    if closed_today:
        pos_line += f" · 오늘 청산 {len(closed_today)}건 {pnl:+,.0f}원"
    lines.append(pos_line)
    lines.append(f"Gemini 호출 {llm_calls}회 (오늘)")
    return "\n".join(lines)


def main() -> int:
    now = datetime.now(_KST)
    if not is_trading_day(now):
        print("[ai_daily_digest] 거래일 아님 — 종료")
        return 0
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[ai_daily_digest] REDIS_URL 미설정 — 종료")
        return 1

    import json

    import redis

    from kr_research.bot.notify import Notifier
    from kr_research.core.config import load_config

    r = redis.from_url(redis_url, decode_responses=True)
    store = AiStore()
    try:
        rows = store.get_judgments(None)
        positions = store.get_positions()
    finally:
        store.close()
    try:
        shortlist = json.loads(r.get(K_SHORTLIST) or "{}")
    except ValueError:
        shortlist = {}
    text = build_digest(rows, positions, shortlist, llm_calls_today(r), now)
    sent = Notifier(load_config()).send(text)
    print(text)
    print(f"[ai_daily_digest] 텔레그램 전송={'성공' if sent else '미설정/실패(본문은 위 출력)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
