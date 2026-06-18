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
# 1. Codul pe VPS (alege un dir dedicat, ex. /opt/nativx)
sudo mkdir -p /opt/nativx && cd /opt/nativx
git clone <repo-url> .          # sau git pull dacă există deja

# 2. .env de prod
cp .env.prod.example .env
nano .env                       # completează (vezi §3 pentru valorile Traefik)

# 3. Provisioning rol bot_runtime pe Supabase (o singură dată, dacă nu e făcut)
BOT_RUNTIME_PASSWORD='...' python scripts/apply_005.py
```

⚠️ Parola Supabase din URL trebuie **percent-encoded** (`@`→`%40`, `#`→`%23`...) —
altfel asyncpg crapă în container.

## §3 — Valori Traefik (recon read-only)

`docker-compose.prod.yml` are 2 placeholdere care trebuie să corespundă Traefik-ului tău:

```bash
# entrypoint-uri + certresolver definite în Traefik:
docker inspect traefik --format '{{range .Args}}{{println .}}{{end}}'
# șablon de copiat — labels Traefik pe un serviciu deja rutat:
docker inspect evolution --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{println}}{{end}}' | grep -i traefik
```

Pune în `.env`:
- `TRAEFIK_ENTRYPOINT` = numele entrypoint-ului HTTPS (ex. `websecure` / `https`)
- `TRAEFIK_CERTRESOLVER` = numele resolver-ului ACME (ex. `letsencrypt` / `le` / `myresolver`)
- `WEBHOOK_HOST` = subdomeniul (ex. `bot.domeniul-tau.ro`)

## Faza 2 — Telegram live (risc ~0, validare)

DNS încă nenecesar (Telegram = polling, fără ingress). Pornește fără webhook expus:

```bash
docker compose -p nativx -f docker-compose.prod.yml up -d --build \
  redis worker dispatcher telegram-poller scheduler

docker compose -p nativx -f docker-compose.prod.yml ps
docker compose -p nativx -f docker-compose.prod.yml logs -f telegram-poller worker
docker stats --no-stream            # ← verifică RAM-ul sub sarcină
```

Trimite un mesaj botului de Telegram → confirmă răspuns e2e. Urmărește `docker stats`
câteva minute. Dacă RAM-ul e ok, treci la WhatsApp.

## Faza 3 — WhatsApp (Meta Cloud API)

1. **DNS:** A-record `WEBHOOK_HOST` → `72.62.34.245` (+ AAAA către IPv6 dacă vrei).
2. **Pornește webhook-ul** (Traefik îi emite certul automat la prima cerere HTTPS):
   ```bash
   docker compose -p nativx -f docker-compose.prod.yml up -d webhook
   docker compose -p nativx -f docker-compose.prod.yml logs -f webhook
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

---

## Operare

```bash
C="docker compose -p nativx -f docker-compose.prod.yml"
$C ps                 # stare
$C logs -f worker     # loguri (fără PII — redaction în logger)
$C restart worker     # restart un serviciu
git pull && $C up -d --build      # update cod (rebuild imagine imutabilă)
$C --profile proactive up -d proactive   # pornește motorul proactiv când e nevoie
$C down               # oprește DOAR stack-ul nativx (nu atinge restul VPS-ului)
```

## Future

- **Swap** (amânat): `fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap
  /swapfile && swapon /swapfile` + linie în `/etc/fstab` + `sysctl vm.swappiness=10`.
  Plasă contra epuizării TOTALE de RAM (peste ce prind `mem_limit`-urile).
- **Widget web** (canal NX-20a, în lucru): va sta pe rute suplimentare în serviciul
  `webhook` → deja rutat de Traefik, fără container nou. Dacă cere WebSocket, Traefik
  îl trece pe același router.
