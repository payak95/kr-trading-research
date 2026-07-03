# 네이버 금융 시가총액 상위로 스크리닝 유니버스 종목 조회 — 무인증 공개(requests), KRX 서버차단 회피
"""KRX data.krx.co.kr 는 서버 IP 스크래핑을 차단(LOGOUT/403, pykrx 동일)해 네이버 금융 공개 시세로 대체.
**시가총액 상위**(sise_market_sum)는 ETF/ETN 없이 실제 상장 종목만 깔끔히 정렬 → 유동성 유니버스로 적합
(네이버 '거래량 상위'는 레버리지·인버스 ETF 가 대부분이라 부적합). 코스피+코스닥 상위 N(시총=유동·우량 프록시).
- 인증 불필요·서버 접근 가능(확인). HTML 파싱이라 포맷 변동 가능 → 호출부가 실패를 격리(빈 [] → 유니버스 미갱신).
"""
import re

import requests

_URL = "https://finance.naver.com/sise/sise_market_sum.naver"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# 시총 페이지의 '종목명' 링크(class=tltle)만 — 실제 종목 행. 다른 /item 링크(관련주 등) 배제.
_ROW_RE = re.compile(r'/item/main\.naver\?code=(\d{6})"[^>]*class="tltle"[^>]*>([^<]+)<')
_PAGE_ROWS = 50  # 시총 페이지당 행 수
# ETF/ETN 브랜드 접두(시총 상위에도 일부 섞임) — 스크리닝 유니버스에서 제외(개별 종목만).
_ETF_PREFIXES = ("KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF", "HANARO", "SOL ", "ACE ",
                 "PLUS ", "RISE ", "TIMEFOLIO", "KINDEX", "KIWOOM", "FOCUS", "TREX", "WON ",
                 "HK ", "BNK ", "1Q ", "KCGI", "마이다스", "히어로즈")
_ETF_MARKERS = ("ETN", "레버리지", "인버스", "선물")  # 이름 어디든 있으면 ETF/ETN


def is_etf(name: str) -> bool:
    """종목명이 ETF/ETN 인가(브랜드 접두 또는 레버리지·인버스·선물·ETN 포함)."""
    return name.startswith(_ETF_PREFIXES) or any(m in name for m in _ETF_MARKERS)


def parse_rows(html: str) -> list[dict]:
    """시총 페이지 HTML → [{code,name}] 랭크 순서(class=tltle 행 중 ETF/ETN 제외 = 실제 종목만)."""
    return [{"code": c, "name": n.strip()}
            for c, n in _ROW_RE.findall(html) if not is_etf(n.strip())]


def _fetch_market(sosok: str, need: int, timeout: int = 15) -> list[dict]:
    """한 시장(sosok 0 코스피/1 코스닥) 시총 상위 need 종목 — 필요한 페이지만 순차 조회·중복 제거."""
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, need // _PAGE_ROWS + 3):
        r = requests.get(_URL, params={"sosok": sosok, "page": page}, headers=_HEADERS, timeout=timeout)
        r.encoding = "euc-kr"
        rows = parse_rows(r.text)
        if not rows:
            break
        for row in rows:
            if row["code"] not in seen:
                seen.add(row["code"])
                out.append(row)
        if len(out) >= need:
            break
    return out[:need]


def top_marketcap_codes(count: int = 300) -> list[dict]:
    """코스피+코스닥 시가총액 상위 합쳐 count 종목 [{code,name}](코스피:코스닥 ≈ 2:1).
    한 시장 실패는 받은 만큼만(전체 빈 [] 면 호출부가 유니버스 미갱신 처리)."""
    kospi = max(1, count * 2 // 3)
    out: list[dict] = []
    for sosok, need in (("0", kospi), ("1", count - kospi)):
        try:
            out += _fetch_market(sosok, need)
        except Exception:
            continue  # 한 시장 실패가 전체를 죽이지 않음
    seen: set[str] = set()
    uniq: list[dict] = []
    for row in out:
        if row["code"] not in seen:
            seen.add(row["code"])
            uniq.append(row)
    return uniq[:count]
