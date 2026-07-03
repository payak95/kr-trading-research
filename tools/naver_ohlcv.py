# 네이버 금융 fchart 로 일봉 OHLCV 조회 — 연구용(백테스트·유니버스) 데이터 소스(무인증·무한도, KIS 분리)
"""KIS 초당 한도가 연구 작업(유니버스 410종목 워밍·반복 백테스트)의 병목이라, 연구용 일봉은
네이버 fchart 공개 API 로 분리한다(KIS 는 라이브 매매 전용 유지). 무인증·서버 접근 가능·초당 한도 없음,
1회 호출에 count 봉(KIS 는 2페이지) → 빠름.
- 응답: `<item data="YYYYMMDD|시가|고가|저가|종가|거래량" />` (EUC-KR XML). 시간순.
- 반환 형태는 KIS daily_ohlcv 와 동일: [{date(YYYYMMDD), open, high, low, close, volume}] (백테스트/스크리닝 호환).
- 네트워크/포맷 실패는 호출부가 격리(빈 [] → 해당 종목 skip).
"""
import re

import requests

_URL = "https://fchart.stock.naver.com/sise.nhn"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.naver.com/"}
_ITEM_RE = re.compile(r'data="(\d{8})\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)"')


def parse_items(xml: str) -> list[dict]:
    """fchart XML → [{date,open,high,low,close,volume}] 시간순(KIS daily_ohlcv 와 동일 형태)."""
    return [{"date": d, "open": int(o), "high": int(h), "low": int(lo),
             "close": int(c), "volume": int(v)}
            for d, o, h, lo, c, v in _ITEM_RE.findall(xml)]


def daily_ohlcv(code: str, count: int = 120, timeout: int = 15) -> list[dict]:
    """code 의 최근 count 거래일 일봉(시간순). 실패는 [](호출부 skip). count=평가에 쓸 봉 수."""
    r = requests.get(_URL, headers=_HEADERS, timeout=timeout,
                     params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"})
    return parse_items(r.text)
