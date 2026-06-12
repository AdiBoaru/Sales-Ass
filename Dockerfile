# syntax=docker/dockerfile:1

# --- Stage 1: builder — instalează dependențele runtime într-un prefix izolat ---
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: runtime — imagine mică, non-root, doar ce trebuie ---
FROM python:3.12-slim AS runtime

# Copiază pachetele instalate din builder
COPY --from=builder /install /usr/local

# Cod aplicație
WORKDIR /app
COPY src/ ./src/

# User non-root (uid 1000)
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app
USER app

# Fără CMD hardcodat — comanda vine din docker-compose (webhook vs worker)
# webhook: uvicorn src.webhook.app:app --host 0.0.0.0 --port 8000
# worker:  python -m src.worker.consumer
