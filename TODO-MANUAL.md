# Taskuri manuale — Adi (conturi & setup extern)

> Lista lucrurilor pe care **doar tu** le poți face (conturi, dashboard-uri, verificări externe),
> în timp ce Claude scrie codul. Claude adaugă aici pe măsură ce taskurile de cod ating dependențe manuale.
> Bifează pe măsură ce termini. Secretele merg în `.env` local, NICIODATĂ în repo.

_Ultima actualizare: 2026-06-13_

---

## 🎯 Drumul critic pentru „Telegram e2e live" (cel mai rapid test)

În ordine, doar partea ta (Claude livrează codul în paralel):
1. **TG-TEST** — token de la BotFather (~15 min)
2. **Deploy VPS** — Docker + `.env` (cu `REDIS_PASSWORD` generat + token) + `docker compose up`
3. rulezi seed-ul canalului (după ce Claude livrează NX-63) → scrii botului → echo

Restul (T013/T015/T016 WhatsApp, T017 OpenAI) NU blochează testul Telegram pe echo.
T017 (cheia OpenAI) e necesar doar ca botul să răspundă „inteligent", nu doar echo.

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
- [ ] Billing → Usage limits: hard limit (ex. 50 USD dev / 200 USD prod) + alerte 50%/80%
- [ ] Test 1 apel pe fiecare model: `gpt-5.4-mini`, `gpt-5.4-nano`, `text-embedding-3-small`
- [ ] `OPENAI_API_KEY` (dev) în `.env` la tine + junior

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

### NX-50 — rol DB de login `bot_runtime` (înainte de load real, NU acum)

Securitate P0-A din audit, AMÂNAT conștient. Azi worker-ul intră ca `postgres` +
`SET ROLE bot_runtime` (merge, dar sub multiplexare poolerul poate scurge starea).
Înainte de trafic real cu mai mulți clienți, faci cutover-ul:

- [ ] Rulezi (Claude îți dă SQL-ul exact la momentul respectiv): `ALTER ROLE bot_runtime LOGIN PASSWORD '...'`
- [ ] Pui `DATABASE_URL_BOT=postgresql://bot_runtime:...@...:5432/postgres` în `.env` (separat de cel `postgres`)
- [ ] Confirmi că niciun `.env` de runtime nu mai are user `postgres`

> Nu blochează testul Telegram. E pe lista „înainte de clienți reali", lângă Supabase Pro.

### TG-TEST — Bot Telegram pentru testul rapid pe VPS  ·  ~15 min  ⬅️ cale de test fără birocrație

Ca să testăm botul vorbind direct pe Telegram (pe VPS), fără Meta/HTTPS/tunel.
WhatsApp rămâne canalul primar — Telegram e DOAR pentru iterare rapidă (NX-61/62/63).

- [ ] Telegram → caută **@BotFather** → `/newbot` → nume + username → copiază **tokenul**
- [ ] `TELEGRAM_BOT_TOKEN=...` în `.env` (pe VPS; și local dacă testezi)
- [ ] (long polling → NU e nevoie de setWebhook, HTTPS sau tunel)
- [ ] După ce Claude livrează NX-61/62/63 + seed-ul canalului: scrie „salut" botului → aștepți echo

---

## 🚀 Deploy pe VPS (pentru testul Telegram) · ~1h  ⬅️ pasul care lipsea

Aici rulezi efectiv proiectul ca să poți vorbi cu botul. DB rămâne Supabase remote
(NU instalezi Postgres pe VPS). Depinde de: TG-TEST (tokenul) + un VPS cu Docker.

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
- [ ] **Auto-delete branches:** Settings → General → bifează "Automatically delete
      head branches" — curăță branch-urile după merge și reduce riscul de
      commit-uri orfane (s-a întâmplat de 3 ori: #15, #17, #23). Pe cele ~20
      de branch-uri vechi deja merged: spune-i lui Claude să le șteargă.

---

## ✅ Făcute

- [X] T001 — Repo privat + .gitignore
- [X] T002 — Branch protection pe main
