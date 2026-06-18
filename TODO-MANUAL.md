# Taskuri manuale — Adi (conturi & setup extern)

> Lista lucrurilor pe care **doar tu** le poți face (conturi, dashboard-uri, verificări externe),
> în timp ce Claude scrie codul. Claude adaugă aici pe măsură ce taskurile de cod ating dependențe manuale.
> Bifează pe măsură ce termini. Secretele merg în `.env` local, NICIODATĂ în repo.

_Ultima actualizare: 2026-06-16_

---

## 🎯 „Telegram e2e live" — ✅ ATINS (2026-06-13, LOCAL pe laptop)

Botul **@solechat_bot** răspunde echo pe Telegram, cu stack-ul rulând local prin
Docker Desktop (WSL2). Lanțul complet `poller → worker → dispatcher → Telegram`
e dovedit pe infrastructură reală.

Ce a mai rămas (OPȚIONAL / pasul următor):
- **Deploy VPS** (secțiunea de mai jos) — ca botul să ruleze CONTINUU, nu doar cât e laptopul pornit.
- **T017 spend limit** — înainte de G3 (botul „inteligent", nu doar echo).

---

## 🔴 Blochează progresul imediat (fă-le primele)

### T013 — Meta developer app + WhatsApp test number  ·  ~1.5h

Deblochează tot WhatsApp-ul. Fără el, nimic din webhook/mesaje nu se poate testa live.

- [ ] developers.facebook.com → Create App → tip **Business** → adaugă produsul **WhatsApp**
- [ ] Notează **Phone Number ID** + **test phone number** (sandbox, instant)
- [ ] Business Settings → **System User** cu rol admin → generează **token permanent** (scopes: `whatsapp_business_messaging`, `whatsapp_business_management`, fără expirare). NU rămâne pe token-ul de 24h!
- [ ] Settings → Basic → notează **APP_SECRET**
- [ ] Adaugă telefoanele tale + ale juniorului ca **recipient phone numbers** (max 5)
- [ ] Trimite un "hello world" din Graph API Explorer către telefonul tău (confirmă că merge)
- [ ] Pune în `.env` local: `META_ACCESS_TOKEN`, `META_APP_SECRET`, `META_PHONE_NUMBER_ID`
- [ ] Dă-i lui Claude **Phone Number ID** → inserează rândul în `channels` pentru
  business-ul demo (fără el, worker-ul nu poate mapa mesajele live la tenant)

### T017 — OpenAI: chei + limite de spend  ·  ~0.5h  ⬅️ **BLOCKER pe drumul critic**

Protecție financiară înainte de primul apel LLM. Din 2026-06-13: e SINGURUL
lucru care blochează G3 (triaj + agent live) și `embed_products` — codul de
pipeline e gata să-l primească.

- [ ] platform.openai.com → 2 proiecte: `nativx-dev`, `nativx-prod` (cheie per proiect)
- [ ] **Billing → Usage limits — limite MICI inițial (⛔ ÎNCĂ NEPUSE, 2026-06-13):**
      hard limit **$10 dev** + alertă email la **$5** (50%); prod lăsat nesetat până la
      primul client plătitor. Decizie: mic acum, creștem la consumul real. **De pus
      ÎNAINTE de testarea G3 mai grea** (acum botul e echo, nu apelează încă LLM).
- [X] ✅ Modelele confirmate că EXISTĂ în cont (din pagina de rate limits, 2026-06-13):
      `gpt-5.4-mini`, `gpt-5.4-nano`, `text-embedding-3-small` — riscul de 404 eliminat.
- [X] ✅ Test 1 apel REAL pe fiecare model — `scripts/check_openai.py` rulat,
      toate 3 modelele răspund (Adi, 2026-06-13).
- [X] ✅ `OPENAI_API_KEY` (dev) pus în `.env` (Adi, 2026-06-13). Junior: încă de pus.

### T018 — Supabase: connection strings + PITR  ·  ~0.5h

Conexiunea corectă pentru bot + backup confirmat.

- [ ] Settings → Database → notează **pooler 6543** (mod transaction, pt asyncpg) ȘI **direct 5432** (pt migrări)
- [ ] Settings → Backups → confirmă PITR / daily backups (activează PITR înainte de clienți reali)
- [X] `SUPABASE_DB_URL` în `.env` la tine + junior
- [ ] (service_role key NU se folosește de bot; anon key deloc)
- [X] ✅ `SUPABASE_DB_URL` setat (Session pooler) + `003_bot_runtime_role.sql` APLICAT + RLS testat (Claude, 2026-06-12)
- [X] Backups verificat: pe **Free**, fără backups — OK pentru dev (date demo re-seedabile)

> ⚠️ **ÎNAINTE de primul client plătitor:** trecere pe Supabase **Pro** (daily backups).
> Date reale de client fără backup = riscul din T018. Nu acum, dar nu uita.

### NX-50 — rol DB de login `bot_runtime` (CODUL E GATA; cutover înainte de load real)

Securitate P0-A din audit. **Codul e livrat** (două pool-uri: `bot_runtime` login
pentru tenant path + admin pentru control plane — vezi `docs/db_connections.md`).
Până faci pașii de mai jos, codul cade **grațios** pe modul compat (`SUPABASE_DB_URL`
+ `SET ROLE` în init) → nimic nu se rupe acum. Cutover-ul (recomandat înainte de
trafic real cu mai mulți clienți):

- [ ] Generează o parolă pt `bot_runtime` (vault) și rulează:
      `BOT_RUNTIME_PASSWORD='...' python scripts/apply_005.py`
      (face `ALTER ROLE bot_runtime LOGIN PASSWORD` + verifică login/bypassrls/super)
- [ ] Pune în `.env`: `DATABASE_URL_BOT=postgresql://bot_runtime:<parola>@<host>:5432/postgres`
      (separat de `SUPABASE_DB_URL`). ⚠️ Parola percent-encoded dacă are caractere speciale.
- [ ] ⚠️ **Verifică conectivitatea:** pe Supabase, login-ul cu rol custom (`bot_runtime`)
      merge pe **conexiunea directă** (`db.<ref>.supabase.co:5432`) sau pe Session pooler
      cu user `bot_runtime.<ref>`. Confirmă care funcționează din rețeaua ta
      (la fel ca nuanța IPv4 din T018). Dacă login-ul direct nu merge, rămâi pe compat.
- [ ] Confirmă că `.env`-ul de runtime al workerului folosește `bot_runtime`, nu `postgres`
      (acesta din urmă rămâne DOAR pt admin/migrări — control plane).

> Nu blochează testul Telegram. E pe lista „înainte de clienți reali", lângă Supabase Pro.

### TG-TEST — Bot Telegram pentru testul rapid pe VPS  ·  ~15 min  ⬅️ cale de test fără birocrație

Ca să testăm botul vorbind direct pe Telegram (pe VPS), fără Meta/HTTPS/tunel.
WhatsApp rămâne canalul primar — Telegram e DOAR pentru iterare rapidă (NX-61/62/63).

- [X] Telegram → caută **@BotFather** → `/newbot` → nume + username → copiază **tokenul**
- [X] `TELEGRAM_BOT_TOKEN=...` în `.env` (pe VPS; și local dacă testezi)
- [X] (long polling → NU e nevoie de setWebhook, HTTPS sau tunel)
- [X] ✅ Echo confirmat: **@solechat_bot** răspunde pe Telegram (LOCAL, 2026-06-13)

---

## 🚀 Deploy pe VPS (rulare CONTINUĂ) · ~1h

> ℹ️ Echo-ul a fost deja testat **local** (Docker Desktop + WSL2 pe laptop, 2026-06-13).
> VPS-ul e pentru ca botul să ruleze non-stop, independent de laptop. Pașii de mai jos
> (REDIS_PASSWORD, seed canal, compose) sunt deja validați local — se repetă pe VPS.
> ⚠️ Parola `SUPABASE_DB_URL` trebuie percent-encoded (`@`→`%40`) — vezi README troubleshooting.

DB rămâne Supabase remote (NU instalezi Postgres pe VPS). Depinde de: un VPS cu Docker.

- [ ] VPS cu **Docker + docker compose** instalate (Ubuntu e ok)
- [ ] Clonează repo-ul pe VPS (deploy key sau PAT GitHub — repo-ul e privat)
- [ ] Creează `.env` pe VPS (NU se commitează) cu:
  - [ ] `SUPABASE_DB_URL=...` (Session pooler, ca local)
  - [ ] `REDIS_PASSWORD=...` — **generează** una reală: `openssl rand -base64 32`
        (înlocuiește placeholder-ul; pune-o ȘI în `REDIS_URL=redis://:PAROLA@redis:6379/0`)
  - [ ] `TELEGRAM_BOT_TOKEN=...` (din BotFather, TG-TEST)
  - [ ] `OPENAI_API_KEY=...` (când e gata T017; fără el botul merge pe echo)
  - [ ] `META_*` — lasă-le goale deocamdată (Telegram nu le folosește)
- [ ] După ce Claude livrează NX-63 (seed canal): rulează scriptul de seed o dată
      (inserează rândul `channels` telegram pentru demo — poate rula și de pe laptop)
- [ ] `docker compose up -d` → verifică `docker compose ps` (redis, worker, dispatcher, telegram-poller verzi)
- [ ] Scrie „salut" botului pe Telegram → aștepți echo-ul
- [ ] (când schimbi codul: `git pull && docker compose up -d --build`)

> 🔒 Secretele trăiesc DOAR în `.env`-ul de pe VPS, niciodată în repo. Firewall:
> expune doar portul de webhook DACĂ ajungi pe WhatsApp; pentru Telegram (long
> polling) nu trebuie niciun port deschis spre exterior.

---

## 🟢 Comerț / bucla de bani (F2) — când demonstrezi vânzarea cu link

Codul F2 e gata (PR #63–#65): agentul poate genera un **link de cumpărare** cu `?ref=`,
webhookul de comenzi îl **atribuie** (`assisted revenue`), iar jobul nocturn agregă în
`usage_daily`. Fără provisioning-ul de mai jos, codul **degradează grațios** (botul
recomandă fără link; atribuirea nu rulează) — deci nu blochează nimic, dar bucla de bani
nu e vizibilă pe demo până nu-l faci.

### F2-A — Base URL de checkout  ·  ~5 min
- [ ] Decide URL-ul de checkout al magazinului demo (ex. `https://shop.sole-demo.ro/checkout`).
- [ ] Pune-l fie în `.env` (`CHECKOUT_BASE_URL=...`), fie cere-i lui Claude să-l scrie în
      `businesses.settings["checkout_url"]` pt demo (are prioritate față de `.env`).
- [ ] Gol = `checkout_link` întoarce `ok=False` (botul răspunde fără link). `?ref=<turn_id>`
      se adaugă automat în cod.

### F2-B — Secret webhook comenzi  ·  ~5 min
- [ ] Generează un secret aleator → `.env`: `ORDERS_WEBHOOK_SECRET=...` (gol = endpointul
      `POST /webhook/orders/{business_id}` întoarce 403).
- [ ] Platforma magazinului (sau un script de test) trimite comenzile la
      `POST /webhook/orders/<business_id>` cu headerul **`X-Orders-Signature: sha256=<hmac>`**
      (HMAC-SHA256 peste corpul BRUT, cu `ORDERS_WEBHOOK_SECRET`, NX-94 — nu mai trimite
      secretul în clar) și corpul JSON neutru (`external_id, status, total, ref, placed_at,
      items[]`). `ref` = `?ref=` din linkul botului → declanșează atribuirea.
      Semnare în shell (test):
      `SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"`
      apoi `curl -H "X-Orders-Signature: $SIG" -d "$BODY" .../webhook/orders/<business_id>`.

### F2-C — Scheduler rollup nocturn  ·  ~10 min (la deploy)
- [ ] Cron/n8n care rulează zilnic `python -m src.jobs.rollup_usage` (ziua de ieri, UTC) →
      populează `usage_daily` (sursa pt dashboard + facturare).

---

## 🟢 Seed FAQ pe demo (NX-74) · ~5 min

Codul stratului gratuit FAQ e gata (PR NX-74), dar `faqs=0` pe demo → nu servește nimic
până popularezi tabelul. Jobul e idempotent (re-rulabil) și scrie în Supabase (admin).

- [ ] Rulează pe demo (cu OPENAI_API_KEY + SUPABASE_DB_URL în `.env`):
      `python -m src.jobs.seed_faqs --generate`
      (fără `--generate` = doar baza curatată RO; cu = + întrebări generate de LLM)
- [ ] (Opțional) verifică în Supabase: `select count(*) from faqs where embedding is not null;`
- [ ] ⚠️ Răspunsurile din baza curatată sunt valori DEMO (retur 14 zile, livrare 19,99 lei,
      gratis peste 200 lei etc.) — editează-le în DB cu politicile REALE ale clientului
      înainte de producție (FAQ-ul e „un singur adevăr editat de client", nu inventat).

---

## 🟢 Seed imagini produse pe demo (poze reale beauty) · ~10 min

Catalogul demo are 2500 de imagini placeholder text (`placehold.co/...?text=NUME`) —
arată fals. `scripts/seed_product_images.py` le înlocuiește cu poze REALE beauty de pe
Pexels, mapate pe categorie (un „ser" arată un ser, o „cremă" o cremă). Codul e gata și
idempotent; îi lipsește doar cheia API.

- [ ] Cont gratuit pe https://www.pexels.com/api/ → copiază „Your API Key"
- [ ] `PEXELS_API_KEY=...` în `.env` local
- [ ] Dă-i lui Claude semnal → rulează (sau rulezi tu):
      `python scripts/seed_product_images.py --limit 5 --dry-run`  (verifici)
      apoi `python scripts/seed_product_images.py`  (toate cele 500)
- [ ] (Opțional) verifică în Supabase: `select count(*) from product_images where url like '%pexels%';`

> Pexels free: 200 req/oră, 20k/lună — scriptul face ≤58 căutări (una per categorie),
> deci sub limită cu mult. Hot-link la CDN-ul lor (licența permite), fără descărcare.

---

## 🟡 Pornește acum, durează zile (birocrație)

### T016 — Verificare Meta Business (producție)  ·  ~1h + așteptare 3-15 zile

**Pornește-l AZI** — e cel mai lung proces din tot proiectul. Demo merge pe sandbox, dar primul client plătitor cere număr de producție.

- [ ] Precondiții: site live pe nativxtech.com cu **datele firmei vizibile** + email pe domeniu (contact@nativxtech.com, NU gmail)
- [ ] business.facebook.com → Security Center → **Start Verification** cu documentele SRL (CUI/certificat)
- [ ] Reminder recurent (luni) de check status

- Depinde de: T013

---

## 🟢 Setup local (când ajungi la testat webhook-ul live)

### T015 — Tunel local (cloudflared)  ·  ~1h

Expune localhost:8000 spre Meta prin HTTPS public, fără deploy.

- [ ] Instalează `cloudflared` (tu + junior)
- [ ] `cloudflared tunnel --url http://localhost:8000` → copiază URL-ul https
- [ ] Pune `URL/webhook` + verify token în Meta config → **Verify and Save** (verde — testează codul din T014)
- [ ] Subscribe la câmpurile `messages` ȘI `message_status`
- [ ] (URL-ul se schimbă la fiecare restart de tunel → re-verifică în Meta. Tunel cu domeniu fix = bonus P1)

- Depinde de: T014 (✓ codul e gata, PR #9)

---

## ⚙️ GitHub (config repo — rapid, în browser)

- [ ] **Branch protection finalizare:** Settings → Branches → regula `main` → adaugă required checks `Lint (ruff)` + `Test (pytest)` (apar în search după ce au rulat o dată)
- [ ] **CODEOWNERS review:** bifează "Require review from Code Owners" (forțează review-ul tău pe `prompts/` și `docs/*.sql`)
- [ ] (Opțional) "Allow specified actors to bypass" → adaugă-te pe tine dacă vrei 1-approval pentru junior dar bypass pentru tine
- [ ] **Secret CI `SUPABASE_DB_URL`** (pt jobul nightly `isolation-concurrent`, NX-53):
  Settings → Secrets and variables → Actions → New repository secret →
  `SUPABASE_DB_URL` = URL-ul pooler (Session pooler, rezolvabil pe IPv4 din Actions).
  Fără el, jobul nightly de izolare pică la conectare. (Jobul rulează DOAR pe main +
  nightly, nu pe PR-uri.)
- [ ] **Auto-delete branches:** Settings → General → bifează "Automatically delete
  head branches" — curăță branch-urile după merge și reduce riscul de
  commit-uri orfane (s-a întâmplat de 3 ori: #15, #17, #23). Pe cele ~20
  de branch-uri vechi deja merged: spune-i lui Claude să le șteargă.

---

## ✅ Făcute

- [X] T001 — Repo privat + .gitignore
- [X] T002 — Branch protection pe main
