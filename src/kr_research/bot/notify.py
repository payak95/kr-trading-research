# 텔레그램 알림 — 스크리닝/전진검증 결과를 개인 봇으로 전송 (토큰 없으면 no-op)
"""kr-trading-bot 의 bot/notify.py 와 동일 로직(운영 봇과 별도 토큰 — 격리). 토큰·chat_id
미설정이면 조용히 no-op(스캐폴딩/테스트 안전)."""
import requests

from kr_research.core.config import Config

_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, cfg: Config):
        self.token = cfg.telegram_token
        self.chat_id = cfg.telegram_chat_id

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        try:
            r = requests.post(_API.format(token=self.token),
                              json={"chat_id": self.chat_id, "text": text},
                              timeout=10)
            return r.ok
        except Exception:
            return False  # 알림 실패가 배치를 막지 않게 — 단 반복 실패는 로깅할 것
