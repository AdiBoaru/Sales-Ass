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
# Poarta de boot a workerului (NX-123) importă `scripts.migrate` și citește migrările din
# `docs/*.sql` (DOCS_DIR). Fără ele, `python -m src.worker.consumer` crapă la boot cu
# ModuleNotFoundError → restart-loop (webhook n-are poarta, deci nu se vede acolo). Copiem
# DOAR ce-i necesar (migrate.py n-are deps interne; SQL-urile de migrare) — nu tot docs/
# (PDF/xlsx) și nu scripts/sim. Permite și `docker compose run --rm worker python scripts/migrate.py`.
COPY scripts/migrate.py ./scripts/migrate.py
COPY docs/*.sql ./docs/

# User non-root (uid 1000)
RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app
USER app

# Fără CMD hardcodat — comanda vine din docker-compose (webhook vs worker)
# webhook:   uvicorn src.webhook.app:app --host 0.0.0.0 --port 8000
# worker:    python -m src.worker.consumer
# scheduler: python -m src.jobs.scheduler   (NX-83: joburi de mentenanță)
