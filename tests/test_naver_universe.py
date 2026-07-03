# 네이버 유니버스 헬퍼 — 행 파싱·ETF 필터 검증(네트워크 없음)
"""실행: python tests/test_naver_universe.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.naver_universe import is_etf, parse_rows

_HTML = """
<a href="/item/main.naver?code=005930" onclick="x" class="tltle">삼성전자</a>
<a href="/item/main.naver?code=000660" class="tltle">SK하이닉스</a>
<a href="/item/main.naver?code=069500" class="tltle">KODEX 200</a>
<a href="/item/main.naver?code=360750" class="tltle">TIGER 미국S&P500</a>
<a href="/item/main.naver?code=530036" class="tltle">삼성 인버스 2X WTI원유 선물 ETN</a>
<a href="/item/main.naver?code=247540" class="tltle">에코프로비엠</a>
<a href="/item/main.naver?code=999999">관련주링크(클래스없음)</a>
"""


def main() -> int:
    # ── is_etf: 브랜드 접두·마커 식별 ──
    assert is_etf("KODEX 200") and is_etf("TIGER 미국S&P500")
    assert is_etf("삼성 인버스 2X WTI원유 선물 ETN") and is_etf("KODEX 코스닥150레버리지")
    assert not is_etf("삼성전자") and not is_etf("에코프로비엠") and not is_etf("SK하이닉스")

    # ── parse_rows: class=tltle 행만 + ETF 제외 ──
    rows = parse_rows(_HTML)
    codes = [r["code"] for r in rows]
    assert codes == ["005930", "000660", "247540"], f"실제 종목만(ETF·비-tltle 제외): {codes}"
    assert rows[0] == {"code": "005930", "name": "삼성전자"}

    print("✅ test_naver_universe: is_etf·parse_rows(tltle 한정·ETF 제외) 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
