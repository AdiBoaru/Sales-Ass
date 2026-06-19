# Deploy Nativx pe VPS partajat (Hostinger + Traefik)

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
BOT_RUNTIME_PASSWORD='...' python scripts/apply_005.py
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
   WEB_CORS_ORIGINS=https://shop.nativextech.com   # originea EXACTĂ a site-ului, fără slash final
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
   în `WEB_CORS_ORIGINS`; `https://shop.nativextech.com` ≠ `https://www.shop.nativextech.com`
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
git pull && docker compose up -d --build       # update cod (rebuild imagine imutabilă)
docker compose --profile proactive up -d proactive   # motor proactiv când e nevoie
docker compose down               # oprește DOAR stack-ul nativx (nu atinge restul VPS-ului)
```

## Future

- **Swap** (amânat): `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap
  /swapfile && swapon /swapfile` + linie în `/etc/fstab` + `sysctl vm.swappiness=10`.
  Plasă contra epuizării TOTALE de RAM (peste ce prind `mem_limit`-urile).
- **Widget web** — LIVE (vezi Faza 4). Rute în serviciul `webhook`, fără container nou.
  Dacă traficul web crește, mută `/web/*` într-un serviciu `webgw` dedicat (aceeași imagine,
  alt router Traefik) ca să nu concureze cu ingestia de webhook pe CPU/RAM.
