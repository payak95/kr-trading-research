# 전략·지표·백테스트·스크리닝·AI 섀도 판단 연구 레이어 — 상시 실행 프로세스 없음, 전부 cron/데몬 워커로 기동
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e .
COPY . .
