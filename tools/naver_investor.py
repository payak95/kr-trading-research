# 네이버 금융 frgn 페이지로 종목별 외국인·기관 일별 순매매 수량 조회 — 연구용(무인증·무한도, KIS 분리)
"""스크리닝 강화(큰손 추적, AlphaForge `flow.foreign_accumulation` 벤치마크) — KIS `inquire-investor`
(tr_id FHKST01010900)와 같은 데이터를 종목별 호출 없이 얻으려고 네이버로 분리했다(KIS 는 라이브 매매
전용 유지, `tools/naver_ohlcv.py`와 동일 방침). 실제 두 소스를 대조해 수치가 일치함을 확인함
(docs/planning/pipeline-automation-design.md 아님 — 별도 스크리닝 강화 계획 참고).

- 페이지: `finance.naver.com/item/frgn.naver?code=<코드>&page=<N>` (HTML). **1페이지=20거래일**.
- `<td>` 레벨로 파싱(span 레벨 아님) — 전일비/등락률 셀 마크업이 상승/하락/보합에 따라 class 가 달라져
  span 단위로 파싱하면 뒤 컬럼이 밀리는 걸 실측으로 확인, td 순서는 항상 고정(날짜·종가·전일비·등락률·
  거래량·기관순매매량·외국인순매매량·외국인보유주수·외국인보유율)이라 안정적.
- 네트워크/포맷 실패는 호출부가 격리(빈 [] → 해당 종목 skip, naver_ohlcv.py 와 동일 fail-safe).
"""
import re

import requests

_URL = "https://finance.naver.com/item/frgn.naver"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.naver.com/"}
_ROW_RE = re.compile(r'<tr onMouseOver="mouseOver\(this\)" onMouseOut="mouseOut\(this\)">(.*?)</tr>', re.DOTALL)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_DATE_RE = re.compile(r'(\d{4})\.(\d{2})\.(\d{2})')
ROWS_PER_PAGE = 20


def _num(s: str):
    s = s.strip().replace(",", "").replace("+", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_page(html: str) -> list[dict]:
    """frgn 1페이지 → [{date,close,volume,orgn_ntby_qty,frgn_ntby_qty}, ...] 시간순(페이지 자체는
    최신순으로 나열돼 있어 reverse). td 9개 미만인 행(헤더·구분행)은 skip."""
    out = []
    for block in _ROW_RE.findall(html):
        tds = _TD_RE.findall(block)
        if len(tds) < 7:
            continue
        cells = [_TAG_RE.sub("", td).strip() for td in tds]
        m = _DATE_RE.match(cells[0])
        if not m:
            continue
        close, volume = _num(cells[1]), _num(cells[4])
        if close is None or volume is None:
            continue
        out.append({"date": f"{m.group(1)}{m.group(2)}{m.group(3)}", "close": close, "volume": volume,
                    "orgn_ntby_qty": _num(cells[5]), "frgn_ntby_qty": _num(cells[6])})
    out.reverse()
    return out


def daily_investor_flow(code: str, days: int = 20, timeout: int = 15) -> list[dict]:
    """code 의 최근 days 거래일 기관/외국인 순매매(시간순). 1페이지=20일이라 기본값은 1콜.
    days>20 이면 필요한 만큼 다음 페이지 이어붙임(최신 페이지부터 받아 역순으로 합침).
    실패/빈 응답은 그 페이지에서 중단(부분 결과라도 반환) — 완전 실패면 []."""
    pages_needed = max(1, -(-days // ROWS_PER_PAGE))
    pages = []
    for page in range(1, pages_needed + 1):
        try:
            r = requests.get(_URL, headers=_HEADERS, timeout=timeout, params={"code": code, "page": page})
            rows = parse_page(r.text)
        except Exception:
            break
        if not rows:
            break
        pages.append(rows)
    if not pages:
        return []
    combined = []
    for rows in reversed(pages):  # 뒤 페이지가 더 과거 → 먼저 이어붙여 전체를 시간순으로
        combined.extend(rows)
    return combined[-days:]
