# holiday-radar — one image, two roles (web + scheduler) selected by command.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Tallinn

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY config.yaml .
COPY presets/ ./presets/

# data/ (SQLite + climate cache) is a mounted volume
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/health || exit 1

# default role: the read-only UI/API. The scheduler service overrides this.
CMD ["python", "-m", "app.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]
