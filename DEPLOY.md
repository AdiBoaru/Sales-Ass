# Deploy Nativx pe VPS partajat (Hostinger + Traefik)

> ## ⚠️ Runbook-ul canonic de RELEASE e altul (NX-248)
>
> Din NX-248, deployul se face prin **promovarea unui digest semnat**, cu aprobare umană — nu prin
> `git pull && docker compose pull && up -d`. Pentru orice deploy, rollback sau migrare:
>
> - **[docs/RELEASE-RUNBOOK.md](docs/RELEASE-RUNBOOK.md)** — comenzi exacte, coduri de ieșire, rollback
> - **[docs/PRODUCTION-READINESS.md](docs/PRODUCTION-READINESS.md)** — ce s-a măsurat, ce lipsește
> - **[docs/DISASTER-RECOVERY.md](docs/DISASTER-RECOVERY.md)** — surse de adevăr, restore, RPO/RTO
> - **[docs/SECRETS-ROTATION.md](docs/SECRETS-ROTATION.md)** — inventar, livrare prin fișier, rotație
>
> Fișierul ăsta rămâne pentru **topologia** VPS-ului (coabitare, Traefik, DNS, profile de canal) și
> pentru istoricul deciziilor. Unde cele două se contrazic, **runbook-ul de release câștigă**;
> secțiunile depășite sunt marcate ca atare mai jos.

Runbook pentru a rula Nativx **alături** de stack-ul existent al VPS-ului, fără
să-l atingem. DB = Supabase remote (nu se atinge Postgres-ul local). Canale:
**WhatsApp** (Meta Cloud API, număr separat) + **Telegram** (long polling).

## Garanții de coabitare (ce NU atingem)

- Proiect compose dedicat `nativx` → toate containerele prefixate `nativx-*`.
- **Niciun port publicat pe host.** Doar `webhook` se atașează la rețeaua ta
  `shared_network` ca să-l ruteze Traefik-ul existent (după Host-header).
- Redis-ul nostru e DEDICAT (rețea internă, parolă proprie) — separat de orice
  alt redis. Nu partajăm volume cu nimeni.
- `mem_limit` pe fiecare container → un eventual leak la noi ucide DOAR containerul
  nostru (restart automat), nu un proces al altui client.
- NU atingem: `traefik`, `evolution`, `n8n`, `postgres`, `redis`, `frontend`,
  `bot-server`, `adminer` și nici rețeaua `shared_network` (doar ne atașăm la ea).

---

## Faza 0 — Pre-requisite

- [x] Docker 29 + Compose v5 — deja instalate.
- [ ] `sudo reboot` pentru kernelul nou (rulează ÎNAINTE de a porni Nativx, nu după).
- [ ] **Swap — AMÂNAT** (decizie 2026-06-18). Plasa curentă = `mem_limit`-urile.
      Vezi §Future când vrei plasa în plus la presiune cumulată de RAM.

> RAM: VPS 3.8GB, 0 swap. Suma `mem_limit` ≈ 1.25GB (proactive off). Real ~0.5–0.7GB.
> Urmărește `docker stats` la prima pornire (§Faza 2).

## Faza 1 — Pregătire

```bash
# 1. Codul pe VPS — în dir-ul dedicat, lângă restul platformei nativextech
sudo mkdir -p /opt/nativextech/nativx && cd /opt/nativextech/nativx
git clone <repo-url> .          # sau git pull dacă există deja

# 2. .env de prod
cp .env.prod.example .env
nano .env                       # completează (vezi §3 pentru valorile Traefik)

# 3. Provisioning rol bot_runtime pe Supabase (o singură dată, dacă nu e făcut)
BOT_RUNTIME_PASSWORD='...' python scripts/archive/apply_005.py
```

> `.env` conține `COMPOSE_FILE=docker-compose.prod.yml` + `COMPOSE_PROJECT_NAME=nativx`,
> deci de aici toate comenzile sunt **`docker compose ...` simplu** (fără `-p`/`-f`),
> exact ca celelalte stack-uri din `/opt/nativextech/` — și NU pornește accidental
> `docker-compose.yml` (cel de DEV din repo).

⚠️ Parola Supabase din URL trebuie **percent-encoded** (`@`→`%40`, `#`→`%23`...) —
altfel asyncpg crapă în container.

## §3 — Traefik (CONFIRMAT din recon 2026-06-18)

Traefik rulează cu `--providers.docker.network=shared_network`, entrypoint HTTPS
`websecure` (:443), resolver ACME `letsencrypt` (TLS-ALPN) și redirect HTTP→HTTPS
global pe entrypoint `web`. Valorile sunt deja **hardcodate** în
`docker-compose.prod.yml` (la fel ca labels-urile lui `evolution`), deci în `.env`
trebuie DOAR:
- `WEBHOOK_HOST` = subdomeniul (ex. `bot.nativextech.com`) + **A-record către `72.62.34.245`**.

Traefik emite certul Let's Encrypt automat la prima cerere HTTPS pe acel host.

### Body-size cap la margine (defense-in-depth, NX-120)

Aplicația respinge deja corpuri prea mari cu `413` ÎNAINTE de a le citi (gardul de app =
**sursa de adevăr**, fiindcă un `curl` direct la container ocolește Traefik). Adăugăm și la
Traefik un middleware de `buffering` ca pachetele mari să fie oprite la margine, pe VPS-ul mic
(1vCPU/4GB/0-swap, vezi topologia) — un POST de mulți MB nu trebuie să ajungă nici măcar la app:

```yaml
# labels pe serviciul `webhook` (în docker-compose.prod.yml), pe lângă cele de routing:
- "traefik.http.middlewares.nativx-body.buffering.maxRequestBodyBytes=262144"   # 256KB (webhook Meta)
- "traefik.http.middlewares.nativx-body.buffering.memRequestBodyBytes=262144"
- "traefik.http.routers.<router-webhook>.middlewares=nativx-body"
```

> Web (`/web/*`) e capat la 16KB în app (`WEB_MAX_BODY_BYTES`); dacă pui un router Traefik
> separat pentru `/web`, folosește `maxRequestBodyBytes=16384` pe el. Capul Traefik e un plus —
> NU înlocuiește gardul din app.

## Faza 2 — Telegram live (risc ~0, validare)

DNS încă nenecesar (Telegram = polling, fără ingress). Pornește fără webhook expus:

```bash
cd /opt/nativextech/nativx
docker compose up -d --build redis worker dispatcher telegram-poller scheduler
docker compose ps
docker compose logs -f telegram-poller worker
docker stats --no-stream            # ← verifică RAM-ul sub sarcină
```

Trimite un mesaj botului de Telegram → confirmă răspuns e2e. Urmărește `docker stats`
câteva minute. Dacă RAM-ul e ok, treci la WhatsApp.

## Faza 3 — WhatsApp (Meta Cloud API)

1. **DNS:** A-record `WEBHOOK_HOST` → `72.62.34.245` (+ AAAA către IPv6 dacă vrei).
2. **Pornește webhook-ul** (Traefik îi emite certul automat la prima cerere HTTPS):
   ```bash
   docker compose up -d webhook
   docker compose logs -f webhook
   ```
3. **Meta dashboard** (T013 — număr WhatsApp Business propriu, NU Evolution):
   - completează în `.env`: `META_ACCESS_TOKEN`, `META_APP_SECRET`,
     `META_PHONE_NUMBER_ID`, `META_VERIFY_TOKEN` → `up -d webhook` din nou.
   - Webhook callback URL: `https://WEBHOOK_HOST/webhook`
   - Verify token: același `META_VERIFY_TOKEN`. Meta face GET `/webhook?hub.*` →
     trebuie 200 cu challenge-ul (vezi `src/webhook/app.py`).
   - Subscribe la câmpul `messages`.
4. Trimite un mesaj real pe numărul Meta → confirmă răspuns.

> Verificarea Meta Business poate dura zile — începe paperwork-ul T013 din timp.

## Faza 4 — Widget web (chat pe site)

Al treilea canal: widget de chat embeddabil pe site-ul clientului. Rulează pe rute
suplimentare în serviciul `webhook` (deja rutat de Traefik) — **fără container nou, fără
DNS nou** (același `WEBHOOK_HOST`). `/web/chat` (sincron) rulează pipeline-ul IN-PROCES în
`webhook`, deci acel container are nevoie de `OPENAI_API_KEY` + `DATABASE_URL_BOT` (deja în `.env`).

1. **Seed canalul webchat** (o singură dată, generează `public_token` + `session_secret`):
   ```bash
   cd /opt/nativextech/nativx
   docker compose run --rm webhook python scripts/seed_web_channel.py
   # → public_token (= VITE_CHAT_PUBLIC_TOKEN în frontend)
   ```
2. **Activează gateway-ul** în `.env` (deja setate în `.env.prod.example`):
   ```bash
   WEB_ENABLED=true
   WEB_CORS_ORIGINS=https://demo.nativextech.com   # originea EXACTĂ a site-ului, fără slash final
   ```
   apoi `docker compose up -d webhook`. Endpointurile devin live:
   `https://WEBHOOK_HOST/web/bootstrap` + `/web/chat`.
3. **Test e2e** (fără frontend):
   ```bash
   # bootstrap → ia visitor_id + sig
   curl "https://WEBHOOK_HOST/web/bootstrap?token=PUBLIC_TOKEN"
   # chat sincron (cu visitor_id + sig din pasul precedent)
   curl -X POST "https://WEBHOOK_HOST/web/chat" -H "Content-Type: application/json" \
     -d '{"token":"PUBLIC_TOKEN","visitor_id":"web_...","sig":"...","message":"ce ai pentru ten gras?"}'
   # → {"content":"...","products":[...],"suggestions":[...]}
   ```
   Verifică din browser-ul site-ului că NU apare eroare CORS (originea trebuie să fie EXACT
   în `WEB_CORS_ORIGINS`; `https://demo.nativextech.com` ≠ `https://www.demo.nativextech.com`
   ≠ cu slash final).
4. **Frontend**: dă echipei `VITE_CHAT_API_BASE=https://WEBHOOK_HOST` + `VITE_CHAT_PUBLIC_TOKEN=<public_token>`
   (vezi `docs/web-widget-embed.md` §sincron pt contractul `{content, products, suggestions}`).

> Carduri de produs: apar doar dacă rândurile din `products` au `image` + `product_url` în
> Supabase (embeddings deja făcute → căutarea semantică merge). Lipsesc curat dacă datele lipsesc.

---

## Operare

Din `/opt/nativextech/nativx/` (COMPOSE_FILE + COMPOSE_PROJECT_NAME sunt în `.env`),
deci aceleași comenzi ca la celelalte stack-uri ale tale:

```bash
cd /opt/nativextech/nativx
docker compose ps                 # stare
docker compose logs -f worker     # loguri (fără PII — redaction în logger)
docker compose restart worker     # restart un serviciu
docker compose --profile proactive up -d proactive   # motor proactiv când e nevoie
docker compose down               # oprește DOAR stack-ul nativx (nu atinge restul VPS-ului)

# ⚠️ DEPĂȘIT (NX-248): `git pull && docker compose pull && up -d` NU mai e calea de update.
# Hostul nu mai decide ce configurație rulează, iar imaginea e un DIGEST, nu `latest`.
# Update = promovare prin release.yml → docs/RELEASE-RUNBOOK.md §1.
# Migrarea e un job separat, cu credentialul de DDL:
docker compose --profile migrate run --rm migrate
# Ce rulează acum, cu adevărat:
curl -s localhost:8000/health/ready | python -m json.tool
python scripts/release/verify_manifest.py --manifest manifest.json --base-url http://localhost:8000
```

### Redis în restart loop după recrearea containerului

Simptom: `nativx-redis-1 … Restarting (1)`, iar restul stack-ului rămâne blocat pe
`dependency failed to start: container nativx-redis-1 is unhealthy`. În log:

```
redis-1  | find: ./appendonlydir: Permission denied
```

Cauza: entrypoint-ul oficial de Redis, pornit ca root, face `chown` pe `/data` înainte de a coborî
la userul `redis` — dar `cap_drop: [ALL]` îi ia root-ului `CAP_DAC_OVERRIDE`, deci nu poate nici
măcar traversa `appendonlydir` (uid 999). Rezolvat prin `user: "999:999"` pe serviciul `redis` în
`docker-compose.prod.yml`: procesul pornește direct ca proprietarul datelor, iar ramura cu `chown`
din entrypoint nu se mai execută.

Dacă hostul are un container Redis mai vechi decât fixul, `docker compose up -d` îl recreează
corect. **Nu** rezolva prin re-adăugarea capabilităților și **nu** șterge volumul `redis-data` —
conține coada `inbound` și lease-urile de admission.

### Executorul web async + recovery (NX-233)

Executorul turelor v2 și sweeperul de recovery rulează **în procesul `worker`** (task-uri
asyncio), fără serviciu nou de compose. Flags în `.env` (vezi `.env.prod.example`, secțiunea
NX-232/233, cu ordinea de rollout): `WEB_TURN_EXECUTOR_ENABLED`, `WEB_TURN_RECOVERY_ENABLED`,
apoi `WEB_TURN_V2_ENABLED` / `WEB_TURN_SSE_ENABLED`. Operare:

```bash
docker compose logs -f worker | grep web_turn    # claim/reclaim/fenced/sweeper (fără PII)
docker compose restart worker                    # safe: turul curent rămâne `running` cu lease,
                                                 # alt worker (sau același, după boot) îl reclamă
```

Semnale de urmărit în loguri: `web_turn executor … pornit`, `sweeper web_turns: scanned=…`,
`fenced` (un zombie a fost respins — normal la restart, alarmant dacă e susținut). Rollback:
stinge `WEB_TURN_V2_ENABLED` (acceptul nou), dar **lasă executorul + recovery aprinse** până se
drenează turele deja acceptate; nu șterge rânduri și nu reseta epochuri.

## Auto-deploy (CI/CD) — ÎNLOCUIT de NX-248

**Nu mai există auto-deploy la push pe `main`.** Un merge nu e o aprobare de deploy: producția se
promovează manual, cu un digest semnat, prin `Actions → Release → Run workflow`. Fluxul complet și
motivele: [docs/RELEASE-RUNBOOK.md](docs/RELEASE-RUNBOOK.md).

`.github/workflows/deploy.yml` a rămas fără trigger automat, ca fallback documentat, până la primul
release complet prin `release.yml`. Rularea lui cere o confirmare scrisă explicit.

Setup (o singură dată), pe lângă cel de mai jos:
- **GitHub Environments** `staging` + `production`, cu approvals și secrete separate.
- **`VPS_HOST_KEY`** = cheia publică a HOSTULUI, obținută out-of-band (consola providerului) și
  aprobată de un om. Înlocuiește `ssh-keyscan` + `StrictHostKeyChecking=no`.
- **`.env.migrate`** pe VPS (mod 0400) cu `DATABASE_URL_MIGRATION` — credentialul de DDL, montat
  DOAR în jobul de migrare.
- **`IMAGE_DIGEST`** în `.env` — pus de deploy din manifest. Fără el, `docker compose` refuză să
  pornească (intenționat).

## Future

- **Swap** (amânat): `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap
  /swapfile && swapon /swapfile` + linie în `/etc/fstab` + `sysctl vm.swappiness=10`.
  Plasă contra epuizării TOTALE de RAM (peste ce prind `mem_limit`-urile).
- **Widget web** — LIVE (vezi Faza 4). Rute în serviciul `webhook`, fără container nou.
  Dacă traficul web crește, mută `/web/*` într-un serviciu `webgw` dedicat (aceeași imagine,
  alt router Traefik) ca să nu concureze cu ingestia de webhook pe CPU/RAM.
