# 환경변수 단일 접근점 — 이 저장소는 브로커 자격증명이 전혀 없음(Redis·텔레그램·Gemini만)
"""kr-trading-bot 의 core/config.py 와 달리 KIS OAuth 필드(app_key/account_8/hts_id 등)가 없다 —
이 저장소는 브로커를 아예 모르는 순수 연구/스크리닝/AI 판단 전용이라 그런 자격증명이 필요 없다.
`.env` 가 없어도 기본값(빈 문자열)으로 동작(스캐폴딩/스모크 안전)."""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv(".env")  # 없으면 무시
except Exception:
    pass


@dataclass(frozen=True)
class Config:
    redis_url: str
    telegram_token: str
    telegram_chat_id: str
    gemini_api_key: str


def load_config() -> Config:
    return Config(
        redis_url=os.environ.get("REDIS_URL", ""),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
    )
