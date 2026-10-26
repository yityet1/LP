FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    APP_ENV=paper \
    LIVE_TRADING_ENABLED=false

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY configs/ ./configs/
COPY datasets/ ./datasets/
COPY docs/ ./docs/
COPY issues/ ./issues/
COPY src/ ./src/
COPY tests/ ./tests/

RUN useradd -m -u 1000 arbuser && chown -R arbuser:arbuser /app
USER arbuser

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health')" || exit 1

CMD ["python3", "-m", "arb.ui.server", "--host", "0.0.0.0", "--port", "8080"]
