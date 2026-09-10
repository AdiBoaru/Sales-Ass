# Nativx Assistant

Platformă multi-tenant de AI Sales Assistant pentru ecommerce, livrată ca **web widget**
(by Nativx Technology).
Arhitectura completă, schema și principiile sunt în [`CLAUDE.md`](CLAUDE.md).

> De la `git clone` la teste verzi în ~10 minute. Pașii sunt scriși executându-i
> pe curat — dacă ceva nu merge, vezi [Troubleshooting](#troubleshooting).

---

## Cerințe

| Tool | Pentru ce | Note |
|---|---|---|
| **Python 3.12** | runtime + teste | local merge și 3.11; CI rulează 3.12 |
| **gh** (GitHub CLI) | PR-uri | `winget install GitHub.cli`, apoi `gh auth login` |
| **Docker** | rulare stack containerizat | OPȚIONAL — necesar doar pe VPS / pentru `docker compose`. Dev local merge fără. |

DB-ul (Postgres) NU rulează local — e **Supabase remote**.

---

## Setup

```bash
git clone https://github.com/AdiBoaru/Sales-Ass.git
cd Sales-Ass

python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

pip install -r requirements-dev.txt

cp .env.example .env      # apoi completează valorile (vezi mai jos)
```

### Completează `.env`

Valorile reale le iei din vault-ul echipei / dashboard-uri. Minim pentru a rula:

- **`SUPABASE_DB_URL`** — connection string Postgres. Folosește **Session pooler**
  din Supabase (Settings → Database → Connection string → *Session pooler*):
  ```
  postgresql://postgres.<ref>:<PAROLA>@aws-0-<region>.pooler.supabase.com:5432/postgres
  ```
  ⚠️ NU conexiunea directă `db.<ref>.supabase.co` — nu se rezolvă pe rețele IPv4.
- `OPENAI_API_KEY` — vezi `.env.example` (necesar pentru LLM, nu pentru testele de bază).

### DB: aplică plasa RLS (o singură dată per proiect)

```bash
python scripts/archive/apply_003.py  # rol bot_runtime + RLS + guard 8KB; testează izolarea
python scripts/db_check.py       # verificare read-only: tabele, extensii, business demo
```

---

## Rulare teste

```bash
# Unit (fără DB) — ce rulează și în CI:
pytest -x -q -m "not integration"

# Integration (ating Supabase real, exclus din CI) — necesită SUPABASE_DB_URL:
pytest -m integration

# Lint + format (obligatoriu înainte de PR):
ruff check . && ruff format --check .
```

> Pe Windows, `ruff` și `pytest` se rulează prin `python -m ruff ...` /
> `python -m pytest ...` dacă nu sunt în PATH.

---

## Rulare stack (opțional, necesită Docker)

```bash
docker compose up      # redis + webhook + worker + dispatcher
```
Postgres nu e în compose (e Supabase). Pe Win10 Home, Docker Desktop cere backend
**WSL2** (vezi Troubleshooting). Fără Docker, rulezi procesele direct:
```bash
uvicorn src.webhook.app:app --reload      # API (endpointurile /web/*, health, comenzi)
python -m src.worker.consumer             # worker (consumer → pipeline → outbox)
python -m src.worker.dispatcher           # dispatcher (outbox → canal)
```
> ⚠️ Procesele directe au nevoie de un Redis accesibil (`REDIS_URL`). `docker
> compose` îl pornește pe `redis`; local fără Docker ai nevoie de un Redis separat.

## Canalul: web widget (singurul)

`WEB_ENABLED=true` montează endpointurile `/web/*` pe aplicația FastAPI. Widgetul propriu-zis
trăiește într-un repo FE separat; backendul emite doar JSON
([`docs/FRONTEND-CONTRACT-IZI.md`](docs/FRONTEND-CONTRACT-IZI.md)).

```bash
# canalul webchat al tenantului (idempotent — întoarce public_token pt data-token din widget):
python scripts/seed_web_channel.py --business sole-ro
# audit conversațional pe calea REALĂ (fără HTTP server):
python scripts/sim/web_audit.py
```

> NX-289: WhatsApp și Telegram au fost ȘTERSE din proiect (erau înghețate din NX-179).
> Nu mai există poller, webhook Meta, tunel `cloudflared` sau template-uri aprobate.

---

## Cum lucrezi un task

1. Citește [`CONTRIBUTING.md`](CONTRIBUTING.md) (branch-uri, commit-uri, PR).
2. Cardul taskului e în [`tasks/TXXX.md`](tasks/) — implementezi STRICT ce cere.
3. Branch din card → implementare → `ruff` + `pytest` verzi → PR.
4. Schema DB: [`docs/schema_reference.md`](docs/schema_reference.md) e harta numelor reale.

---

## Troubleshooting

| Simptom | Cauză / fix |
|---|---|
| `getaddrinfo failed` la conectare DB | Folosești conexiunea directă Supabase (IPv6-only). Treci pe **Session pooler**. |
| `password authentication failed` | Parolă greșită în `SUPABASE_DB_URL`, sau conține caractere care strică URL-ul (resetează la una alfanumerică în Supabase). |
| `socket.gaierror: Name or service not known` la worker/dispatcher **în Docker** | Parola din `SUPABASE_DB_URL` are caractere speciale (`@`, `/`...) **neescapate**. Pe Windows merge (urlparse tolerează, split la ultimul `@`), dar asyncpg în container parsează greșit host-ul → „DNS broken" fals. Fix: **percent-encode parola** (`@`→`%40` etc.). |
| `getaddrinfo failed` intermitent pe **Windows** | Bug asyncpg/ProactorEventLoop. Codul (`connection.py`, scripturile) rezolvă IPv4 sincron + conectează pe IP — deja gestionat. |
| `UnicodeEncodeError` în consolă (Windows) | Output cu diacritice pe cp1252. Scripturile fac `sys.stdout.reconfigure(encoding="utf-8")`. |
| `docker: command not found` | Docker nu e instalat — opțional. Rulează direct cu uvicorn/python sau folosește doar testele. |
| `wsl --install` → `The system cannot find the file specified` (Win10) | Feature-urile WSL nu-s activate. Admin PowerShell: `dism /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart` + `…VirtualMachinePlatform…` → reboot → kernel de la aka.ms/wsl2kernel → `wsl --set-default-version 2`. Docker Desktop pe Win10 **Home** cere backend WSL2. |
| Port 8000 ocupat | Oprește procesul care îl folosește sau schimbă portul în comanda uvicorn. |
| `.env` lipsă / variabilă lipsă | `cp .env.example .env` și completează. `SUPABASE_DB_URL` e obligatoriu pentru integration. |
