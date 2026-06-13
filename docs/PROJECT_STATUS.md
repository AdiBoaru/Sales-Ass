# Nativx Assistant — Status proiect

_Actualizat: 2026-06-13 · Bază: `main` la zi (PR #1–#39) · Document VIU — se
actualizează la fiecare milestone; data stă aici, nu în numele fișierului._

Document de referință pentru: (1) ce e implementat și în ce stadiu, (2) riscuri
și datorie tehnică, (3) ce urmează — material pentru generarea taskurilor.

---

## 1. Executive summary

- **🎉 Milestone „Telegram echo e2e LIVE" ATINS.** Un mesaj REAL trimis botului
  pe Telegram (`@solechat_bot`) parcurge tot lanțul și primește răspuns automat:
  `poller (long polling) → Redis stream → worker → pipeline → outbox →
  dispatcher → Telegram`. Pe infrastructură reală (Supabase + Redis + Docker
  local), nu doar în teste. Arhitectura canal-agnostică e validată în practică.
- **Capătul de ieșire FUNCȚIONEAZĂ.** Dispatcher live (outbox → canal),
  idempotent, cu retry/backoff + dead-letter + reaper implicit (visibility
  timeout). Canal-agnostic (NX-60): WhatsApp (Meta) și Telegram (test) prin
  `ChannelSender` registry — alegere după `channel_kind`.
- **OpenAI verificat** (T017 parțial). Cheia validă + cele 3 modele
  (`gpt-5.4-mini`/`nano`, `text-embedding-3-small`) confirmate live cu
  `scripts/check_openai.py`. **Rămâne** de pus limita de spend în dashboard.
- **Botul e încă ECHO.** Pipeline-ul are un singur stagiu real (`echo_stage`,
  scaffold). Următorul salt de valoare = **G3 (triaj nano → agent mini)**, acum
  DEBLOCAT (cheia merge).
- **Stack-ul rulează LOCAL.** Docker Desktop + WSL2 instalate 2026-06-13 →
  `docker compose up` pornește redis + worker + dispatcher + telegram-poller +
  webhook pe Windows. (VPS rămâne ținta de producție.)

## 2. Ce e în main (până la PR #39)

| Componentă | Stare | PR |
|---|---|---|
| G1: queries runtime (contacts, conversations, messages, outbox) | ✅ | #19 |
| G2a: POST /webhook (semnătură, parser Meta, dedupe L1, XADD) | ✅ | #20 |
| G2b: consumer + runner + processor (echo) | ✅ | #21 |
| NX-51: dedupe layer 2 (`inbound_dedupe`, 004 aplicat live) | ✅ | #23 |
| Dispatcher (outbox → canal, retry, dead-letter) | ✅ | #25 |
| Status webhook (delivered/read/failed → `messages.status`) | ✅ | #26 |
| NX-02: Redis durabil + securizat (AOF, parolă, noeviction) | ✅ | #27 |
| Observabilitate: `analytics_events` persistate din runner | ✅ | #28 |
| NX-60: abstracție de canal (envelope neutru + ChannelSender registry) | ✅ | #30 |
| NX-61/62: Telegram inbound (poller) + outbound (TelegramClient) | ✅ | #31 |
| NX-63: onboarding canal Telegram demo (seed + upsert_channel) | ✅ | #33 |
| Fix verify-token din settings (nu os.environ) | ✅ | #34 |
| T017: `check_openai.py` + notare spend ca TODO | ✅ | #35, #36 |
| Fix seed rulabil direct (sys.path) | ✅ | #37 |
| Fix worker: entrypoint `__main__` + log per-tur + silențiere token | ✅ | #38 |
| `.gitignore .env.bak*` + doc gotcha parolă DB | ✅ | #39 |

Fundațiile anterioare (infra, schema v2 + RLS 003, config/pool/models,
search_products SQL) — vezi istoricul PR #1–#18.

## 3. Stadiu pe pipeline (cele 9 stagii)

| # | Stagiu | Stare |
|---|---|---|
| 1 | Webhook: GET verify + POST inbound | ✅ live |
| 2 | Redis backbone: stream + consumer group + dedupe 2L | ✅ live (TODO: debounce, lock multi-consumer, rate limit, cost guard) |
| 3 | Gates (bot_active, handoff, limbă, risc, media) | ❌ |
| 4 | Straturi gratuite (alias, cache semantic, clarificare) | ❌ (faqs=0, cache=0) |
| 5 | Triaj (nano) | ❌ — **deblocat** (cheia OpenAI merge) |
| 6 | Context builder | ❌ |
| 7 | Agent (mini) + tools | 🟡 doar `search_products` SQL (fără ranking semantic — embeddings=0) |
| 8 | Validator | ❌ |
| 9 | Sender → outbox → dispatcher | ✅ **live cap-coadă** |
| — | Status webhook (delivered/read/failed) | ✅ |
| — | Proactiv / Jobs (embed, rollup, cleanup partiții) | ❌ (doar `cleanup_dedupe` ✅) |

## 4. Starea DATELOR demo (verificat live în Supabase, 2026-06-13)

business_id `6098812a-50fc-44bd-a1ba-bc77e6399158` (slug `nativex-demo`):

| Tabel | Count | Notă |
|---|---|---|
| `products` | 500 | seedate |
| `product_embeddings` | **0** | ⚠️ neembed-uite → search semantic indisponibil. Job `embed_products` de scris (acum deblocat — cheia merge) |
| `products.product_url` | **0 populate** | ⚠️ toate NULL → agentul n-are linkuri de produs. Gaură de DATE (seed), nu de cod |
| `products.ai_summary` | 500 | prezente, dar generate templat (calitate de îmbunătățit) |
| `faqs` | 0 | stratul gratuit FAQ gol |
| `channels` | telegram ✅ | `@solechat_bot` (id 7980364420) seedat; WhatsApp încă 0 (cere T013) |

## 5. Riscuri & datorie tehnică (curente)

1. **Worker-ul se loghează ca `postgres` + SET ROLE** (`tenant_conn`) — NX-50
   (P0-A audit) cere rol de LOGIN `bot_runtime`. De făcut înainte de load real;
   NX-04 (assert la checkout) + NX-53 (test concurent) vin peste.
2. **`product_embeddings` = 0** → `search_products` merge doar pe filtre SQL, fără
   ranking semantic. De scris `embed_products` (deblocat de cheia OpenAI).
3. **`product_url` = 0 (toate NULL)** → agentul/validatorul n-au linkuri de produs.
   De rezolvat sursa URL-urilor în catalog/seed.
4. **Limita de spend OpenAI NEPUSĂ** — protecția financiară (dashboard) e încă de
   făcut (tracked în TODO-MANUAL T017). Cost actual ~$0 (botul e echo).
5. **Echo stage e scaffold** — marcat explicit; se înlocuiește în G3.
6. **Dedupe claim-first:** crash între claim și finalizarea turului = mesaj marcat
   văzut dar neprocesat. Dead-letter / reaper = follow-up.
7. **`get_or_create_conversation` race teoretic** pe primul mesaj al unui contact
   nou (fără unique pe open-conv). Advisory lock = follow-up.
8. **Tokenul Telegram a apărut în loguri** (httpx loga URL-ul complet) — silențiat
   în #38. Pentru zero risc: `/revoke` la BotFather după teste.
9. **DB password gotcha** (#39): parola din `SUPABASE_DB_URL` trebuie percent-encoded
   sau asyncpg crapă în container (nu pe Windows). Documentat în README.

## 6. Ce urmează (ordine recomandată)

1. **`embed_products` (embeddings)** — job mic, cost ~$0.01, deblochează search
   semantic. Quick win + dovedește modelul embed live.
2. **G3: adapter OpenAI (v2) + Triaj (nano) + prompt_builder** — primul stagiu LLM;
   mock/replay în CI. Botul începe să „gândească" (vizibil pe @solechat_bot).
3. **G4: agent (mini) + tools + validator** — răspuns real de vânzare, zero prețuri
   inventate. (Validatorul are nevoie de `product_url` populat.)
4. **NX-50/04/53** (rol login + assert + test concurent) — înainte de load real.
5. **G5/G6**: gates, straturi gratuite, context builder, proactiv, jobs.
6. **WhatsApp e2e** (T013/T015 manual) + deploy VPS pentru rulare continuă.

**Milestone următor:** „bot care răspunde inteligent pe Telegram" — triaj + agent
înlocuiesc echo-ul, vizibil instant pe `@solechat_bot`.

## 7. Decizii de arhitectură luate pe parcurs

- **Stream unic `inbound`** (nu per conversație): conversation_id nu e cunoscut la
  webhook fără DB; ordinea per conversație se rezolvă în worker (lock, TODO).
- **`admin_conn` (control plane)** — excepție unică documentată de la „business_id
  pe tot": lookup canal→business precede tenantul.
- **Dedupe NU pe `messages`** — unique-ul include cheia de partiționare; 2 straturi
  (Redis + `inbound_dedupe`).
- **Abstracție de canal la 2 margini (NX-60)** — parser ingestie → envelope neutru;
  `ChannelSender` registry la trimitere. Pipeline-ul/worker-ul nu știu de canal.
- **Parola DB percent-encoded** — `urlparse` (Windows) tolerează `@` neescapat,
  asyncpg (container) nu → encode obligatoriu pentru paritate local/prod.
- **Dev local containerizat** — Docker Desktop + WSL2 pe Win10 Home; `docker compose`
  rulează tot stack-ul local (înainte: doar pe VPS).
