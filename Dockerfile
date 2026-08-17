# syntax=docker/dockerfile:1
#
# NX-248 — imagine REPRODUCTIBILĂ și minimă.
#
# Trei proprietăți care nu existau înainte și care se verifică, nu se promit:
#
#   1. **Baza e pin-uită pe digest**, nu pe tag. `python:3.12-slim` de mâine e alți bytes decât
#      cel de azi; un build „identic" al aceluiași commit ar produce altă imagine, deci
#      „promovăm exact ce am testat" ar fi fals de la primul strat.
#   2. **Dependențele se instalează cu HASH-uri** (`requirements.lock`, `--require-hashes`).
#      Un pin de versiune la nivelul de sus (`fastapi==0.136.3`) nu spune nimic despre
#      tranzitive și nu detectează un artefact înlocuit pe index. Cu `--require-hashes`, pip
#      refuză orice wheel care nu se potrivește bit cu bit.
#   3. **Runtime-ul n-are unelte de build.** Compilatoarele și cache-urile rămân în stagiul de
#      builder; ce ajunge în imaginea finală e interpretorul, bibliotecile și codul.
#
# Regenerarea lock-ului (pe LINUX, ca hash-urile să fie cele instalate în imagine):
#   docker run --rm -v "$PWD:/w" -w /w python@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a \
#     sh -c "pip install -q pip-tools==7.4.1 && pip-compile --generate-hashes --no-header \
#            --strip-extras --output-file=/w/requirements.lock requirements.txt"

ARG BASE_DIGEST=sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

# --- Stage 1: builder — instalează dependențele runtime într-un prefix izolat ---
FROM python@${BASE_DIGEST} AS builder

WORKDIR /build

COPY requirements.lock .
# `--require-hashes` e implicit când fișierul are hash-uri, dar îl scriem EXPLICIT: dacă cineva
# regenerează lock-ul fără `--generate-hashes`, vrem ca buildul să pice, nu să instaleze tăcut
# fără verificare. `--no-deps`: lock-ul e deja închis tranzitiv, iar un resolver care ar mai
# adăuga ceva ar adăuga exact ce n-are hash.
RUN pip install --no-cache-dir --require-hashes --no-deps --prefix=/install -r requirements.lock

# --- Stage 2: runtime — imagine mică, non-root, doar ce trebuie ---
FROM python@${BASE_DIGEST} AS runtime

# Metadate OCI: leagă imaginea de commit-ul din care a ieșit, fără să pună nimic secret în ea.
# `docker inspect` pe VPS răspunde „ce rulează aici?" chiar și fără acces la CI.
ARG RELEASE_SHA=unknown
ARG BUILT_AT=unknown
LABEL org.opencontainers.image.source="https://github.com/adiboaru/sales-ass" \
      org.opencontainers.image.revision="${RELEASE_SHA}" \
      org.opencontainers.image.created="${BUILT_AT}" \
      org.opencontainers.image.title="nativx-assistant" \
      org.opencontainers.image.description="Nativx Assistant — AI Sales Assistant (web widget)"

# ENV, nu secrete: SHA-ul și data buildului sunt publice prin definiție (sunt în git).
# `IMAGE_DIGEST` NU se poate coace aici (ar fi o recursie — vezi src/ops/build_info.py); îl pune
# deployul din manifest.
ENV RELEASE_SHA=${RELEASE_SHA} \
    BUILT_AT=${BUILT_AT} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copiază pachetele instalate din builder
COPY --from=builder /install /usr/local

# User non-root cu UID/GID EXPLICIT. Explicit fiindcă `read_only: true` + volume montate cer un
# owner previzibil: un UID atribuit automat ar face ca un mount să fie scriibil pe o mașină și
# nu pe alta.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app
# `--chown=root:root` + drepturi de citire: aplicația NU-și poate rescrie propriul cod. Cu
# `read_only: true` în compose ar fi oricum imposibil, dar apărarea nu depinde de un flag din
# alt fișier — o imagine rulată fără el rămâne la fel de strânsă.
COPY --chown=root:root src/ ./src/
# Poarta de boot a workerului (NX-123) importă `scripts.migrate` și citește migrările din
# `docs/*.sql` (DOCS_DIR). Fără ele, `python -m src.worker.consumer` crapă la boot cu
# ModuleNotFoundError → restart-loop. Copiem DOAR ce-i necesar (runner-ul + SQL-urile de
# migrare) — nu tot docs/ (PDF/xlsx) și nu scripts/sim. Aceleași fișiere servesc jobul dedicat
# de migrare (`docker compose --profile migrate run --rm migrate`).
COPY --chown=root:root scripts/migrate.py ./scripts/migrate.py
COPY --chown=root:root docs/*.sql ./docs/
# Registrul de contraindicații NX-173 (P0). LIPSEA din imagine: `.dockerignore` excludea `db/seed`
# integral, iar poarta de boot a workerului (`registry_healthy()`) refuză pornirea fără el — deci
# imaginea nu putea porni workerul deloc. Aceeași clasă de bug ca PR #132 (scripts/docs lipsă), și
# același motiv pentru care `tests/test_ops_image.py` verifică acum conținutul imaginii, nu doar
# că buildul trece.
COPY --chown=root:root db/seed/safety_rules.json ./db/seed/safety_rules.json

USER 10001:10001

# Fără CMD hardcodat — comanda vine din docker-compose (webhook vs worker)
# webhook:   uvicorn src.webhook.app:app --host 0.0.0.0 --port 8000
# worker:    python -m src.worker.consumer
# scheduler: python -m src.jobs.scheduler   (NX-83: joburi de mentenanță)
# migrate:   python scripts/migrate.py      (job one-shot, credential de DDL — NX-248)
