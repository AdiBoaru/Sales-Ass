# Production readiness — NX-248

**Stare: `NOT_READY` pentru promovare în producție.** Nu fiindcă ceva a picat, ci fiindcă șapte
din zece elemente critice de evidență n-au fost încă MĂSURATE — cele care cer un runner de CI, un
host de staging și accesul la providerul Postgres. Verdictul e produs de
`python scripts/release/evidence.py`, nu de o opinie, și e reprodus în
[`reports/nx248/evidence.json`](../reports/nx248/evidence.json).

Distincția e aceeași ca la NX-238 (`NOT-READY`) și la gate-ul de calitate NX-246 felia 3:

| Verdict | Înseamnă | Ce faci |
|---|---|---|
| `READY` | toate elementele critice există și trec | NX-249 poate începe |
| `BLOCKED` | un element critic există și a PICAT | ai un bug de reparat |
| `NOT_READY` | un element critic LIPSEȘTE | ai o măsurătoare de făcut |

---

## 1. Ce s-a măsurat efectiv (2026-08-17)

Trei elemente critice au fost rulate pe infrastructură reală, local, și au trecut. Artefactele
sunt în `reports/nx248/evidence/`.

### 1.1 Migrări — `migrations.json`

Postgres 16 efemer (`pgvector/pgvector:pg16`), trei drill-uri:

| Drill | Rezultat | Ce dovedește |
|---|---|---|
| `fresh` | 38/38 aplicate, `applied=042` | schema se construiește de la ZERO |
| `idempotent` | a doua rulare aplică 0 | o reîncercare de deploy nu e o migrare nouă |
| `concurrent` | coduri `[0, 3]` | UN singur migrator; al doilea iese `EXIT_LOCKED` fără să scrie |

Drill-ul de concurență a fost întâi un **fals verde**: pe o schemă deja la zi, ambele procese
n-aveau nimic de aplicat și ieșeau cu `[0, 0]`. Acum drill-ul aduce DB-ul la „exact o migrare
pending" înainte de a porni procesele, deci lock-ul chiar e exersat.

**Trei lucruri descoperite de drill-uri, nu de citit cod:**

1. **`fresh → latest` nu înseamnă „rulează migrările".** Migrările `003→042` sunt DELTE peste
   [`docs/schema_v2_production.sql`](schema_v2_production.sql). Pe un DB gol, prima migrare pică
   cu `relation "products" does not exist`. Instalarea de la zero e: schema de bază + delte.
2. **Schema depinde de PLATFORMĂ.** Politicile RLS de dashboard folosesc `auth.uid()` /
   `auth.users` și dau GRANT rolurilor `anon` / `authenticated` / `service_role` — toate create de
   Supabase. Pe un Postgres gol nu există. Inventarul minim e în
   `scripts/release/migration_drill.py::_PLATFORM_SHIM`; contează pentru DR (vezi
   [DISASTER-RECOVERY.md](DISASTER-RECOVERY.md) §Dependențe de platformă).
3. **`005_bot_runtime_login.sql` nu se poate aplica de runner.** Conține un parametru psql
   (`:'bot_password'`), gândit pentru `apply_005.py`. Pe producție nu se vede (e marcat aplicat
   prin `--baseline`), dar orice instalare nouă îl lovește. Vezi §Datorii cunoscute.

### 1.2 Readiness — `readiness.json`

Imaginea reală, pe o rețea Docker izolată, cu Postgres + Redis adevărate. Patru scenarii:

| Scenariu | `api` | `worker` |
|---|---|---|
| toate sus | ready ✓ (6 sonde) | ready ✓ (+ executor 0/24) |
| **Redis jos** | **ready = FALSE** (required) | **ready = TRUE, `degraded=[redis]`** |
| schema prea nouă (`099`) | ready = FALSE, `schema_too_new` | ready = FALSE, `schema_too_new` |
| **Postgres jos** | `live` = **200**, `ready` = **503** | — |

Rândul cu Redis e chiar demonstrația că `required` și `optional` nu sunt etichete decorative:
aceeași pană scoate marginea din rotație (rate-limitul de accept e `fail_closed`) și lasă workerul
să lucreze (Postgres e autoritatea, NX-233). Rândul cu Postgres jos e la fel de important: `live`
rămâne 200, deci o pană de DB **nu repornește flota**.

Răspunsul `503` public conține doar `status`, `release`, `track`, `config`, `schema`, `checked_at`
— nicio dependență, niciun reason code. Detaliile ies doar pe `/health/detail`, cu token.

### 1.3 Contractul imaginii — `image_contract.json`

Verificat pe imaginea construită local: non-root (`uid=10001`), read-only + `cap-drop ALL` +
`no-new-privileges` (aplicația pornește, heartbeat-ul scrie pe tmpfs, scrierea în `/app` e
refuzată), fișierele necesare prezente, zero secrete în `docker history` / ENV / label-uri.

**Poarta poate să și pice**, ceea ce e singura dovadă că merită rulată: pe o imagine construită
intenționat greșit (root, fără registrul de siguranță, cu o cheie în `ENV`) a raportat toate cele
patru probleme.

---

## 2. Bugul găsit pe drum: imaginea nu putea porni workerul

`.dockerignore` excludea `db/seed` integral. Registrul de contraindicații NX-173
(`db/seed/safety_rules.json`) nu ajungea în imagine, iar poarta de boot a workerului îl cere:

```python
ok, info = registry_healthy()
if not ok:
    raise RuntimeError(f"registru de contraindicații invalid — boot refuzat: {info}")
```

Deci buildul era verde și `python -m src.worker.consumer` se oprea la pornire. Aceeași clasă ca
PR #132 (`scripts/`+`docs/` lipsă din imagine) — motiv pentru care verificarea nu mai e „buildul a
trecut", ci `scripts/release/image_contract.py`, care se uită ÎN imagine.

---

## 3. Ce s-a schimbat structural

| Înainte | Acum | De ce contează |
|---|---|---|
| deploy la fiecare push pe `main` | promovare cu aprobare umană (GitHub Environments) | un merge nu mai e o decizie de deploy |
| imagine `:latest` | `image@sha256:…` din manifest | „ce am testat" = „ce rulează", literal |
| `git pull` pe VPS | zero git pe host; configurația vine din release | hostul nu mai decide ce rulează |
| `StrictHostKeyChecking=no` + `ssh-keyscan` | host key din secret, `StrictHostKeyChecking=yes` | TOFU repetat nu e trust |
| `uses: action@v4` | `uses: action@<sha40>` | un tag e cod mutabil cu acces la GHCR |
| `fastapi==0.136.3` (pin de top-level) | `requirements.lock` cu hash-uri, `--require-hashes` | tranzitivele erau nepinnate |
| healthcheck pe socket | `/health/ready` semantic + health de proces | un socket deschis nu e sănătate |
| migrare din runtime | job one-shot, cu advisory lock și credential propriu | runtime-ul nu mai poate face DDL |
| container root-capabil | non-root, read-only, cap-drop, no-new-privs, pids/cpu limits | raza de explozie a unui RCE |

---

## 4. Dependențe — clasificare, cu motivul din cod

Nu e o preferință, e derivată din comportamentul la eroare (`src/ops/health.py`):

| Componentă | `api` | `worker` | De ce |
|---|---|---|---|
| `postgres_control` | required | required | rezolvarea tenantului precedă orice |
| `postgres_tenant` | required | required | ledger + date de client; sonda verifică și ROLUL (`bot_runtime`) |
| `schema` | required | required | în afara intervalului tolerat ⇒ 503, nu „merge cumva" |
| `ledger` | required | required | `has_table_privilege` — grant lipsă = accept imposibil |
| `redis` | **required** | **optional** | accept: rate limit `fail_closed`; worker: DB e autoritatea |
| `executor` | n/a | optional | saturația e `degraded`, nu `failed` |

**De ce ledgerul se verifică prin privilegii, nu printr-un INSERT cu ROLLBACK:** un INSERT ar avea
nevoie de FK-uri valide, adică de un tenant și o conversație fabricate. O sondă care inventează
date ca să se testeze pe sine e exact ce nu vrem într-un endpoint public.

---

## 5. Ce mai lipsește până la `READY`

| Element | Cine îl poate produce | Blochează |
|---|---|---|
| `manifest`, `sbom`, `scan`, `signature` | prima rulare a `release.yml` (CI, GHCR, cosign) | promovarea |
| `staging_smoke` | un host de staging + `STAGING_*` în GitHub Environments | promovarea |
| `rollback_drill` | staging: promovare + revenire la digestul precedent, cronometrată | promovarea |
| `dr_restore` | restore izolat la providerul Postgres + `scripts/dr/restore_verify.py` | NX-249 |

**RPO ≤ 5 min / RTO ≤ 60 min sunt `UNVERIFIED`.** Nu sunt ținte atinse, sunt ținte propuse; se
marchează `PASS` doar cu timestampuri dintr-un drill real. `UNVERIFIED` blochează NX-249 —
deliberat, fiindcă singura alternativă e să afli în incident.

---

## 6. Datorii cunoscute (cu owner și termen)

| # | Datorie | Impact | Propunere |
|---|---|---|---|
| D1 | `005_bot_runtime_login.sql` are parametru psql | orice instalare NOUĂ pică pe el | migrare nouă care ia parola din `current_setting`, sau scoaterea lui din calea runnerului |
| D2 | `.env` root-readable pe VPS | secretele sunt în env-ul containerului, vizibile în `docker inspect` | suportul `_FILE` există deja (`src/config.py`); migrarea la fișiere montate = pas de operare |
| D3 | `SUPABASE_DB_URL` e privilegiat și în runtime | control-plane-ul rulează cu un rol care poate mai mult decât îi trebuie | rol dedicat de control plane (doar `channels`/`businesses`), separat de cel de DDL |
| D4 | `src/db/connection.py` forțează SSL pe Windows | dev/CI nu se pot conecta la un Postgres local fără TLS | același tratament ca în `scripts/migrate.py` (respectă `sslmode=disable`) |
| D5 | scanul de secrete pe istoricul git | `.git` nu mai intră în imagine, dar istoricul rămâne | rulare unică + rotația a ce se găsește |

D1 și D4 au fost descoperite rulând drill-urile din cardul ăsta; niciuna nu e regresie introdusă
de NX-248.

---

## 7. Dependențe (runtime) — de ce s-a subțiat lista

`fastapi[standard]` aducea în imaginea de RUNTIME un CLI (`fastapi-cli`, `typer`, `rich`), un CLI
de cloud (`fastapi-cloud-cli`, `rignore`, `fastar`) și `sentry-sdk` — un SDK de telemetrie către
un terț, într-un proces care trece tot ce iese prin sanitizarea NX-230/246. Nimic din `src/` sau
`tests/` nu folosește `UploadFile`/`Form`/`EmailStr` și nici `fastapi run` (compose pornește
`uvicorn` direct).

Rezultat: **73 → 54 pachete**, verificat prin build real (`sentry_sdk: False`, `fastapi_cli: False`
în imagine). `uvicorn[standard]` rămâne: `uvloop` și `httptools` sunt componente de server.

---

## 8. Comenzi

```bash
# poarta de repo (offline, fără DB)
ruff check . && ruff format --check . && pytest -x -q
docker build --pull -t nativx-stage1:test .
docker compose -f docker-compose.prod.yml config --quiet   # cere IMAGE_DIGEST

# verificări pe imagine
python scripts/release/image_contract.py nativx-stage1:test

# drill-uri de migrare (Postgres EFEMER — refuză orice host care nu e local/CI)
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=nativx_drill \
  -p 55432:5432 pgvector/pgvector:pg16
export DATABASE_URL_MIGRATION="postgresql://postgres:postgres@localhost:55432/nativx_drill?sslmode=disable"
python scripts/release/migration_drill.py fresh
python scripts/release/migration_drill.py idempotent
python scripts/release/migration_drill.py concurrent

# starea de release
python scripts/release/evidence.py
```

Runbook-urile: [RELEASE-RUNBOOK.md](RELEASE-RUNBOOK.md) ·
[DISASTER-RECOVERY.md](DISASTER-RECOVERY.md) · [SECRETS-ROTATION.md](SECRETS-ROTATION.md)
