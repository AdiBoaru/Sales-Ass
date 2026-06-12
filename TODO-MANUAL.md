# Taskuri manuale — Adi (conturi & setup extern)

> Lista lucrurilor pe care **doar tu** le poți face (conturi, dashboard-uri, verificări externe),
> în timp ce Claude scrie codul. Claude adaugă aici pe măsură ce taskurile de cod ating dependențe manuale.
> Bifează pe măsură ce termini. Secretele merg în `.env` local, NICIODATĂ în repo.

_Ultima actualizare: 2026-06-13_

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
