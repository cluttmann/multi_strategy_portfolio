# Cloud Run Job image für den Quant-Desk (Daten-Pipeline + Executors).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 bash curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY quant/cloud/requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

# Nur den Quant-Code + Ops (nicht main.py des ETF-Bots)
COPY quant/ /app/quant/

ENV PYTHONUNBUFFERED=1
# Secrets/Config kommen als Env aus Secret Manager; .env ist optional
ENTRYPOINT ["python", "-m", "quant.cloud.entrypoint"]
